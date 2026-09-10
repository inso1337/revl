# `revl attest`: cryptographic attestation of verified compositions

*After the gate admits a composition, sign a portable, tamper-evident record
that **this exact** composition passed — so a downstream consumer can confirm it
without re-running revl.*

Implementation: `src/revl/attest.py` (the pure attestation model, signing,
verification, the loaders and renders), `src/revl/cert.py` (the component
certificate of section 7), `src/revl/__main__.py` (`revl attest`),
`tests/test_attest.py`. Roadmap item 127; the certificate envelope is roadmap
item 474 (issue #826) and is documented in section 7.

---

## 1. What it is

`revl audit` / the gauntlet decide *whether* a composition is admissible.
`revl attest` records *that it was*, as a signed statement other tools can trust
offline:

```
$ revl attest service.rvl --key ci-signer.key
attestation: admitted  (revl.attestation v2.0)
  composition sha256: 6f1e…（64 hex）
  guarantees: G1, G2, G3, G4, G5, G6, G7, G8, G9
  checker:    revl 2.0.0, ruleset 4ccbf1242e16…
  signed:     2026-08-25T00:00:00+00:00  (hmac-sha256, key 3a9c…)
  signer:     ci@revl
  signature:  b2d4…（64 hex）
```

The attestation binds one composition, identified by a canonical hash of its
admitted IR, to a verdict, the guarantees that verdict discharged, the identity
of the checker that reached it, a timestamp and a signer identity, under an HMAC
signature. It travels with a release, a registry entry, or an audit trail;
anyone holding the shared key can verify it.

It is **not** the gate. It records a verdict the gate reached, and it cannot be
produced without one: signing takes a `GateVerdict` from `attest.run_gate`,
which runs the reference frontend over the composition. `revl attest` takes
COMPOSITION SOURCE for that reason. A composition the frontend refuses is never
signed, and neither is a pre-compiled IR document, which carries no source to
run the gate over. A composition with open holes (a *draft*, `docs/holes.md`)
compiles but is never admitted, so it is refused too.

## 2. What is attested

The signed body carries these members:

| member             | meaning                                                            |
| ------------------ | ------------------------------------------------------------------ |
| `kind` / `version` | `revl.attestation` / `2.0`, the envelope identity                  |
| `verdict`          | `admitted`, the only verdict an attestation records                |
| `hash_alg`         | `sha256`                                                           |
| `composition_hash` | SHA-256 over the **canonical** IR, the composition's stable identity |
| `guarantees`       | the G-codes the gate verdict discharged (see below)                |
| `checker`          | `{compiler, ruleset}`, WHICH frontend reached the verdict          |
| `timestamp`        | ISO-8601 UTC instant the attestation was signed                    |
| `sign_alg`         | `hmac-sha256`, checked at verify time, not merely recorded         |
| `signer`           | an optional human/agent label (proven identity is the key, not this) |
| `key_id`           | a non-secret fingerprint of the signing key                        |
| `evidence_bindings`| OPTIONAL: facet name → sha256 over the item-293 evidence bundle, sorted so the signed bytes are a pure function of the bindings and not of insertion order. Present only when bindings were supplied; folded into the MAC and validated at verify time |
| `signature`        | HMAC-SHA256 over the canonical bytes of every member above         |

**The canonical hash.** The composition hash is
`sha256(json.dumps(ir, sort_keys=True, separators=(",", ":")))` — the same
byte-stable IR spelling `revl fmt` relies on (`formatter._canonical_ir`).
Because it is taken over the *post-lowering IR*, a composition attested from a
`.rvl` source and re-verified against its compiled IR (`revl compile -o`) hash
to the same value: source formatting does not move the hash, a semantic change
does. The IR is loaded read-only through `composition_diff.load_composition`,
which accepts a `.rvl` source, a compiled IR, or an `audit --json` document.

**The guarantees, and what they do and do not mean.** Through envelope v1 this
member was a constant: `sorted()` over `diagnostics.GUARANTEES`, written by a
signer that never compiled anything. An IR the compiler refuses by name for
violating G2 still carried a valid signature asserting G2 held. In v2 the member
is derived from a run:

- signing requires a `GateVerdict` from `attest.run_gate`, which compiles the
  composition and applies the admission gate, so a refused composition produces
  no attestation at all;
- the verdict's own composition hash must equal the hash being signed, so a real
  verdict for a different composition cannot be laundered onto this one;
- the codes are `attest.discharged_guarantees()`, the numbered G-rules the
  SHIPPED frontend ruleset modules actually cite in their refusals, intersected
  with the catalogue vocabulary. Delete the G2 check from the frontend and G2
  stops being attested.

Read the field as: "the frontend identified by `checker` ran over this exact
composition and admitted it, and that ruleset enforces these G-rules." It is not
a proof of the rules themselves, and the code scan behind
`discharged_guarantees()` is lexical: it sees that the shipped ruleset names a
rule in its refusals, not that the rule is implemented correctly.

**The checker.** `checker.compiler` is the toolchain version and
`checker.ruleset` is a SHA-256 over the frontend ruleset module bytes. Two
attestations with the same `ruleset` were signed by toolchains whose frontend is
byte-identical. It is an identity, not a quality claim: a verifier that does not
know the signer's ruleset learns which checker asserted the record, not that the
checker is any good.

## 3. Signing scheme, and where the key comes from

The signature is `HMAC-SHA256(key, SIGN_DOMAIN + canonical_bytes(body))`, where
`body` is the attestation with its `signature` member removed and `SIGN_DOMAIN`
is the constant `b"revl.attestation/v2\x00"`. Signing over the *whole* body
(not just the IR hash) means tampering with the verdict, the timestamp, the
recorded guarantees, the checker or the signer is caught exactly like a tampered
hash.

**Domain separation.** `deploy.verify_receipt` MACs an admission receipt with
the same construction over the same canonical spelling. Without a per-protocol
prefix an ACCEPT receipt verified as a valid attestation and an attestation
verified as a valid receipt, which are entirely different claims ("this was
admitted" versus "I admitted this"). The receipt MAC carries
`b"revl.deploy.receipt/v1\x00"` and neither record verifies as the other.

**Dependency-free by design.** Only `hashlib` (content hash) and `hmac` (keyed
signature) — both standard library. A symmetric, secret-keyed signature fits the
model revl needs (a signer and a verifier who share a secret, e.g. a CI system
and the registry it publishes to) and pulls in no crypto dependency.

**The key** is resolved, never hardcoded, in this order:

1. `--key PATH` — a key file (a trailing newline is stripped);
2. `REVL_ATTEST_KEY_FILE` — a path to a key file;
3. `REVL_ATTEST_KEY` — the secret bytes directly.

A missing key is an error, so no secret is ever assumed or committed. The
attestation records only a non-secret `key_id` (a truncated SHA-256 of the key),
so a verifier can tell *which* key it needs without the key being present.

*Future work — asymmetric signatures.* An Ed25519 upgrade would let untrusted
parties verify with only a public key. The envelope already carries a `sign_alg`
member for that migration; today its only accepted value is `hmac-sha256`, and
verification refuses any other value rather than MAC-ing anyway. That check is
load-bearing: `deploy.admit` decides the cross-trust-domain question, and a
record relabelled `ed25519` used to slip past it while still being verified with
the symmetric key. This is noted, not stubbed: no unpinned crypto dependency is
pulled in speculatively.

## 4. Verifying

```
$ revl attest att.json --verify --against service.rvl --key ci-signer.key
attestation: VALID — valid: attestation is authentic and the composition matches
  composition sha256: 6f1e…
```

`--verify` is a **check**: it exits `0` when valid and **nonzero** when invalid.
Three failure modes, reported distinctly:

- **signature mismatch**, the HMAC does not match. The key is wrong, or a member
  of the attestation was altered after signing, or the record belongs to another
  protocol domain (a deploy receipt). Checked first: it proves the record is
  authentic before its contents are read. A `signature` that is not ASCII is
  refused with its own reason rather than compared: the MAC is hex, so it could
  never have matched, and `hmac.compare_digest` raises on such an input.
- **envelope refused**, the record is authentic but is not an attestation of the
  shape this build accepts: a mislabelled `sign_alg`, a verdict other than
  `admitted`, a `version` other than `2.0`, a guarantee list naming codes the
  catalogue does not define, or a missing `checker`. Authenticity is not
  authority. Under a symmetric algorithm every verifier is already a key holder,
  so a well-signed record can say anything, and what it says is checked too.
- **hash mismatch** *(only with `--against`)*, the attestation is authentic and
  well formed, but the composition presented now hashes differently from the one
  it was signed for. The composition changed.

**Compatibility.** v1 attestations do not verify against this build. The signed
body changed shape, the MAC changed domain, and the guarantee list changed
meaning, so a v1 record is a different claim rather than a v2 record missing a
field. Re-attest with the current toolchain. `revl bundle`, `revl publish` and
`revl deploy` regenerate theirs on the next run.

Omit `--against` to check only the signature over the attestation's embedded
hash (does this record verify under my key?); pass `--against COMPOSITION` to
also confirm a specific composition is the attested one.

Signing (`revl attest COMPOSITION`) exits `0` on success.

## 5. `--json`

Both directions mirror `revl diff` / `metrics` / `profile`. `revl attest
COMPOSITION --json` prints the attestation document itself (write it to a file
and ship it). `revl attest ATT --verify --json` prints the verdict:

```json
{
  "valid": false,
  "reason": "hash mismatch: the composition changed since it was attested …",
  "composition_hash": "6f1e…",
  "checked_composition": true
}
```

## 6. Public API

`src/revl/attest.py`:

- `run_gate(paths=None, *, source=None, filename=..., manifest=None,
  modules=None, normalize=None) -> GateVerdict` runs the reference frontend and
  reports what it decided. Never raises for a refusal: a refused composition comes back as
  `admitted=False` carrying the frontend's own `error`.
- `make_attestation(ir, key, *, verdict, now=None, signer=None,
  evidence_bindings=None) -> dict`, pure and deterministic given `now`; raises without an admitted `GateVerdict` whose
  hash matches `ir`, on a draft (open holes), or on an empty key.
- `verify_attestation(att, key, ir=None) -> (ok, reason)`.
- `canonical_hash(ir) -> str`, the IR content hash.
- `discharged_guarantees()` / `catalogued_guarantees()` / `ruleset_digest()` /
  `checker_identity()`, the guarantee derivation and the checker identity.
- `resolve_key(path, *, env=None)` / `load_key(path)` / `load_attestation(path)`
  — the IO helpers.

`src/revl/cert.py` (the component certificate of section 7):

- `formal_state(formal=None) -> dict`, the recorded proof state read out of the
  formal package: the per-guarantee rows, the requirements, the caveats, the
  proof model and the artifact digests.
- `make_certificate(ir, key, *, verdict, source_path, formal=None, now=None,
  signer=None) -> dict`, the signed envelope; raises without an admitted
  `GateVerdict` whose hash matches `ir`, without the source file, or when the
  formal package does not back a status it would have to state.
- `verify_certificate(cert, key, *, against=None, formal=None) -> (ok, reason)`,
  which re-derives the evidence and never raises.
- `load_certificate(path)` / `render_certificate(cert)` /
  `render_verify(ok, reason, cert)`, the IO helper and the two renders.
- `guarantee_map(text, *, source)` / `status_of(cell, *, source, code)` /
  `registry(text, *, source)`, the readers that turn `formal/STATUS.md` and
  `formal/scripts/nonvacuity.tsv` into the coverage table.

## 7. Component certificates

Roadmap item 474, issue #826. An attestation (sections 1 to 5) says that a
composition was admitted. A *component certificate* says that **and** what the
composition's guarantees rest on, because "the gate admitted it" and "this is
how far the proof goes" are two different claims and a record that carries only
the first reads as a green check.

```
$ revl attest service.rvl --certificate --key ci-signer.key
certificate: admitted  (revl.component-certificate v1.0)
  subject:   service.rvl  (source 6edb64a9969e, ir c38dc4f80f98)
  proof:     leanprover/lean4:v4.33.1  (as of 267862fb455a)
  checker:   revl 2.0.0, ruleset 3e202568c8a0
  signed:    2026-09-10T04:17:27+00:00  (hmac-sha256, key a03904d368b21d03)
  evidence:  26 requirements over 9 guarantees
  coverage:
    G1 partial  partial
    G2 proved   full
    ...
    G9 partial  rule proved; **coverage unproved and unstatable**
  caveats:
    G1: partial, `declared_only_access` is real and witnessed, ...
    G9: partial, `Flow` starts from a path that is *given*. ...
  signature: 60f2ba1f7a8240ae0431a04b1a96ab07f3a23d1339ad1a361b9151d9d52ddd3c
```

`revl attest CERT --verify-certificate [--against COMPOSITION]` checks one, and
like `--verify` it is a **check**: exit `0` when valid, nonzero when not.

### What a certificate carries

| member          | meaning                                                          |
| --------------- | ---------------------------------------------------------------- |
| `kind` / `version` | `revl.component-certificate` / `1.0`, the envelope identity   |
| `subject`       | the file, its source sha256 and its canonical IR hash            |
| `proof_model`   | the pinned Lean toolchain, a digest of the lake manifest and the dependency pins that manifest names, all three re-derived from the package at verify time, so "checked against v4.33.1" is a statement a reader can check rather than take on trust |
| `statuses`      | one row per catalogued guarantee: the status this build reads out of `formal/STATUS.md`, the map's own status cell verbatim and the map's own gap cell verbatim |
| `requirements`  | what each status rests on: the guarantee rows of the non-vacuity registry, the registry itself, the map, the axioms gate and its axiom policy, the model pin, the oracle census, the injection table and the mutation sweep |
| `caveats`       | the qualifications the artifacts record, quoted rather than summarised |
| `artifacts`     | one entry per formal artifact with its sha256, so a verifier on another machine can say *which* document moved |
| `as_of_commit`, `checker`, `timestamp`, `sign_alg`, `signer`, `key_id`, `signature` | the attestation members, unchanged in meaning in the envelope, checked as follows at verify time: `checker` against the identity this build resolves, `key_id` against the key the record is checked with, `as_of_commit` against this repository's history, and `timestamp` / `signer` recorded rather than re-derived (below) |

The status member is made of the document's prose, not of a constant in this
repository. A row whose status cell this build cannot place (`rule proved;
coverage unproved and unstatable` is placed as `partial`, with the cell carried
beside it) is a refusal rather than a rounding, and a `partial` row whose gap
cell is empty is a refusal too, so a certificate cannot report a weaker
guarantee with no reason attached.

### The trust boundary

A certificate is an attestation, and an attestation is only as good as what it
is a statement *about*. The three statements below are the boundary, and a
reader who takes the coverage table for more than them has misread it.

**The key proves authorship, not honesty.** The HMAC shows that whoever holds
the shared secret produced this record. Under a symmetric algorithm every
verifier is already a key holder, so a signer can sign any claim it likes. The
signature is checked first, and then what the record *says* is checked, because
authenticity is not authority. This is the same boundary as section 3, carried
into a wider envelope.

**The evidence is the formal artifacts, not the certificate.** At verify time
`revl attest CERT --verify-certificate` re-derives the evidence from the
artifacts on the verifying machine, `formal/STATUS.md`,
`formal/scripts/nonvacuity.tsv` and `formal/scripts/run_gate.sh`, and compares
it with what was signed. It never reads a status *out of* the document it is
checking, and it never reads a status out of the key. Re-derived member by
member, and compared:

- every artifact digest, so the verifier says *which* document moved;
- every member of every status row: the status, the map's own status cell it was
  read out of, that cell's name, the gap cell, the theorem count, the oracle cell,
  the registered theorem names and the contentless findings;
- every caveat, quoted from the artifacts;
- every requirement, both the `detail` and the `check` recorded behind it;
- the whole proof model: the pinned toolchain, the manifest digest and the
  dependency pins the manifest names;
- the checker identity, `checker.compiler` and `checker.ruleset`, against
  `attest.checker_identity()`;
- the subject's source digest, when the file the `subject` names is readable
  here;
- the commit the record names, as a revision of this repository's history;
- `key_id`, against the key the record is being checked with.

A certificate therefore cannot assert coverage the tree does not have, cannot
drop a caveat the tree records, and cannot restate a gap, a theorem list, a
requirement's reason, a toolchain pin or a dependency pin the tree no longer
supports: each of those is an evidence mismatch when it does not match.
Verification fails closed
on an unknown guarantee name, a missing or duplicated status row, a caveat the
artifacts still record, a requirement the artifacts no longer support, an
unknown requirement kind, a malformed envelope, a signed member dropped from the
record, a subject hash that is not a
digest, a signature that is not ASCII, and, with `--against`, a composition
whose hash differs from the `subject` hash. Each refusal names which of the key,
the envelope and the evidence failed.

Two signed members are **not** re-derived: `timestamp` and `signer`. They are
statements about the signing *event* rather than about the tree, so the
verifying machine has nothing to re-derive them from. They stay inside the MAC,
so neither can be edited after signing, and no successful verification can be
read as a claim about them: the success reason always ends `recorded rather than
re-derived here: timestamp, signer`. The same tail names any member a particular
run could not reach, such as the subject's source file when the machine
verifying does not hold it.

**The secret never appears in `argv`.** `--key PATH` names a key *file*;
`REVL_ATTEST_KEY_FILE` names one too, and `REVL_ATTEST_KEY` carries the bytes in
the environment of the process rather than on the command line. There is no
default key, so a certificate that anyone who can type the command could sign
does not exist. A process listing of a signing run shows the path and not the
secret.

What the boundary does **not** cover, stated rather than left to be inferred:
the verifier trusts the `formal/` artifacts it reads at verify time. It checks
that they are the revision this certificate was signed over, by digest, so a
reader who trusts the package on their disk and the recorded commit learns that
the certificate was signed against *that* revision. If the package on disk is
not the one that was signed over, verification fails; the failure says which
artifact moved. It does not establish that the Lean development in that
revision has no mistakes, and it does not establish that the prose in
`formal/STATUS.md` is an accurate description of the development. It records
what the status document says, honestly, including the rows that say the
coverage is not there.

Two signed members are outside that re-derivation by construction, and a reader
who needs an argument about them needs an argument this document does not make:
`timestamp` is whatever the signing process wrote, and `signer` is a free-text
label with no proven identity behind it (the key is the identity). Neither is
checked against anything, because there is nothing on the verifying machine to
check them against; both are inside MAC, so both are at least un-editable after
signing, and both are named on every successful verification so that a green
result is not read as a claim about them. The `subject.filename` member is
re-derived only as far as its *content*: the verifier hashes the file the member
names and compares that digest, so a record pointing at another file with other
bytes is refused, while a copy of the same bytes under a different path verifies.
`as_of_commit` is checked as a revision of this repository's history rather than
as an equality with `HEAD`, so a checkout that has legitimately moved forward
still verifies while a commit this history does not contain is refused. The
member is signed `null` when the package is not a git work tree, and then there
is no revision to check; a record that does not carry the member at all is
refused as a missing required member, so "there is no commit to check" is always
the record's own statement rather than something a reader has to infer from
silence.

`--formal DIR` names the formal package to read instead of the one found from
the working directory, `$REVL_FORMAL_DIR`, or the ancestors of the installed
module. A named directory that holds no `STATUS.md` is refused rather than
silently falling back to a package found elsewhere, because a certificate over
the wrong package would be a coverage claim about a document the caller did not
name. The same flag is available in both modes, so a verifier can check a
certificate against a package it checked out itself.

**Domain separation.** The certificate MAC carries
`b"revl.component-certificate/v1\x00"` under the same key as the attestation
MAC, which carries `b"revl.attestation/v2\x00"`. Neither document verifies as
the other, which is what keeps "this was admitted" and "this is how far the
proof goes" from being interchangeable.

**What this does not replace.** `revl attest` without `--certificate` is
unchanged, byte for byte, and a certificate is not an attestation with extra
fields: the envelope is a different shape and its own `--verify` refuses it.
The certificate is also not the gate. It is built from a `GateVerdict` the same
way, so a composition the frontend refuses is never certified, and a
pre-compiled IR document has no source to run the gate over and is refused too.
