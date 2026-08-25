# `revl attest`: cryptographic attestation of verified compositions

*After the gate admits a composition, sign a portable, tamper-evident record
that **this exact** composition passed — so a downstream consumer can confirm it
without re-running revl.*

Implementation: `src/revl/attest.py` (the pure attestation model, signing,
verification, the loaders and renders), `src/revl/__main__.py` (`revl attest`),
`tests/test_attest.py`. Roadmap item 127.

---

## 1. What it is

`revl audit` / the gauntlet decide *whether* a composition is admissible.
`revl attest` records *that it was*, as a signed statement other tools can trust
offline:

```
$ revl attest service.rvl --key ci-signer.key
attestation: admitted  (revl.attestation v1.0)
  composition sha256: 6f1e…（64 hex）
  guarantees: G1, G2, G3, G4, G5, G6, G7, G8
  signed:     2026-08-25T00:00:00+00:00  (hmac-sha256, key 3a9c…)
  signer:     ci@revl
  signature:  b2d4…（64 hex）
```

The attestation binds one composition — identified by a canonical hash of its
admitted IR — to a verdict, the guarantees that verdict proves, a timestamp, and
a signer identity, under an HMAC signature. It travels with a release, a
registry entry, or an audit trail; anyone holding the shared key can verify it.

It is **not** the gate. It attests a verdict the gate already reached — a
composition with open holes (a *draft*, `docs/holes.md`) compiles but is never
admitted, so `revl attest` refuses to sign one.

## 2. What is attested

The signed body carries exactly these members:

| member             | meaning                                                            |
| ------------------ | ------------------------------------------------------------------ |
| `kind` / `version` | `revl.attestation` / `1.0` — the envelope identity                 |
| `verdict`          | `admitted` — the only verdict an attestation records               |
| `hash_alg`         | `sha256`                                                           |
| `composition_hash` | SHA-256 over the **canonical** IR — the composition's stable identity |
| `guarantees`       | the G-codes an `admitted` verdict proves: `G1`…`G8` (DESIGN §4)     |
| `timestamp`        | ISO-8601 UTC instant the attestation was signed                    |
| `sign_alg`         | `hmac-sha256`                                                     |
| `signer`           | an optional human/agent label (proven identity is the key, not this) |
| `key_id`           | a non-secret fingerprint of the signing key                        |
| `signature`        | HMAC-SHA256 over the canonical bytes of every member above         |

**The canonical hash.** The composition hash is
`sha256(json.dumps(ir, sort_keys=True, separators=(",", ":")))` — the same
byte-stable IR spelling `revl fmt` relies on (`formatter._canonical_ir`).
Because it is taken over the *post-lowering IR*, a composition attested from a
`.rvl` source and re-verified against its compiled IR (`revl compile -o`) hash
to the same value: source formatting does not move the hash, a semantic change
does. The IR is loaded read-only through `composition_diff.load_composition`,
which accepts a `.rvl` source, a compiled IR, or an `audit --json` document.

**The guarantees.** `admitted` proves the composition-level G-rules G1–G8 (a
component reads only what it requires, one provider per key per realm, acyclic
dependencies, reversible mutations, teardown registers no effects, purity
outside effect forms, LIFO teardown, an enumerable boundary). The list is drawn
live from `diagnostics.GUARANTEES`, so it cannot drift from the catalog.

## 3. Signing scheme, and where the key comes from

The signature is `HMAC-SHA256(key, canonical_bytes(body))`, where `body` is the
attestation with its `signature` member removed. Signing over the *whole* body
(not just the IR hash) means tampering with the verdict, the timestamp, the
recorded guarantees, or the signer is caught exactly like a tampered hash.

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
member for that migration; today its only value is `hmac-sha256`. This is noted,
not stubbed — no unpinned crypto dependency is pulled in speculatively.

## 4. Verifying

```
$ revl attest att.json --verify --against service.rvl --key ci-signer.key
attestation: VALID — valid: attestation is authentic and the composition matches
  composition sha256: 6f1e…
```

`--verify` is a **check**: it exits `0` when valid and **nonzero** when invalid.
Two failure modes, reported distinctly:

- **signature mismatch** — the HMAC does not match. The key is wrong, or a
  member of the attestation was altered after signing. Checked first: it proves
  the attestation itself is authentic before its contents are trusted.
- **hash mismatch** *(only with `--against`)* — the attestation is authentic,
  but the composition presented now hashes differently from the one it was
  signed for. The composition changed.

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

- `make_attestation(ir, key, *, now=None, signer=None) -> dict` — pure and
  deterministic given `now`; raises on a draft (open holes) or an empty key.
- `verify_attestation(att, key, ir=None) -> (ok, reason)`.
- `canonical_hash(ir) -> str` — the IR content hash.
- `resolve_key(path, *, env=None)` / `load_key(path)` / `load_attestation(path)`
  — the IO helpers.
