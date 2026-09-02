# Depending on revl: the security contract (`revl.gate`)

This is for a host that does `pip install revl` and calls
`from revl.gate import admit, gate_version, ...` directly — an MCP server, a
CI system, an agent framework, a registry — rather than shelling out to the
`revl` command. Read this before writing a line of integration code.

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

## What is and is not deliverable today

The py surface above ships now: `pip install revl` and
`from revl.gate import ...` works today, in-process, with no subprocess and
no wire. `cargo add revl-gate` and `npm i` the wasm gate are named in
`docs/design/338-revl-as-dependency.md` as the polyglot ecosystem exit and
are frontier-gated on work items 332 (the rust crate) and 335 (wasm) that
have not landed. This document describes the py contract only; a native gate
will publish the identical asymmetric contract once it ships, with a
narrower `frontier`.

See also: [`docs/design/338-revl-as-dependency.md`](design/338-revl-as-dependency.md)
for the full design and its adversarial review;
[`docs/stability.md`](stability.md) for what a revl version number promises
more broadly; [`src/revl/gate.py`](../src/revl/gate.py) for the module this
document is a consumer-facing translation of.
