# agent-prefilter-js — a browser/edge consumer of the revl wasm gate

This directory is a standalone project, not part of revl. It is what a browser
page, an edge worker or a serverless function looks like once it pulls revl's
admission gate in as a **wasm component transpiled to JavaScript**, instead of
shelling out to the `revl` CLI or shipping a Python runtime into a tab.

It is the third sibling of [`../ecosystem-consumer/`](../ecosystem-consumer/)
(py) and [`../ecosystem-consumer-rs/`](../ecosystem-consumer-rs/) (rust), and
deliberately not the same program, because **the three tiers do not give the
same guarantee.** This is roadmap item 335 slice 4 (`jco transpile` packaging
plus the harness that makes the consuming pattern copyable) over the component
slices 0-2 landed in `crates/revl-gate-wasm`.

## The contract this project embeds against, first

**A refusal is authoritative and fail-closed. This gate issues no admissions
at all. Everything that is not a refusal is escalated to the reference
toolchain. The gate never confines its host.**

The py gate (`pip install revl`, `revl.gate.admit`) is the full reference
compiler, so it can both refuse and admit. This one cannot. It is
`crates/revl-gate` compiled to wasm: the composition and guarantee layer
(`G1`..`G4`, `A1`, `PRELUDE`, and parse failures as `BAD`) and **not** the
reference type layer. So it has three arms and none of them is an admission:

| `verdict.kind` | what it means | what this project does |
|---|---|---|
| `"refused"` | the reference compiler refuses this source too, same code, same message verbatim | `REJECT`, final. Nothing about the candidate is fetched, compiled, instantiated or run |
| `"no-objection"` | "this gate found nothing it is able to refuse". A type-incorrect program lands here | `ESCALATE`, ask the reference gate |
| `"outside-frontier"` | the gate is not entitled to decide this source at all (a construct outside its frontier table, an oversized source, an abort in the native front end) | `ESCALATE`, ask the reference gate |

Refusing what the reference admits would be an inconvenience. **Admitting what
the reference refuses is the defect class the admission-gate arc exists to
prevent**, and a gate with no admission arm cannot commit it.

The full consumer-facing contract, all three tiers, is
[`../../docs/gate-dependency-contract.md`](../../docs/gate-dependency-contract.md).

## Three places the no-admission property is held, not just described

An npm-shaped gate that let a browser read a non-refusal as an admission would
be the worst possible outcome of this item, so the property is held mechanically
at every point where it could slip:

1. **In the artifact.** `admitted` is `false` on every arm of the WIT `verdict`
   record, because the packaged gate has no admitting arm to reach. That is
   `crates/revl-gate-wasm`'s property and
   `tests/test_gate_wasm_vector.py` holds it over the whole self-host corpus.

2. **In the types a JS consumer programs against.** WIT has no singleton type,
   so the world can only say `admitted: bool` and `jco` faithfully emits
   `admitted: boolean` — a widening on the one field that must not widen, and
   one that would let a TypeScript consumer write `if (v.admitted) run(x)` with
   the type checker's blessing. `tools/build_gate_js.py` narrows the emitted
   declaration back to the literal `false` as part of packaging, so
   `v.admitted === true` is a compile error (`TS2367: types 'false' and 'true'
   have no overlap`), and `--check-surface` fails the build if the narrowing
   did not take.

3. **In this project's own loader.** `gate.mjs` wraps `admit` so a verdict that
   is not one of the three known arms, or that carries `admitted` as anything
   but `false`, raises `UntrustedVerdict` instead of being interpreted. A
   stale, swapped or tampered `dist/` therefore fails closed rather than
   arriving as a non-refusal.

And the pattern the harness exists to make copyable: `loadOrRefuse` in
`gate.mjs` has **no code path that instantiates anything on this gate's word
alone**. Without a reference verdict it throws `EscalationRequired`, and there
is no flag to skip that.

## What is in here

```
gate.mjs            the gate as a module: the two decisions, the two enforcement
                    layers, the fail-closed wrappers. The only revl import.
prefilter.mjs       a CLI over candidates/, the JS sibling of the rust example
worker.mjs          the serverless shape: an edge `fetch` handler, 403/202
browser/index.html  the browser demo: both enforcement layers, live
serve.mjs           40 lines of node:http so the demo has an origin
candidates/*.rvl    four worked candidates, one per arm plus the cross-tier case
artifacts/          a stand-in compiled artifact for the substrate layer
dist/               the transpiled gate. NOT committed; `npm run build` writes it
```

## Building and running

```
npm install                 # jco, pinned
npm run build               # -> dist/ (needs cargo + the wasm32 target,
                            #    wasm-tools, and this repo's Python for the
                            #    build script only; the OUTPUT needs neither)
npm run prefilter           # the CLI over candidates/
node worker.mjs             # the edge handler on :8788
node serve.mjs              # then open http://127.0.0.1:8787/browser/
```

`npm run build` is `python3 ../../tools/build_gate_js.py --out ./dist
--check-surface`, which builds the component from `crates/revl-gate-wasm` and
transpiles it. Nothing under `dist/` is committed, for the same reason the
`.wasm` is not: transpiled JS is a function of an exact `jco` and an exact
rustc, and a committed copy would red on every bump while proving nothing. What
is committed is the crate source (drift-gated), this project, and
`tests/test_gate_consumer_example_js.py`, which transpiles freshly and runs it.

The four candidates are worked examples, not decoration:

| file | this gate | the py reference gate | why it is here |
|---|---|---|---|
| `undeclared_tool.rvl` | `REJECT`, `G1` | refuses | a component reaching `db` it never declared, refused locally in the tab with no server and no Python |
| `unmarked_emission_tool.rvl` | `REJECT`, `G4` | refuses | a plain-declared method reaching an emission extern |
| `double_tool.rvl` | `ESCALATE`, `no-objection` | **admits** | the cross-tier case: py ADMITS this, and this gate merely has nothing to refuse. Reading the second as the first is the mistake the contract exists to prevent |
| `digit_tool.rvl` | `ESCALATE`, `outside-frontier` | admits | uses `.is_digit()`, a reference builtin the self-host does not lower, so the gate declines to decide rather than guessing |

## The double enforcement, which is what the browser demo is for

The design (`docs/design/335-wasm-edge-gate.md` §4) is explicit that a gate is a
checker and not a runtime: it returns a verdict, and the host's loader is the
code that must refuse to instantiate. A gate whose verdict nobody consults gates
nothing. So the demo shows both layers, and shows that they are independent:

* **Layer 1, the decision.** `gate.admit(source)`. On `"refused"` the artifact
  is never fetched, never compiled, never instantiated. Pick
  `undeclared_tool.rvl` and press *Instantiate*: it does not.
* **The escalation gap.** A non-refusal is not an admission, so
  instantiation still does not happen. Pick `double_tool.rvl` (which py
  admits) and press *Instantiate*: `EscalationRequired`. Tick the reference
  box, which stands in for `revl compile` answering, and it proceeds.
* **Layer 2, the substrate.** The artifact declares its reach as wasm
  imports, and the host instantiates it with an import object shaped by the
  policy and nothing else. Untick `revl:host/db` and the wasm engine, not
  this page's code and not the gate's, refuses to link it. That is item 289's
  invariant applied at the edge: an ungranted reach is a missing import
  refused by the substrate itself.

`artifacts/reaching_tool.wat` is the artifact for layer 2, committed as
readable source and assembled to `reaching_tool.wasm` with `wasm-tools parse`.
The test re-assembles it and checks the committed bytes still match, so the
binary in this directory is never something a reader has to take on trust. It is
a stand-in rather than a compiled candidate on purpose: the layer-2
demonstration is about the import object, and nothing about it should imply the
gate had a hand in producing the artifact.

## Measured

On one machine (Apple Silicon, rustc 1.85.1, jco 1.32.1, Chrome 148), against
the playground's Pyodide lane as the baseline this item exists to beat:

| | this lane | the Pyodide lane |
|---|---|---|
| what ships to the client | 354 KB core wasm + 84 KB JS | a 1.4 MB revl wheel + the Pyodide runtime |
| cold load to first verdict | ~25 ms module load | interpreter boot, seconds |
| per `admit`, one-line program | ~15 ms no-objection, ~33 ms refusal | reference-full, but only after boot |
| coverage | the self-host frontier | the full reference language |

Those last two rows are the honest trade in both directions: this lane is the
cheap local **refusal**, and Pyodide remains the browser's full-coverage
fallback for everything this gate escalates.

## What this project deliberately does not do

* **It never admits.** There is no third decision, and the strings `REGISTER`
  and `ADMIT` appear nowhere in its output.
  `tests/test_gate_consumer_example_js.py` holds that as a test.
* **No layer 2 of the gate API.** The witnessed runtime, commit/abort, the
  approver seam and WAL recovery are `revl.gate.Gate` on py: stateful,
  single-process, py-only. An edge gate is a checker, not a runtime.
* **No `admit-artifact`.** It is exported by the component so its arrival is
  additive, and it declines today: the item-289 chain's declared-caps leg is
  the G8 boundary projection, which has no native port (design slice 3). A
  guessed declared set would be exactly the wave-through this arc prevents. The
  demo's layer 2 is therefore the substrate half of the chain only, and says so.
* **No `compile_to`.** The self-host emitters still carry `@py`-only helper
  externs, so the crate has no native emitter to call and the wasm packaging
  inherits that unchanged (roadmap item 332 Stage 4).
* **No npm publish.** `revl-gate` is not on crates.io and this is not on npm
  either; publishing rides revl's release path (item 338's remaining step).
  Until then a consumer builds `dist/` the way this project does.
