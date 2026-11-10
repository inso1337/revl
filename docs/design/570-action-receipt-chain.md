# 570: a signed, chained receipt per consequential action

Roadmap: item 127's "portable receipt-chain delta" (the signed per-ACTION
receipt chain the post-v2.0 horizon review added to the attestation item),
issue #2757. This note is the design the item asks for before code, and it
records the decisions the issue leaves open: key custody, rotation and the
anchoring option.

Status: the first slice is landed with this note. `src/revl/action_receipt.py`
defines the receipt, the two receipt kinds, the commitments and the chain;
`revl receipts export` and `revl receipts verify` are the reachable verbs.
`tests/test_action_receipt_chain_2757.py` pins the three acceptance clauses.

Reconciles with: item 127 and `src/revl/attest.py` (the signed-record
construction and the key-file discipline this reuses), item 413.3 and
`docs/roadmap-archive/13-the-embeddable-compiler-revl-as-a.md` (why an UNKEYED
previous-line hash chain on the WAL was deferred, and why a signed receipt is a
different claim), item 517 and `docs/design/536-model-decision-evidence.md`
(the prompt-digest rule that a commitment over a secret is a confirmation
oracle), item 524 and `docs/design/566-pool-execution-receipts.md` (one key,
many protocols, distinct domain tags), item 516 / #2723 and
`src/revl/anchor.py` (the operator publishes a digest; revl never contacts the
external log), and `docs/design/245-session-commit.md` (what a session verdict
is).

Design-doc number: **570**. 571 to 573 were free on `main` and on every remote
branch when this note was written, checked with `git for-each-ref` rather than
against `main` alone.

---

## 0. The decision in one paragraph

For every consequential action in a session, revl issues a receipt that binds
the bundle and policy digests, the approval used, the capability crossed, a
commitment to the input and a commitment to the result, the worker identity,
the session verdict and the digest of the previous receipt. The receipts are
signed HMAC-SHA256 records under a domain distinct from every other signed
record in the tree, and the chain is exported as one signed envelope that binds
the count, the ordered digests and the head. A verifier can detect removal,
reordering and alteration, and a receipt whose value carried a `secret` or
`confidential` origin carries NO value digest and names the origin that
suppressed it. The signing key is the operator's, held outside the tree and
resolved only from an explicit path or environment variable; rotation is a new
key with a new fingerprint, and a verifier is given every key a chain names.
The chain head can be published to a log the customer controls, which is what
turns "the exporter says this is the head" into "the exporter committed to this
head before it knew the audit would look".

## 1. The gap, measured

The tree already signs one record per artifact kind. `attest` signs a gate
verdict, `deploy` MACs an admission receipt, `erasure_receipt` signs a
measurement of an erasure, `model_evidence` seals a model decision, and
`pool_receipt` signs an execution receipt. Each is a true statement about one
thing. None is a statement about a SESSION: there is no record that says
"these six actions happened, in this order, under this bundle, this policy and
this approval, and the input and result of each were these".

The obvious answer, chaining the WAL by previous-line hash, was measured and
deferred (item 413.3). That chain was UNKEYED, over byte-identical writers, and
recomputable by anyone who can rewrite the same-user file, so it could not tell
a crash-torn line from a spliced one. This chain is keyed. Rewriting a receipt
changes a MAC the rewriter cannot recompute, and rewriting the whole chain
requires the key, which is the operator's.

## 2. What one receipt binds

One record, `kind: "revl.action-receipt"`, version `1.0`. Every member of
`RECEIPT_BODY_MEMBERS` is required and inside the MAC.

| member | what it binds | why it is here |
| --- | --- | --- |
| `kind` / `version` / `hash_alg` / `sign_alg` | the envelope | validated, not merely recorded |
| `key_id` | the signer's fingerprint | the member a verifier uses to choose a key; inside the MAC |
| `session` | the session | one chain is one session |
| `seq` | the position | contiguous from zero; a hole is a removal |
| `prev` | the predecessor's receipt digest, or `null` for the first | the link |
| `receipt_kind` | `effect-crossing` or `approval-use` | which binding members are REQUIRED |
| `task` | the task/request the action belongs to | the item's "task" |
| `component` | the component that acted | the item's "component" |
| `bundle` | the composition bundle digest, or `null` | the item's "bundle hash" |
| `policy` | the policy in force, by digest, or `null` | the item's "policy hash" |
| `capability` | the capability crossed | the item's "capability" |
| `approval` | `{request_id, use}` or `null` | the item's "approval" |
| `worker` | the worker identity | the item's "worker identity" |
| `session_verdict` | `committed` or `aborted` | the item's "session verdict" |
| `input` / `input_origins` | a commitment to the input, and the origins it carried | the item's "inputs commitment" |
| `result` / `result_origins` | a commitment to the result, and its origins | the item's "result commitment" |
| `issued_at` | when the receipt was sealed | |
| `digest` | this receipt's chain-link digest | what the next receipt's `prev` names |

Two members are nullable and that is a decision: a plain run may have no bundle
and no policy. Their absence is a `null` MEMBER, never an omitted one, so a
reader can tell "there was no bundle" from "the writer dropped the field".

The member set is exact. A verifier refuses a body with a missing or an extra
member rather than reading a future field as decoration, and the MAC is
recomputed from every member present, so an added member is caught even if a
future verifier would have ignored it.

The same holds inside `approval`: an export that gives it a member beyond
`request_id` and `use` is refused, not trimmed, so nothing a caller supplied is
dropped without a word.

## 3. Why a chain, and what each tampering fails

A receipt's `digest` is SHA-256 over every member but `digest` and `signature`,
and the next receipt's `prev` is that digest. The links compose because `prev`
is itself inside the next digest.

The chain envelope adds the end: `count`, the ordered `digests` list and
`head`, all inside the envelope MAC (the envelope MAC is taken over
`CHAIN_MEMBERS` only; each receipt is signed on its own, so the envelope binds
their identities and order rather than re-signing their bodies).

| tampering | what fails |
| --- | --- |
| alter one receipt member | the receipt MAC, and its `digest` stops matching the envelope's list |
| delete a middle receipt | the envelope count/digests/head, and the next receipt's `prev` |
| swap two receipts | the envelope digests, and both `prev` links |
| truncate the tail | the envelope count/head/signature |
| reorder and re-sign the whole chain | nothing in the artifact; this is what anchoring is for (§6) |

The last row is stated rather than hidden: a holder of the key can mint any
chain. A signature scheme with a shared secret proves the key holder said it,
not that nobody else did.

## 4. The two receipt kinds

`effect-crossing` records a boundary crossing that happened. `approval-use`
records the spend of an approval and REQUIRES a non-null `approval` member.
Both carry the full body; the kind selects which members are required, never
which are allowed. A body thinned by kind would recreate, at audit time, the
"which field got dropped" question the UI receipt (`revl.ui_action`) exists to
remove.

This slice's export API takes the actions from the caller. The runtime writer
that emits a receipt at a live crossing is the next slice, and it is
deliberately not claimed here: the crossing writer lives in the py backend and
supplying it from a test would be a second, untested emission path. What is
landed is the record shape, the commitment rule and the verifier the writer
will target.

## 5. The commitments, and the confirmation-oracle rule

`input` and `result` are objects `{mode, value, reason}`:

* `salted`: `value` is SHA-256 over a domain tag, the chain's 64-hex salt, the
  commitment label (`input` or `result`) and the canonical spelling of the
  value. The salt makes two chains non-comparable and defeats precomputed
  tables. The label stops a key holder moving an input commitment into the
  result member, even by re-signing the surrounding record.
* `suppressed`: `value` is `null` and `reason` is `secret-origin` or
  `confidential-origin`.

The rule is bidirectional and enforced at verification, not only at sealing:
origins containing a disclosure class REQUIRE a suppressed commitment with the
matching reason, and origins with no disclosure class require a salted
commitment with a digest. A key holder cannot mint a salted digest over a
secret and cannot suppress an ordinary value without naming the class that
made it do so. `secret` wins over `confidential` when both are present, so the
reason is a function of the origin set rather than of their order.

THE RESIDUAL RISK, stated plainly: the salt is in the chain envelope, because a
commitment a reader cannot recompute binds nothing. So the salt does not hide a
LOW-ENTROPY value from a reader who holds the chain; it defeats precomputed
tables and cross-chain correlation, not a guessing attack. That is exactly why
a `secret` or `confidential` origin is suppressed entirely instead of salted.
For an ordinary value the remaining question is whether the value's entropy is
high enough, and the answer is the caller's: the origin declaration is the
input that says so.

## 6. Key custody

The key is the operator's and revl holds none by default. Resolution order,
identical to `revl attest` and `revl retention-receipt`:

1. an explicit `--key PATH` (repeatable on verify);
2. `REVL_RECEIPT_KEY_FILE`, a path;
3. `REVL_RECEIPT_KEY`, the secret bytes themselves.

There is no default path and no generated key inside the tree: a missing key
is an error on the export path (no document is printed) and a refusal on the
verify path (exit 1, never "valid"). Key files are read raw with a trailing
newline stripped, by `attest.load_key`, so the byte spelling has one
implementation.

Each receipt carries `key_id`, a truncated SHA-256 fingerprint from
`attest.key_id` (the same fingerprint the attestations use, so an operator has
one key ledger, not two). The fingerprint is for CHOOSING a key, never for
proving one.

The construction is HMAC-SHA256, so a holder of the key can mint any receipt
and any chain. Nothing here detects a compromised signer; the honest perimeter
is that the key is an operator secret with the same blast radius as the
attestation key. The `sign_alg` member exists so an asymmetric upgrade is an
additive change, not a reshape.

## 7. Rotation

Rotation is a new key, not a re-sign. Because every receipt names its own
`key_id`, a chain may span a rotation: receipts sealed before the rotation keep
verifying under the retired key, and receipts after it verify under the new
one. The verifier is given every key the chain names (`--key` repeats; the
module accepts a single key, an iterable or a `key_id -> key` mapping). A chain
that names a key the verifier does not have is refused as `key-unknown` and the
reason says which receipt.

The envelope is signed by the exporter's CURRENT key, because that is the key
that exported the document. Rotating the signing key does not change a receipt
already sealed; it changes which keys a future verifier must hold. An operator
who destroys the retired key loses the ability to verify the old receipts and
should keep it for the retention period the audit requires. That is a custody
policy, not a code path, and this module cannot enforce it.

## 8. Anchoring (the optional log the customer controls)

The artifact ends at `head`, the last receipt's digest, plus `count`. Both are
inside the envelope MAC. The operator may publish that pair anywhere they like:
a transparency log, a timestamping service, a ledger, a change ticket. revl
never contacts the external system, exactly as `src/revl/anchor.py` decides
for anchor records.

Anchoring is what covers the one tampering the artifact cannot: a key holder
who rewrites the whole chain and re-signs it. A verifier who has the published
head and count can tell that the presented chain is a different one. Without an
anchor, the artifact still detects every tampering by a party without the key,
which is the ordinary audit case. The exit criterion of issue #2757 asks for
anchoring as an OPTION, and this is the option: one digest and one count, no
protocol revl has to implement.

## 9. The verifier

`revl receipts export --actions FILE --session NAME [--session-verdict V]
[--key PATH] [--salt HEX] [--out FILE]` signs one receipt per action and writes
the chain. `revl receipts verify CHAIN [--key PATH ...] [--json]` checks it and
exits 0 only on a full pass, 1 on any refusal with the reason on stderr.

The checks are ordered so a caller branches on the link that fired:

1. `envelope`: the document is an object with the exact chain members, and the
   envelope constants are this build's.
2. `key-unknown`: the envelope's key, and each receipt's key, is in the
   verifier's set.
3. `chain-signature`: the envelope MAC covers `session`, `session_verdict`,
   `salt`, `count`, `head`, `digests` and `key_id`.
4. Per receipt, in order: `signature`, then `body` (exact members, closed
   vocabularies, the digest recomputation) and `commitment` (the disclosure
   rule).
5. `chain-order`: `seq` is contiguous from zero, one session, one verdict, and
   the envelope's `digests` list matches the receipts.
6. `chain-link`: each `prev` names its predecessor's digest.
7. `chain-head`: `head` is the last digest (or `null` for an empty chain).

## 10. What this slice does NOT claim

* It is not a WAL replacement. The WAL remains the runtime's recovery surface;
  this is the audit artifact exported FROM a session.
* It does not wire itself into every crossing yet. The emission point is the
  next slice (§4); what is claimed here is the record, the commitment rule, the
  chain and the verifier.
* It is not a public-key signature, a timestamp authority, or proof that a
  value was what the signer said. It proves the signer committed to that value
  under that key, which is what makes the chain auditable.
* It does not make the salt a confidentiality mechanism (§5).

## 11. Acceptance, mapped

* a design note states key custody, rotation and the anchoring option (§6, §7,
  §8);
* a three-receipt chain exports, and deleting the middle, swapping two or
  altering one field is detected, with the alteration walked over every body
  member (`tests/test_action_receipt_chain_2757.py`);
* a receipt for a call that handled a secret carries no value digest and says
  why, enforced in both directions (`commitment` and `check_commitment`).
