# The gate/reference census

GENERATED FILE. Every number below comes from a run of
`tools/census_artifact.py`, which is the only thing that writes it. Do not
edit it by hand: `python3 tools/census_artifact.py --check` reports the
edit as drift. Regenerate with `python3 tools/census_artifact.py --write`.

## What is being measured

revl has two independently written implementations of its own semantics.
`src/revl/*.py` is the reference compiler. `selfhost/*.rvl`, compiled into
`crates/revl-gate`, is the self-host gate. This census runs both over the
same corpus and classifies every disagreement. Agreement means the same
TAG and the same MESSAGE, not merely the same verdict.

Programs in this run: **848**.
Checker version: `GATE-CENSUS-1+809b1804f3f7`.
Engine: `selfhost`.
Run: `census-selfhost-6a226ef14757`.

Neither identity is a commit or a clock. `run` is a sha256 over the corpus
this run read; the checker version is a sha256 over the files that decide
what the census does. Both are recomputable from a checkout.

## The claim, and why it is not the corpus size

The interesting property is not that the two agree over 848 programs.
It is that the bucket that matters **cannot be written**.

`false-admission` is an issued admission for a program the reference
refuses: the gate telling a host the reference said yes when it said no.
That bucket is in the census's `NEVER_BASELINED` tuple (`false-admission`),
and that has two consequences a re-record cannot undo:

1. `--record` drops it on the way into the baseline, so no run can produce
   a baseline that grants one tolerance.
2. `--check` fails on any member regardless of what the baseline says,
   including a baseline hand-edited to list it.

Every other benchmark is cooked by adjusting what counts as acceptable.
Here the adjustment is not available in the direction that matters. The
rest of this file is a table; that sentence is the artifact.

### The mechanism, driven rather than asserted

This tool does not take the paragraph above on trust. It hands both halves
of the mechanism a synthetic member and records what they did.

| probe | what was driven | outcome |
|---|---|---|
| record | `record_payload` over a census whose only finding is `census-artifact-probe:synthetic-false-admission` | dropped, not written |
| check | `compare` against a baseline that lists that member | refused |
| baseline | committed baseline scanned for a never-baselined key | none present |

The refusal, verbatim:

```
FALSE ADMISSION (issued admission the reference refuses) false-admission: census-artifact-probe:synthetic-false-admission
```

Mechanism holds this run: **yes**.

### Observed, and why it is not a vacuum

`false-admission` members this run: **0**.

An empty bucket proves nothing if the gate never admits anything, which
is what the state looked like before the admission arm opened. So the arm
is measured for non-vacuity too: the gate ISSUED **6**
admissions over this corpus, and the reference admits every one of them.

## The standing false-admit allowance

`false-admit` is the other direction: the reference refuses under a
guarantee the gate claims to decide, and the gate raises no objection. It
is a bypass, it is baselined, and it is **not** zero. Published as it
stands, with every residual named.

Total: **9**. Capped by name in `tests/test_gate_reference_census.py`.

| bucket | program |
|---|---|
| `false-admit/T1` | `examples/rejections/t14_optional_chain_on_nonoptional.rvl` |
| `false-admit/T1` | `examples/rejections/t17_arrow_body_unchecked.rvl` |
| `false-admit/T1` | `examples/rejections/t32_arrow_value_result_flows.rvl` |
| `false-admit/T1` | `examples/rejections/t33_arrow_value_arity.rvl` |
| `false-admit/T1` | `examples/rejections/t35_arrow_annotation_not_quantified.rvl` |
| `false-admit/T1` | `examples/rejections/v2_match_nonexhaustive.rvl` |
| `false-admit/TYPE` | `examples/rejections/t13_unknown_match_case.rvl` |
| `false-admit/TYPE` | `examples/rejections/t18_type_alias_cycle.rvl` |
| `false-admit/TYPE` | `examples/rejections/t5_destructure_nonrecord.rvl` |

A count would let this list churn unread. Names have to be edited, and
`--check` fails in BOTH directions: on a new member, and on a baselined
member that no longer diverges. The second direction is the one that
matters for honesty, because it is what stops a fix from quietly widening
the allowance instead of closing it.

## Corpus provenance

A benchmark whose corpus was written by the thing it grades is worth
nothing. `tools/corpus_provenance.py` names the generation that authored
every scoring document, from `tests/fixtures/corpus_provenance.json`. A
document with no entry resolves to UNDECLARED and counts as
model-authored at every threshold, because the other default would make
dropping an unknown file into a globbed corpus RAISE the measured
independence.

Model-authored means: at or after generation 1.

| corpus | model-authored | total | independent | floor | undeclared | verdict |
|---|---|---|---|---|---|---|
| `census` | 0 | 848 | 100.0% | 80% | 0 | ok |
| `emit_go_corpus` | 0 | 19 | 100.0% | 75% | 0 | ok |
| `emit_java_corpus` | 0 | 34 | 100.0% | 75% | 0 | ok |
| `emit_py_corpus` | 0 | 65 | 100.0% | 75% | 0 | ok |
| `emit_rust_corpus` | 0 | 41 | 100.0% | 75% | 0 | ok |
| `emit_ts_corpus` | 0 | 42 | 100.0% | 75% | 0 | ok |
| `emit_wasm_corpus` | 0 | 22 | 100.0% | 75% | 0 | ok |
| `selfhost_oracle` | 0 | 291 | 100.0% | 90% | 0 | ok |

## Full bucket table

| bucket | count |
|---|---|
| `agree-admit` | 463 |
| `agree-refuse/G4` | 72 |
| `agree-refuse/G1` | 57 |
| `agree-refuse/T1` | 57 |
| `no-objection-out-of-slice` | 54 |
| `agree-refuse/A1` | 35 |
| `agree-refuse/G6` | 21 |
| `refuse-out-of-slice/FOREIGN` | 21 |
| `refuse-out-of-slice/BAD` | 10 |
| `agree-refuse/PRELUDE` | 7 |
| `agree-refuse/TYPE` | 6 |
| `false-admit/T1` (tracked) | 6 |
| `agree-refuse/ROUTE` | 5 |
| `agree-refuse/A6` | 4 |
| `agree-refuse/G2` | 4 |
| `agree-refuse/HOST-METHOD` | 4 |
| `agree-refuse/SPAWN` | 3 |
| `false-admit/TYPE` (tracked) | 3 |
| `frontier-declined` | 3 |
| `refuse-out-of-slice/G1` | 3 |
| `agree-refuse/G3` | 2 |
| `agree-refuse/G8` | 2 |
| `agree-refuse/HANDOFF` | 2 |
| `agree-refuse/T2` | 2 |
| `agree-refuse/BOOT` | 1 |
| `refuse-out-of-slice/G4` | 1 |

## Reproduction

The fast engine is a python mirror of the native gate's guards, so it
could in principle be wrong in the same direction as the thing it
mirrors. The reproduction asks the REAL crate, built by cargo, and is
recorded in `tests/fixtures/census_crate_reproduction.json` because it needs a rust toolchain
and minutes rather than seconds.

- programs: **848**
- tracked buckets agree: **yes**
- crate `false-admission` members: **0**
- recorded at checker version: `GATE-CENSUS-1+809b1804f3f7`
- current for this run: **yes**

What it does not establish: The crate is BUILT from selfhost/lower.rvl by tools/build_gate_crate.py, so it is not a second specification: it is the same rvl source through a different emitter, toolchain and runtime. What this reproduction rules out is the fast engine's python mirror of the native guards being wrong. The independence that carries the census is the OTHER axis, src/revl against selfhost/, and it is in the measurement rather than in this reproduction.

## Running it yourself

No account, no key, no hosted service. From a checkout:

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python tools/gate_reference_census.py --check
.venv/bin/python tools/corpus_provenance.py --check
.venv/bin/python tools/census_artifact.py --check
```

Those three need nothing but python and take seconds. The crate engine,
`tools/gate_reference_census.py --engine crate --check`, additionally
needs cargo, builds the shipped gate, and is the slow half.

## What this does not establish

- This report says nothing about any implementation other than revl's own two. It is not a comparison and carries no comparative claim.
- The gate covers one layer of a larger language. A `no-objection-out-of-slice` result is the reference refusing for a reason the gate does not decide, and is by design.
- Generation zero in the provenance manifest is a declaration about the tree as it stood, not a measurement. What holds from there on is that an arriving document must name its generation, and that an undeclared one counts as model-authored.
- A mislabelled provenance entry defeats the provenance measurement exactly as re-recording the baseline would defeat the census. Neither is detected by a tool; both are edits in a diff somebody reads.

## Schema

`docs/census-artifact.json` is the machine copy. It is an `EVAL-REPORT-1`
document under the frozen eval-honesty protocol
(`docs/design/478-eval-honesty-protocol.md`) and
`python3 tools/check_eval_report.py docs/census-artifact.json` passes on
it, which is a statement about over-claiming and not about correctness:
every public claim in it names the rung its own evidence reaches. The
census results live in its `census` section under `GATE-CENSUS-1`, which
that checker does not read.
