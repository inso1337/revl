# Design: a peer's identity is a key pair (issue #1278)

Status: landed. `src/revl/peer_identity.py` holds the identity, the directory
and the key lifecycle; `src/revl/peer_pool.py` admits on it; `revl pool keygen |
register | rotate | revoke-key` operate it, and `revl pool status` reports which
members are on which backing.

Read first: `docs/design/550-private-peer-pool.md` (the pool this changes, and
the limit it declared), `src/revl/tee_quote.py` (the ECDSA this uses),
`tests/test_ecdsa_differential.py` (why that ECDSA is trusted), and
`src/revl/peer_offer.py` (the offer half).

## The gap, stated as the thing an attacker does

PR #1277 landed item 524's join slice with HMAC-SHA256 under a key the operator
and the peer both hold. That is authentication. Three consequences follow from
the verifier holding the signer's key, and all three are the same fact:

1. An operator whose key store is read forges a join for every peer in it. The
   forged join is not detectably different from a real one, because it is
   produced the same way from the same material.
2. No peer can prove to a third party that it did or did not sign something. If
   two operators disagree about whether a peer joined, there is no record that
   settles it.
3. Revoking a compromised peer's key destroys the ability to check the peer's
   past acts, because the key that verified them is the key being destroyed.

Point 3 is the one that decides the shape below. A peer pool without revocation
cannot withdraw a compromised peer, and a shared-key pool cannot revoke without
also blinding itself.

The primitive to fix this was already in the tree and better tested than most of
it. `src/revl/tee_quote.py` carries deterministic ECDSA over P-256 and P-384
with three test files behind it, one of which generates keys and messages and
requires OpenSSL to agree in both directions including about mutated signatures.
So this is a wiring item. No curve, no hash and no signature encoding is written
here, which is item 272's rule: that item exists because three components
hand-rolled the same crypto in one wave.

## Question 1: exposure. Nothing is published to revl programs, and why

`stdlib/crypto.rvl` publishes `sha256`, `hmac_sha256`, `ct_equal` as `pure` and
`random_token` as an `emission`. This change publishes nothing. Three reasons,
in the order they decide it.

**Signing is not a shape a pool peer may hold.** Signing needs the private
scalar in the program's value domain. The pool's own ladder already refuses to
send `EffectClass.SECRET_BEARING` work to a pool peer at any tier, with the
stated reason that a secret-bearing effect needs a trusted, local or
hardware-attested host and a pool peer is a machine the operator does not trust.
Publishing `sign` would put the peer's identity inside the thing the peer runs,
which is the same class of secret arriving by a different door. If an extern
signer is ever wanted it is an `emission` at best, never `pure`, because it
reads a secret the program did not compute.

**Verification alone is plausibly `pure`, and still costs two implementations.**
A verifier is a total function of (public key, message, signature) with no
entropy and no effect, so the classification would be `pure` and that part is
easy. The cost is not the classification. `stdlib/crypto.rvl`'s tier bar is
`@py` and `@ts`, and the `@ts` body is a self-contained pure-JS implementation
with no host import, exactly as its SHA-256 body is. That means a second
implementation of P-256 written by hand, in a language where big-integer modular
arithmetic is not free, sitting beside the one implementation that OpenSSL is
differentially tested against. Item 272 is about three implementations of one
primitive. Adding a fourth to close it would be a joke at the item's expense.

**Nothing in the pool needs it.** Admitting a peer is a decision the `revl pool`
HOST takes about a deployment. There is no revl program in the loop: the gate
reads a charter, a join and a directory and returns a receipt. A published
primitive would be answering a question nobody asked.

The decision is held by a test rather than by this paragraph.
`tests/test_peer_pool_identity.py::test_the_pool_publishes_no_asymmetric_primitive_to_revl_programs`
asserts `stdlib/crypto.rvl` publishes exactly the four symmetric primitives, so
a later change that publishes a signer or a verifier fails and has to be argued
rather than appearing.

## Question 2: the key lifecycle

An identity that cannot be rotated or revoked is worse than a shared key,
because it lasts longer. PR #1239 declared this same gap for its own signing and
exercised none of it, which is why each of the four ends below has an operator
verb and a test rather than a sentence.

### Generation

`peer_identity.generate_identity` draws `secrets.randbelow(n - 1) + 1`, uniform
over the whole valid scalar range with no modular bias. It runs on the machine
that will hold the key: `revl pool keygen` is a PEER verb and touches no pool
directory. `write_private_identity` is the only function in the tree that puts a
private scalar on disk and it writes 0600. `PeerIdentity.__repr__` redacts the
scalar, because a key that reaches a traceback or a log line is as compromised
as one that reached anything else.

`identity_from_seed` is deterministic and exists for fixtures. Its docstring is
the whole warning: the scalar is a hash of the seed, so a seed in a repository
is a published private key.

### Distribution

The peer hands the operator the public half and tells it the fingerprint over a
second channel. `revl pool register` pins it. That verb is the only way a key
enters the directory, and the point of that is negative: a join request cannot
introduce the key it will be checked under. `verify_record` is handed the pinned
key and COMPARES the record's own `public_key` member against it; it never reads
the record's key as the key to verify with. A verifier that did would accept
every record an attacker signed with an attacker's key, which is a real and
common failure, so the ordering is structural rather than advisory.

Registering over an existing active key raises. Replacing one is `rotate`, which
records what it replaced, because a silent overwrite makes a rotation
indistinguishable from a first registration in the ledger and that difference is
the audit trail.

### Rotation

`revl pool rotate` appends the new key as active and marks the old one
`superseded` with `not_after` set to that instant. The old key stays in the
directory forever. A rotated key is assumed uncompromised, so its past acts stay
attributable with no caveat; what it loses is the ability to authorise anything
from the rotation onward.

### Revocation

`revl pool revoke-key` marks a key `revoked`. `revl pool withdraw` does it too,
for the withdrawn member's key, and the withdrawal receipt names the key it
revoked.

A revoked key still verifies. That is not a gap in the revocation, it is what
revocation means here: the signature is a fact about bytes that were produced,
and no later event un-produces them. Pretending otherwise would be the
single-boolean answer item 546 rules out.

So the directory's reading function never returns a boolean. It returns an
`Attribution` with two members:

| member | question | changes over time |
|---|---|---|
| `verified` | were these bytes signed by this key? | no. Once true, always true |
| `status` | may this key act now? | yes. `active`, `superseded`, `revoked`, `unknown` |

and `confers_authority` is the conjunction, spelled out so nobody has to
remember which of the two a bare boolean meant. `verified=True,
status="revoked"` is a past act that really happened and authorises nothing.

This is item 546's restore-versus-compensate distinction applied to identity.
Authority has an inverse and revocation is it: after revoking, the key's
authority is exactly the authority of a key that was never registered. A
signature having been made has no inverse. The two must not be reported by one
word.

Two consequences worth naming because they are easy to get backwards:

* A peer whose key is revoked is still an ASYMMETRIC peer.
  `IdentityDirectory.has_identity` asks whether the peer has a key in any state,
  not whether it has an active one. Otherwise revoking a key would move the peer
  back onto the shared-key path, which is the downgrade revocation exists to
  prevent.
* A revoked key is not re-registered. Re-admission needs a fresh identity, and a
  re-admitted identity restarts at the entry tier with evidence zero, which is
  what item 546 already made the pool do with evidence.

## Question 3: what the signature binds, member by member

The covered set is derived from the record: every member except `signature`.
There is no field list to fall out of date with the body, which is item 517's
discipline and the defence against the failure it was written after. The message
is `domain ++ canonical_json(covered)`.

| member | what it binds | what an attacker gains without it |
|---|---|---|
| `kind`, `version` | which protocol and which envelope version | a v1 record replayed as a future v2 one, read under different rules |
| `sign_alg` | which verifier checks this record | an algorithm-confusion step: present the record to the weaker verifier |
| `pool_id` | which pool | a join to pool A accepted at pool B |
| `charter_digest` | the exact TERMS agreed to, by hash | an operator widens the ceiling, re-signs under the same name, and every old join admits under the new terms |
| `peer_id` | who is claiming | a join relabelled onto another peer's name |
| `offer` (whole sub-record) | the advertised ceiling and attestation the grant is diffed against | swap the offer after signing and be handed more than the peer said it would accept |
| `artifact_digest` | which candidate runs | admitted for artifact A, runs artifact B |
| `nonce` | used once, against the roster's spent set | replay a captured join as a second admission |
| `issued_at` | freshness, against the charter window | replay a captured join forever |
| `key_id` | which of the peer's own keys signed | present a record under a rotated key and have it read as current |
| `public_key` | the key the record claims | substitute the attacker's public key and have the verifier follow it |

Three structural notes on that table.

**Reordering is not an attack and must not read as one.** Canonical JSON sorts
members, so the covered bytes are a function of content and not of layout. A
verifier that depended on member order would refuse an honest record that passed
through a JSON library, which is a false reject and the worse failure.
`test_reordering_members_changes_nothing_because_canonical_json_sorts` holds it.

**Truncation is a signature failure, not a default.** Removing any member
changes the canonical bytes. There is a test per member of the join asserting
that removing it breaks the signature, because "the verifier ignored a member
that was missing" is how covered-set bugs actually present.

**`public_key` being covered is necessary and not sufficient.** A record
carrying the attacker's public key and a matching signature is internally
consistent. What defeats it is that the verifier uses the PINNED key and refuses
a record whose embedded key differs. Both halves are needed and both are tested:
the substitution is caught as a signature failure by OpenSSL as well, because
the substituted member is inside the message.

## The gate, and what did not change

Everything PR #1277 designed against survives, because each of those properties
was a separate decision:

* the charter is still pinned by DIGEST, the join is still fresh and used once,
  and the signature now covers all three members that carry those properties;
* the admit-authority check still runs BEFORE the join is parsed and its refusal
  text is still independent of the join's contents, so an operator with no admit
  authority cannot use the gate as an oracle. A test asserts the refusal for a
  valid join and for garbage are byte-identical;
* the authority view still splits three ways;
* `Withdrawal` still returns three disjoint sets and never a boolean.
  `retained` grew two members: whether the member's past signatures are still
  verifiable, and the key they are verifiable under;
* no new guarantee code. Four new lowercase links, none in
  `revl.diagnostics.GUARANTEES`.

### Four new links, and why the two obvious ones are two

| link | when |
|---|---|
| `identity-mode` | the record's `sign_alg` is unreadable, or names a backing this charter does not admit |
| `identity-downgrade` | a peer with a pinned public key presented a shared-key join, or a join and the offer inside it are backed differently |
| `unknown-key` | the record names a key the directory does not pin for that peer |
| `revoked-key` | the signature VERIFIES and the key is revoked or superseded |

`unknown-key` and `revoked-key` are separate from `join-signature` on purpose. A
forgery and a real act by a key that no longer acts are different findings, and
an operator reading a refusal has to be able to tell them apart. The
`revoked-key` refusal says outright that the peer did sign the record.

### There is no fallback, in either direction

`sign_alg` SELECTS one verifier and that verifier's failure is the answer. The
gate never answers a failed asymmetric check by trying the shared key it may
also hold. That is the fail-closed direction and the absence of a fallback is
the whole point: a downgrade an attacker can trigger is not a migration path.

Per-peer, the rule is stronger. A peer the directory holds ANY key for, in any
state, must present an asymmetric join. An attacker who learns a legacy shared
secret cannot step that peer back onto it.

Per-record, the rule covers both halves. A join and the offer it carries must be
backed the same way and signed by the same key, because the offer is the record
carrying the advertised ceiling the grant is diffed against. A strong join
around a weak offer is half a migration reading as a whole one.

## Migration, and making the mixture visible

A charter declares `identity_mode`:

* `asymmetric`: key pairs only. `revl pool init` writes this.
* `shared-key`: item 524's original backing.
* `mixed`: both, for the window where some peers have re-keyed and some have
  not. The `PoolCharter` default, so a charter written by item 524's code keeps
  working.

`identity_mode` is inside the charter body, so it is covered by the charter
signature. An operator who could flip it without re-signing could downgrade a
whole pool with one edit.

`revl pool status` reports the split per MEMBER, not per pool:

```
  identity  mode=mixed asymmetric=4 shared-key=1
    WEAKEST LINK: peer-5 still join under a shared key, so their joins are
    forgeable by any holder of that secret
```

A single word for the whole pool would hide exactly the member an attacker would
go for. `identity_census` is the same data as JSON for `pool status --json`.

## What non-repudiation here rests on, stated

A verified signature proves that the holder of the private half of a named
public key produced these exact bytes. That is non-repudiation under ONE
assumption, and the assumption is a deployment property, not a cryptographic
one:

> the private half was generated on the peer's own machine, never left it, and
> the public half reached the verifier over a channel the verifier trusts.

An operator that generates a peer's key pair and hands it over can forge that
peer's signatures exactly as it could forge its MAC. Elliptic curves do not
help. `pool keygen` is a peer verb, `write_private_identity` is the only writer
of a private scalar and writes 0600, and nothing in `peer_identity` transmits,
escrows or derives one from operator-supplied material, so the honest deployment
is the default one. The assumption is still an assumption and this section is it
being stated rather than implied by the word "signed".

## What was verified, and what was not

Verified by running it:

* the whole pool suite and the identity suite, 170 tests, green;
* the cross-implementation differential, run against `cryptography` 50.0.1 in
  both directions over three domains: every signature this layer produces
  verifies under OpenSSL over the message `signed_bytes` names; every signature
  OpenSSL produces over that message verifies here; and the two agree about 13
  mutation shapes per round, at both the scalar level and the record level. The
  file has no `importorskip` and a guard test asserts it cannot grow one;
* non-vacuity as a count rather than a claim, below.

NOT verified, stated rather than left to be discovered:

* **The curve arithmetic is trusted, not re-derived.** `ecdsa_sign` and
  `ecdsa_verify` are taken as correct on the strength of
  `tests/test_ecdsa_vectors.py`, `tests/test_ecdsa_adversarial.py` and
  `tests/test_ecdsa_differential.py`. This item added no coverage there and
  claims none.
* **No side-channel claim.** The P-256 scalar multiplication in `tee_quote` is a
  straightforward double-and-add over Python integers and is not constant time.
  For VERIFICATION that is irrelevant, because the inputs are public. For
  SIGNING on a host an attacker can measure, it is a real limitation and this
  item does not address it.
* **The charter and the admit receipt are still MAC'd.** Both are the operator's
  own records, read by parties that already hold the operator key, and neither
  is a peer's claim about itself. Moving them to key pairs is a separate change
  with its own migration.
* **Key distribution is out of band and unmeasured.** The tree has no channel
  for handing a fingerprint to an operator and no test can supply one. The
  `keygen` output tells the operator to check the fingerprint over a second
  channel, and whether anyone does is a deployment fact.
* **CI being green is not the cryptography being correct.** It is the tests
  passing. The differential is the strongest statement available here and it
  covers agreement with one other implementation on the inputs it drew.

## Non-vacuity, as numbers

The baseline is not "no gate". It is item 524's gate, which refuses fourteen
shaped attacks. What it cannot refuse is the operator.

Eight peers that never asked to join, three forgery shapes each, all built from
material the pool hands the operator:

| shape | shared-key pool | asymmetric pool |
|---|---|---|
| forge with the peer's shared secret | ADMIT | REFUSE |
| sign with a key pair the operator generated | ADMIT (as the MAC forgery) | REFUSE |
| claim the peer's real public key, sign with the operator's scalar | ADMIT (as the MAC forgery) | REFUSE |

**24 admitted before, 24 refused after.** The control is the same eight peers
joining honestly: **8 admitted in the shared-key pool and 8 in the asymmetric
one**, so this is not a gate that refuses everything.
`test_non_vacuity_counts` asserts those three numbers rather than describing
them.

## Honest limits

* The directory is a JSON file with no concurrency control, the same limit
  `550` states for the roster. Two operators registering at once on a shared
  directory would race.
* A revoked key stops a peer joining. It does not reach out and stop work the
  peer is already running; that is the dispatcher's problem and
  `Roster.outstanding` is where it plugs in, unchanged from `550`.
* There is no revocation DISTRIBUTION. The directory is one operator's file. A
  second operator holding the same public keys learns about a revocation when
  somebody tells it. A shared or signed revocation list is not designed here.
* `identity_mode` defaults to `mixed` on the `PoolCharter` dataclass so item
  524's code keeps working, while `revl pool init` writes `asymmetric`. A pool
  constructed in Python without naming a mode is therefore the permissive one.
  That is a deliberate compatibility choice and it is the one thing in this
  design that fails open rather than closed.
* `promote` is not signed asymmetrically. It is an operator act gated on the
  charter's attest authority, and the evidence it counts is still supplied by
  the caller, which is item 3 of `550`'s remaining work and unchanged here.
