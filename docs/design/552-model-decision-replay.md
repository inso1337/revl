# 552: a recorded run replays a model decision from the artifact alone

Roadmap: item 517 (issue #1191), slices 2 and 3. Slice 1 landed as PR #1239
with `docs/design/536-model-decision-evidence.md` and deliberately did not
claim the exit clause; this note is the half that does.

Reconciles with: item 250 Slice 3a and `revl.wal.RECORD_MODEL_DECISION` (the
record these ride on), item 250 Slice 3b and `revl.replay_modes` (the offline
reader extended here), item 512 and `src/revl/model_route.py` (the route table
now cross-checked, which was on an unlanded branch when Slice 1 was written),
item 121 §4 and `backends/python/runtime.py:revl_prompt_digest` (the gate the
prompt binding inherits), item 257 (the outcome vocabulary), item 538 (the
provider that emits what this signs), item 272 (why the backend holds a
callable and not a copy of the MAC).

Design-doc number: **552**, assigned by the orchestrator.

---

## 0. What was missing

Slice 1 shipped a sealed, MAC-covered `revl.model-decision` object and a
verifier that fails closed on every check. It also said, in its own §9, that
nothing wrote one during a run: every record in `tests/test_model_evidence_517.py`
is constructed by hand and the gate is a pure function tested as one. The exit
clause (*a recorded run replays a model decision from the artifact alone, and
an edited field fails its digest*) was therefore half met. The second half was
real; the first half had no writer and no reader.

This note adds both, and they meet on the WAL. A model crossing at execution
seals a record onto the `model-decision` record item 250 Slice 3a already
writes, keyed by `(component, stepIndex)`, which is `crossing_key`, so no
second correlation appears. `revl replay` then verifies it and rebuilds the
decision with the record as its only input.

---

## 1. Which way it fails

The failure direction is the first thing, because an accountability feature
that degrades quietly is worse than none: it makes a reader believe a gap is a
fact.

Evidence is **opt-in**, and that is the whole of the compatibility story. Three
states, and there is no fourth:

| state | what the WAL record carries | what the run does |
| ----- | --------------------------- | ----------------- |
| no sealer engaged | the Slice-3a record, byte for byte | continues, unchanged |
| engaged, sealed | `evidence`: the sealed object | continues |
| engaged, unsealable | `evidenceRefused`: `{link, reason}` | **stops at the crossing** |

A run that never calls `revl_engage_model_evidence` writes exactly what it
wrote before and an offline reader says "not sealed" rather than inferring
anything. A run that HAS engaged has said every model crossing must be
accountable; from then on a crossing that cannot be sealed raises
`RevlModelEvidenceRefused` out of the crossing. There is no arm in which an
engaged run silently writes the unsigned record and carries on.

**The write happens before the raise**, and the ordering is load-bearing. The
artifact is what a post-mortem reader is handed, and it has to say *which*
crossing refused and *why* even though the process stopped there. If the raise
came first, a refused crossing would be indistinguishable from a run that never
engaged evidence, which is the exact "a gap reads as a fact" failure this
section exists to prevent. `test_the_refusal_reaches_the_artifact_before_the_run_stops`
asserts both halves.

Four ways a crossing refuses, all of them exercised:

* the provider published **no declaration**. revl cannot see what a model was
  given, which weights answered or under which rule through an opaque host
  body, so this is a refusal and never a guess;
* the declaration **restates what the crossing owns** (`component`,
  `step_index`, `outcome`). A provider able to set those could seal a record
  about a crossing that did not happen, or call an exhausted budget a validated
  answer;
* the body **fails Slice 1's own gate**: a content-addressed prompt binding
  over a confidential input, a candidate set that does not describe one
  decision, a sampling set missing a key;
* the placement **contradicts item 512's route table** (§4).

And one that looks like a no-op and is not: engaged with **no durable sink** (no
WAL attached, or one closed underneath). An evidence object that is never
written is not evidence, so that refuses too.

---

## 2. Slice 2: sealing at the crossing

`backends/python/runtime.py` ships with the cordis-py runtime and imports
nothing from `revl`. It cannot take a MAC, and it must not learn how: a second
copy of the construction living in the backend beside a signing key is the item
272 duplication mistake in the worst possible place.

So the runtime holds a **callable** and knows nothing about what it does. That
is not a new pattern here. Item 250 Slice 3a's WAL sink is a callable for the
same reason, because the runtime holds no WAL handle either.

```
revl_engage_model_evidence(sealer)   # per run; None disengages
revl_declare_model_decision(**body)  # per crossing, from inside the host body
```

The sealer's contract is `(crossing, draft, outcome) -> (record, None)` or
`(None, {"link", "reason"})`. It is **total**: a malformed declaration is an
answer, not an exception the runtime would have to classify without the
vocabulary to do it. `revl.model_evidence.CrossingSealer` is that callable in
this tree, and it holds the key.

The draft is a contextvar, like every register beside it, so a child Task copies
rather than shares and two live activations never cross-attribute. It is
**consumed** by the crossing that follows, so a later crossing can never seal an
earlier provider's declaration as its own, which is the keyed-observation
discipline item 242 already keeps.

### 2.1 What the crossing owns, and why

`component` and `step_index` come from the crossing the recorder just made;
`outcome` comes from the validation seam that measured the completion. A
declaration naming any of the three is refused.

One consequence needed stating rather than refusing. A provider declares from
inside the host body, *before* the validation seam has decided anything, so it
can legitimately name the candidate the host returned and the retry can then
exhaust around it. `_check_candidates` already says what the record means in
that case ("an outcome of `exhausted` took no candidate"), so the sealer
states it: on a non-`validated` outcome, `chosen` is `None`. Nothing is lost, because
the candidate set is still on the record in the order it was offered; what
changes is the claim that one was *taken*. Refusing instead would fail every
run whose retry budget exhausts, for a contradiction the provider could not have
foreseen.

### 2.2 Absent by default

`WriteAheadLog.record_model_decision` omits both new members entirely when they
are absent, so a run that seals nothing writes the Slice-3a record byte for
byte and no WAL golden moves. That is the same absent-by-default discipline
Slice 2 of item 250 keeps for `scope` / `compensated` / `undoIdempotent`, and
here it also keeps "not sealed" from ever reading as "sealed and refused".

The evidence rides **on** the `model-decision` record rather than on a record of
its own. `revl.wal.model_decisions` therefore indexes the observation and the
account together, with no reader change and no second correlation.

---

## 3. Slice 3: reading the decision back

`revl.model_evidence.reconstruct(record, key)` verifies and then returns what
the decision was: the crossing, the placement, what answered and on what host
profile, how the prompt was bound, what the candidates were and which was
taken, how it was asked, under which rule and how far down the ladder, plus
`reproducible()`.

**`decision` is `None` whenever `verified` is false.** This is stronger than the
exit clause's "an edited field fails its digest", and deliberately: a reader
that returned a reading *plus a warning* is a reader some caller eventually uses
without checking the warning. Editing a field does not produce a wrong answer
here, it produces no answer.

`revl replay WAL --evidence-key PATH` runs it over every decision on a WAL. The
plan keeps four states apart, because collapsing any two is how a reader ends up
trusting something it did not check:

* **not sealed**: no `evidence` member. A run that never engaged and a WAL
  written before this slice read identically, and this reader says so rather
  than guessing, the same honesty `PRE_3A_NOTE` keeps about Slice 3a;
* **refused at run time**: `evidenceRefused`, with the link and the reason.
  That run stopped there, so it is a fact about a run that did not continue;
* **sealed, unverified**: a seal is present and no key was supplied. Nothing
  was checked and nothing is read out. No key is not a pass;
* **sealed and verified**: the decision is reconstructed.

Two entries in the readiness map flip, which is exactly the extension the
`_present_inputs` comment was written for:

* `sealedEvidence`, met only when a seal is present **and** verified;
* `promptDigest`, met only by a verified `content-addressed` binding. That is
  the unsalted mode, legal only over an input whose declared origins carry
  neither `secret` nor `confidential`, and it is the only binding that means
  anything across runs. `salted-within-run` is stable within one run and
  meaningless across runs, so a cross-run replay bound to it is bound to
  nothing, and it leaves this false.

Nothing else moves. The response text is still never written anywhere, so
`exact` and `tool-only` stay out of reach from a WAL and an evidence object does
not pretend otherwise. **Reading a decision back is not re-executing it**:
`executable` stays false for every mode, and the plan says so in
`decisionNote`. The live Slice-3b executor is still the live Slice-3b executor.

### 3.1 The join, checked

`reconstruct` takes the crossing the reader FOUND the record at and compares it
to the one the sealed body names. A genuine, genuinely-signed record of one
crossing, lifted onto another crossing's WAL record, has a valid MAC and is
still a false account of the crossing it was found at. `verify` cannot see
this, because it holds one side, so it is a refusal at the reader
(`EVIDENCE_CROSSING`), and a reader that holds both sides and does not make the
comparison is the fail-open half. This is `cert.affirm_key_id`'s reading applied
to the join instead of to the signer.

---

## 4. The placement, cross-checked against item 512

Slice 1's §9 listed this as an open gap: `role` and `residence` were bound by
value and nothing compared them to anything, because item 512's table was on an
unlanded branch. It has landed, so `check_placement(body, roles)` is that
comparison, against `model_route.roles()`: item 512's own table, derived from
a program by item 512's own code, never a dict written in a test.

Two refusals, and they are different claims:

* the record names a **role the program never declared**. A placement is a fact
  about the program, so `role="gpu-box"` for a program whose roles are `local`
  and `cloud` describes a run of some other program;
* the record names a declared role with the **other residence**. This is the one
  that matters: `role="cloud", residence="on_device"` asserts a prompt stayed on
  a device the program declared it leaves. Both words are inside `RESIDENCES`,
  so the closed-vocabulary check passes it and only the table refutes it. It
  gets its own link (`EVIDENCE_PLACEMENT`) rather than `EVIDENCE_VOCABULARY`,
  because two legal words making a refuted claim is a different fact from a word
  outside the vocabulary and a caller branching on `link` must see both.

The check runs **at seal time**, so a contradicting record is never minted and no
verifier has to be trusted to catch it later; and offline, so one that arrives
from elsewhere is still refused. The table is optional in both places for one
honest reason: a program that declares no `model role` has no table to check
against, and its crossings are still recordable. The residence is bound by value
either way, so a reader that acquires the table later can still refuse the
record.

`placement_table` accepts item 512's `{name: Role}` and a plain
`{name: residence}` mapping. The second is not a convenience: a `Role` is a
compiler object and does not survive the artifact a post-mortem reader is
handed. A malformed table is refused, never treated as a table that admits
everything.

---

## 5. Non-vacuity

`tests/test_model_evidence_replay_552.py`: 53 tests, all passing, no skips. The
file cannot be satisfied by a constructed record: every assertion about a
sealed object reads it back off disk with `revl.wal.read_wal`, after a
completion that went through the real nesting (`validate_retry` → `make_call` →
`Timeline.record_emission` → the fiber-local sink → `WriteAheadLog`).

The tamper demonstration Slice 1 established is carried onto the artifact and
kept **generated** from the record's own keys, parametrised over `BODY_MEMBERS`,
so a member added later is covered without anyone remembering to cover it. Each
member is edited on the WAL FILE and the reading must be refused with
`EVIDENCE_SIGNATURE` and yield no decision.

And the demonstration that the demonstration is worth something: the cited
historical defect is **injected**. `_sign` is replaced with a MAC over a
hand-written field list that omits `key_id`, which is the shape of the receipt
this tree actually shipped, and the artifact-level check on an edited `key_id`
is shown
to stop firing as a tamper refusal and fall through to the weaker key-identity
refusal. The same test then undoes the injection and shows the real build
catching the same edit with `EVIDENCE_SIGNATURE`. A tamper test that would still
pass under the bug proves nothing, and this is what rules that out.

---

## 6. Things stated here that are not verified

* **Everything Slice 1's §9 said about the cryptography still holds, unweakened
  and unextended.** The construction is HMAC-SHA256 over canonical JSON with a
  domain-separation prefix, reused from `revl.attest`. These slices exercise
  that the MAC covers every member of a record written by a real run and that
  an edit to any of them withholds the reading. They do **not** exercise the
  primitives, do not constitute a cryptographic review, and say nothing about
  key management, key rotation or key distribution. Nothing in slices 2 or 3
  touches any of the three.
* **The 16-hex `key_id` fingerprint's collision resistance is still
  unexercised.** It is a fingerprint for CHOOSING a key, and `revl replay` uses
  it that way and no other way: a record whose `key_id` does not match is
  refused, and a match is never treated as proof of anything.
* **The threat model is a shared-secret one, and sealing at the crossing does
  not change it.** A holder of the key can seal any well-formed record. What
  the crossing adds is that the runtime does not hold the key, which narrows
  where the key must live; it does not make a lying signer detectable.
* **The provider is trusted for what only it can see.** `model_digest`,
  `placement_digest`, the candidate set and the prompt binding are DECLARED.
  revl cannot see through an opaque host body and the record says what it was
  told. What is enforced is that the declaration is well-formed, that it does
  not restate what the crossing owns, that it obeys the disclosure gate, and
  that its placement agrees with the program.
* **Only the py tier writes these.** The sealer hook is on
  `backends/python/runtime.py`. A non-py tier's runtime writes the Slice-3a
  record and no evidence, which reads as "not sealed": correctly, and
  indistinguishably from a py run that engaged nothing. Item 538's
  cross-tier provider story is where that is closed, not here.
* **`revl canary` is not wired to these.** Slice 4 of item 517 (when the first
  differing step is a model crossing, the two evidence objects say WHICH member
  differs) needs these two slices under it and is not built here.
* **`reproducible()` is still a derived predicate, not a refusal.** Slice 1's
  §8 sketched a replay of a non-reproducible record refusing rather than
  re-running with a different seed. There is no live re-runner to refuse in,
  because reading a decision back is not re-executing it; the plan reports
  `reproducible` per decision and the refusal belongs to whatever eventually
  re-runs.
