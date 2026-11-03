# 536: the model decision as a signed evidence object

Roadmap: item 517 (issue #1191), from the 2026-09-19 external review. Slice 1
is LANDED with this note; slices 2 to 4 are designed here and not written.

Reconciles with: item 512 and `docs/design/531-model-placement.md` (the
placement this object records, §9 of which scopes what reaches here), item 249
and `docs/design/249-taint-provenance.md` (the origin lattice), item 121 §4 and
`backends/python/runtime.py:revl_prompt_digest` (the prompt-digest gate this
inherits), item 250 Slice 3a and `revl.wal.RECORD_MODEL_DECISION` (the record
that exists today), item 257 and `docs/design/257-typed-model-boundary.md` (the
boundary being crossed), item 272 and `stdlib/crypto.rvl` (the primitives),
item 428 F1 and `revl.attest` (the signed-record construction and the
algorithm-confusion refusal), items 496 and 59 (`revl replay`, `revl canary`,
the divergence surfaces this feeds), item 515 and item 538 (the provider side
that owns a device profile).

Design-doc number: **536**. 531 to 535 are claimed on open branches
(`agent/1186-route-model`, `agent/1195-typed-computer-use`,
`agent/1205-evolution-curriculum`, `agent/1206-evolution-reward`,
`agent/1207-held-out-scoring`); 536 was free on main and on every open branch
at the time of writing, checked across all remote refs rather than against main
alone.

---

## 0. The decision in one paragraph

A revl execution attaches evidence to every step but one. An effect carries a
WAL record with a named inverse, an emission carries a deferred record and a
flush proof, a boundary crossing carries a receipt. A model completion carries
a `model-decision` WAL record whose own writer documents what it leaves out,
and the omissions are precisely the things a replay would need. So `revl
replay` and `revl canary` can attribute a divergence to an exact `(component,
realm)` and then say nothing about why the two worlds chose differently. This
note adds the missing artifact: a sealed, MAC-covered `ModelDecision` record
binding the crossing, the placement, the model, the prompt binding, the
candidate set, the request parameters, the policy and the fallback depth. It
does not make a model call deterministic and does not claim to. It makes the
call **accountable**, and it does so fail-closed: every member is required,
every vocabulary is closed, and a verifier that has not run every check does
not return a pass.

---

## 1. The gap, measured

The record already exists and is already not evidence.
`revl.wal.RECORD_MODEL_DECISION` (item 250 Slice 3a) writes
`{component, stepIndex, outcome, llm}` at a completion's crossing. Its writer
in `backends/python/replay.py` states the omissions itself:

> Deliberately NOT here, stated so nobody infers them from silence: the prompt
> and response TEXT, `promptDigest`, `producedSeq`, and the request-side
> parameters revl cannot see through the host body (temperature, seed, tool
> calls).

And the `llm` payload it does carry is mostly host-reported and marked as such:
`model`, `tokensIn`, `tokensOut` and `cost` are all tagged
`"provenance": "host-reported"`, with only the latency bracket and the attempt
count against the static ceiling marked `revl-measured` and `revl-controlled`.

So three separate facts hold today, and each is a different kind of gap:

1. **Nothing binds the decision to its inputs.** No prompt binding, no origin
   set, no policy. A reader of the WAL cannot say what the model was given.
2. **Nothing records the alternatives.** `outcome` is `validated` or
   `exhausted`. A decision among five candidates and a decision with one
   forced answer produce the same record, which is the difference a divergence
   investigation most wants.
3. **Nothing is signed.** The record is a line in a file. An edited field
   produces a valid-looking record, so the artifact cannot travel and cannot
   be the basis of any claim a second party checks.

There is also a fourth fact, and it is a constraint rather than a gap.
`revl_prompt_digest` is a **fail-closed gate**, not an unimplemented feature:
it emits a digest only when the taint analysis is engaged and proves the args
carry neither a `secret` nor a `confidential` origin, and returns `None`
otherwise. An evidence object that required a prompt hash unconditionally would
be a second, wider path to the exact value that gate exists to withhold. §4
is what this note does about that, and it is the part of the design that is not
in the issue.

---

## 2. What the object binds

One record, `kind: "revl.model-decision"`, version `2.0`, in
`src/revl/model_evidence.py`. `BODY_MEMBERS` is the closed body; every member
is required and every member is inside the MAC. Version `1.0` is still
accepted, under its own body and its own MAC domain (§2.1).

| member | what it binds | why it is here |
| ------ | ------------- | -------------- |
| `kind` / `version` / `sign_alg` / `hash_alg` | the envelope | validated, not merely recorded (§6) |
| `key_id` | the signer's fingerprint | the member a reader uses to choose a key; inside the MAC (§6) |
| `recorded_at` | when | |
| `component` / `step_index` | the crossing | exactly `revl.wal.model_decisions`' key (§5) |
| `role` / `residence` | the placement | item 512, declared not observed (§3) |
| `model_digest` | the weights that answered | the item's "model hash" |
| `model_digest_basis` | what the digest is a claim about | issue #2750's pinned-artifact seam (§4.5) |
| `placement_digest` | the provider's own profile digest | the item-538 seam (§4.2) |
| `prompt_binding` | the input, as a mode and a value | the item's "prompt hash", gated (§4.1) |
| `template_digest` / `template_absence_reason` | the template behind the prompt, or a named absence | issue #2750's "which template" (§2.1) |
| `origins` | item 249's origin classes on the input | the item's "input taint" |
| `candidates` / `chosen` / `outcome` | what could have been said, and what was | the item's "candidate set" |
| `sampling` | temperature, top_p, top_k, seed, max_tokens, stop_digest | the item's "sampling parameters" (§4) |
| `policy_digest` | the policy in force | the item's "policy in force" |
| `fallback_depth` | how far down the ladder | the item's "fallback depth" |
| `fallback_reason` | why the ladder moved | issue #2750's "why a fallback" (§2.1) |
| `retained` | the explicit retention decision, or nothing | the item's content rule (§4.3) |

Four members are worth their own sentence.

**`candidates` is a list of digests in the order offered, with `chosen` an
index into it.** Not a count and not the chosen digest alone. A count cannot be
compared across two worlds; a lone chosen digest cannot say whether the other
world had the same options. A divergence investigation asks "did the two worlds
see the same menu", and only the ordered set answers it.

**`fallback_depth` is a number, not a flag.** Zero is a clean first hit.
Non-zero means something earlier refused or was unavailable, which item 538
requires to be a refusal the ladder consumes rather than a silent degradation.
A record without the depth cannot tell the two apart, and "the ladder ran" is
exactly the fact an audit of a bad answer starts from. `fallback_reason` is the
other half of that fact: the depth says the ladder moved and the reason says
whether the rung was unavailable, declined or refused (§2.1).

**`origins` is sorted and duplicate-free, and that is checked.** Two records
over the same input must compare equal byte for byte, or the object cannot be
used for the comparison it exists for.

### 2.1 Version 2.0: the fallback reason, the template, and the digest basis

Version `1.0` bound the fallback DEPTH and the prompt, but not why the ladder
moved or which template produced the prompt, and it bound `model_digest` with
no statement of what the digest had been checked against. Issue #2750 adds
three claims and bumps the envelope to `2.0`.

`fallback_reason` is a closed vocabulary, `declined`, `refused` or
`unavailable`, and it is present exactly when `fallback_depth` is above zero.
The words are the KINDS the provider side already spells before its detail; the
detail is free text and stays in the provider's log, because a member an audit
compares across records cannot be free text. `None` at depth zero is required:
a reason would claim a descent the depth says did not happen. The pair is
checked as one claim, so a depth with no reason and a reason at depth zero are
both refused.

`template_digest` is the SHA-256 of the template text with its placeholders
UNFILLED. `template_absence_reason` is the explicit other arm: `no-template`
when the prompt was not built from a template, `unpublished` when it was but
its identity is not available. Exactly one arm is set. Silence is refused:
"which template produced this prompt" with no answer is the gap the member
exists to close, so a record must name an absence as a reason rather than imply
one. A filled prompt is what `prompt_binding` already binds, and the template
member never carries it.

`model_digest_basis` is `declared` or `loaded-bytes`, and says whether
`model_digest` was checked against the bytes actually loaded or is only the
provider's declaration. §4.5 is what that does and does not prove.

**Why a version bump, and not additive members.** A `1.0` record verifies under
its own body, its own closed member set and its own MAC domain
(`SIGN_DOMAINS`). Adding the members to `1.0` would have made every 1.0 record
that did not carry them fail, and it would have left a verifier unable to tell
an old record from a new one whose member was forgotten. Bumping the version
instead means each record is checked against the body it was sealed with, and
neither version can be relabelled. A `2.0` record rewritten as `1.0` fails its
MAC, and even a key holder who re-MACs it is refused for carrying members `1.0`
does not define. A `1.0` record rewritten as `2.0` is missing the claims `2.0`
requires.

The limit, stated rather than implied: `seal()` only ever writes `2.0`, so a
record this tree produces carries the three claims. A provider that holds the
key can still choose to MINT a fresh `1.0` record, exactly as it can lie inside
a well-formed `2.0` body; the shared-secret threat model in §9 is what bounds
that, not the version check. What the version check rules out is presenting a
sealed `2.0` decision as an old one to shed the members.

---

## 3. What comes from item 512, and what does not

`docs/design/531-model-placement.md` §9 scopes this item in one sentence:

> **517**, the signed decision object. Records the placement in force. The
> field it needs from here is the role name and its residence, both of which
> are declared rather than observed, so a `ModelDecision` can bind them by
> value without asking a host.

That is taken literally, and it decides the branch point. **This slice is cut
from `main`, not from `agent/1186-route-model`.** The reason is not
convenience: the object binds two declared strings by value, so it needs item
512's *vocabulary* and none of item 512's *code*. Branching from an unlanded
PR would have made this PR's required checks depend on that PR landing first,
for a dependency that is two words.

The cost of that choice is stated rather than hidden: `RESIDENCES` is written
out here as `("off_device", "on_device")`, which is a second spelling of a
vocabulary item 512 owns. The reconciliation is one line when 512 lands, and
until then it is guarded rather than trusted:
`test_the_residence_vocabulary_agrees_with_model_route_once_it_lands` imports
`revl.model_route` if it exists and asserts agreement, and skips with a reason
otherwise. **Only the cross-module agreement is conditional.** The check on a
record is unconditional: a residence outside the closed vocabulary is refused
whether or not `model_route` exists, so the skip cannot become a hole.

What this object does NOT take from 512: the route table, the arm-matching, and
any question about whether a placement was *correct*. A `ModelDecision` records
which placement was in force. Whether that placement was admissible is decided
at admission by `model_route.check()` and is item 512's; an evidence object that
re-derived the route table from the AST would be the duplicated-authority shape
531 §9 warns 514 away from.

---

## 4. The requirement the issue does not state: a record is not a replay

The issue lists "the sampling parameters" and stops. An architecture review of
this wave asked the sharper question: if temperature, seed, quantisation and
the weights identity are not in the object, a trajectory can be described and
not re-run. The decision here splits the four, and the split is by **who owns
the fact**, which is a line the roadmap already drew.

### 4.1 Request parameters: here, closed, and mandatory

`temperature`, `top_p`, `top_k`, `seed`, `max_tokens` and `stop_digest` are
things a **caller supplies**. The record's job is to bind what was asked, so
they are here. The design decision is not whether to include them but how:
`sampling` is a **closed set with all six keys mandatory**. A free-form
parameter bag that may be missing a key is a description of a request; a closed
set is an instruction for re-issuing one.

The fail-closed reading is precise and worth stating, because it is the line
between this and the fail-open shape: **the KEY is mandatory, the VALUE may be
an explicit `None`.** `seed: None` is the record saying the request did not set
a seed. A missing `seed` key is refused. A verifier that passed on the second
because it cannot tell it from the first would be the "passes when a field is
absent" shape this repo has measured ten times.

`model_digest` is here for the same reason and with no argument needed: the
roadmap item names it.

### 4.2 Quantisation and the device profile: NOT here, and named where they go

Quantisation, runtime build, device profile and load point are **not** members
of this object. Two roadmap items decide that, and neither is this one:

* **515** says one model at three quantisation and memory points is three
  *placements*, so quantisation is a property of a placement and not of a call.
* **538** says a device profile, a load cost and a quantisation "are properties
  of a host and nothing under the compiler should learn what one is", and its
  own exit clause says the provider "publishes the profile 515 routes on and
  **emits the record 517 signs**".

Putting a quantisation field here would make the compiler learn what a
quantisation is, against a rule 538 states in the same sentence that assigns it
the job of producing this record.

**But leaving them out with no hook would be worse, and this is the half that
would otherwise be nobody's.** Two quantisations of the same weights produce
different distributions under the same `model_digest`, the same seed and the
same temperature. A record with the six sampling members and a model hash is
therefore *still* not re-runnable, and would look re-runnable.

So the object carries **`placement_digest`**: a digest the **provider** computes
over its own profile, which revl binds and does not interpret. The consequences,
both directions, as the review asked for:

* **The field is mandatory here.** A record must carry a `placement_digest` or
  a literal `None`, and the choice is inside the MAC. revl's obligation ends at
  binding the value.
* **Its MEANING is item 538's.** What goes into the digest (quantisation,
  runtime build, weights file, device) is the provider's published profile.
  This note does not specify it, and `test_the_placement_digest_is_opaque_here`
  pins that any 64-hex value is accepted and nothing reads a quantisation out
  of it.
* **`None` is visible rather than silent.** `reproducible(record)` returns
  `(ok, [what is missing])` and lists `placement_digest` when it is absent. A
  reader is told the trajectory is describable and not re-runnable, instead of
  inferring it.

If that seam is judged wrong, the alternative is a field on this object and a
contradiction with 538 to resolve; what it must not become again is a
requirement neither item names. **This note's ask of item 538 is one sentence:
publish what `placement_digest` is computed over.**

`reproducible()` is deliberately a **predicate and not a refusal**. A sampling
run with no seed is a real run and must be recordable. What must not happen is
a reader assuming it can re-issue one, and the predicate is how the object says
which of the two it is holding.

### 4.3 The prompt binding: a mode, because a hash would be fail-open here

This is the part of the design that departs from the issue's text, and it is
the most load-bearing.

The issue asks for "the prompt hash". A bare prompt hash contradicts two things
this tree already decided:

1. `revl_prompt_digest` emits a digest **only** when the taint analysis is
   engaged and proves the args carry neither a `secret` nor a `confidential`
   origin. An unsalted digest over a confidential prompt is a confirmation
   oracle: an adversary who can guess a candidate prompt can confirm it, and the
   artifact travels. Item 121 §4 calls that CRITICAL and the shipped runtime
   refuses to produce one.
2. The digest that gate *does* produce is salted with a **per-process nonce**
   (`revl_digest_nonce`). It is stable within one run and meaningless across
   runs. A cross-run replay bound to it is bound to nothing, and a record that
   simply carried "a digest" would hide which of the two it held.

So `prompt_binding` is `{mode, value, reason}`:

| mode | value | comparable | legal when |
| ---- | ----- | ---------- | ---------- |
| `content-addressed` | unsalted sha256 | across runs | `origins` carries no `secret` and no `confidential` |
| `salted-within-run` | `hmac-sha256:<64 hex>`, the runtime's own spelling | within one run | same |
| `suppressed` | `None` | not at all | always; `reason` names the arm that fired |

The rule the verifier enforces, and the item's second named refusal: **a record
whose `origins` carry `secret` or `confidential` and whose `prompt_binding` is
anything but `suppressed` is refused** with link `disclosure`. This is the
runtime gate restated at the evidence layer, and it is not redundant with it. A
provider on another tier (item 538) holds the signing key and writes its own
records; the runtime gate lives in the py driver and cannot reach it. The
evidence-layer check is what refuses a record written *around* the runtime.

The fail-closed shape here is the same as §4.1's: the **member** is mandatory,
the **value** may be an explicit absence with a reason from a closed set. A
record cannot omit the prompt binding. It must state that the binding was
suppressed and which arm of the gate fired, and that statement is inside the
MAC.

### 4.4 Retained content: explicit, and still refused where it discloses

The issue's rule: hashes are the binding, and retaining content is an explicit
decision that carries the taint. `retained` is `None` by default, and:

* `seal()` refuses a `retained` block unless `retain_content=True` is passed
  **by name**, so retention is a decision a caller writes rather than a field a
  caller drifts into;
* `retain_content=True` with nothing to retain is also refused, so the record
  cannot claim a retention decision it did not make;
* the block declares the origins its bytes carry, and those must be a subset of
  the decision's own `origins`, because a record whose taint is narrower
  than the content it holds is refused;
* and retention over a `secret` or `confidential` input is refused **however
  explicit the caller was**. Explicitness is what makes a legal retention a
  decision; it is not a waiver for the one case the issue rules out.

### 4.5 Pinned-artifact verification before load

A provider that loads a model artifact can check it against the digest it was
pinned to and then compute the identity from the bytes it actually loaded. That
check happens on the host, before the load, and revl never sees it: item 538
gives the host the load, and the compiler must not learn what a load is.

So the record binds the CLAIM, not the check. `model_digest_basis` names which
of the two the provider is asserting:

| value | the provider says |
| ----- | ----------------- |
| `loaded-bytes` | it checked the artifact against the pinned digest, loaded it, and computed `model_digest` over the loaded bytes |
| `declared` | `model_digest` is only a declaration, not recomputed from anything it loaded |

`loaded-bytes` is the stronger claim and `declared` is the honest floor; the
sealer's default is `declared`, so a caller that only holds a digest does not
claim more by silence. The verifier checks that the member is one of the two
words and says nothing about whether the bytes were checked. This is the same
shared-secret limit this note already states in §9: the record says the named
signer asserted this, not that the assertion is true. A reader that needs proof
of the load has to hold the host's own evidence for it.

---

## 5. Where it slots in

`revl canary` attributes a divergence to an exact `(component, realm)`, and
`revl.wal.model_decisions` indexes the existing record by `(component,
stepIndex)`. The evidence object **invents no third correlation**:
`crossing_key(record)` returns `(component, step_index)`, which is the key
`model_decisions` already builds, and `test_the_crossing_key_is_the_wal_index_key`
pins the join by constructing a WAL record and looking the evidence object's key
up in the real index.

`OUTCOMES` is `(exhausted, refused, validated)`. The first two words are the WAL
writer's own vocabulary (item 257), so an evidence object carries the WAL
record's outcome with no translation table; `refused` is the third the fallback
ladder needs, because item 538 requires an unavailable member to be a refusal
rather than a lower-quality completion.

What is **not** built in this slice, stated so nobody infers it from silence:
nothing writes an evidence object during a run. Slice 1 is the object, its
sealing, its verification and its join key. §8 is the rest.

---

## 6. The signing, and the bug this is written not to repeat

This tree has shipped a signed receipt whose `key_id` was added to the body
**after** the MAC was taken, over a hand-written field list. The consequence was
that the one member every reader used to decide which key to fetch was the one
member nothing covered. Three things here are aimed at exactly that class.

**One: the covered set is derived from the record.** `_sign` MACs
`{k: v for k, v in body.items() if k != "signature"}`. There is no field list
to fall out of date with the body, so an added member, a removed member and an
edited member all change the MAC input by construction. (`revl.attest._sign`
already does this; the point of restating it is that it is the property, not a
detail.)

**Two: `seal` builds the whole body, `key_id` included, and signs that.**
`signature` is the only member attached afterwards.

**Three: the test is generated, not written out.**
`test_every_covered_member_edited_fails_the_signature` is parametrised over
`BODY_MEMBERS`, so a member added to the body in a later slice is covered
without anyone remembering to cover it. A hand-written version of that test
would have passed on the receipt with the bug.

The proof that it is not vacuous, run rather than asserted: re-introducing the
exact historical bug (MAC over a hand-written list that omits `key_id`, `key_id`
attached to the body afterwards) reds 4 tests, including the generated
per-member case on `key_id`. §7 has the rest of the numbers.

Two further readings are borrowed rather than re-derived:

* **Domain separation.** `revl.attest`, `revl.deploy` and this module MAC
  canonical JSON with the same construction and may share key material, so
  without a per-protocol tag an attestation would verify as a decision record
  and back. `SIGN_DOMAINS` gives each accepted envelope version its own tag
  (`revl.model-decision/v1\x00`, `revl.model-decision/v2\x00`), `SIGN_DOMAIN`
  is the current one, and `_sign` selects from that closed map by the record's
  version. A relabelled record therefore cannot be MAC-verified under the other
  version's tag, and the tests seal a body with `attest._sign` and require this
  verifier to refuse it. The key fingerprints are domain-separated too, so one
  secret fingerprints differently for the two protocols and a fingerprint
  cannot be lifted from one record type onto the other.
* **The algorithm is validated, not recorded.** `sign_alg` is checked against
  this build's constant rather than read off the record. That is item 428 F1's
  finding on `revl attest`: a record claiming `ed25519` that verified as HMAC
  was an algorithm-confusion downgrade that "needs no forgery skill".

### 6.1 Verification order, and why step 4 is not redundant

`verify` runs: (1) it is a mapping with a signature, (2) the MAC, (3) the key
identity, (4) the body.

Step 3 is `revl.cert.affirm_key_id`'s reading: a record whose `key_id` is not
this key's fingerprint is about a different signer **even when its MAC is
right**, and the tests construct exactly that case by signing a body that names
the wrong key with the right key.

Step 4 is the one that looks redundant and is not. The MAC proves the record is
the one its signer sealed. It says nothing about whether that signer built a
well-formed body, and under item 538 the signer is a provider on another tier
holding the same key. So a validly-signed narrow body is the case this verifier
exists for. Every member is removed in turn, re-signed with the real key, and
required to be refused; probe 2 in §7 is what that check is worth.

### 6.2 One check function, used by both sides

`seal` and `verify` call the same `_check_body`. A sealer that can mint a record
its own verifier rejects is a gate that fires only on other people's records,
and a verifier laxer than its sealer is the other half of the same split.

---

## 7. Non-vacuity

262 tests pass in `tests/test_model_evidence_517.py`, none skip. A passing test
is not evidence until it has been seen to fail, so three defects were injected
into a clean tree and the suite re-run. The numbers below were measured when
this note landed; the generated per-member sets have grown with the body since,
and the `1.0` tamper set is generated over `BODY_MEMBERS_V1` separately.

| injected defect | tests red |
| --------------- | --------- |
| the cited bug verbatim: MAC over a hand-written field list that omits `key_id` | **4** (including the generated per-member case on `key_id`, and the added-member case) |
| `verify` becomes a MAC check with decoration (the body checks removed) | **97** |
| the disclosure gate removed (`secret`/`confidential` stop disclosing) | **6** |

Two controls hold identically on a tree without this work, so the suite is not
measuring its own fixture: `attest.key_id` recomputed against its own
construction, and `attest._canonical_bytes` pinned to its exact output. Nothing
here touches the attestation path it borrows canonicalization from.

One more agreement is pinned rather than assumed. A Python module cannot `use`
an `.rvl` module, so "the digest is the shipped primitive" is proved by
compiling a consumer of `stdlib/crypto.rvl`'s classified `sha256` and
`hmac_sha256` and **executing it on the py tier**, asserting it produces the
exact values this module produces over the exact strings this module digests.
Item 272 exists because three components hand-rolled the same crypto in one
wave; this is what stops a fourth.

---

## 8. Slices

**Slice 1 (LANDED with this note).** `src/revl/model_evidence.py`: the object,
`seal`, `verify`, the closed vocabularies, the disclosure gate, `crossing_key`
and `reproducible`. `tests/test_model_evidence_517.py`. No compiler change, no
IR change, no emitter change, no CLI surface.

**Slice 2: the run writes one.** The driver seals an evidence object at a model
crossing and writes it beside the existing `model-decision` WAL record.
Needs: a key source (the `REVL_ATTEST_KEY` / `REVL_ATTEST_KEY_FILE` pattern),
the origin set threaded from item 444's compile-to-runtime taint channel, and
the `prompt_binding` mode chosen from that same gate rather than re-derived.
The `role`/`residence` values come from item 512's route table once it lands,
and until then from configuration with the residence still closed.

**Slice 3: `revl replay` reads one.** The exit clause's first half, a recorded
run replaying a model decision from the artifact alone. This is where
`reproducible()` stops being informational: a replay of a non-reproducible
record must refuse rather than re-run with a different seed and report
agreement.

**Slice 4: `revl canary` attributes with one.** When the first differing step is
a model crossing, the two evidence objects say which member differs: the same
menu and a different pick, a different menu, a different placement, or a
different policy. This is the item's actual motivation and it needs slices 2 and 3 under
it.

**Not a slice of this item**: what `placement_digest` is computed over (item
538), whether a placement was admissible (item 512), and an asymmetric
signature. The envelope carries `sign_alg` so an Ed25519 upgrade is additive,
and today the only accepted value is `hmac-sha256` and it is checked.

**Post-2.0 addendum (issue #2750).** Envelope `2.0` adds the fallback reason,
the template identity and the model-digest basis (§2.1, §4.5) as required
members, and `verify` still accepts `1.0` records under their own body and MAC
domain. No compiler, IR, emitter or CLI surface changes; `reconstruct` reports
the new members, as `None` for a `1.0` record, alongside the envelope version.

---

## 9. Things stated here that are not verified

* **The cryptography is standard and is not proven here.** The construction is
  HMAC-SHA256 over canonical JSON with a domain-separation prefix, which is
  `revl.attest`'s shipped construction reused rather than a new one. What the
  tests exercise is that the MAC covers every member, that domain separation
  holds against `attest`, and that the envelope is validated. They do not
  exercise the primitives themselves, do not constitute a cryptographic review,
  and say nothing about key management, key rotation or key distribution, none
  of which this slice touches.
* **The threat model is a shared-secret one.** A holder of the signing key can
  seal any well-formed record. Nothing here detects a signer who lies inside a
  valid body, and the verifier's body checks bound *well-formedness*, not
  *truthfulness*. `revl.attest`'s own honest spelling applies unchanged: the
  object says the named signer asserted this, not that the assertion is true.
* **Nothing writes one during a run yet.** Slice 1 is the object and its
  verification. The exit clause's "a recorded run replays a model decision from
  the artifact alone" is slices 2 and 3, and is not claimed here.
* **The `role` and `residence` values are not cross-checked against item 512's
  route table**, because that table is on an unlanded branch. The vocabulary is
  closed and the guarded agreement test is in place; whether the role a record
  names is a role the component was actually permitted is a slice-2 question.
* **The salted binding's spelling is pinned to the runtime's, not to the
  runtime.** `hmac-sha256:<64 hex>` is what `revl_prompt_digest` returns today,
  asserted against the documented shape rather than against a live call.
