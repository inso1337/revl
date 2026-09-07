# Eval honesty protocol

**Protocol version: EVAL-1. Status: FROZEN (2026-09-06).**

This document is the frozen rulebook for how a benchmark number about revl earns
the right to be stated in public. It exists because revl advertises AI-authored
quality, and a quality claim is only worth the discipline behind the number it
rests on. The rules here are the discipline.

A protocol version is a contract, not a note. Once frozen, the rules in this
document do not change. A change to the scoring rules, the gates, or the ladder
is a NEW protocol version (EVAL-2, and so on), cut in a separate reviewed
change, with the reason for the change written down. A published number always
names the protocol version it was measured under, so a reader can look up the
exact rules that produced it. Do not edit the rules of a frozen version in
place; supersede it.

This is deliberately a pure function of the rules stated below. It defines no
new tooling and reads no source tree. It can be referenced by a number, a
commit message, a README line, or a review, and it will mean the same thing
every time.

## 1. The primitive: model-free re-scoring

Every number under this protocol is produced by re-scoring a FIXED, COMMITTED
corpus of generations with the current compiler. No model runs while a number
is being measured.

- The corpus is the set of `.rvl` files already committed under
  `bench/results/<run>/`. They were produced by a model in an earlier, separate
  run (`bench/run.py`, which costs money and calls a provider) and then frozen
  into the tree.
- The score of one cell is the result of compiling that committed file with the
  checker in the working tree (`bench/rescore.py`). It calls no provider, costs
  nothing, and is a pure function of (file bytes, compiler commit).
- Every table names the compiler commit it was measured at, so a number traces
  back to a sha.

The generation step and the scoring step are separate steps run by separate
tools. This separation is the whole point, and it is what the `noSelfScore`
rule (section 3) makes non-negotiable.

## 2. The recorded metric: passAtK = 1

The headline metric under this protocol is **first-pass compile rate**, recorded
as **passAtK with K = 1**.

- K = 1 means: score `attempt-1.rvl` only. That is the file the model produced
  BEFORE it ever saw a compiler message. It is the honest measure of what the
  model writes unaided.
- Later attempts (`attempt-2.rvl` and beyond) are the error-feedback loop. They
  are useful telemetry and may be reported SEPARATELY and clearly labelled, but
  they are never the headline number and never a passAtK = 1 number.
- A passAtK = 1 number is one fraction: admitted cells over total cells, at a
  named compiler commit, over a named run. No averaging across runs into a
  single blended figure without naming each run and its size.

If a future protocol wants a different K, that is a different protocol version.
Under EVAL-1, the recorded metric is passAtK = 1.

## 3. noSelfScore: the agent never grades its own output

**An agent never grades its own output.** The party (model) that GENERATED a
cell is never the party that decides whether the cell passed.

Under this protocol the grader is the compiler, a deterministic program, and
the generator is a model. They are different tools, run at different times, and
they cannot be the same identity. Concretely, the following must all hold for a
number to be valid under EVAL-1:

1. **Model-free grading input.** The input to the grader is bytes read from a
   committed file under `bench/results/`. It is never text handed back from a
   live model call during scoring.
2. **The grader is the compiler.** The scoring function is the revl compiler
   entrypoint (`revl.compile_source`), not any generation driver
   (`run_cline` / `run_local` / `run_mock` in `bench/run.py`) and not a model
   client of any kind.
3. **Determinism.** Scoring the same file twice with the same compiler commit
   yields the same result. A grader whose answer can vary between two runs of
   the same input is a model, not a compiler, and fails this rule.
4. **No self-review shortcut.** A model's own judgement that its output is
   correct is never evidence under this protocol. Neither is a second model
   asked to judge the first. Only the compiler's admit/refuse decision counts.

The intent is narrow and strict: the thing being sold is that revl's gate
admits or refuses a component, so the gate is the only judge whose verdict
becomes a number. A test that enforces items 1 to 3 mechanically lives beside
`bench/rescore.py`; see section 6.

## 4. Per-task hard gates

A "task" here is one component spec in `bench/specs.json`. Each spec is graded
against HARD gates: named, per-task, pass or fail, decided by the compiler. A
hard gate is not a score to be averaged into a soft aggregate; it is a bar the
generation clears or does not.

The named hard gates under EVAL-1, per task, per variant cell:

- **`compiles`**: the committed `attempt-1.rvl` compiles under the current
  checker with no error and no compiler crash. A crash counts as a failure and
  is additionally a compiler bug. This is the gate that feeds passAtK = 1.
- **`pinnedInterfaces`**: the spec's given service interfaces are used
  verbatim. The brief pins the interfaces and leaves the implementation to the
  model (`bench/README.md`); a generation that rewrote a pinned interface is
  not a valid cell for this spec.
- **`noResidueForRawBaseline`**: for the raw-TypeScript paradigm baseline,
  where "compiles" is meaningless (raw Cordis always loads), the hard gate is
  the residue probe reporting zero leaked contract categories across N
  mount/unmount cycles (`bench/score_raw_ts.py`). A generation that leaks is a
  refusal revl would have made at compile time.

Each gate is named so a report can say exactly which bar a cell cleared. A
report that says "it passed" without naming the gate is not a report under this
protocol. Adding, removing, or redefining a gate is a new protocol version.

## 5. The ladder: measured is not supported

There are two rungs, and they are not the same claim. Keeping them apart is the
honesty this protocol protects.

- **measured**: a number produced under this protocol: a passAtK = 1 fraction,
  at a named compiler commit, over a named run, from the committed corpus. A
  measured number is a fact about one corpus at one commit. It may be stated in
  public AS a measured number, with its commit, run, and protocol version
  attached. Example shape: "measured 24/30 first-pass (passAtK = 1) on
  `typed-deepseek-v4-pro` at compiler `abc1234`, protocol EVAL-1."
- **supported**: a durable public claim that revl DELIVERS a level of
  AI-authored quality (the kind of claim a README headline or a marketing line
  makes). A capability is "supported" only after a review promotes it.

The ladder rule, stated once and frozen:

> A measured number is NOT a supported claim. A capability moves from measured
> to supported only by a new frozen protocol version plus a review that signs
> off the promotion. Until that review lands, every public statement about the
> capability is phrased as measured (with its commit, run, and protocol
> version), never as supported.

The review that promotes measured to supported is a conflict-of-interest review:
the reviewer signing the promotion is not the agent that produced the
generations being promoted. This is the `noSelfScore` rule (section 3) applied
one level up, to the claim instead of the cell.

Downgrades need no ceremony. If a measured number regresses, the measured
statement is corrected or withdrawn immediately; only PROMOTION up the ladder
is gated.

## 6. Enforcement

The `noSelfScore` rule is not only prose. `bench/rescore.py` carries an
`assert_model_free` check that enforces section 3 items 1 to 3 mechanically
before any cell is scored, and it runs on every re-score. Its test lives at
`tests/test_rescore_no_self_score.py`.

What enforcement can and cannot know, stated plainly so no one over-reads it:

- It CAN prove the grader is the compiler, the grading input is a committed file
  on disk, and the score is deterministic. Those are the three things a machine
  can check, and they are exactly the mechanical core of `noSelfScore`.
- It CANNOT prove that a human review actually happened before a "supported"
  claim, or that a reviewer was genuinely independent. The ladder in section 5
  is a process rule, kept honest by the review record, not by a program.

Do not narrow the check to make a red line pass, and do not widen the reporting
to claim the check proves more than the three items above. Regenerate the corpus
or fix the compiler; do not weaken the gate.

## 7. Cross-references

- `bench/rescore.py`: the model-free re-scoring primitive and the
  `assert_model_free` guard.
- `bench/run.py`: the generation step (a real model, real cost), kept separate
  from scoring by design.
- `bench/score_raw_ts.py`: the residue-probe scoring for the raw-TS baseline.
- `bench/specs.json`: the task list; one entry is one task in section 4.
- `docs/vision.md`: the honest scope of revl's public claims, which this
  protocol keeps honest for benchmark numbers specifically.
