# agent-ci-gate — a third-party consumer of `revl.gate`

This directory is a standalone project, not part of revl. It has its own
`pyproject.toml` declaring `revl` as a dependency, and `ci_gate.py` is what a
CI system, an MCP server, or an agent framework looks like once it depends on
revl's admission gate as a library instead of shelling out to the `revl` CLI
(roadmap item 338, `docs/design/338-revl-as-dependency.md`).

## The contract this project embeds against, first

**A refusal is authoritative and fail-closed. An admission is a compile-time
judgment scoped to the gate's `frontier`, not a runtime confinement. The
runtime half is a separate, py-only dependency this project does not use.
The gate never confines its host.**

Read each clause before reading the code:

1. **A refusal is dependable.** If `revl.gate.admit(source)` returns
   `admitted = False`, the reference `revl` compiler would refuse `source`
   too. `ci_gate.py` treats that as final: a refused candidate is reported
   and never registered, loaded, or run.

2. **An admission is not a safety verdict.** `admitted = True` means the
   candidate type-checks, has no open holes, and its effects are classified
   — as of the frontier named in `gate_version()['frontier']`. It does **not**
   mean the candidate is sandboxed once it runs: a granted `extern` host body
   is arbitrary host code the gate *surfaced*, not code it neutered. This
   project stops at "eligible to register" for exactly this reason — it never
   claims a bare admission makes a candidate safe to execute unwitnessed.

3. **Revertible execution is a separate adoption.** The witnessed-effect
   runtime, commit/abort, and the approver seam
   (`revl.gate.Gate`, item 334's `propose`) live in a py-only, single-process
   layer this project does not import. A consumer that wants "admitted AND
   run revertibly" adopts that layer explicitly; `ci_gate.py` only calls the
   stateless layer-1 `admit`.

4. **The gate does not confine this project's own process.** Depending on
   revl changes nothing about what this project's own code is allowed to do;
   it only changes what candidates this project chooses to trust.

The full contract, written for a consumer rather than a design note, is
[`../../docs/gate-dependency-contract.md`](../../docs/gate-dependency-contract.md).

[`../ecosystem-consumer-rs/`](../ecosystem-consumer-rs/) is the rust sibling of
this project, depending on the native `revl-gate` crate instead of the wheel.
It is deliberately not the same program: that gate issues NO admissions (it
decides the composition and guarantee layer, not the reference type layer), so
its consumer has only two decisions, reject on a refusal and escalate on
everything else. Clause 2's "scoped to the frontier" is not a hypothetical
across those two directories — `double_tool.rvl` below is admitted here and
merely not-refused there.

## What `ci_gate.py` actually does

```
python ci_gate.py candidates/
```

Walks `candidates/*.rvl`, admits each file with `revl.gate.admit`, and prints
one line per candidate: `REGISTER` for an admission, `REFUSE <code> —
<message>` for a refusal. It logs `gate_version()` once per run, and every
cached or reported verdict carries the `frontier` that produced it — the
field a consumer must record with any admission it caches or transmits,
because a verdict from a different gate tier (a narrower rust or wasm
frontier, once those ship) is not the same fact as this one.

The three files under `candidates/` are worked examples, not decoration:

| file | verdict | why |
|---|---|---|
| `double_tool.rvl` | admitted | a complete, self-contained tool component |
| `leaky_tool.rvl` | refused | `requires`s a service nothing in the file provides — an agent proposing a dependency it was never granted |
| `draft_tool.rvl` | refused, code `T3` | compiles as a checkable draft (a typed hole is legal to write) but is refused by the SAME admission gate `revl run` applies — proof that `admit` is the real admission decision, not `compile_source` alone |

## `compile_gate.py` — the programmatic-compile half of the surface

`ci_gate.py` above uses the ADMIT half of `revl.gate` (a verdict). The promised
surface also carries `compile_to(source, tier)`, which returns that same verdict
*plus*, on admission, the emitted target source for a reference backend.
`compile_gate.py` is the sibling consumer that uses it:

```
python compile_gate.py candidates/ --tier py --out dist/
```

Walks `candidates/*.rvl`, compiles each to the chosen `--tier`, and gates the
EMIT decision on the SAME verdict `ci_gate.py` gates `REGISTER` on: it writes
real target source under `--out` **only for an admission**, prints `REFUSE
<code> — <message>` for a refusal, and writes nothing for one. The asymmetric
contract is identical, one field wider:

- a **refusal** is authoritative — `emit.output` is `None`, there is no target
  source to write, and nothing is emitted, written, or run for the candidate;
- an **admission plus emitted `output`** is a compile-time judgment scoped to
  `gate_version()['frontier']`, **not** a guarantee the emitted program is safe
  to run unwitnessed — this project stops at "wrote it for a human/operator to
  review", never executing what it emits;
- an **unknown `--tier`** is a control verdict (`UNKNOWN_TIER`), fail closed, so
  a bad tier can never be mistaken for an emission.

`draft_tool.rvl` (the typed-hole draft) is the sharp case: it *compiles* as a
checkable draft but may never run, so `compile_to` refuses it with the same
`T3` admission diagnostic `admit` gives and emits nothing — `compile_to` never
emits target source `admit` would refuse. `tests/test_gate_consumer_compile.py`
runs this script against a freshly assembled, isolated copy of the packaged
`revl` (emitters shipped inside it as `revl/backends`, the installed-wheel
layout), proving the compile surface works as a dependency, not an in-tree
import.

## The versioning obligation this project honors

`VerdictCache` in `ci_gate.py` keys every stored verdict on the full
`gate_version()` triple — `api`, `language`, and `frontier` together, never
`language` alone. A revl release that bumps `language` (a new construct
admitted or refused differently) invalidates every previously cached verdict
under the old key automatically: the cache simply has no entry under the new
key, so the candidate is re-admitted rather than served a stale "admitted"
from before the language moved
(`docs/design/338-revl-as-dependency.md`, "Language skew").

## Running it

This directory ships as a standalone project on purpose: `pyproject.toml`
declares `revl` as its only dependency, and `ci_gate.py` imports nothing else
under `revl.*`. From inside a revl checkout, run it with the checkout's `src/`
on `PYTHONPATH` in place of a real `pip install`:

```sh
PYTHONPATH=/path/to/revl/src python examples/ecosystem-consumer/ci_gate.py \
    examples/ecosystem-consumer/candidates/
```

`tests/test_gate_consumer_example.py` runs this same script against a
freshly assembled, isolated copy of the installed `revl` package (not the
checkout on `sys.path`), which is what proves this example exercises the
PACKAGED surface `pip install revl` ships, not an in-tree import.
