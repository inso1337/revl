"""The model decision as a signed evidence object (roadmap item 517, issue #1191).

Design note: `docs/design/536-model-decision-evidence.md`.

A revl execution attaches evidence to every step except one. An effect carries
a WAL record with an inverse; an emission carries a deferred record and a flush
proof; a boundary crossing carries a receipt. A model completion carries a
`model-decision` WAL record (item 250 Slice 3a, :data:`revl.wal.
RECORD_MODEL_DECISION`) that names `{component, stepIndex, outcome, llm}` and
is deliberately silent about everything a replay would need: its own docstring
in ``backends/python/replay.py`` lists the omissions, and they are the prompt
binding, the request parameters and the placement. So `revl replay` and `revl
canary` can attribute a divergence to an exact `(component, realm)` and then
say nothing about WHY the two worlds chose differently.

This module is the evidence object that fills that gap: a sealed, MAC-covered
record binding the decision to the things that produced it. It does not make a
model call deterministic and does not claim to. It makes the call ACCOUNTABLE:
what answered, where it ran, what it was given, what it could have said, how it
was asked, and under which rule.

What the object binds
---------------------
Every member of :data:`BODY_MEMBERS` is REQUIRED and covered by the MAC.

* ``component`` / ``step_index`` — the crossing. This pair is exactly
  :func:`revl.wal.model_decisions`' key, so an evidence object and the WAL
  record it is evidence FOR index to the same crossing with no new correlation
  invented (:func:`crossing_key`).
* ``role`` / ``residence`` — the placement (item 512, `docs/design/531-model-
  placement.md` §9). Both are DECLARED, so the object binds them by value and
  asks no host. ``residence`` is the closed vocabulary :data:`RESIDENCES`.
* ``model_digest`` and ``model_digest_basis``: the identity of the weights that
  answered, and whether that digest was checked against the bytes actually
  loaded or is only the provider's declaration.
* ``placement_digest`` — the provider's own digest over its profile (see
  "Reproducibility" below). Opaque here on purpose.
* ``prompt_binding`` — the binding to the input, as a MODE and a value, never
  raw text by default. See "The prompt binding" below.
* ``template_digest`` / ``template_absence_reason``: the identity of the
  template that produced the prompt, with its placeholders unfilled, or an
  explicit reason the identity is absent. See "The template, the fallback, and
  the digest's basis" below.
* ``origins`` — the item 249 origin classes the input carried, drawn from
  :data:`ORIGIN_CLASSES`, which is :mod:`revl.taint`'s own lattice and not a
  second copy of it.
* ``reasoning`` — how much of the model's reasoning span the host returned
  (``full``, ``summary``, ``hidden`` or ``none-reported``), the span's size as
  the host reported it, and a digest of the returned reasoning text only when
  the input's origins permit it. Version 3.0; see "The reasoning span" below.
* ``candidates`` / ``chosen`` / ``outcome`` — the candidate set as digests in
  the order offered, and which one was taken. A decision with one candidate is
  a different claim from a decision with five, and only the record can say so.
* ``sampling`` — exactly the members of :data:`SAMPLING_MEMBERS`. The re-run
  instruction.
* ``policy_digest`` — the policy in force, by digest.
* ``fallback_depth`` — how far down the ladder this answer came from. Zero is
  the first choice; a non-zero depth means something earlier refused or was
  unavailable, which a record that omits it cannot distinguish from a clean
  first hit. ``fallback_reason`` names which of the ladder's three reasons it
  was.
* ``retained`` — ``None``, or the explicit, taint-carrying retention decision.

The prompt binding, and why it is a mode rather than a digest
-------------------------------------------------------------
The issue asks for "the prompt hash". A bare prompt hash would contradict two
things this tree already decided, so the member is a structured mode instead.

1. ``backends/python/runtime.py``'s :func:`revl_prompt_digest` is a FAIL-CLOSED
   gate (item 121 §4): it emits a digest only when the taint analysis is
   engaged and proves the args carry neither a ``secret`` nor a
   ``confidential`` origin, and returns ``None`` otherwise. A digest over a
   confidential prompt is a confirmation oracle — an adversary who can guess a
   candidate prompt can confirm it — so the shipped runtime refuses to produce
   one. An evidence object that required a digest unconditionally would be a
   second, wider path to the value that gate exists to withhold.
2. The digest that gate DOES produce is salted with a per-process nonce
   (``revl_digest_nonce``), so it is stable WITHIN one run and meaningless
   ACROSS runs. A cross-run replay bound to it is bound to nothing.

So ``prompt_binding`` is ``{"mode": ..., "value": ..., "reason": ...}`` with
``mode`` in :data:`PROMPT_MODES`:

* ``content-addressed`` — an unsalted digest, comparable across runs. Legal
  only when ``origins`` proves no ``secret`` and no ``confidential``, which is
  the runtime gate restated at the evidence layer.
* ``salted-within-run`` — the existing runtime value. Comparable within one
  run only; ``reason`` is ``None``.
* ``suppressed`` — no value, and ``reason`` from :data:`SUPPRESSION_REASONS`
  saying which arm of the gate fired.

The fail-closed shape is that the MEMBER is mandatory while the VALUE may be an
explicit absence. A record cannot omit the prompt binding; it must state that
the binding was suppressed and why, and that statement is inside the MAC.

The template, the fallback, and the digest's basis (version 2.0)
----------------------------------------------------------------
Envelope version ``1.0`` bound the prompt but not the template that produced
it, and it bound the fallback DEPTH but not the reason. Two runs that differed
only in template compared equal, and a depth of 2 could not say whether the
rung above was unavailable, declined or refused. Version ``2.0`` adds three
claims, and the version is bumped rather than the members added to ``1.0`` so
that an old record still verifies under the body it was sealed with and a new
record cannot be presented as an old one.

* ``template_digest`` is the SHA-256 of the template TEXT with its
  placeholders UNFILLED, never the filled prompt. ``template_absence_reason``
  is the explicit other arm: ``no-template`` when the prompt was not built from
  a template, ``unpublished`` when it was but its identity is not available.
  Exactly one arm is set. Both unset is the silence this member exists to
  refuse, and both set is a record that says two things about one template.
* ``fallback_reason`` is the descent reason, from :data:`FALLBACK_REASONS`. Its
  values are the ladder's three kinds, the words a provider spells before the
  detail: ``unavailable``, ``declined`` and ``refused``. The detail after the
  kind stays in the provider's own log and is deliberately not bound, because
  it is free text and a closed vocabulary is what makes the member comparable
  across records. ``None`` is required at depth zero and refused above it, so
  depth and reason are one claim rather than a number with an optional
  annotation.
* ``model_digest_basis`` says whether ``model_digest`` was checked against the
  bytes that were actually loaded (``loaded-bytes``) or is only the provider's
  declaration (``declared``). revl cannot see a host's load, so this is a
  DECLARATION, like ``role`` and ``residence``: the verifier checks that it is
  one of the two words, not that the bytes were checked.

Version ``1.0`` records still verify, under the ``1.0`` body and the ``1.0``
MAC domain, and ``2.0`` records under theirs; they simply do not carry the
claims their successors added. :func:`reconstruct` reports ``None`` for a
member a record's own version does not define rather than inventing a value,
and reports the record's own ``version`` so a reader can tell why.

The reasoning span (version 3.0, issue #2808)
--------------------------------------------
A model decision said what was asked and what was chosen, but nothing about
whether the model also reasoned in a span the host did not return. Silence
there reads like a run that had nothing to hide. Version ``3.0`` adds one
required member, ``reasoning``, with three parts:

* ``visibility`` — ``full`` or ``summary`` when the host returned the span or a
  summary of it, ``hidden`` when it reported a span without the text, and
  ``none-reported`` when it reported no span at all;
* ``tokens`` — ``{"value": <non-negative int or None>, "provenance":
  "host-reported"}``. revl cannot measure the reasoning channel, so the tag is
  the host's own report and not a measurement;
* ``digest`` — the SHA-256 of the returned reasoning text, and ONLY that. The
  text itself is never written. Over an input whose ``origins`` carry
  ``secret`` or ``confidential`` the digest is refused, because a digest of
  reasoning derived from such a prompt is the same cross-run confirmation
  oracle that suppresses ``prompt_binding``; the visibility and the size are
  still recorded. A ``hidden`` span has no returned text, so it cannot carry a
  digest either, and ``none-reported`` carries neither a size nor a digest.

The member records that an opaque span EXISTED and its reported size, and
makes no claim about what it contained. A per-entry replay status
(``revl.replay_modes``) turns it into ``rederivable``: a ``hidden`` or
``summary`` span means the answer cannot be reproduced from the prompt, while
a retained answer can still be served from the record.

Reproducibility: what is here, and what is deliberately not
------------------------------------------------------------
A record is not a replay. ``temperature``, ``top_p``, ``top_k``, ``seed``,
``max_tokens`` and ``stop_digest`` are REQUEST parameters — the caller supplies
them and the record's job is to bind what was asked — so they are here, in a
closed set, all six keys mandatory. ``model_digest`` is here for the same
reason: the roadmap item names it.

Quantisation, runtime build, device profile and load point are NOT here, and
that is not an omission. Item 515 says one model at three quantisation points
is three placements; item 538 says a device profile, a load cost and a
quantisation are properties of a HOST and nothing under the compiler should
learn what one is, and its exit clause says the provider "emits the record 517
signs". Putting them here would make the compiler learn what a quantisation is.

Leaving them out with no hook would be worse: two quantisations of the same
weights give different distributions under the same ``model_digest``, seed and
temperature, so a trajectory would be describable and not re-runnable. So the
object carries ``placement_digest``, a digest the PROVIDER computes over its
own profile and revl binds without interpreting. The field is mandatory here
and its MEANING is item 538's. :func:`reproducible` reports whether a record
has what a re-run needs, and it is a derived predicate rather than a refusal,
because a run with no seed is still a real run that must be recordable.

Not repeating the `key_id`-outside-the-MAC bug
-----------------------------------------------
This tree has shipped a signed record whose ``key_id`` was added to the body
AFTER the MAC was taken, so nothing ever verified it. Three things here are
aimed at exactly that class:

* :func:`_sign` derives its input FROM THE RECORD (every member except
  ``signature``) rather than from a hand-written field list. A hand-written
  list is how a member gets added to the body and forgotten in the MAC.
* :func:`seal` builds the complete body, ``key_id`` included, and signs it;
  ``signature`` is the only member added afterwards.
* ``tests/test_model_evidence_517.py`` GENERATES a mutation per member of a
  sealed record and requires every one of them to be refused. It reads the
  record's own keys, so a member added later is covered without anyone
  remembering to cover it.

Verification is fail-closed throughout: :func:`verify` returns a
:class:`Verdict` whose ``ok`` is true only after every check passed, an absent
member is a refusal rather than a skipped check, and every vocabulary is closed
(an unknown residence, origin, mode or outcome is refused, never defaulted).

From a constructed record to a recorded run (issue #1191, Slices 2 and 3)
-----------------------------------------------------------------------
Slice 1 shipped the object and the gate as a pure function, and said plainly
that "a recorded run replays a model decision from the artifact alone" was not
yet claimed: nothing wrote an evidence object during a run. Slices 2 and 3 are
the two halves of that clause, and they meet on the WAL.

* **Slice 2, the writer.** :class:`CrossingSealer` is installed into a run and
  handed to ``backends/python/runtime.py``, which calls it at every model
  crossing and rides it onto the ``model-decision`` WAL record item 250 Slice
  3a already writes (:data:`WAL_EVIDENCE_MEMBER`). The runtime holds a
  callable and no key, because that backend is stdlib-only and a second copy
  of the MAC living beside a signing key is the item 272 mistake at its worst.
  The crossing owns ``component``, ``step_index`` and ``outcome``
  (:data:`CROSSING_OWNED`); the provider declares the rest, because revl
  cannot see through the host body and will not invent what it cannot see.

  **Which way it fails.** Evidence is OPT-IN: a run that never engages a
  sealer writes exactly the record it wrote before, and `revl replay` says
  "not sealed" rather than guessing. A run that HAS engaged one and cannot
  seal a crossing does not continue as though it had. The refusal is written
  to the WAL first (:data:`WAL_REFUSAL_MEMBER`, carrying the link and the
  reason, so a post-mortem reader can tell a refused crossing from a run that
  never engaged), and then it is RAISED out of the crossing. An unaccountable
  model decision is not a degraded record; under engagement it is a stop.

* **Slice 3, the reader.** :func:`reconstruct` takes a sealed record and the
  key and returns what the decision was, consulting nothing else. It is
  fail-closed in the way that makes the exit clause bite: a record whose MAC
  does not check out yields NO reading at all, not a reading with a warning.
  `revl replay` runs it over every decision on a WAL (``revl.replay_modes``).

* **The placement, cross-checked at last.** Slice 1 bound ``role`` and
  ``residence`` by value and nothing compared them to anything, because item
  512 had not landed. It has (``src/revl/model_route.py``), so
  :func:`check_placement` compares them to its route table — at seal time, so
  a contradicting record is never minted, and offline, so one that arrives
  from elsewhere is still refused.

What is still NOT claimed, in Slices 2 and 3 as in Slice 1: key management,
rotation and distribution are all outside this module, and the 16-hex
:func:`key_id` is a fingerprint for CHOOSING a key, never a proof of one. The
MAC is a shared-secret construction, so a holder of the key can mint any record
it likes; nothing here is a signature in the public-key sense and nothing here
detects a compromised signer.

Public surface
--------------
``seal(key, **members)``      — build and sign a record
``verify(record, key)``       — ``Verdict(ok, link, reason)``
``crossing_key(record)``      — ``(component, step_index)``, the WAL index key
``reproducible(record)``      — ``(bool, [missing...])``
``check_placement(body, roles)`` — item 512's route table, cross-checked
``CrossingSealer(key, route_table)`` — Slice 2: seal at the crossing
``reconstruct(record, key)``  — Slice 3: the decision, from the artifact alone
``from_wal_record(decision)`` — the evidence riding on a WAL record
``reasoning_span(**host)``    — the 3.0 ``reasoning`` member from what the host returned
``digest(text)`` / ``key_id(key)``
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .attest import NotCanonicalizable, _canonical_bytes
from .taint import _ORIGIN_CLASSES

# ---------------------------------------------------------------------------
# envelope identity
# ---------------------------------------------------------------------------

#: The self-identifying tag, the same kind/version idea `revl.interchange` and
#: `revl.attest` use.
EVIDENCE_KIND = "revl.model-decision"

#: MAJOR.MINOR, additive within a MAJOR. `verify` refuses any other value
#: rather than reading a future body as if members were merely missing.
#: Version 2.0 adds ``fallback_reason``, ``template_digest`` /
#: ``template_absence_reason`` and ``model_digest_basis``; version 3.0 adds
#: ``reasoning``, the closed description of the span the host did not return.
#: :func:`verify` accepts a 1.0 or 2.0 record under its own body, so old
#: artifacts keep checking out.
EVIDENCE_VERSION = "3.0"

#: Every envelope version this build can verify, newest first. A record's own
#: version selects its body member set and its MAC domain, so neither a 1.0
#: record relabelled 2.0 nor a 2.0 record relabelled 1.0 can verify: the first
#: loses its MAC and lacks members, the second has members 1.0 does not define.
#: The same rule keeps a 2.0 record from passing as 3.0 without a reasoning
#: member, and a 3.0 record from shedding it as an older one.
EVIDENCE_VERSIONS = ("3.0", "2.0", "1.0")

#: One value today. It is VALIDATED and not merely recorded: :func:`_sign` is
#: unconditionally HMAC-SHA256, so a record claiming another algorithm is a
#: mislabel. Reading the algorithm off the RECORD instead of off this build is
#: the algorithm-confusion downgrade item 428 F1 measured on `revl attest`.
SIGN_ALG = "hmac-sha256"

#: The digest algorithm every 64-hex member in the body is produced by.
HASH_ALG = "sha256"

#: The domain-separation prefix for each accepted envelope version.
#: `revl.attest`, `revl.deploy` and this module MAC canonical JSON with the
#: same construction and may share key material, so without a per-protocol tag
#: an attestation verifies as a decision record and back. The tag carries the
#: version, so a record sealed under one version cannot verify as another even
#: when a key holder rewrites the covered ``version`` member. :func:`_sign`
#: selects the domain from the record's own version, but only from this closed
#: map; an unknown version is signed under :data:`SIGN_DOMAIN` and then refused
#: by :func:`_check_body`, so reading the version off the record never selects
#: an algorithm this build does not implement.
SIGN_DOMAINS = {
    "1.0": b"revl.model-decision/v1\x00",
    "2.0": b"revl.model-decision/v2\x00",
    "3.0": b"revl.model-decision/v3\x00",
}

#: The domain :func:`seal` writes and the fallback for an unknown version. It
#: is the CURRENT version's tag, not a stable 1.0 tag: out-of-tree code that
#: MACs a 1.0 record must use :data:`SIGN_DOMAINS` rather than this constant.
SIGN_DOMAIN = SIGN_DOMAINS[EVIDENCE_VERSION]

#: The one member of a sealed record that the MAC does not cover: itself.
SIGNATURE_FIELD = "signature"


# ---------------------------------------------------------------------------
# the closed vocabularies
# ---------------------------------------------------------------------------

#: Item 512's residence vocabulary (`docs/design/531-model-placement.md` §2).
#: `on_device` means the call runs where the component runs and the prompt does
#: not leave; `off_device` means it leaves. Item 512 lands the declaration that
#: produces these; this is the same two words, bound by value, because §9 of
#: that note says the placement reaches here declared rather than observed.
#: `tests/test_model_evidence_517.py` asserts agreement with `revl.model_route`
#: whenever that module is importable, so the two cannot drift once 512 lands.
RESIDENCES = ("off_device", "on_device")

#: Item 249's origin lattice, read from :mod:`revl.taint` rather than copied.
#: Item 272 exists because three components independently re-derived one piece
#: of shared machinery in a single wave; a second spelling of the lattice would
#: be the same mistake at the vocabulary level.
ORIGIN_CLASSES = tuple(sorted(_ORIGIN_CLASSES))

#: The origins the runtime's own digest gate refuses to hash over
#: (`backends/python/runtime.py:revl_prompt_digest`). Restated here because the
#: evidence layer must refuse a record a non-py provider wrote around the gate.
DISCLOSURE_ORIGINS = ("confidential", "secret")

#: How the record is bound to the input it was given. See the module header.
PROMPT_MODES = ("content-addressed", "salted-within-run", "suppressed")

#: Why a `suppressed` binding has no value. Each names one arm of the runtime
#: gate, so a reader learns which one fired instead of reading a bare `None`.
SUPPRESSION_REASONS = ("analysis-disengaged", "confidential-origin",
                       "secret-origin")

#: Why a decision left the first rung of the ladder, closed. These are the
#: three KINDS the provider spells before its detail (`unavailable:<detail>`,
#: `declined:<detail>`, `refused:<detail>`); the detail is free text and stays
#: in the provider's log, while the kind is comparable across records.
FALLBACK_REASONS = ("declined", "refused", "unavailable")

#: Why a record has no `template_digest`. `no-template` says the prompt was
#: built without one; `unpublished` says a template was used but its identity
#: was not available to the provider. Both are explicit, which is the point:
#: a silent absence is what this member pair exists to refuse.
TEMPLATE_ABSENCE_REASONS = ("no-template", "unpublished")

#: What `model_digest` is a claim ABOUT. `loaded-bytes` says the provider
#: recomputed the digest over the bytes it loaded, after checking them against
#: the pinned artifact; `declared` says the digest is only the provider's
#: declaration. revl cannot check the claim either way (item 538 owns the
#: load), so the closed word is what the record can honestly carry: the
#: assertion that was made, not a proof that it was made.
MODEL_DIGEST_BASES = ("declared", "loaded-bytes")

#: How much of the reasoning the model did is on the record (issue #2808). The
#: host, not revl, knows whether it returned the span in full, returned a
#: summary of it, kept it and reported only that it existed, or reported
#: nothing. `full` and `summary` say reasoning TEXT was returned (only its
#: digest is ever written); `hidden` says the host reported a span without its
#: text; `none-reported` says the host reported no span at all.
REASONING_VISIBILITIES = ("full", "summary", "hidden", "none-reported")

#: The provenance tag a reasoning token count carries. revl cannot measure the
#: reasoning channel, so the only honest tag is the host's own report.
REASONING_TOKEN_PROVENANCE = "host-reported"

#: The members of a `reasoning` block. All three are mandatory; `None` is a
#: legal VALUE for `digest` (suppressed or absent) and for `tokens.value` (a
#: host that reported a span but not its size).
REASONING_MEMBERS = ("visibility", "tokens", "digest")

#: What the crossing produced. `validated` and `exhausted` are item 257's two
#: readings and the two values the WAL record already writes; `refused` is the
#: third the fallback ladder needs (item 538: an unavailable member returns a
#: refusal the ladder consumes, not a lower-quality completion).
OUTCOMES = ("exhausted", "refused", "validated")

#: The sampling members, closed and all mandatory. A free-form parameter bag
#: that may be missing a key is a description of a request; a closed set is an
#: instruction for re-issuing one. `None` is a legal VALUE and means the
#: request did not set that knob; a missing KEY is a refusal.
SAMPLING_MEMBERS = ("max_tokens", "seed", "stop_digest", "temperature",
                    "top_k", "top_p")

#: The 1.0 body, kept because an old sealed record must still verify under the
#: body it was sealed with. This is a frozen list: a member added to a later
#: version goes into that version's tuple, never into this one.
BODY_MEMBERS_V1 = (
    # envelope
    "kind", "version", "sign_alg", "hash_alg", "key_id", "recorded_at",
    # the crossing (revl.wal.model_decisions' key)
    "component", "step_index",
    # the placement (item 512), declared and bound by value
    "role", "residence",
    # what answered, and on what host profile (item 538 owns the profile)
    "model_digest", "placement_digest",
    # what it was given
    "prompt_binding", "origins",
    # what it could have said, and what it said
    "candidates", "chosen", "outcome",
    # how it was asked
    "sampling",
    # under what rule, and how far down the ladder
    "policy_digest", "fallback_depth",
    # the explicit, taint-carrying retention decision
    "retained",
)

#: The signed 2.0 body, in full. Every one of these is required and covered.
#: Frozen: a member added to a later version goes into that version's tuple,
#: never into this one, so a 2.0 record keeps verifying under the body it was
#: sealed with.
BODY_MEMBERS_V2 = (
    # envelope
    "kind", "version", "sign_alg", "hash_alg", "key_id", "recorded_at",
    # the crossing (revl.wal.model_decisions' key)
    "component", "step_index",
    # the placement (item 512), declared and bound by value
    "role", "residence",
    # what answered, on what host profile, and what the digest is a claim about
    "model_digest", "model_digest_basis", "placement_digest",
    # what it was given: the input's binding, and the template behind it
    "prompt_binding", "template_digest", "template_absence_reason", "origins",
    # what it could have said, and what it said
    "candidates", "chosen", "outcome",
    # how it was asked
    "sampling",
    # under what rule, how far down the ladder, and why it left the rung above
    "policy_digest", "fallback_depth", "fallback_reason",
    # the explicit, taint-carrying retention decision
    "retained",
)

#: The signed 3.0 body, in full. Adds the closed description of the model's
#: reasoning span (issue #2808) to the frozen 2.0 set. Required, not optional:
#: a new record that omits it would read exactly like a run that had nothing
#: to hide, which is the silence the member exists to break.
BODY_MEMBERS = BODY_MEMBERS_V2 + ("reasoning",)

#: version -> the body that version defines. A record's own ``version`` selects
#: its member set, so an unknown member is refused rather than read as 1.0.
BODY_MEMBERS_BY_VERSION = {"1.0": BODY_MEMBERS_V1, "2.0": BODY_MEMBERS_V2,
                           "3.0": BODY_MEMBERS}

#: The members of a `retained` block when one is present.
RETAINED_MEMBERS = ("completion", "origins", "prompt")


# ---------------------------------------------------------------------------
# refusal links — the named reason a verification failed
# ---------------------------------------------------------------------------
#
# `revl.deploy` names its admission refusals by link rather than by free text,
# so a caller can branch on WHICH check refused without parsing a sentence.
# Same discipline here. These are not G-codes: this is a verifier refusing a
# RECORD, not the checker refusing a composition, and inventing a guarantee
# code for a library verdict would misfile it in `revl explain`.

#: The MAC does not match — the roadmap's own exit clause, "an edited field
#: fails its digest". Any edit to any covered member lands here.
EVIDENCE_SIGNATURE = "signature"

#: The record names a key that is not the key it is being checked with. Its MAC
#: may be right and the record is still about a different signer.
EVIDENCE_SIGNER = "key-identity"

#: `kind`, `version`, `sign_alg` or `hash_alg` is not this envelope.
EVIDENCE_ENVELOPE = "envelope"

#: A required member is absent, or the record is not a mapping at all.
EVIDENCE_INCOMPLETE = "incomplete"

#: A member is outside its closed vocabulary, or has the wrong shape.
EVIDENCE_VOCABULARY = "vocabulary"

#: The candidate set and the choice do not describe one decision.
EVIDENCE_CANDIDATES = "candidate-set"

#: The record binds or retains input content that its own declared origins say
#: it must not. The evidence-layer restatement of the runtime's digest gate.
EVIDENCE_DISCLOSURE = "disclosure"

#: The sealed record does not name the crossing it was FOUND at. Its MAC can be
#: perfectly valid and it can still be a genuine record of another crossing,
#: lifted onto this one — the `cert.affirm_key_id` reading, applied to the join
#: instead of to the signer. Only a reader that holds both sides (the WAL's
#: index key and the sealed body) can see this, which is why it is a link here
#: and not a check inside :func:`verify`.
EVIDENCE_CROSSING = "crossing"

#: The record's declared placement contradicts item 512's own route table, or
#: names a role that table does not declare. Distinct from
#: :data:`EVIDENCE_VOCABULARY`, which is about the closed word list: a record
#: reading `role="local", residence="off_device"` is two legal words making a
#: claim the PROGRAM refutes, and a reader must be able to tell those apart.
EVIDENCE_PLACEMENT = "placement"

#: Every link this module can return, for a caller that wants to enumerate.
EVIDENCE_LINKS = (EVIDENCE_CANDIDATES, EVIDENCE_CROSSING, EVIDENCE_DISCLOSURE,
                  EVIDENCE_ENVELOPE, EVIDENCE_INCOMPLETE, EVIDENCE_PLACEMENT,
                  EVIDENCE_SIGNATURE, EVIDENCE_SIGNER, EVIDENCE_VOCABULARY)


class EvidenceRefused(ValueError):
    """A record that could not be sealed, or a verification a caller asked to
    raise on. Carries the same ``link`` a :class:`Verdict` would."""

    def __init__(self, link: str, reason: str):
        super().__init__(f"{link}: {reason}")
        self.link = link
        self.reason = reason


@dataclass(frozen=True)
class Verdict:
    """The result of :func:`verify`.

    ``ok`` is true only when every check passed. ``link`` is ``""`` on a pass
    and one of :data:`EVIDENCE_LINKS` on a refusal, so a caller branches on the
    check that fired rather than on the wording."""

    ok: bool
    link: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def digest(text: str) -> str:
    """Hex SHA-256 of the UTF-8 bytes of ``text``.

    The same two lines `stdlib/crypto.rvl`'s `sha256` runs in its `@py` body and
    `revl.attest` runs for a composition hash — one construction, not a fourth
    hand-rolled one (item 272). `tests/test_model_evidence_517.py` executes the
    stdlib primitive through the py backend and pins the agreement, so this
    function and the classified extern cannot drift."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key_id(key: bytes) -> str:
    """A non-secret fingerprint of the signing key, recorded in the body so a
    reader can tell WHICH key it needs without the key being present.

    Domain-separated from :func:`revl.attest.key_id`: the same secret used for
    both protocols fingerprints differently, so a fingerprint cannot be lifted
    from one record type onto the other as if it named the same role."""
    return hashlib.sha256(b"revl-model-evidence-keyid\x00"
                          + bytes(key)).hexdigest()[:16]


def reasoning_span(*, tokens: Optional[int] = None,
                   text: Optional[str] = None,
                   visibility: Optional[str] = None,
                   origins: Sequence[str] = ()) -> dict:
    """Build the ``reasoning`` member from what the host returned (issue #2808).

    ``visibility`` omitted is derived from the observed fields, which is the
    common case a host body has: returned reasoning text is ``full``, a
    reported token count with no text is ``hidden``, and neither is
    ``none-reported``. A host that returned a SUMMARY says so by passing
    ``visibility="summary"`` because only it knows the text was a summary, not
    the span.

    ``tokens`` is the host's reasoning token count, tagged in the member with
    :data:`REASONING_TOKEN_PROVENANCE`. ``text`` is used ONLY to compute the
    digest; the text itself is never a member, and the digest is suppressed
    when ``origins`` names a ``secret`` or ``confidential`` input, because a
    digest of reasoning derived from such a prompt is a confirmation oracle
    (the rule :func:`revl_prompt_digest` already applies to the prompt)."""
    origin_list = list(origins)
    returned = text if isinstance(text, str) else ""
    if visibility is None:
        if returned:
            visibility = "full"
        elif tokens is not None:
            visibility = "hidden"
        else:
            visibility = "none-reported"
    if visibility not in REASONING_VISIBILITIES:
        raise ValueError(
            f"reasoning visibility {visibility!r} is outside "
            f"{list(REASONING_VISIBILITIES)}")
    if tokens is not None and (isinstance(tokens, bool)
                               or not isinstance(tokens, int) or tokens < 0):
        raise ValueError(
            f"a reasoning token count is a non-negative integer or None "
            f"({tokens!r})")
    if visibility in ("hidden", "none-reported") and returned:
        raise ValueError(
            f"visibility {visibility!r} says no reasoning text was returned, "
            "and a text was given")
    if visibility == "none-reported" and tokens is not None:
        raise ValueError(
            "visibility 'none-reported' says the host reported no span, and "
            f"a token count was given ({tokens!r})")
    discloses = bool(set(origin_list) & set(DISCLOSURE_ORIGINS))
    return {
        "visibility": visibility,
        "tokens": {"value": tokens,
                   "provenance": REASONING_TOKEN_PROVENANCE},
        "digest": digest(returned) if returned and not discloses else None,
    }


def _sign(body: Mapping[str, Any], key: bytes) -> str:
    """HMAC-SHA256 over the body's version domain ++ the canonical bytes of
    every member of ``body`` EXCEPT ``signature``.

    The set of covered members is DERIVED FROM THE RECORD. That is the whole
    defence against the failure this module was written not to repeat: a signed
    receipt in this tree once had `key_id` appended to its body after the MAC
    was taken over a hand-written field list, so the member every reader used
    to choose a key was the one member nothing covered. There is no field list
    here to fall out of date with the body.

    The DOMAIN is the one the record's own version names, but only from the
    closed :data:`SIGN_DOMAINS` map. An unknown or malformed version uses
    :data:`SIGN_DOMAIN`, so a rewritten version lands on the MAC first; a key
    holder who re-MACs the unknown version is refused by :func:`_check_body`.
    The version is covered by the MAC either way, so it cannot be rewritten
    without changing the tag."""
    covered = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    version = body.get("version")
    domain = SIGN_DOMAINS.get(version, SIGN_DOMAIN) \
        if isinstance(version, str) else SIGN_DOMAIN
    return hmac.new(bytes(key), domain + _canonical_bytes(covered),
                    hashlib.sha256).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# the body checks — one function, used by BOTH seal and verify
# ---------------------------------------------------------------------------

def _check_body(body: Mapping[str, Any]) -> Optional[Verdict]:
    """Every structural rule the body must satisfy, or ``None`` when it does.

    :func:`seal` and :func:`verify` run THIS function, not two similar ones. A
    sealer that can mint a record its own verifier refuses is a gate that fires
    only on other people's records, and a verifier that is laxer than its
    sealer is the fail-open half of the same split."""
    version = body.get("version")
    members = BODY_MEMBERS_BY_VERSION.get(version, BODY_MEMBERS) \
        if isinstance(version, str) else BODY_MEMBERS
    missing = [m for m in members if m not in body]
    if missing:
        return Verdict(False, EVIDENCE_INCOMPLETE,
                       "required members absent from the body: "
                       + ", ".join(missing))
    extra = [m for m in body if m not in members and m != SIGNATURE_FIELD]
    if extra:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"members this version ({version!r}) does not define: "
                       + ", ".join(sorted(extra)))

    # -- envelope -----------------------------------------------------------
    # The version's VALIDITY is checked after the member sets, so a record
    # missing a member reports as incomplete rather than as an envelope refusal
    # it happened to trigger; the version has already selected which member set
    # is required above, from a closed map and never from free text.
    if version not in EVIDENCE_VERSIONS:
        return Verdict(False, EVIDENCE_ENVELOPE,
                       f"version is {version!r}, not one of "
                       f"{list(EVIDENCE_VERSIONS)}")
    for member, expected in (("kind", EVIDENCE_KIND),
                             ("sign_alg", SIGN_ALG),
                             ("hash_alg", HASH_ALG)):
        if body[member] != expected:
            return Verdict(False, EVIDENCE_ENVELOPE,
                           f"{member} is {body[member]!r}, not {expected!r}")
    if not _HEX16.match(str(body["key_id"])):
        return Verdict(False, EVIDENCE_ENVELOPE,
                       f"key_id is not a key fingerprint ({body['key_id']!r})")
    if not isinstance(body["recorded_at"], str) or not body["recorded_at"]:
        return Verdict(False, EVIDENCE_ENVELOPE,
                       f"recorded_at is not a timestamp "
                       f"({body['recorded_at']!r})")

    # -- the crossing -------------------------------------------------------
    if not isinstance(body["component"], str) or not body["component"]:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"component is not a name ({body['component']!r})")
    step = body["step_index"]
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"step_index is not a step ordinal ({step!r})")

    # -- the placement (item 512) -------------------------------------------
    if not isinstance(body["role"], str) or not body["role"]:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"role is not a declared role name ({body['role']!r})")
    if body["residence"] not in RESIDENCES:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"residence {body['residence']!r} is outside the closed "
                       f"vocabulary {list(RESIDENCES)}")

    # -- what answered ------------------------------------------------------
    if not _HEX64.match(str(body["model_digest"])):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"model_digest is not a {HASH_ALG} digest "
                       f"({body['model_digest']!r})")
    placement = body["placement_digest"]
    if placement is not None and not _HEX64.match(str(placement)):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"placement_digest is neither absent nor a {HASH_ALG} "
                       f"digest ({placement!r})")

    # -- the origins (item 249) ---------------------------------------------
    origins = body["origins"]
    if not isinstance(origins, list) or any(not isinstance(o, str)
                                            for o in origins):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"origins is not a list of origin classes ({origins!r})")
    unknown = sorted(set(origins) - set(ORIGIN_CLASSES))
    if unknown:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"origins outside item 249's lattice: {unknown} "
                       f"(the lattice is {list(ORIGIN_CLASSES)})")
    if list(origins) != sorted(set(origins)):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "origins is not the sorted, duplicate-free set it "
                       "claims to be, so two records over the same input "
                       "would not compare equal")
    discloses = bool(set(origins) & set(DISCLOSURE_ORIGINS))

    verdict = _check_prompt_binding(body["prompt_binding"], discloses)
    if verdict is not None:
        return verdict

    # -- the candidate set --------------------------------------------------
    verdict = _check_candidates(body["candidates"], body["chosen"],
                               body["outcome"])
    if verdict is not None:
        return verdict

    # -- how it was asked ---------------------------------------------------
    verdict = _check_sampling(body["sampling"])
    if verdict is not None:
        return verdict

    # -- the rule, and the ladder -------------------------------------------
    if not _HEX64.match(str(body["policy_digest"])):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"policy_digest is not a {HASH_ALG} digest "
                       f"({body['policy_digest']!r})")
    depth = body["fallback_depth"]
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"fallback_depth is not a ladder depth ({depth!r})")

    if version in ("2.0", "3.0"):
        verdict = _check_fallback_reason(body["fallback_reason"], depth)
        if verdict is not None:
            return verdict
        verdict = _check_template(body["template_digest"],
                                 body["template_absence_reason"])
        if verdict is not None:
            return verdict
        basis = body["model_digest_basis"]
        if basis not in MODEL_DIGEST_BASES:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"model_digest_basis {basis!r} is outside the "
                           f"closed vocabulary {list(MODEL_DIGEST_BASES)}")

    if version == "3.0":
        verdict = _check_reasoning(body["reasoning"], discloses)
        if verdict is not None:
            return verdict

    return _check_retained(body["retained"], origins, discloses)


def _check_fallback_reason(reason: Any, depth: int) -> Optional[Verdict]:
    """The descent reason, as one claim WITH the depth it explains.

    A non-zero depth is the record saying the ladder moved; a reason is the
    record saying why. Neither may appear without the other: a depth with no
    reason cannot tell an audit whether the rung was unavailable, declined or
    refused, and a reason at depth zero claims a descent that did not happen.
    """
    if depth == 0:
        if reason is not None:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"a first-choice decision (fallback_depth 0) names "
                           f"fallback_reason {reason!r}; a reason is a claim "
                           "the ladder descended, and the depth says it did "
                           "not")
        return None
    if reason is None:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"fallback_depth is {depth} and fallback_reason is "
                       "absent; a depth without a reason cannot say whether "
                       "the rung above was unavailable, declined or refused")
    if reason not in FALLBACK_REASONS:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"fallback_reason {reason!r} is outside the closed "
                       f"vocabulary {list(FALLBACK_REASONS)}")
    return None


def _check_template(template_digest: Any,
                    absence_reason: Any) -> Optional[Verdict]:
    """A template identity, or an explicit reason there is none.

    Exactly one arm is set. The digest is over the template text with its
    placeholders UNFILLED, so it identifies the template and not the prompt; a
    filled prompt is what ``prompt_binding`` already covers, and binding it
    twice would make the two members disagree about what each is for.
    """
    if absence_reason is not None and template_digest is not None:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "the record carries both a template_digest and a "
                       "template_absence_reason, so it says two things about "
                       "one template")
    if absence_reason is not None:
        if absence_reason not in TEMPLATE_ABSENCE_REASONS:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"template_absence_reason {absence_reason!r} is "
                           f"outside the closed vocabulary "
                           f"{list(TEMPLATE_ABSENCE_REASONS)}")
        return None
    if template_digest is None:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "the record names neither a template_digest nor a "
                       "template_absence_reason, so which template produced "
                       "the prompt is silent; an absent identity must be "
                       "explicit and named")
    if not isinstance(template_digest, str) \
            or not _HEX64.match(template_digest):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"template_digest is not a {HASH_ALG} digest of the "
                       f"unfilled template text ({template_digest!r})")
    return None


def _check_reasoning(reasoning: Any, discloses: bool) -> Optional[Verdict]:
    """The reasoning span as one closed claim (version 3.0, issue #2808).

    The member says how much of the span the host returned, how many tokens it
    reported, and a digest of the returned text when the input's origins permit
    one. It never carries the text. The coherence rules are what keep the three
    facts from contradicting one another:

    * ``none-reported`` cannot carry a token count or a digest: nothing was
      reported, so neither exists;
    * ``hidden`` cannot carry a digest: there was no returned text to digest;
    * ``full`` and ``summary`` DID return text, so over a non-disclosing input
      the digest is required rather than optional — otherwise the record is
      silent about the one thing it could bind;
    * over a ``secret`` or ``confidential`` input the digest is REFUSED, for the
      same reason a content-addressed ``prompt_binding`` is: a digest of
      reasoning derived from such a prompt is a cross-run confirmation oracle.
    """
    if not isinstance(reasoning, Mapping):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"reasoning is not a reasoning span ({reasoning!r})")
    if set(reasoning) != set(REASONING_MEMBERS):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "reasoning names "
                       f"{sorted(reasoning)}, not {list(REASONING_MEMBERS)}")
    visibility = reasoning["visibility"]
    if visibility not in REASONING_VISIBILITIES:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"reasoning visibility {visibility!r} is outside the "
                       f"closed vocabulary {list(REASONING_VISIBILITIES)}")
    tokens = reasoning["tokens"]
    if not isinstance(tokens, Mapping) or \
            set(tokens) != {"value", "provenance"}:
        named = sorted(tokens) if isinstance(tokens, Mapping) else tokens
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"reasoning.tokens names {named!r}, "
                       "not ['provenance', 'value']")
    if tokens["provenance"] != REASONING_TOKEN_PROVENANCE:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "reasoning token provenance is "
                       f"{tokens['provenance']!r}; revl cannot measure the "
                       "reasoning channel, so the only honest tag is "
                       f"{REASONING_TOKEN_PROVENANCE!r}")
    count = tokens["value"]
    if count is not None and (isinstance(count, bool)
                              or not isinstance(count, int) or count < 0):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "reasoning.tokens.value is neither absent nor a "
                       f"non-negative integer ({count!r})")
    digest_value = reasoning["digest"]
    if digest_value is not None and not _HEX64.match(str(digest_value)):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"reasoning.digest is neither absent nor a {HASH_ALG} "
                       f"digest ({digest_value!r})")
    if visibility == "none-reported":
        if count is not None:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           "reasoning visibility is 'none-reported' and the "
                           f"member carries a token count ({count!r}); a host "
                           "that reported no span reported no size either")
        if digest_value is not None:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           "reasoning visibility is 'none-reported' and the "
                           "member carries a digest; there was no returned "
                           "text to digest")
        return None
    if visibility == "hidden":
        if digest_value is not None:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           "reasoning visibility is 'hidden' and the member "
                           "carries a digest; a hidden span has no returned "
                           "text to digest, and drawing a digest of it would "
                           "claim the text was in hand")
        return None
    # full or summary: a text was returned
    if discloses:
        if digest_value is not None:
            return Verdict(False, EVIDENCE_DISCLOSURE,
                           f"a reasoning digest over an input carrying "
                           f"{sorted(set(DISCLOSURE_ORIGINS))} origins is a "
                           "cross-run confirmation oracle for the reasoning "
                           "text; the permission rule that governs "
                           "prompt_binding suppresses it")
        return None
    if digest_value is None:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"a {visibility!r} reasoning span over a "
                       "non-disclosing input carries no digest; the host "
                       "returned text and the record can bind it, so a silent "
                       "absence is the hole this member exists to close")
    return None


def _check_prompt_binding(binding: Any, discloses: bool) -> Optional[Verdict]:
    """The prompt binding's shape, and the gate it inherits from the runtime."""
    if not isinstance(binding, dict):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"prompt_binding is not a binding ({binding!r})")
    if set(binding) != {"mode", "reason", "value"}:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "prompt_binding names "
                       f"{sorted(binding)}, not ['mode', 'reason', 'value']")
    mode, value, reason = binding["mode"], binding["value"], binding["reason"]
    if mode not in PROMPT_MODES:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"prompt_binding mode {mode!r} is outside "
                       f"{list(PROMPT_MODES)}")
    if mode == "suppressed":
        if value is not None:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           "a suppressed prompt binding carries a value "
                           f"({value!r}), which is the thing suppression means "
                           "it does not have")
        if reason not in SUPPRESSION_REASONS:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"suppression reason {reason!r} is outside "
                           f"{list(SUPPRESSION_REASONS)}")
        return None
    if reason is not None:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"a {mode} prompt binding carries a suppression reason "
                       f"({reason!r})")
    if mode == "content-addressed":
        if not _HEX64.match(str(value)):
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"a content-addressed prompt binding is not a "
                           f"{HASH_ALG} digest ({value!r})")
        if discloses:
            # The evidence-layer restatement of `revl_prompt_digest`'s gate.
            # An unsalted digest over a confidential prompt is a confirmation
            # oracle that outlives the run; the runtime refuses to make one, so
            # a record carrying one was written around the runtime.
            return Verdict(False, EVIDENCE_DISCLOSURE,
                           "a content-addressed prompt binding over an input "
                           f"carrying {sorted(set(DISCLOSURE_ORIGINS))} origins "
                           "is a cross-run confirmation oracle for the prompt; "
                           "the runtime digest gate suppresses it (item 121 §4)")
        return None
    # salted-within-run
    if not isinstance(value, str) or not value.startswith("hmac-sha256:") \
            or not _HEX64.match(value[len("hmac-sha256:"):]):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "a salted-within-run prompt binding is not the "
                       "runtime's `hmac-sha256:<64 hex>` salted digest "
                       f"({value!r})")
    if discloses:
        return Verdict(False, EVIDENCE_DISCLOSURE,
                       "a salted prompt binding over an input carrying "
                       f"{sorted(set(DISCLOSURE_ORIGINS))} origins is what "
                       "`revl_prompt_digest` returns None for; the record "
                       "must say suppressed, with the reason")
    return None


def _check_candidates(candidates: Any, chosen: Any,
                      outcome: Any) -> Optional[Verdict]:
    """The candidate set, the choice and the outcome as ONE claim."""
    if outcome not in OUTCOMES:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"outcome {outcome!r} is outside {list(OUTCOMES)}")
    if not isinstance(candidates, list):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"candidates is not a list ({candidates!r})")
    for entry in candidates:
        if not isinstance(entry, str) or not _HEX64.match(entry):
            return Verdict(False, EVIDENCE_CANDIDATES,
                           f"a candidate is not a {HASH_ALG} digest "
                           f"({entry!r})")
    if len(set(candidates)) != len(candidates):
        return Verdict(False, EVIDENCE_CANDIDATES,
                       "the candidate set repeats a digest, so 'which one was "
                       "chosen' has more than one answer")
    if outcome == "validated":
        if not candidates:
            return Verdict(False, EVIDENCE_CANDIDATES,
                           "a validated decision with an empty candidate set "
                           "records a choice among nothing")
        if not isinstance(chosen, int) or isinstance(chosen, bool) \
                or not 0 <= chosen < len(candidates):
            return Verdict(False, EVIDENCE_CANDIDATES,
                           f"chosen {chosen!r} is not an index into the "
                           f"{len(candidates)} candidates offered")
        return None
    if chosen is not None:
        return Verdict(False, EVIDENCE_CANDIDATES,
                       f"an outcome of {outcome!r} took no candidate, but the "
                       f"record names chosen={chosen!r}")
    return None


def _check_sampling(sampling: Any) -> Optional[Verdict]:
    """The closed sampling set. Every key mandatory; `None` is a legal value
    and states that the request did not set that knob."""
    if not isinstance(sampling, dict):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"sampling is not a parameter set ({sampling!r})")
    absent = [m for m in SAMPLING_MEMBERS if m not in sampling]
    if absent:
        return Verdict(False, EVIDENCE_INCOMPLETE,
                       "sampling omits " + ", ".join(absent)
                       + "; a re-run instruction with a missing knob is a "
                         "description, and `None` is how the record says the "
                         "request did not set one")
    unknown = sorted(set(sampling) - set(SAMPLING_MEMBERS))
    if unknown:
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"sampling names members this envelope does not "
                       f"define: {unknown}")
    for member in ("temperature", "top_p"):
        value = sampling[member]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"sampling.{member} is not a number ({value!r})")
        if not 0.0 <= float(value):
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"sampling.{member} is negative ({value!r})")
    for member in ("max_tokens", "seed", "top_k"):
        value = sampling[member]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return Verdict(False, EVIDENCE_VOCABULARY,
                           f"sampling.{member} is not a non-negative integer "
                           f"({value!r})")
    stop = sampling["stop_digest"]
    if stop is not None and not _HEX64.match(str(stop)):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"sampling.stop_digest is neither absent nor a "
                       f"{HASH_ALG} digest ({stop!r})")
    return None


def _check_retained(retained: Any, origins: Sequence[str],
                    discloses: bool) -> Optional[Verdict]:
    """The retention decision. Absent by default; present only as an explicit
    claim that carries its own taint."""
    if retained is None:
        return None
    if not isinstance(retained, dict):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"retained is neither absent nor a retention block "
                       f"({retained!r})")
    if set(retained) != set(RETAINED_MEMBERS):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"a retention block names {sorted(retained)}, not "
                       f"{list(RETAINED_MEMBERS)}")
    if discloses:
        return Verdict(False, EVIDENCE_DISCLOSURE,
                       "retaining content from an input carrying "
                       f"{sorted(set(DISCLOSURE_ORIGINS))} origins writes the "
                       "confidential bytes into an artifact that travels; the "
                       "digest is the binding and this is not a digest")
    if not isinstance(retained["prompt"], str):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "a retention block retains no prompt text, so it "
                       "carries the taint of content it does not hold")
    completion = retained["completion"]
    if completion is not None and not isinstance(completion, str):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"retained completion is neither absent nor text "
                       f"({completion!r})")
    carried = retained["origins"]
    if not isinstance(carried, list) or not carried \
            or any(not isinstance(o, str) for o in carried):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       "a retention block declares no origins for the bytes "
                       f"it holds ({carried!r})")
    if list(carried) != sorted(set(carried)):
        return Verdict(False, EVIDENCE_VOCABULARY,
                       f"retained origins is not a sorted set ({carried!r})")
    stray = sorted(set(carried) - set(origins))
    if stray:
        return Verdict(False, EVIDENCE_DISCLOSURE,
                       f"the retained bytes claim origins {stray} the decision "
                       "itself does not carry, so the record's own taint is "
                       "narrower than the content it holds")
    return None


# ---------------------------------------------------------------------------
# seal / verify
# ---------------------------------------------------------------------------

def seal(key: bytes, *, component: str, step_index: int, role: str,
         residence: str, model_digest: str,
         model_digest_basis: str = "declared",
         placement_digest: Optional[str],
         prompt_binding: Mapping[str, Any],
         template_digest: Optional[str] = None,
         template_absence_reason: Optional[str] = None,
         origins: Sequence[str], reasoning: Mapping[str, Any],
         candidates: Sequence[str],
         chosen: Optional[int], outcome: str,
         sampling: Mapping[str, Any], policy_digest: str,
         fallback_depth: int, fallback_reason: Optional[str] = None,
         retained: Optional[Mapping[str, Any]] = None,
         retain_content: bool = False,
         recorded_at: Optional[str] = None) -> dict:
    """Build and sign one model-decision evidence record.

    Raises :class:`EvidenceRefused` when the members do not make a well-formed
    body, using the SAME checks :func:`verify` applies, so this function cannot
    mint a record its own verifier would reject.

    ``retained`` is refused unless ``retain_content=True`` is passed by name.
    The issue's rule is that hashes are the binding and retaining content is an
    explicit decision that carries the taint; a keyword the caller has to write
    is what makes it explicit rather than a default that a caller falls into by
    filling in a field.

    The 2.0 members follow that same rule where silence would be a hole.
    ``template_digest`` and ``template_absence_reason`` have no working
    default: a caller that names neither is refused, because "the provider did
    not say which template produced the prompt" must be spelled ``no-template``
    or ``unpublished`` rather than inferred. ``fallback_reason`` defaults to
    ``None`` because depth zero is the common, valid case, and a non-zero depth
    with no reason is refused. ``model_digest_basis`` defaults to ``declared``,
    the honest floor: a caller that recomputed the digest over the loaded bytes
    says so, and one that only holds a digest does not claim more by silence.

    ``reasoning`` (version 3.0) has no default either, and for the same reason:
    a record that omits it reads exactly like a run that had nothing to hide.
    A host that reported no span says ``none-reported``, which
    :func:`reasoning_span` builds when given neither a token count nor text.
    """
    if retained is not None and not retain_content:
        raise EvidenceRefused(
            EVIDENCE_DISCLOSURE,
            "retaining prompt or completion text needs retain_content=True by "
            "name; hashes are the binding and retention is a decision, not a "
            "field a caller drifts into")
    if retain_content and retained is None:
        raise EvidenceRefused(
            EVIDENCE_DISCLOSURE,
            "retain_content=True was asked for with nothing to retain, so the "
            "record would claim a retention decision it did not make")

    body = {
        "kind": EVIDENCE_KIND,
        "version": EVIDENCE_VERSION,
        "sign_alg": SIGN_ALG,
        "hash_alg": HASH_ALG,
        # `key_id` is a member of the body BEFORE the MAC is taken, and the MAC
        # covers every member of the body. This is the ordering the bug this
        # module cites got backwards.
        "key_id": key_id(key),
        "recorded_at": recorded_at if recorded_at is not None else _utc_now(),
        "component": component,
        "step_index": step_index,
        "role": role,
        "residence": residence,
        "model_digest": model_digest,
        "model_digest_basis": model_digest_basis,
        "placement_digest": placement_digest,
        "prompt_binding": dict(prompt_binding)
        if isinstance(prompt_binding, Mapping) else prompt_binding,
        "template_digest": template_digest,
        "template_absence_reason": template_absence_reason,
        "origins": list(origins) if isinstance(origins, (list, tuple))
        else origins,
        "reasoning": dict(reasoning) if isinstance(reasoning, Mapping)
        else reasoning,
        "candidates": list(candidates) if isinstance(candidates, (list, tuple))
        else candidates,
        "chosen": chosen,
        "outcome": outcome,
        "sampling": dict(sampling) if isinstance(sampling, Mapping)
        else sampling,
        "policy_digest": policy_digest,
        "fallback_depth": fallback_depth,
        "fallback_reason": fallback_reason,
        "retained": dict(retained) if isinstance(retained, Mapping)
        else retained,
    }
    refusal = _check_body(body)
    if refusal is not None:
        raise EvidenceRefused(refusal.link, refusal.reason)
    try:
        signature = _sign(body, key)
    except NotCanonicalizable as error:
        raise EvidenceRefused(EVIDENCE_VOCABULARY, str(error)) from None
    record = dict(body)
    record[SIGNATURE_FIELD] = signature
    return record


def verify(record: Any, key: bytes) -> Verdict:
    """Check one sealed record against ``key``. Fail-closed at every step.

    Order, and why:

    1. the record is a mapping with a ``signature`` — nothing else can be said
       about a value that is not a record;
    2. **the MAC**, recomputed over every member except ``signature``, under
       the one domain :data:`SIGN_DOMAINS` assigns to the record's version.
       The version is read here only to select from that closed map, never to
       select an algorithm. This runs before any member is interpreted, so a
       tampered record is refused as tampered rather than as malformed, and
       because the covered set is read off the record an ADDED, REMOVED or
       EDITED member all land here;
    3. the key identity: a record whose ``key_id`` is not this key's
       fingerprint is about a different signer even when its MAC is right
       (the reading `revl.cert.affirm_key_id` already ships);
    4. the body: envelope constants, required members, closed vocabularies,
       the candidate/choice coherence, and the disclosure rules.

    Step 4 is not made redundant by step 2. The MAC proves the record is the
    one its signer sealed; it says nothing about whether that signer built a
    well-formed body. A provider on another tier (item 538) holds the same key
    and writes its own records, so the narrow body is the case this verifier
    exists for, and passing it would be the fail-open shape.
    """
    if not isinstance(record, Mapping):
        return Verdict(False, EVIDENCE_INCOMPLETE,
                       f"not a record ({type(record).__name__})")
    if SIGNATURE_FIELD not in record:
        return Verdict(False, EVIDENCE_INCOMPLETE,
                       "the record carries no signature")
    claimed = record[SIGNATURE_FIELD]
    if not isinstance(claimed, str) or not _HEX64.match(claimed):
        return Verdict(False, EVIDENCE_SIGNATURE,
                       f"the signature is not an {SIGN_ALG} tag ({claimed!r})")
    try:
        expected = _sign(record, key)
    except NotCanonicalizable as error:
        return Verdict(False, EVIDENCE_VOCABULARY, str(error))
    if not hmac.compare_digest(expected, claimed):
        return Verdict(False, EVIDENCE_SIGNATURE,
                       "the signature does not cover this record: a member "
                       "was added, removed or edited after it was sealed, or "
                       "it was sealed with another key")
    named = record.get("key_id")
    if named != key_id(key):
        return Verdict(False, EVIDENCE_SIGNER,
                       f"the record names key {str(named)[:64]!r}, which is "
                       f"not the key it is being checked with (this key is "
                       f"{key_id(key)})")
    refusal = _check_body(record)
    if refusal is not None:
        return refusal
    return Verdict(True)


# ---------------------------------------------------------------------------
# what a reader does with a verified record
# ---------------------------------------------------------------------------

def crossing_key(record: Mapping[str, Any]) -> tuple:
    """``(component, step_index)`` — the key :func:`revl.wal.model_decisions`
    indexes its ``model-decision`` records by.

    The evidence object invents no second correlation. A reader holding a WAL
    and a set of evidence records joins them on this pair, which is the
    completion's own ``effect`` record identity and the one thing the writer
    and a post-mortem reader both hold."""
    return (record.get("component"), record.get("step_index"))


#: The members a re-run needs that may legitimately be `None` in a valid
#: record. :func:`reproducible` reports which of them are absent.
REPRODUCIBILITY_MEMBERS = ("model_digest", "placement_digest",
                           "sampling.seed", "sampling.temperature",
                           "sampling.top_p", "prompt_binding")


def reproducible(record: Mapping[str, Any]) -> tuple:
    """``(ok, [what is missing])`` — whether this record describes a trajectory
    that can be RE-RUN, not merely read.

    A derived predicate and deliberately not a refusal. A sampling run with no
    seed, or a decision whose prompt binding was suppressed because the input
    was confidential, is a real decision that must be recordable; what must not
    happen is a reader assuming it can re-issue one. The distinction this draws
    is the item's own: a record is not a replay, and the object says which of
    the two it is holding rather than leaving the reader to guess.

    ``placement_digest`` is in the list because two quantisation points of one
    model share a ``model_digest`` and do not share a distribution; what that
    digest is computed over belongs to item 538, and this predicate is why its
    absence is visible instead of silent."""
    missing = []
    if not record.get("model_digest"):
        missing.append("model_digest")
    if not record.get("placement_digest"):
        missing.append("placement_digest")
    sampling = record.get("sampling")
    sampling = sampling if isinstance(sampling, Mapping) else {}
    if sampling.get("seed") is None:
        missing.append("sampling.seed")
    for member in ("temperature", "top_p"):
        if sampling.get(member) is None:
            missing.append(f"sampling.{member}")
    binding = record.get("prompt_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    if binding.get("mode") != "content-addressed":
        missing.append("prompt_binding")
    return (not missing, missing)


# ---------------------------------------------------------------------------
# the placement cross-check (item 512's route table)
# ---------------------------------------------------------------------------

def placement_table(roles: Any) -> dict:
    """``{role: residence}`` from item 512's own table.

    Accepts :func:`revl.model_route.roles`' return value (``{name: Role}``)
    and a plain ``{name: residence}`` mapping, and nothing else. The second
    shape is not a convenience: a :class:`~revl.model_route.Role` is a compiler
    object and does not survive the artifact a post-mortem reader is handed, so
    an offline reader carrying the table as JSON needs a shape that does. Both
    reduce to the one relation this module checks — a declared role name and
    the one residence item 512 declared for it.
    """
    if not isinstance(roles, Mapping):
        raise EvidenceRefused(
            EVIDENCE_PLACEMENT,
            f"a placement table is a mapping of role name to residence, not "
            f"{type(roles).__name__}")
    table = {}
    for name, value in roles.items():
        residence = value if isinstance(value, str) \
            else getattr(value, "residence", None)
        if residence not in RESIDENCES:
            raise EvidenceRefused(
                EVIDENCE_PLACEMENT,
                f"the placement table gives role {name!r} the residence "
                f"{residence!r}, which is outside {list(RESIDENCES)}")
        table[str(name)] = residence
    return table


def check_placement(body: Mapping[str, Any], roles: Any) -> Optional[Verdict]:
    """Cross-check a record's declared ``role``/``residence`` against item
    512's route table, or ``None`` when they agree.

    Slice 1 bound the placement BY VALUE and asked no host, which is right —
    the record must say where the call ran without a reader having to hold the
    program. But binding a value is not checking it, and item 512 had not
    landed when that was written, so nothing compared the two. It has landed
    (``src/revl/model_route.py``), so this is that comparison.

    Two refusals, and they are different claims:

    * the record names a role the program never declared. The placement is not
      a fact about the record, it is a fact about the program, and a record
      naming ``role="gpu-box"`` for a program whose only roles are ``local``
      and ``cloud`` is describing a run of some other program.
    * the record names a declared role with the OTHER residence. This is the
      one that matters: a record claiming ``on_device`` for a role the program
      declared ``off_device`` asserts the prompt did not leave a device it did
      leave. Both words are inside :data:`RESIDENCES`, so the vocabulary check
      passes it and only the table refutes it.

    This is a check on a BODY, so :class:`CrossingSealer` runs it before
    sealing and an offline reader runs it after verifying. A record that
    contradicts the table is never minted in the first place, and one that
    arrives from elsewhere is still refused.
    """
    table = placement_table(roles)
    role = body.get("role")
    residence = body.get("residence")
    if role not in table:
        return Verdict(
            False, EVIDENCE_PLACEMENT,
            f"the record names model role {role!r}, which item 512's route "
            f"table does not declare (it declares {sorted(table)}); the "
            "placement is a fact about the program, so a role the program "
            "never declared describes a run of another program")
    declared = table[role]
    if residence != declared:
        return Verdict(
            False, EVIDENCE_PLACEMENT,
            f"the record places role {role!r} {residence!r}, and item 512's "
            f"route table declares it {declared!r}; both words are legal, so "
            "only the table refutes this, and the direction that matters is a "
            "record claiming a prompt stayed on a device it left")
    return None


# ---------------------------------------------------------------------------
# Slice 2: sealing AT THE CROSSING, for a runtime that holds no key
# ---------------------------------------------------------------------------

#: What the runtime supplies and a provider's declaration may therefore NOT.
#: ``component`` and ``step_index`` are the crossing the recorder just made and
#: ``outcome`` is what the validation seam measured; a provider that could
#: restate any of the three could seal a record about a crossing that did not
#: happen, or call an exhausted budget a validated answer. The runtime owns
#: them, the provider owns everything else.
CROSSING_OWNED = ("component", "step_index", "outcome")


@dataclass(frozen=True)
class CrossingSealer:
    """Seal one model decision at the crossing that produced it.

    ``backends/python/runtime.py`` is stdlib-only by construction: it cannot
    import this module, and a second copy of the MAC living in the backend is
    the item 272 mistake with a signing key attached. So the runtime holds a
    CALLABLE and learns nothing — exactly the shape item 250 Slice 3a already
    uses for the WAL sink, which is a callable because the runtime holds no WAL
    handle either.

    The call is total. It returns ``(record, None)`` on a seal and
    ``(None, {"link", "reason"})`` on a refusal, so the runtime never has to
    catch an exception it cannot name, and the refusal reaches the artifact as
    a stated fact rather than as a traceback.

    ``route_table`` is item 512's, and is optional here for one reason only: a
    program that declares no ``model role`` has no table to check against, and
    that program's crossings are still recordable. Supplying one engages
    :func:`check_placement`.
    """

    key: bytes
    route_table: Any = None

    def __call__(self, crossing: Any, draft: Any,
                 outcome: str) -> tuple:
        """``(sealed record, None)`` or ``(None, refusal)``. Never raises for a
        malformed draft: a refusal is the answer, not an accident."""
        if not (isinstance(crossing, (tuple, list)) and len(crossing) == 2):
            return None, {
                "link": EVIDENCE_INCOMPLETE,
                "reason": f"the crossing is not a (component, step_index) "
                          f"pair ({crossing!r}), so the record would key to "
                          f"nothing `revl.wal.model_decisions` indexes"}
        component, step_index = crossing
        if component is None or step_index is None:
            return None, {
                "link": EVIDENCE_INCOMPLETE,
                "reason": f"the crossing is ({component!r}, {step_index!r}); "
                          "an evidence object with no crossing cannot be "
                          "joined to the WAL record it is evidence for"}
        if not isinstance(draft, Mapping):
            return None, {
                "link": EVIDENCE_INCOMPLETE,
                "reason": "model-decision evidence is engaged for this run and "
                          f"the crossing ({component!r}, {step_index!r}) "
                          "published no declaration, so there is nothing to "
                          "seal. The provider declares what answered, where, "
                          "what it was given and under which rule; revl cannot "
                          "see any of it through the host body and will not "
                          "invent it"}
        restated = [m for m in CROSSING_OWNED if m in draft]
        if restated:
            return None, {
                "link": EVIDENCE_VOCABULARY,
                "reason": "the declaration restates " + ", ".join(restated)
                          + ", which the runtime owns: the crossing is the one "
                            "the recorder just made and the outcome is the one "
                            "the validation seam measured, so a provider that "
                            "could set them could seal a record about a "
                            "crossing that did not happen"}
        members = dict(draft)
        members["component"] = component
        members["step_index"] = step_index
        members["outcome"] = outcome
        if outcome != "validated":
            # The crossing owns the outcome, so it owns the coherence of the
            # choice WITH the outcome. A provider declares from inside the host
            # body, before the validation seam has decided anything, so it can
            # legitimately name the candidate the host returned and the retry
            # can then exhaust around it. `_check_candidates` already says what
            # the record means in that case — "an outcome of 'exhausted' took
            # no candidate" — so this states it rather than refusing a run for
            # a contradiction the provider could not have foreseen. Nothing is
            # lost: the candidate set is still on the record in the order it
            # was offered; what changes is the claim that one was TAKEN.
            members["chosen"] = None
        if self.route_table is not None:
            try:
                verdict = check_placement(members, self.route_table)
            except EvidenceRefused as error:
                return None, {"link": error.link, "reason": error.reason}
            if verdict is not None:
                return None, {"link": verdict.link, "reason": verdict.reason}
        try:
            record = seal(self.key, **members)
        except EvidenceRefused as error:
            return None, {"link": error.link, "reason": error.reason}
        except TypeError as error:
            return None, {
                "link": EVIDENCE_VOCABULARY,
                "reason": f"the declaration does not name the members of a "
                          f"model-decision record ({error})"}
        return record, None


# ---------------------------------------------------------------------------
# Slice 3: reading the decision back out of the artifact alone
# ---------------------------------------------------------------------------

#: Where a model-evidence key comes from when one is not passed explicitly.
#: Domain-separated from `revl.attest`'s pair for the same reason
#: :func:`key_id` is: these are two protocols that may share key material, and
#: an operator who points one at the other's key should get a key-identity
#: refusal, not a silent cross-protocol verification. A secret is NEVER
#: hardcoded here.
KEY_ENV = "REVL_MODEL_EVIDENCE_KEY"
KEY_FILE_ENV = "REVL_MODEL_EVIDENCE_KEY_FILE"


def resolve_key(key_path: Optional[str], *, env=None) -> Optional[bytes]:
    """The evidence key from, in order: an explicit path, :data:`KEY_FILE_ENV`
    (a path), :data:`KEY_ENV` (the secret bytes) — or ``None``.

    ``None`` is a legal answer here and is NOT a hole. `revl attest` raises
    without a key because signing without one is impossible; an offline reader
    with no key has a real and useful job, which is to report that a seal is
    present and was not checked. Reading nothing out of an unchecked seal is
    the fail-closed behaviour; refusing to run at all would only push the
    reader to skip the command."""
    if env is None:
        import os  # noqa: PLC0415 - lazy, so the module keeps no import effect
        env = os.environ
    from .attest import load_key  # noqa: PLC0415 - one key-file reader, not two
    if key_path:
        return load_key(key_path)
    file_env = env.get(KEY_FILE_ENV)
    if file_env:
        return load_key(file_env)
    inline = env.get(KEY_ENV)
    if inline:
        return inline.encode("utf-8")
    return None


#: The member of a `model-decision` WAL record that carries the sealed evidence
#: object, and the member that carries the refusal when sealing was engaged and
#: failed. Absent by default on both counts: a WAL written by a run that never
#: engaged evidence is byte-identical to a pre-517 one.
WAL_EVIDENCE_MEMBER = "evidence"
WAL_REFUSAL_MEMBER = "evidenceRefused"


def from_wal_record(decision: Any) -> Any:
    """The sealed evidence object carried by one ``model-decision`` WAL record,
    or ``None``.

    The join is :func:`crossing_key`'s and no second correlation is introduced:
    the evidence rides ON the record it is evidence for, so
    ``revl.wal.model_decisions`` indexes both at once and a reader holding the
    index holds the evidence."""
    if not isinstance(decision, Mapping):
        return None
    return decision.get(WAL_EVIDENCE_MEMBER)


def reconstruct(record: Any, key: bytes, *, roles: Any = None,
                at: Any = None) -> dict:
    """Rebuild one model decision FROM THE ARTIFACT ALONE — the roadmap item's
    own exit clause, and the half Slice 1 explicitly did not claim.

    Takes a sealed record (off a WAL, out of a bundle, from anywhere) and the
    key, and returns what the decision WAS: the crossing, the placement, what
    answered, what it was given, what it could have said and what it said, how
    it was asked, and under which rule. Nothing here consults a live process, a
    host, a provider or the program; the record is the whole input.

    Fail-closed, and the ordering is the point: ``decision`` is ``None``
    whenever ``verified`` is false. A reader never gets the contents of a
    record whose MAC did not check out, so an edited field does not merely
    'fail its digest' in a field somewhere — it withholds the reading. That is
    what makes the tamper test non-vacuous at this layer: flipping one byte of
    a sealed record on disk does not give a wrong answer, it gives no answer.

    ``roles`` engages the item 512 cross-check (:func:`check_placement`) after
    the MAC verifies. Offline it is optional for the same reason it is optional
    on the sealer: an offline reader may not hold the program's table, and a
    reader that does hold it gets the stronger claim.

    ``at`` is the crossing the reader FOUND this record at — for a WAL, the
    ``(component, stepIndex)`` :func:`revl.wal.model_decisions` indexed it by.
    Supplying it engages the join check: a record whose sealed body names
    another crossing is refused with :data:`EVIDENCE_CROSSING` even when its
    MAC is valid, because a genuine record of one crossing lifted onto another
    is still a false account of this one. :func:`verify` cannot make this check
    — it sees one side — and a reader that holds both and does not make it is
    the fail-open half.
    """
    verdict = verify(record, key)
    if verdict.ok and at is not None:
        found = tuple(at)
        named = crossing_key(record)
        if found != named:
            verdict = Verdict(
                False, EVIDENCE_CROSSING,
                f"the record was found at crossing {list(found)} and its "
                f"sealed body names {list(named)}; the MAC is valid, so this "
                "is a genuine record of another crossing lifted onto this one")
    if verdict.ok and roles is not None:
        try:
            placement = check_placement(record, roles)
        except EvidenceRefused as error:
            placement = Verdict(False, error.link, error.reason)
        if placement is not None:
            verdict = placement
    if not verdict.ok:
        return {"verified": False, "link": verdict.link,
                "reason": verdict.reason, "crossing": None, "decision": None,
                "reproducible": None}
    sampling = dict(record["sampling"])
    binding = dict(record["prompt_binding"])
    candidates = list(record["candidates"])
    chosen = record["chosen"]
    ok, missing = reproducible(record)
    return {
        "verified": True,
        "link": "",
        "reason": "",
        "crossing": list(crossing_key(record)),
        "decision": {
            # the envelope version the record was sealed under: a 1.0 record is
            # readable, and the members it does not carry read as None below
            "version": record["version"],
            # the crossing, which is the WAL index key
            "component": record["component"],
            "stepIndex": record["step_index"],
            # where it ran
            "role": record["role"],
            "residence": record["residence"],
            # what answered, on what host profile, and what the digest claims
            "modelDigest": record["model_digest"],
            "modelDigestBasis": record.get("model_digest_basis"),
            "placementDigest": record["placement_digest"],
            # what it was given
            "promptBindingMode": binding["mode"],
            "promptBinding": binding["value"],
            "promptSuppressionReason": binding["reason"],
            "templateDigest": record.get("template_digest"),
            "templateAbsenceReason": record.get("template_absence_reason"),
            "origins": list(record["origins"]),
            # the span the host did or did not return (3.0); None on an older
            # record, which is the honest answer rather than an empty span
            "reasoning": record.get("reasoning"),
            # what it could have said, and what it said
            "candidates": candidates,
            "chosen": chosen,
            "chosenDigest": candidates[chosen] if isinstance(chosen, int)
            and not isinstance(chosen, bool) else None,
            "outcome": record["outcome"],
            # how it was asked
            "sampling": sampling,
            # under what rule, how far down the ladder, and why it descended
            "policyDigest": record["policy_digest"],
            "fallbackDepth": record["fallback_depth"],
            "fallbackReason": record.get("fallback_reason"),
            # what it kept, and whether the ANSWER itself was kept (the one
            # thing a replay can serve without calling the model)
            "retained": record["retained"] is not None,
            "retainedCompletion": bool(
                isinstance(record["retained"], Mapping)
                and record["retained"].get("completion") is not None),
            # who sealed it, and when
            "keyId": record["key_id"],
            "recordedAt": record["recorded_at"],
        },
        "reproducible": {"ok": bool(ok), "missing": list(missing)},
    }
