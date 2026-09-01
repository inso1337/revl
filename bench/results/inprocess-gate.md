# In-process admission gate (item 333, Slice 1, py)

An agent tool-generation loop embeds revl's admission gate as a native function
and admits every component it proposes IN ITS OWN PROCESS - no `revl mcp serve`,
no IPC, no wire. The verdict the loop gets in-process is the SAME verdict the
reference `revl` admission path gives; that identity is the whole product claim
and the whole security claim. This file records the proof and the cost, produced
by `bench/inprocess_gate_harness.py`.

Gate surface: `api=1.0.0`, `language=2.0.0`,
`frontier=reference-full:2.0.0` (logged so a drift caused by a version skew is
attributable, design A4).

## Verdict identity: in-process == reference admission oracle

Each candidate is admitted in-process (`revl.gate.admit` / `admit_into`) and,
independently, by the reference ADMISSION oracle run as a FRESH subprocess
constructed from the IDENTICAL inputs. The oracle is the admission path
(`compile_source` + `refuse_admission`), **not** `revl compile` - see below. The
API-stable verdict `(admitted, code)` must match for every candidate.

| candidate | entry | in-process | oracle | match |
|---|---|---|---|---|
| `cache_layer` | admit_into | admit | admit | yes |
| `standalone_twin` | admit | admit | admit | yes |
| `calls_missing_method` | admit_into | refuse | refuse | yes |
| `incomplete_provide` | admit_into | refuse | refuse | yes |
| `redeclare_running_service` | admit_into | refuse | refuse | yes |
| `syntax_error` | admit | refuse | refuse | yes |
| `hole_draft` | admit | refuse (T3) | refuse (T3) | yes |
| `hole_draft_into` | admit_into | refuse (T3) | refuse (T3) | yes |

2 admitted, 6 refused; **every in-process verdict matches the
reference admission oracle**. Verdicts are order-independent: admitting the batch
in a fixed order and in a shuffled order in the same process yields identical
per-candidate verdicts (holds), which is the property that proves the
layer-1 gate is stateless (design A2).

### The oracle is the admission path, not `revl compile` (design A1, CRITICAL)

`revl compile` is a CHECK verb: it does not call `refuse_admission`, so it
accepts a draft-with-holes at exit 0. The batch includes a hole-draft candidate
(`hole_draft`) that the admission gate REFUSES (code T3) both in-process and via
the oracle. A naive `revl compile` oracle, run on that same draft here, ACCEPTS
it (exit 0) - so oracling against `revl compile` would report a spurious
mismatch, or tempt stripping `refuse_admission` from `admit` and reintroducing
the false-admit the gate exists to close. The hole-draft candidate makes that
wrong wiring fail loudly.

## Cost: a distribution that scales with candidate and manifest size

The per-candidate round-trip is `compile_source(candidate, manifest=running)` +
`refuse_admission`: parse + check/lower the candidate + link it against the
running manifest. No disk I/O, network, or toolchain is in the timed section.
The cost is **not** a single universal constant: parse/check/lower dominate and
scale with candidate source size, and the G2/G3 link scales with running +
candidate declaration count. So it is reported as a distribution over cells
spanning small/medium/large candidates against small/large manifests.

| candidate size | manifest components | median (ms) | p90 (ms) | p99 (ms) | samples |
|---|---|---|---|---|---|
| small (3 methods) | 2 | 0.409 | 0.446 | 0.848 | 400 |
| medium (12 methods) | 2 | 0.962 | 1.168 | 3.344 | 400 |
| large (48 methods) | 2 | 3.358 | 3.816 | 4.219 | 400 |
| small (3 methods) | 22 | 0.471 | 0.485 | 2.768 | 400 |
| medium (12 methods) | 22 | 1.092 | 1.390 | 3.211 | 400 |
| large (48 methods) | 22 | 3.435 | 3.697 | 6.005 | 400 |

**Headline: tenths of a millisecond per candidate on the representative
scenario, scaling with candidate and manifest size.** The representative
scenario (the item-16 candidate/manifest) measured median
**0.276 ms** (p90 0.413 ms, p99 0.756 ms,
n=400) here, comparable to item 16's committed ~0.165 ms. Across all
cells the median ranged 0.409-3.435 ms; the cell table
shows how a larger model-authored component or a larger held composition raises
the round-trip - roughly with size, no super-linear phase observed.

## Why in-process matters: hop + tokens removed

The alternative to the in-process embed is the MCP bridge (docs/mcp-bridge.md),
where every candidate costs a process hop plus a JSON-RPC request/response whose
schema and result are output tokens the agent pays for on every candidate
(item 50, token economy). The in-process gate removes BOTH the hop and the
tokens: the verdict is a native return value at the cost measured above and zero
tokens. This is a structural difference, stated as such; a head-to-head bridge
latency number is only worth producing against a bridge that is already stood
up, not a strawman invented to lose.

## The honest boundary: this admits at COMPILE time, it does not confine the runtime

A component the gate REFUSES never runs in the embedder's process (the embedder
gates on the verdict before it loads or calls the component). A component the
gate ADMITS carries the reference's compile-time guarantees: it type-checks, has
no open holes, its effects are classified, its requires/provides resolve against
the running composition. The gate does NOT sandbox the admitted code as it runs:
an admitted emission still fires when called, and an `extern` host block is
arbitrary host code the gate SURFACES (G8), not neuters. **`admitted` is not
`safe to run unwitnessed`.** The reversible-run half - embed the gate plus the
witnessed-effect runtime, run the admitted tool under revertible effects, roll
back residue-free on abort - is item 334.

## Re-run

```
python bench/inprocess_gate_harness.py            # run, print, verify
python bench/inprocess_gate_harness.py --write     # rewrite this file
```

A guard test, `tests/test_inprocess_gate.py`, runs a small batch in CI: it
asserts every in-process verdict matches its admission oracle (the hole draft
included), that the naive `revl compile` oracle disagrees on the hole draft
(proving the oracle is the admission verb), that verdicts are order-independent,
and applies only a generous order-of-magnitude latency ceiling (machines vary).

## Methodology

- **Machine:** Darwin 25.2.0 · Apple M1 Max · Python 3.14.3
- **Base composition:** `Store` provider `Kv` + consumer `App`, reused from
  `bench/admission_latency.py` (item 16), compiled once outside every timer.
- **Batch:** 8 candidates spanning both verdict directions and both
  entry points (`admit`, `admit_into`), including two hole-draft probes. Defined
  in `bench/inprocess_gate_harness.py::correctness_batch`.
- **Oracle:** a fresh subprocess per candidate running the SAME admission engine
  (`compile_source` + `refuse_admission`) on the SAME inputs; `admit_into`
  candidates recompile the SAME base source into the SAME manifest.
- **Distribution:** 400 timed iterations per cell; median/p90/p99
  reported, never a single lucky sample.
- **Determinism:** pure `compile_source`/`admit_into` on in-memory strings - no
  disk I/O in the timed section, no network, no toolchain.
