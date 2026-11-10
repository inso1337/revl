# `revl attest --anchor`: a chain-neutral record over existing digests

*Commit to a set of digests that revl already signs, and publish one value to
any external log, timestamping service or ledger you choose. revl does not
contact that system, and does not trust it.*

Implementation: `src/revl/anchor.py` (the record, its signing and
verification), `src/revl/cli/observe.py` (`revl attest --anchor` and
`revl verify --anchor`), `tests/test_anchor.py`. Issue #2723.

---

## 1. What it is

revl signs digests of the things an operator may want to prove later:
`attest.canonical_hash` identifies an admitted composition, `revl attest` signs
that identity into an attestation, and the peer pool's execution receipts carry
their own digest. Each record is about one artifact.

An **anchor** commits to a set of those digests together and signs the set under
the existing attestation key. The operator publishes the record's `anchor_hash`
wherever it likes: a transparency log, an RFC 3161 timestamping service, a
ledger. Nothing in revl depends on that system, and verifying an anchor needs no
network access.

```
$ revl attest --anchor service.rvl \
    --digest receipt:peer-1=6f1e... \
    --digest attestation=9d2c... \
    --key ci-signer.key --json > anchor.json

$ revl verify --anchor anchor.json --key ci-signer.key
anchor: VALID: valid: anchor is authentic and commits to the digests presented
  anchor sha256: 3b7a...
```

## 2. What an anchor proves

* **The named digests were signed together under the key.** The HMAC covers
  every member of the record, so an edited digest, name, timestamp, signer,
  algorithm label or version makes verification fail.
* **The published value commits to exactly that set.** `anchor_hash` is
  recomputed from the digests the record carries, so a key holder cannot sign a
  commitment that does not match its own contents.
* **A verifier can tie the record to its own artifacts.** With `--digest
  NAME=DIGEST`, the verifier supplies the digests it holds. The anchor may not
  omit one of them or commit to a different value, even under a genuine
  signature.

## 3. What an anchor does not prove

* **It is not an attestation.** It carries no verdict, no guarantee codes and
  no checker identity, so it says nothing about whether a composition was
  admitted or whether the code behind a digest is correct. `revl attest` is the
  verb for that claim.
* **It is not a timestamp authority.** `timestamp` is the instant the signer
  says it signed, inside the MAC so it cannot be edited afterwards. Proving
  when a value existed is the external system's job, and the record does not
  pretend otherwise.
* **It does not prove publication.** Publishing is the operator's step. An
  anchor in hand shows what was signed, not that any log accepted or retained
  it.
* **It is not proof of an honest signer.** Under the current HMAC key, every
  verifier is also a key holder, so the key proves authorship and not honesty.
  A key holder can author a false claim, which is why the verifier's expected
  digests matter. An asymmetric upgrade is possible later through the existing
  `sign_alg` member, exactly as for attestations.
* **It has no chain client, contract or key custody.** Any contract that checks
  a record is outside revl. The external system is untrusted, and revl treats
  it that way.

## 4. The record

`revl attest --anchor` emits kind `revl.anchor`, version `1.0`:

| member        | meaning                                                          |
| ------------- | ---------------------------------------------------------------- |
| `kind` / `version` | `revl.anchor` / `1.0`, the envelope identity                |
| `hash_alg`    | `sha256`                                                         |
| `digests`     | name to one lowercase sha256 hex digest; at least one entry      |
| `anchor_hash` | sha256 over the canonical spelling of `digests`                  |
| `timestamp`   | ISO-8601 UTC instant the anchor was signed                       |
| `sign_alg`    | `hmac-sha256`, checked at verify time, not only recorded         |
| `signer`      | an optional label; identity is the key, not this string         |
| `key_id`      | a non-secret fingerprint of the signing key; a verifier refuses a record naming any other key |
| `signature`   | HMAC-SHA256 over every member above                              |

A digest name is 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, `/`
or `-`, starting with a letter or digit. A digest value is exactly 64 lowercase
hex characters. Mixed-case hex is refused so that one digest has one spelling.

**The published value.** `anchor_hash` is recomputable without revl:

```
sha256(json.dumps(digests, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False).encode("utf-8"))
```

`anchor_hash` commits to the digest set only. The timestamp and signer are
covered by the signature, not by the hash, so signing the same set again at
another time gives the same published value with a different signature.

**The signature.** HMAC-SHA256 over the canonical spelling of the whole record
minus `signature`, byte strings formed the same way, prefixed with
`revl.anchor/v1` and a NUL byte. The prefix separates the anchor protocol from
the attestation, certificate, deploy receipt and pool receipt protocols that
share the same key material.

**Versioning.** The version is an envelope property. A verifier refuses a
version it does not implement instead of reading an unknown shape as version
1.0. A future record that also changes the MAC domain fails at the signature
check first; one that keeps the version 1.0 domain and changes only the version
member is refused by the envelope check. Either way it is refused, never
silently read as version 1.0.

## 5. Emitting

```
revl attest --anchor [COMPOSITION] --digest NAME=DIGEST ... --key KEY [--json]
```

* `COMPOSITION` is optional. When given, its canonical composition hash is added
  under the name `composition`; the gate is not run, because an anchor records a
  digest set and not a verdict.
* `--digest NAME=DIGEST` is repeatable and supplies every other digest.
* `--key`, `REVL_ATTEST_KEY_FILE` and `REVL_ATTEST_KEY` are the same key sources
  `revl attest` uses.
* `--json` prints the full record to ship; without it, the human form shows the
  value to publish.

## 6. Verifying

```
revl verify --anchor FILE [--key KEY] [--digest NAME=DIGEST ...] [--json]
```

`revl verify --anchor` is a check: exit `0` when the anchor is valid, `1` when
it is read and invalid, and `2` when the record or key cannot be read. It
reports five distinct failure modes: a signature mismatch, a `key_id` that
names a different key, an envelope this build does not implement (including an
unknown version), an `anchor_hash` that does not match the digests it carries,
and a mismatch against a digest supplied with `--digest`. No network access is
performed.

`revl verify FILE` without `--anchor` remains the bundle verifier
(`docs/bundle.md`).
