# revl — the vision, and the honest scope of the claim

This is the "why" companion to [DESIGN.md](../DESIGN.md) (the what) and
[docs/syntax-2.0.md](syntax-2.0.md) (the how). README links here for the
*honest scope* of the "future of programming" framing; this file is that
honesty, kept deliberately small.

## What revl is for

revl exists to make **dynamic composition safe by construction**: loading,
unloading, and hot-swapping components in a running system, where "unloading
leaves no residue" and "dependencies stay coherent" are compile-time
guarantees (G1–G8) rather than runtime discipline. It is the language-level
realization of the [Cordis paradigm](https://github.com/cordiverse/paper):
the paper proves strong metatheorems, but every one rests on hypotheses no
library can enforce (confinement, witnessed inverses, declared-only access,
acyclic provisioning). revl's job is to move those hypotheses from *runtime
discipline* into the *type system* — the jump C++ RAII made to become Rust's
ownership. See DESIGN.md §1 for the hypothesis-by-hypothesis table.

The second motivation is forward-looking and is where the "future of
programming" phrase comes from: if components are increasingly written by AI
agents, then *machine-checkable safety of a generated component before it
self-deploys* matters more than syntax familiarity. revl is designed to be a
language such a harness could require its own components to pass — the
generate → check → run loop closes inside one file (`test`/`verified`,
syntax-2.0 §7), and the checker's totality lets a corpus be
compile-verified rather than hoped-for.

## One language, six tiers

The same checked front-end lowers to a family of hardened runtimes rather
than to raw native code (DESIGN.md §8 explains why native is deliberately not
first — the paradigm's hardest requirement is reclaiming everything a
component owned, which `dlopen`/`dlclose` cannot promise):

| tier | runtime | what it proves |
|---|---|---|
| reference | cordis-py | the semantics + the checker, on a runtime whose paper-conformance suite doubles as ours |
| portability | cordis (TypeScript, v4) | the same IR runs unchanged on a second host |
| performance + enforcement | cordis-wasm | confinement becomes physical (sandbox), instances drop cleanly |
| spikes | cordis-rs (Rust), cordis4j (Java) | the backend contract is small enough to target any Cordis runtime |
| third-party runtime | cordis-go (Go) | the same contract holds for a Cordis runtime nobody on the project wrote ([stc-go](https://github.com/0xdenny218/stc-go)) |

One `.rvl` source, one IR, six emitters. The [2.0 roadmap](v2.0-roadmap.md)
tracks per-tier coverage.

Where each "what it proves" is actually proved — a claim in this project gets
a command or it gets softened:

| tier | gate | note |
|---|---|---|
| cordis-py | `cd backends/python && .venv/bin/pytest -q` | R1–R5 against the real runtime; needs `setup.sh` |
| cordis (TS) | `cd backends/typescript && npm ci && npx vitest run` | same IR, second host |
| cordis-wasm | `pytest backends/wasm/test_v3_emit.py tests/test_wasm_backend.py -q` | executes on real `wasmtime`; skips without it |
| cordis-rs, cordis4j | `pytest backends/rust/test_emit_rust.py backends/java/test_emit_java.py -q` | needs cargo + a JDK; skips loudly otherwise |
| cordis-go | `pytest backends/go/test_emit_go.py -q` | emitted code executes on real stc-go under `go test`; skips loudly otherwise |
| the formal backbone | `make formal` (`sh formal/scripts/run_gate.sh`) | Lean 4: `formal/`, a layering gate plus an axioms gate (no `sorry`, no project-defined axiom). G2, G3 and G7 are `full` and oracle-checked against the shipped checker; G1 and G6 are `partial`, and G9's coverage is the one UNPROVED row. `formal/STATUS.md` is the per-theorem ledger, and it names each gap. Runs as CI's `formal` job |

What every tier can and cannot *express* is measured by
`python3 tools/conformance.py` and recorded in
[conformance.md](conformance.md); `--validate` additionally hands each tier's
output to that tier's real compiler. Composing tiers across a process seam
(`--placement`, [interop-bridge.md](interop-bridge.md)) is demonstrated by the
`demo/bridge_*` scripts and not covered by a test — only the static
transport-safety verdict is (`tests/test_distribute.py`).

## The honest scope — what this does *not* claim

- **Not a general-purpose application language.** revl writes *components*;
  a host application assembles compiled components. Stratum 1 is a
  TypeScript *subset*, chosen to make a model's strongest priors correct,
  not to be a full TS.
- **Semantic inverse correctness is not decided in general.** The checker
  proves an inverse is *present and well-shaped* (G4), and derives teardown
  correctly for compositions of checked primitives; proving an arbitrary
  `undo` truly reverts its effect is undecidable and stays opt-in
  (`verified`, DESIGN.md §6).
- **The extern boundary is trusted, not verified** — but it is *enumerable*
  (G8, `revl audit`): revl cannot check foreign code, only force every escape
  hatch to be declared and auditable.
- **The calculus's metatheorems transfer by construction, they are not
  re-proved here.** The research claim is a *surface language whose every
  well-typed program satisfies the calculus's hypotheses* (DESIGN.md §5), not
  new type theory. What IS proved here, in Lean, is part of revl's own
  admission judgment: G2 (provision disjointness) and G3 (acyclicity) are `full`
  in `formal/`, axiom-gated, with non-vacuity witnesses and a differential
  oracle against `src/revl`. G1 and G6 are `partial` and G9's coverage is
  unproved, each for a named modelling reason. `formal/STATUS.md` is the ledger,
  and it is written to be read gap-first.

The pitch, kept honest: **Cordis has revertible effects as a discipline; revl
makes them a type system.** Everything beyond that is roadmap, not claim.
