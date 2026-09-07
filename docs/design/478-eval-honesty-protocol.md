# 478 — Eval honesty protocol: the report-honesty layer

**Report-schema version: EVAL-REPORT-1. Status: FROZEN (2026-09-07).**

This document is the machine-checkable half of the eval honesty protocol. The
frozen rulebook `docs/eval-protocol.md` (protocol EVAL-1) states, in prose, how
a benchmark number earns the right to be stated in public. This document does
not change any of those rules. It operationalizes them: it fixes the shape of an
**eval report** so a program can decide whether a report obeys the protocol
before any of its numbers or claims reach a README, a site, or a commit message.

Two things live here that a prose rulebook cannot:

1. a **frozen report schema** a checker reads, so "the report obeys the
   protocol" is a decision a machine makes, not a habit a human remembers; and
2. the **four-rung evidence ladder** that refines EVAL-1 section 5's
   `measured != supported` principle into named rungs with, for each rung, the
   exact evidence a report must carry to stand on it.

`tools/check_eval_report.py` is the checker; `tests/test_check_eval_report.py`
is its test. The relationship to EVAL-1 is deliberately one-directional: EVAL-1
sections 1 to 4 define what a number *is* and how it is produced; this layer
refuses to let a report *say more than it measured*. Changing EVAL-1's scoring
rules is a new EVAL protocol version with its own review. Changing this schema
or the ladder is a new EVAL-REPORT version, cut the same way and named the same
way. Do not edit a frozen version of either in place; supersede it.

## 1. Why a report layer at all

EVAL-1 already makes the *grading* honest: `bench/rescore.py` carries
`assert_model_free`, which proves the grader is the compiler, the input is a
committed file, and the score is deterministic before it produces a number.
That guard sits at the point a number is *produced*.

It says nothing about the point a number is *stated*. A number produced
correctly can still be over-claimed the moment a human writes it into a headline
as a durable capability rather than a fact about one corpus at one commit. The
gap EVAL-1 section 5 names in prose — measured is not supported — is exactly a
claim-side gap, and prose is not enforceable. This layer closes it by making the
claim itself a checkable object: a claim declares the rung it stands on, and the
checker refuses a claim that stands higher than the evidence attached to it.

## 2. The eval report

An eval report is one JSON document. It is the single artifact the checker reads.
It has this shape (fields marked optional may be omitted):

```json
{
  "protocol": "EVAL-1",
  "report_schema": "EVAL-REPORT-1",
  "compiler_commit": "abc1234",
  "metric": "passAtK=1",
  "grader":    { "tool": "revl.compile_source", "kind": "compiler" },
  "generator": { "model": "deepseek-v4-pro", "run": "typed-deepseek-v4-pro" },
  "briefs": [
    { "spec": "auth-service", "variant": "v2",
      "hard_gate": "compiles", "result": "pass" }
  ],
  "claims": [
    { "text": "measured 24/30 first-pass on typed-deepseek-v4-pro at abc1234",
      "rung": "measured",
      "public": true,
      "evidence": {
        "compiler_commit": "abc1234",
        "run": "typed-deepseek-v4-pro",
        "protocol": "EVAL-1"
      } }
  ]
}
```

- `protocol` names the EVAL protocol version the numbers were produced under.
  The checker requires it and requires that the report also name a
  `report_schema` it validates against.
- `grader` / `generator` are identities. The checker compares them; they must
  not be the same party. This is `noSelfScore` at the report level.
- `briefs` is the per-task record. One entry is one task (one component spec in
  `bench/specs.json`), per variant. Every entry must name a `hard_gate` drawn
  from the frozen set in section 4, and a `result` of `pass` or `fail`.
- `claims` is what the report wants to state in public. Each claim declares a
  `rung` and carries the `evidence` for that rung. `public` defaults to true.

The checker reads nothing but this document. It does not re-run the compiler,
does not read the corpus, and does not trust any number: it decides only whether
the report is *internally* honest under the protocol. A report that passes the
checker is a report whose claims do not outrun its own stated evidence; it is
not a claim that the numbers are correct. Correctness of a number is EVAL-1's
job (re-score at a named commit); honesty of a report is this layer's job.

## 3. noSelfScore at the report level

EVAL-1 section 3 forbids the generator from grading its own cell. This layer
carries the same rule to the report:

- **The grader is not the generator.** `grader` and `generator` must be
  different identities. A report whose `grader.model` equals its
  `generator.model`, or whose grader names the generating model at all, is
  refused.
- **The grader is the compiler.** `grader.kind` must be `compiler` and
  `grader.tool` must be the revl compiler entrypoint (`revl.compile_source`).
  A grader that is a model client, a generation driver, or a second model asked
  to judge the first is refused. This mirrors `assert_model_free` item 2, at the
  report layer instead of the scoring layer.

The checker enforces these as hard refusals. It cannot prove that the identity a
report *writes down* is the identity that *actually graded*; that is the same
limit `assert_model_free` names for itself. What it can do, and does, is refuse a
report that admits on its face that the generator scored itself.

## 4. Named hard gates, per brief

EVAL-1 section 4 defines the named hard gates. This layer requires that every
brief in a report name one of them, so a report can never say "it passed"
without saying which bar was cleared. The frozen set the checker accepts under
EVAL-REPORT-1 is exactly EVAL-1's set:

- `compiles`
- `pinnedInterfaces`
- `noResidueForRawBaseline`

A brief with no `hard_gate`, or with a gate name outside this set, is refused.
Adding, removing, or renaming a gate is a new EVAL protocol version *and* a new
report-schema version, because both the rulebook and this checker's accepted set
must move together.

## 5. The four-rung evidence ladder

EVAL-1 section 5 draws one line: measured is not supported. This layer refines
that single line into four named rungs, lowest to highest, so a report can be
precise about how far its evidence actually reaches. The rungs are ordered; a
claim's declared rung must be no higher than the rung its attached evidence
justifies.

| rung | index | what it asserts | evidence the checker requires |
|---|---|---|---|
| `claimed` | 0 | a bare assertion; no evidence offered | none — but a `claimed` claim may not be marked `public` |
| `measured` | 1 | a fact about one corpus at one commit | `compiler_commit`, `run`, and `protocol`, and the report as a whole passes noSelfScore (section 3) |
| `demonstrated` | 2 | the measured number reproduced independently | everything `measured` needs, plus a `reproduced_by` that is not the generator |
| `supported` | 3 | a durable public capability claim | everything `demonstrated` needs, plus a `review` naming a promoting protocol version and a `reviewer` that is not the generator |

The ladder rule, stated once and frozen:

> A claim may stand no higher than the rung its own evidence justifies. The
> checker computes, from the evidence attached to a claim, the highest rung that
> evidence reaches, and refuses any claim whose declared rung is higher. It
> never promotes a claim; it only refuses over-claims.

Rung by rung, what "justifies" means:

- **claimed** is the floor. It carries no evidence and asserts nothing
  measurable, so it may never be `public`. A `claimed` rung is for an internal
  note or a hypothesis, and the checker refuses it the moment it is marked
  public.
- **measured** is EVAL-1's measured number. Its evidence is the triple that lets
  a reader re-score it: the compiler commit, the run, and the protocol version.
  The report must also pass the noSelfScore checks, because a measured number
  produced by a self-scoring setup is not a measured number under EVAL-1.
- **demonstrated** is a measured number that a second, independent party
  reproduced. The conflict-of-interest rule from EVAL-1 section 3 applies: the
  reproducer named in `reproduced_by` must not be the generator. A number the
  generator alone measured is `measured`, not `demonstrated`.
- **supported** is the durable capability claim of EVAL-1 section 5. It requires
  a promotion review: a `review` block naming the frozen protocol version that
  promotes the capability and a `reviewer` who is not the generator. Without
  that review on the record, the highest a report may claim is `demonstrated`.

Downgrades need no ceremony, matching EVAL-1: a report may always claim a lower
rung than its evidence would allow. Only claiming *higher* than the evidence is
refused. This keeps the asymmetry EVAL-1 wants — promotion is gated, correction
is free.

## 6. What the checker decides, and what it does not

Stated plainly so no one over-reads a green check:

- The checker CAN decide: the report names a protocol and a report schema; the
  grader is the compiler and is not the generator; every brief names a hard gate
  from the frozen set; every claim stands no higher than its evidence justifies;
  no public claim sits at the `claimed` floor.
- The checker CANNOT decide: whether a number is numerically correct (re-score
  under EVAL-1 decides that), whether the written-down grader identity is the one
  that truly graded, or whether a review named in a `supported` claim was a
  genuine, independent human review. Those are the same limits EVAL-1 section 6
  names for `assert_model_free`, carried up to the report layer.

A green checker means "this report does not over-claim on its face." It does not
mean "these numbers are true." Do not narrow the checker to make a red report
pass, and do not widen the reporting to say the checker proves more than the
list above. Fix the report or lower the claim; do not weaken the gate.

## 7. Cross-references

- `docs/eval-protocol.md`: the frozen rulebook, protocol EVAL-1. This document
  is its report-layer companion and does not modify it.
- `tools/check_eval_report.py`: the checker frozen by this schema.
- `tests/test_check_eval_report.py`: the checker's test.
- `bench/rescore.py`: the model-free re-scoring primitive and `assert_model_free`,
  the scoring-layer counterpart of section 3.
- `bench/specs.json`: the task list; one entry is one brief in section 4.
