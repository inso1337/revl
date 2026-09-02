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

The signed body carries exactly these members:

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
  authentic before its contents are read.
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
  normalize=None) -> GateVerdict` runs the reference frontend and reports what
  it decided. Never raises for a refusal: a refused composition comes back as
  `admitted=False` carrying the frontend's own `error`.
- `make_attestation(ir, key, *, verdict, now=None, signer=None) -> dict`, pure
  and deterministic given `now`; raises without an admitted `GateVerdict` whose
  hash matches `ir`, on a draft (open holes), or on an empty key.
- `verify_attestation(att, key, ir=None) -> (ok, reason)`.
- `canonical_hash(ir) -> str`, the IR content hash.
- `discharged_guarantees()` / `catalogued_guarantees()` / `ruleset_digest()` /
  `checker_identity()`, the guarantee derivation and the checker identity.
- `resolve_key(path, *, env=None)` / `load_key(path)` / `load_attestation(path)`
  — the IO helpers.
