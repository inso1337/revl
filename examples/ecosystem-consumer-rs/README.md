# agent-prefilter — a third-party consumer of the `revl-gate` crate

This directory is a standalone project, not part of revl. It has its own
`Cargo.toml` declaring `revl-gate` as its only dependency, and `src/main.rs`
is what a CI system, an MCP server, or an agent framework looks like once it
pulls revl's **native** admission gate in as a rust library instead of
shelling out to the `revl` CLI (roadmap item 338,
`docs/design/338-revl-as-dependency.md`).

It is the rust sibling of [`../ecosystem-consumer/`](../ecosystem-consumer/),
which does the same thing on py — and the two are deliberately not the same
program, because **the two tiers do not give the same guarantee.**

## The contract this project embeds against, first

**A refusal is authoritative and fail-closed. This gate issues no admissions
at all. Everything that is not a refusal is escalated to the reference
toolchain. The gate never confines its host.**

The py gate (`pip install revl`, `revl.gate.admit`) is the full reference
compiler, so it can both refuse and admit. This crate cannot. It compiles
`selfhost/lower.rvl`'s `admit_src` to rust: the composition and guarantee
layer (`G1`..`G4`, `A1`, `PRELUDE`, and parse failures as `BAD`) and **not**
the reference type layer. So it has three arms, and none of them is an
admission:

| arm | what it means | what this project does |
|---|---|---|
| `Refused { code, message }` | the reference compiler refuses this source too, same code, same message verbatim | `REJECT` — final; nothing about the candidate is loaded, registered or run |
| `NoObjection` | "this gate found nothing it is able to refuse". A type-incorrect program lands here | `ESCALATE` — ask the reference gate |
| `OutsideFrontier { reason }` | the gate is not entitled to decide this source at all (a construct outside its frontier table, an oversized source, an abort in the native front end) | `ESCALATE` — ask the reference gate |

Refusing what the reference admits would be an inconvenience. **Admitting what
the reference refuses is the defect class the admission-gate arc exists to
prevent**, and a gate with no admission arm cannot commit it. That asymmetry
is the whole reason a native gate is worth depending on: it buys a local,
in-process, Python-free REFUSAL that byte-agrees with the reference — a cheap
pre-filter in front of an expensive authoritative check, never a replacement
for one.

The words `REGISTER` and `ADMIT` therefore appear nowhere in this project's
output, and `tests/test_gate_consumer_example_rs.py` holds that as a test.

The full consumer-facing contract, both tiers, is
[`../../docs/gate-dependency-contract.md`](../../docs/gate-dependency-contract.md).

## What `src/main.rs` actually does

```
agent-prefilter candidates/           # human log
agent-prefilter candidates/ --json    # machine-readable summary
```

Walks `candidates/*.rvl`, pre-filters each file with `revl_gate::admit`, and
prints one line per candidate. It logs `gate_version()` once per run —
**four** fields here, not py's three:

```
gate_version: api=1.0.0 language=2.0.0 frontier=selfhost-admit:<hash>
gate_layer: composition + guarantee layer (G1..G4, A1, PRELUDE) and parse (BAD); NOT the reference type layer
```

`layer` is the field that tells a consumer what was actually decided, and
`frontier` is the field that stops this verdict being confused with a py one.
Both tiers report the same `language`; their `frontier` values differ
(`selfhost-admit:<hash>` here, `reference-full:<language>` on py), which is
exactly why a verdict is only a fact together with the frontier that produced
it.

The four files under `candidates/` are worked examples, not decoration — one
per arm, plus the cross-tier case:

| file | this gate | the py reference gate | why it is here |
|---|---|---|---|
| `undeclared_tool.rvl` | `REJECT`, `G1` | refuses | a component reaching `db` it never declared — the native gate refuses it locally, verbatim, with no Python on the machine |
| `unmarked_emission_tool.rvl` | `REJECT`, `G4` | refuses | a plain-declared method reaching an emission extern |
| `double_tool.rvl` | `ESCALATE`, `no_objection` | **admits** | the cross-tier case: py ADMITS this, and this gate merely has nothing to refuse. Reading the second as the first is the mistake the contract exists to prevent |
| `digit_tool.rvl` | `ESCALATE`, `outside_frontier` | admits | uses `.is_digit()`, a reference builtin the self-host does not lower, so the gate declines to decide rather than guessing |

## The versioning obligation this project honors

`VerdictCache` in `src/main.rs` keys every stored verdict on the full
`gate_version()` triple — `api`, `language` and `frontier` together, never
`language` alone. Two gates at the same `language` and different `frontier`
disagree on the same source by construction (see `double_tool.rvl` above), so
a cached verdict is valid only for the exact gate that produced it, and every
stored record carries its `frontier`
(`docs/design/338-revl-as-dependency.md`, "Frontier skew").

## Depending on it

`revl-gate` is not on crates.io yet — publishing is 338's remaining step and
rides revl's release path. Until then an out-of-tree consumer vendors or
checks out `crates/revl-gate` and points a path dependency at it, which is
exactly what this directory's `Cargo.toml` does:

```toml
[dependencies]
revl-gate = { path = "../../crates/revl-gate" }
```

Once the crate is published that line becomes `cargo add revl-gate` and
nothing else about the project changes: the imported names, the arms, and the
contract above are all already the published surface. (A git dependency is not
an option today — the repo has no root `Cargo.toml`, so cargo has nothing to
resolve `revl-gate` from without the path.)

The crate builds with **no Python on the machine** — that is why
`crates/revl-gate` is committed generated source rather than something a build
script produces. `tests/test_gate_consumer_example_rs.py` builds this example
from a copy outside the checkout, with `PYTHONPATH`/`VIRTUAL_ENV` stripped
from the environment, and runs it against `candidates/`.

## What this project deliberately does not do

* **No layer 2.** The witnessed runtime, commit/abort, the approver seam and
  WAL recovery are `revl.gate.Gate` on py — stateful, single-process, and
  py-only. `revl_gate::session` is a reserved empty module (item 334). A rust
  consumer gets a verdict, and owns whatever runtime discipline its code runs
  under.
* **No `admit_into`.** Admission INTO a running composition spans a manifest;
  the self-host pipeline has no manifest parameter, and a stub that ignored one
  would be the wave-through this crate exists to prevent. Use
  `revl.gate.admit_into` on py.
* **No `compile_to` output.** The self-host emitters still carry `@py`-only
  helper externs; the function is exported and refuses unconditionally.
