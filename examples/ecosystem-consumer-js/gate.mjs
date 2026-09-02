// The revl gate, as a JavaScript module — the ONLY revl import this project
// makes (roadmap item 335 slice 4, item 338's polyglot exit).
//
// `dist/` is not committed. It is what `jco transpile` writes from the
// `revl:gate@1.0.0` component that `crates/revl-gate-wasm` compiles to:
//
//     python3 tools/build_gate_js.py --out examples/ecosystem-consumer-js/dist
//
// or, from this directory, `npm install && npm run build`.
//
// READ THIS BEFORE YOU COPY ANY OF IT: THE ASYMMETRIC CONTRACT, JS EDITION
// =======================================================================
// The py gate (`pip install revl`, `revl.gate.admit`) is the full reference
// compiler and can both refuse and ADMIT. This module cannot ADMIT ANYTHING.
// It is `crates/revl-gate` packaged for wasm: the composition and guarantee
// layer (G1..G4, A1, PRELUDE, and parse failures as BAD) and NOT the reference
// type layer. So `verdict.admitted` is `false` on every arm — that is the WIT
// record's type, not a runtime accident — and the real signal is `kind`:
//
//   kind                what it means                        you may
//   ------------------  -----------------------------------  ------------------
//   "refused"           the reference compiler refuses this   REJECT. Final.
//                       source too, same code, same message
//   "no-objection"      "this gate found nothing it is able   ESCALATE
//                       to refuse". A type-incorrect program
//                       lands here.
//   "outside-frontier"  the gate is not entitled to decide    ESCALATE
//                       this source at all
//
// There are exactly two safe readings, REJECT and ESCALATE, and this module
// exposes exactly those two. It exports no boolean a caller could branch on as
// "admitted", and no function whose name suggests one.
//
// Refusing what the reference admits is an inconvenience. ADMITTING what the
// reference refuses is the defect class this whole arc exists to prevent, and
// a gate with no admission arm cannot commit it.

/** A refusal is authoritative: this candidate is out, and stays out. */
export const REJECT = "REJECT";
/**
 * Everything else. This gate is not entitled to accept, so the reference
 * toolchain decides. Never a local admission.
 */
export const ESCALATE = "ESCALATE";

/** The three arms this gate has. There is no fourth, and none of them admits. */
export const ARMS = Object.freeze(["refused", "no-objection", "outside-frontier"]);

/**
 * Thrown when the artifact hands back something that is not one of this gate's
 * three arms, or hands back `admitted` as anything but `false`.
 *
 * This is a tripwire, not an expected path. The component cannot produce either
 * shape today (the crate has no admitting arm to reach and the vector holds the
 * arms), so seeing one means the loaded `dist/` is not the gate it claims to be
 * — a stale cache, a swapped file, a tampered CDN copy. The fail-closed answer
 * to "I do not recognise this verdict" is to raise, never to keep the record
 * and let a caller read the unknown arm as a non-refusal.
 */
export class UntrustedVerdict extends Error {
  constructor(verdict) {
    super(`the loaded revl gate returned a verdict shape this contract does ` +
          `not recognise (${JSON.stringify(verdict)}); refusing to interpret it`);
    this.name = "UntrustedVerdict";
    this.verdict = verdict;
  }
}

/** Fail closed on anything that is not one of the three known non-admissions. */
function checked(verdict) {
  if (verdict?.admitted !== false || !ARMS.includes(verdict?.kind)) {
    throw new UntrustedVerdict(verdict);
  }
  return verdict;
}

/**
 * Load the transpiled gate.
 *
 * `specifier` defaults to the sibling `dist/` this project's build script
 * writes. A host that vendors the transpiled package elsewhere passes its own.
 *
 * `admit` and `admitArtifact` are wrapped in `checked` rather than re-exported
 * raw: the artifact's own guarantee is that `admitted` is false on every arm,
 * and this is the consumer-side half of that, so no verdict this project ever
 * hands on can carry an admission even if the loaded artifact is not the one
 * that was built.
 */
export async function loadGate(specifier = "./dist/revl_gate.js") {
  const mod = await import(specifier);
  return {
    /** The typed WIT record: { admitted: false, kind, code?, message? }. */
    admit: (source) => checked(mod.admit(source)),
    /** The crate's wire bytes, byte-identical to the rust tier's to_json. */
    admitJson: mod.admitJson,
    /** The item-289 chain over an artifact. Declines today; see README. */
    admitArtifact: (ir, policy, imports) =>
      checked(mod.admitArtifact(ir, policy, imports)),
    /** { api, language, frontier, layer, tier }: read `layer` before trusting
     *  any non-refusal, and record `frontier` with every verdict you keep. */
    version: JSON.parse(mod.gateVersion()),
  };
}

/**
 * The whole security posture of a native-gate consumer, in one function: act
 * on a refusal, escalate everything else, and never invent an acceptance the
 * gate did not give.
 *
 * Deliberately total over `kind` rather than keyed on `admitted`: `admitted`
 * is the constant `false`, so branching on it would read as a decision when it
 * is not one.
 */
export function decide(verdict) {
  return verdict.kind === "refused" ? REJECT : ESCALATE;
}

/**
 * Thrown when a caller asks to instantiate something on this gate's word alone.
 *
 * This is the no-admission property made structural in JS. `loadOrRefuse`
 * below has NO code path that instantiates an artifact without an
 * authoritative reference verdict, so a browser page built on this module
 * cannot degrade into "the wasm gate did not refuse it, so run it" — which is
 * the single worst outcome an npm-packaged admission gate could have.
 */
export class EscalationRequired extends Error {
  constructor(verdict) {
    super(
      `the revl wasm gate returned "${verdict.kind}", which is NOT an ` +
      `admission: this gate decides the composition/guarantee layer and not ` +
      `the reference type layer, so it has no admission to give. Get a ` +
      `verdict from the reference toolchain (revl compile, or revl.gate.admit ` +
      `on py) and pass it as options.reference before instantiating.`,
    );
    this.name = "EscalationRequired";
    this.verdict = verdict;
  }
}

/** Thrown when the gate REFUSES. Final: nothing about the candidate runs. */
export class Refused extends Error {
  constructor(verdict) {
    super(`${verdict.code}: ${verdict.message}`);
    this.name = "Refused";
    this.verdict = verdict;
  }
}

/**
 * The double-enforcement pattern, both layers, in the order a host runs them.
 *
 * Layer 1 — DECISION. `gate.admit(source)`. On `refused` this throws and the
 * artifact is never fetched, never compiled, never instantiated. That is the
 * layer the wasm gate provides, and it is the only layer it provides.
 *
 * The escalation gap — a non-refusal is not an admission, so `options.reference`
 * must carry `{ admitted: true }` from the REFERENCE toolchain. Without it
 * this throws `EscalationRequired`. There is no flag to skip this.
 *
 * Layer 2 — SUBSTRATE. The artifact is instantiated with an import object
 * shaped by `options.policy` and nothing else. An ungranted reach is then a
 * MISSING IMPORT, refused by the wasm engine itself (item 289: "an ungranted
 * reach is a missing import refused by the substrate itself"). This layer does
 * not trust layer 1's absence and layer 1 does not trust its own presence.
 *
 * @param {object} gate      the object `loadGate` returned
 * @param {string} source    the candidate's revl source
 * @param {object} options
 * @param {Uint8Array} options.artifact  the compiled wasm to run
 * @param {object} options.policy        { "<import module>": { <name>: fn } }
 * @param {object} [options.reference]   the reference toolchain's verdict
 * @returns {Promise<WebAssembly.Instance>}
 */
export async function loadOrRefuse(gate, source, options) {
  const verdict = gate.admit(source);

  // Layer 1. A refusal is authoritative and fail-closed.
  if (verdict.kind === "refused") throw new Refused(verdict);

  // Not a refusal is not an admission.
  if (!options.reference || options.reference.admitted !== true) {
    throw new EscalationRequired(verdict);
  }

  // Layer 2. The policy IS the import object: what it does not name, the
  // artifact cannot reach, and the engine — not this code — enforces that.
  const { instance } = await WebAssembly.instantiate(
    options.artifact,
    options.policy,
  );
  return instance;
}
