# Depending on revl: the security contract (`revl.gate`)

This is for a host that depends on revl's admission gate as a LIBRARY — an
MCP server, a CI system, an agent framework, a registry — rather than
shelling out to the `revl` command. Read this before writing a line of
integration code.

Two dependency forms exist, and they do not give the same guarantee. Sections
one to five below are the py wheel (`pip install revl`, then
`from revl.gate import admit, gate_version, ...`), which is the full
reference compiler and can both refuse and admit.
["The rust tier"](#the-rust-tier-the-revl-gate-crate) is the native
`revl-gate` crate, which can only REFUSE — it issues no admissions at all.
The contract is the same asymmetric one in both; the crate simply has less of
the admitting half, none of it.

## The contract, stated once

**A refusal is authoritative and fail-closed. An admission is a
compile-time judgment scoped to `gate_version().frontier`, not a runtime
confinement. The runtime half is a separate, py-only dependency you adopt
explicitly. The gate never confines its host.**

That sentence, not "revl as a safety kernel," is what you are allowed to
build on. "Safety kernel" describes an aspiration for the full stack — admit,
plus the py-only revertible runtime, plus operator-configured confinement —
never the guarantee a bare `admit(source)` call gives you. A host that reads
"admitted" as "safe to run" has built an unsafe system on a sentence revl
never promised.

## Clause 1: a refusal is dependable

If `admit(source).admitted` is `False`, the reference `revl` compiler would
refuse `source` too — same code, same message, verbatim. You may rely on a
refusal absolutely: a component the gate refuses is one the reference
refuses, and you must not run it. This is the strong half of the contract,
and there is no asterisk on it.

## Clause 2: an admission is compile-time, and it is scoped

`admitted = True` means: the source type-checks, has no open holes, its
effects are classified, its requires/provides resolve, and — under the
untrusted-author admission profile — it reaches only what it was granted. It
does **not** mean the admitted code is confined once it runs. An admitted
component's granted `extern` host body is arbitrary host code the gate
*surfaced*, not code it neutered. `admit != safe to run unwitnessed`.

The admission is also scoped to the gate that produced it. `gate_version()`
returns three fields:

```python
{"api": "1.0.0", "language": "2.0.0", "frontier": "reference-full:2.0.0"}
```

- **`api`** — the semver of the `revl.gate` surface itself. Branch your
  integration code on it; pin a compatible range (`api ~= 1.0`).
- **`language`** — the revl language version this gate admits against.
- **`frontier`** — the identifier of what this gate actually COVERS. On the
  py wheel it is `reference-full:<language>`: the whole reference compiler. A
  future native gate (a rust crate, a wasm component) would pin a narrower
  frontier, e.g. `selfhost:<corpus>` — a self-hosted subset of the language.

**`frontier` is not an advanced-user footnote — treat it as part of the
verdict itself.** Two gates at different frontiers can disagree on the same
source. If you cache a verdict, transmit it to another service, or compare it
against a verdict from a different revl deployment, record `frontier`
alongside it. "revl admitted it" is not a portable fact on its own; "revl's
`reference-full:2.0.0` gate admitted it" is.

## Clause 3: the runtime half is a separate adoption

The reversible-execution guarantees — witnessed effects, session
commit/abort, the approver seam, WAL recovery, the self-extension `propose`
verb — live in `revl.gate.Gate`, a stateful, py-only,
single-gate-per-process, synchronous facade. Calling `admit`/`admit_into`
gets you none of this. If you want "admitted AND run revertibly," you adopt
`Gate` explicitly and take on its walls (one live gate per process, no
async). If you only `pip install revl` and call `admit`, you are on layer 1
alone, and you own whatever runtime discipline your admitted code runs
under.

## Clause 4: the gate does not confine you

Depending on revl does not change what your own process is allowed to do. A
library cannot jail its own host. revl's guarantees govern the code it
admits, not the process embedding it.

## The versioning obligations this implies

- **Surface skew.** `revl.gate` adds a function: `api` minor-bumps, your
  pinned range is unaffected (additive-only).
- **Language skew.** revl adds a language feature: a component might be
  re-admitted differently under the new `language`. **If you cache verdicts,
  key the cache on the full `gate_version()` triple** — `api`, `language`,
  `frontier` — so a language bump invalidates stale entries instead of you
  trusting an old admission forever.
- **Frontier skew.** The dangerous direction (a narrower-frontier gate
  admitting what the reference refuses) stays closed by revl's own
  differential release gate. But you must still know WHICH gate's verdict
  you are holding before treating it as authoritative for a different
  deployment.

`examples/ecosystem-consumer/` is a small out-of-tree project that
demonstrates all of the above end to end: it declares `revl` as a dependency
in its own `pyproject.toml`, imports only `revl.gate`, admits a batch of
agent-authored `.rvl` candidates, keys its verdict cache on the full
`gate_version()` triple, and gates its "register" decision on `admitted`
while logging `frontier` with every verdict. Read its `README.md` for the
same contract in a consumer's own words.

## The promised import surface

`revl.gate.__all__` is the entire dependency surface: `Verdict`, `Emit`,
`admit`, `admit_into`, `compile_to`, `gate_version`, `Gate`, `GateError`,
`GateRefused`, `AdmitResult`, `ProposeResult`, `Handle`, `recover`. Nothing
else under `revl.*` is promised. The wheel is not minimal — installing revl
pulls in the whole compiler, the six backends, the stdlib, the MCP server,
and the CLI, and every module under `revl.*` is importable — but only
`revl.gate` is versioned. `tests/test_gate_compat.py` pins this list exactly
in CI, so an added or removed name is a reviewable change, never silent
drift.

**The rule, one line:** branch on `api` and `code`, gate your run/accept
decision on `admitted`, record `frontier` with every verdict you keep, log
`message` but never parse it, and treat anything outside `revl.gate.__all__`
as private and unversioned — you can import it, but a patch release may
change it under you without warning.

## The rust tier: the `revl-gate` crate

Everything above describes the py wheel. There is a second dependency form —
`crates/revl-gate`, the native gate as a rust library — and its contract is
the SAME asymmetric contract with the asymmetry taken further. Read this
before depending on it, because the difference is not a detail:

**The rust gate issues no admissions at all.** It is `selfhost/lower.rvl`'s
`admit_src` compiled to rust: the composition and guarantee layer (`G1`..`G4`,
`A1`, `PRELUDE`, and parse failures as `BAD`), and **not** the reference type
layer. A type-incorrect program is not something it can refuse. So its
`Verdict` has three arms and no `Admitted`:

| arm | meaning | what you may do with it |
|---|---|---|
| `Refused { code, message }` | the reference compiler refuses this source too, same code, same message verbatim | act on it: this is Clause 1, and it is the whole reason to depend on the crate |
| `NoObjection` | "this gate found nothing it is able to refuse" | **not an admission.** Get a reference verdict before accepting or running anything |
| `OutsideFrontier { reason }` | the gate is not entitled to decide this source (a construct outside its generated frontier table, an oversized source, an abort in the native front end) | same: ask the reference |

The wire shape fails closed to match: `Verdict::to_json` reports
`"admitted": false` on **every** arm, so a consumer written against the py
tier's fixed `{admitted, code, message}` shape reads this gate as "never
admits" rather than mistaking a no-objection for an admission. The real arm
travels in an extra `"verdict"` field.

`gate_version()` carries a fourth field here, `layer`, which says in prose
what was decided (`"composition + guarantee layer … NOT the reference type
layer"`). Read it before trusting a non-refusal. And the `frontier` differs
from py's at the same `language` — `selfhost-admit:<hash>` versus
`reference-full:<language>` — which is the frontier skew of §"The versioning
obligations" made concrete: **never serve a verdict cached from one tier to a
reader of the other.**

What the crate buys, then, is not a second admission gate. It is a local,
in-process, **Python-free refusal** that byte-agrees with the reference on the
covered corpus: a cheap pre-filter in front of an expensive authoritative
check. Refusing what the reference admits would be an inconvenience; admitting
what the reference refuses is the defect class this arc exists to prevent, and
a gate with no admission arm cannot commit it.

Also absent, deliberately: `admit_into` (admission into a running composition
spans a manifest the self-host pipeline has no parameter for), `compile_to`
output (exported, refuses unconditionally), and layer 2 —
`revl_gate::session` is a reserved, empty, documented module. The witnessed
runtime half stays py-only.

[`examples/ecosystem-consumer-rs/`](../examples/ecosystem-consumer-rs/) is a
standalone rust project that demonstrates all of this: one dependency, four
candidates covering all three arms plus a py-admitted case, a verdict cache
keyed on the full `gate_version()` triple, and exactly two decisions —
`REJECT` on a refusal and `ESCALATE` on everything else. It never invents an
acceptance the gate did not give, and
`tests/test_gate_consumer_example_rs.py` holds that as a test.

## What is and is not deliverable today

The py surface above ships now: `pip install revl` and
`from revl.gate import ...` works today, in-process, with no subprocess and
no wire. The rust crate ships as committed source in this repo and builds
with no Python on the machine, so a rust consumer can depend on it via a path
dependency today (see the example's `README.md`); what is NOT done is the
PUBLISH step — `revl-gate` is not on crates.io, so `cargo add revl-gate` is
the shape a consumer gets once revl's release path cuts it, not a command
that works right now. `npm i` the wasm gate is named in
`docs/design/338-revl-as-dependency.md` as the remaining polyglot exit and is
frontier-gated on item 335 (cordis-rs on wasm32), which has not landed.

See also: [`docs/design/338-revl-as-dependency.md`](design/338-revl-as-dependency.md)
for the full design and its adversarial review;
[`docs/stability.md`](stability.md) for what a revl version number promises
more broadly; [`src/revl/gate.py`](../src/revl/gate.py) for the module this
document is a consumer-facing translation of.
