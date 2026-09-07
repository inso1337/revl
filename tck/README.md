# revl runtime TCK — the seventh runtime's weekend

A **published compatibility kit** for revl's runtime contract. It turns the
R1–R5 runtime requirements and the A1–A8 / G7 semantics into an **executable
suite any candidate runtime adapter runs**, ending in a conformance report:
per-requirement *pass* / *pending* / *pinned divergence*, with the known
divergences pinned so the report changes only deliberately.

This generalizes what stc-go proved. A third party built a Go runtime, and the
hand-written scenario reference was "the executable oracle the emitter targets"
(`backends/go/README.md`). The TCK lifts that oracle out of one backend: the
scenarios and their expected observations live here, language-agnostic, and a
runtime proves itself by running them. Tier seven can arrive as a PR with a
green TCK report instead of a first-party integration wave.

## The contract, as runnable cases

| req | case id | what it asserts | source |
|---|---|---|---|
| **R1** | `r1_lifo_recovery` | Unload runs accumulated undos newest-first, including effects from provide-method calls while active. | backend-ir.md |
| **R2** | `r2_reactive_resolution` | Activates only when every requirement is provided; deactivates on withdrawal; reactivates against a replacement (fresh activation). | backend-ir.md |
| **R3** | `r3_withdrawal_ordering` | Dependents fully deactivate before the provider tears down; an `undo` may still call its required service. | backend-ir.md |
| **R4** | `r4_no_residue` | After unloading everything, the host holds no bindings, listeners, or effects. | backend-ir.md |
| **R5** | `r5_derived_withdrawal` | Disposing a provider withdraws its provision and deactivates dependents purely through the runtime's revertible provide/set. | backend-ir.md |
| **A1** | `a1_divert_at_boundary` | A divert during a component `await` skips every later step; the accumulated inverse still runs. | contract-errata.md |
| **A5** | `a5a_compensate_discharged` | A clean unload DISCHARGES an `emit`'s `compensate`: it never runs, the forward emission survives, only the bracket inverse replays. | contract-errata.md |
| **A5** | `a5b_two_phase_abort` | On an abort, Phase 1 replays proof inverses LIFO to completion, THEN Phase 2 runs compensations LIFO — the `compensate` DELETE fires after the earlier bracket unlock. | contract-errata.md |
| **A8** | `a8_sync_failure_contained` | A mid-body acquire failure reverts accumulated effects, lands the fiber FAILED, leaves siblings unaffected (L-Raise). | contract-errata.md |
| **A8** | `a8_async_body_failure` | The same containment for an `await`-containing (async) body: inverses run LIFO, no residue, fiber lands FAILED. | contract-errata.md |
| **G7** | `g7_lifo_complete_teardown` | Provisions + method-time effects + activation inverses recover newest-first in one drain; every dependent inverse precedes the provider's close. | contract-errata.md |

**A2, A3, A4, A6, A7** are compile-time / lowering / advisory obligations on the
*emitter and checker* (linker rule, host-name renaming, `$$` escaping, typed
service methods, advisory emission flags). A runtime adapter cannot exercise
them, so the kit lists them and reports them **pending**, pointing at where they
are actually enforced. They are never rendered green — "nothing checked" must
never read as "clean".

## Pinned divergences

Two known divergences are pinned, keyed to the runtime family that exhibits
them, exactly like `tests/test_conformance_validate.py`'s baseline and the
`DIVERGENCES` pins in `tests/test_cross_tier_execution.py`:

- **A8 async body — `cordis-py`.** An async body routes the setup failure to
  `_make_effect_guard`: the inverses run LIFO with no residue (A8 containment
  holds) but the fiber lands `ACTIVE` instead of `FAILED`. A candidate that
  matches cordis-py reports the *same* pinned divergence.
- **A1 boundary — `cordis-rs`.** Activation is driven to completion under the
  fiber transition lock, so post-boundary emissions run and are then reverted
  LIFO. Torn-state freedom holds; "no emission after divert" does not. Pinned
  in `backends/rust/scenarios/scenarios.rs`.

The pin discipline (in `runner.py`) is the point: for the runtime that owns a
pin, matching it is a recorded divergence, but **a pinned case that starts
meeting the ideal fails the kit** — so a report can only change when someone
re-baselines a pin deliberately. A runtime *not* pinned for a case that behaves
like some other runtime's known bug fails too — that is a finding, not a pass.

## Hostile-wire seam-envelope section (issue #475)

The cases above score a runtime's *semantics*. A second conformance section
scores the runtime's *wire*: how the sealed seam envelope
(`revl.deploy.Correlation` + `CorrelationGuard`) behaves when the transport
underneath it is adversarial. The shapes are protocol-agnostic on purpose, so
each is a property of the envelope rather than of a transport:

- **truncated** sealed envelope, refuses and leaves no ledger residue;
- **reordered** pair of correlated calls, correlates off the envelope not
  arrival order, with no order-accept path that admits a replay;
- **duplicated** frame, never silently deduped by the frame layer, a keyed
  replay is an explicit `duplicate-envelope` verdict;
- **partition** mid-envelope, the partial is a non-crossing, never dispatched,
  no residue;
- **reconnect-storm** soak, a consumer that bounces the connection over and
  over, the seam stays intact and dispatches exactly the whole crossings.

These live at two altitudes in `tests/test_hostile_wire_tck.py`: a
protocol-agnostic property suite over `CorrelationGuard.admit`, and a
system-level suite driving a real `bridge.serve` UDS provider byte by byte with
a raw socket. They run pure-python (no runtime adapter, no toolchain) and are
gated in the `conformance` CI job. One capability the sealed envelope does not
have — a per-crossing sequence number for in-order-delivery *enforcement* — is
called out as a `strict` `xfail` rather than faked; it flips to a real ordering
test if a `sequence` field ever lands on `Correlation` (rides with the F8
network seam, #421 / #107 T3).

```sh
pytest tests/test_hostile_wire_tck.py -q
```

## Running it

The worked example drives the real cordis-py runtime, so run it under the python
backend's venv (where `cordis` resolves; `backends/python/setup.sh` builds it):

```sh
backends/python/.venv/bin/python -m tck.conformance --adapter py
backends/python/.venv/bin/python -m tck.conformance --adapter py --json   # for CI
```

Exit status is 0 when the run is OK (every case passed, is a pinned divergence,
or is honestly pending) and 1 on any failure — including a stale pin.

The reference run today:

```
summary : 9 pass, 1 pinned divergence, 5 pending, 0 fail
verdict : OK (pending is not pass; a stale pin fails)
```

R1–R5, A1, A5, A8(sync) and G7 are **green** against cordis-py; the A8 async
body is the one **pinned divergence** (same as cordis-py, by construction); the
five compile-time amendments are **pending**.

The kit's own discipline is tested with synthetic adapters (no runtime needed):

```sh
backends/python/.venv/bin/python -m pytest tck/tests/test_tck.py -q
```

## Reading the report

One line per case: `PASS`, `DIV` (pinned divergence — expected, recorded),
`PEND` (not exercised — never a pass), `FAIL`. The verdict is `OK` unless
something failed. A `DIV` line names the divergence and the runtime family it
belongs to; a `PEND` line says why it was not exercised and where the
requirement is actually enforced.

## Writing an adapter — the "tier seven" path

An adapter is one small class (`tck/adapter.py`, `RuntimeAdapter`):

- `name` — your runtime family, e.g. `"cordis-py"`. Load-bearing: it is how a
  pinned divergence is attributed. A brand-new runtime uses a new name and is
  held to the *ideal* on every case (it inherits nobody's pins).
- `runtime_version` — informational.
- `supports(case_id) -> bool` — return `False` for a case you cannot yet drive;
  the kit reports it *pending*, never green.
- `run(case_id) -> Observation` — run the scenario against your real runtime and
  return a normalized `Observation`:
  - `trace` — ordered host-op log, instance serials stripped (`map#3.drop` ->
    `map.drop`);
  - `states` — terminal lifecycle state per component label (`ACTIVE` /
    `PENDING` / `FAILED`);
  - `residue` — post-unload host introspection (`store`, `registry`, `hooks`,
    `disposables`);
  - `errors` — count of teardown faults the host logged.

The scenario *specifications* — what components, what operations, what to
observe — are the cases in `tck/spec.py` plus the worked implementation in
`tck/adapters/py_adapter.py` to read as a template. A runtime in another
language typically implements the scenarios as its own hand-written reference
(the stc-go / cordis-rs `scenarios/` model), emits a canonical JSON observation,
and the adapter parses it back with `Observation.from_json`. Register the
adapter's factory and run:

```sh
python -m tck.conformance --adapter your_pkg.your_adapter:build
```

A green (or pinned-divergence-only) report is the artifact that ships the
seventh runtime.

## Layout

| file | role |
|---|---|
| `adapter.py` | the `RuntimeAdapter` interface + normalized `Observation`. |
| `spec.py` | the R1–R5 / A1–A8 / G7 catalog: each case's oracle and pinned divergences. |
| `runner.py` | drives an adapter over the catalog and scores each case (the pin discipline). |
| `report.py` | renders a report as text or JSON. |
| `conformance.py` | CLI (`python -m tck.conformance`). |
| `adapters/py_adapter.py` | worked example: the real cordis-py runtime, driven in-process. |
| `tests/test_tck.py` | the kit's own tests: the scoring discipline + a py smoke run. |

The kit only *reads* `backends/*`, `examples/*`, and `docs/*`; it never edits
them.
