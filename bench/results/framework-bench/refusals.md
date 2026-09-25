# The refused column: the cost of the guarantee

Every count below is recomputed from a committed ledger at the moment
this file was generated. None of them is transcribed from prose in a
roadmap item or a design note, because those go stale and this
repository has held several different totals for the same quantity at
the same time. Each row names the gate that fails when the ledger
drifts.

## Documents the fully-native chain will not reproduce

Source: `tests/test_selfhost_compile.py:LOWER_GAP_DOCS`  
Gate: `pytest tests/test_selfhost_compile.py::test_the_residual_is_located_in_lower_not_in_the_emitter`

These are documents the reference compiler accepts. A raw
TypeScript host and an agent framework run the equivalent work
without objection; the fully-native revl chain does not reproduce
them. That is the cost, stated first.

| tier | residual | corpus | reproduced |
|---|---:|---:|---:|
| go | **0** | 23 | 23 |
| java | **0** | 59 | 59 |
| py | **5** | 57 | 52 |
| rust | **2** | 40 | 38 |
| ts | **4** | 61 | 57 |
| wasm | **0** | 21 | 21 |
| **total** | **11** | 261 | 250 |

Tiers with no residual: go, java, wasm.

## Constructs the self-host port does not implement

Source: `tests/fixtures/selfhost_blind_spots.json`  
Gate: `python3 tools/selfhost_coverage.py --check`

| tier | unported constructs | distinct reasons | blind |
|---|---:|---:|---:|
| go | **65** | 10 | 4 |
| java | **30** | 14 | 6 |
| py | **28** | 11 | 1 |
| rust | **20** | 7 | 14 |
| ts | **16** | 8 | 4 |
| wasm | **46** | 11 | 4 |
| **total** | **205** | | |

## Dispatch arms no corpus document exercises

Source: `tests/fixtures/oracle_construct_reach_ledger.json`  
Gate: `python3 tools/oracle_construct_reach.py --check`

This is a weaker statement than a refusal and is listed
separately for that reason: an unreached arm is untested, not
known-broken.

| oracle | unreached arms |
|---|---:|
| emit_go | 69 |
| emit_wasm | 50 |
| emit_java | 36 |
| emit_rust | 34 |
| emit_py | 29 |
| emit_ts | 20 |
| lower_ir | 6 |
| gate_census | 5 |
| compile | 1 |
| **total** | **250** |

## Where the gate is more permissive than the reference

Source: `tools/gate_reference_census_baseline.json`  
Gate: `python3 tools/gate_reference_census.py --check`

The fail-open direction. A suite that published only refusals and
left this out would be making the selective argument it claims to
be correcting.

| bucket | programs |
|---|---:|
| **total** | **0** |

