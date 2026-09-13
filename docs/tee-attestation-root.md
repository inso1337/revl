# The attestation root: real quote formats, and a key the peer does not hold

Roadmap item 475 (issue #827). `src/revl/tee_quote.py` and the hardware half of
`src/revl/tee_attestation.py`.

An attested-TEE placement demands that whatever runs a process can prove it is
running an approved bundle inside an approved enclave. Two earlier slices built
everything around that demand: the typed requirement and the fail-closed verifier
(`tee_attestation.py`), and the placement-file spelling that defers its verdict to
the verifier (`docs/attested-tee-placement.md`). Both deferred one thing, and
stated that they were deferring it.

What the verifier had to decide with was a symmetric MAC. A MAC proves that the
holder of a key authored a record. So the demand was worth exactly the assumption
that the peer did not hold the attester key, and a peer that did hold it could
mint a proof of a bundle it had never run. The demand read like a hardware fact
and was an assertion by a second party.

This is the root. A quote is now parsed as the vendor defines it and verified with
real asymmetric crypto up a chain that terminates in a public key the OPERATOR
pinned out of band. The peer holds no part of that chain, so it has nothing to
forge with.

## The two formats

`sign_alg` on the evidence record selects the format, and it is validated rather
than recorded: a record cannot claim one format and carry the other's bytes.

| `sign_alg` | format | signing curve | measurement reported |
| --- | --- | --- | --- |
| `tdx-quote-v4-ecdsa-p256` | Intel TDX Quote v4, ECDSA-256-with-P-256 | NIST P-256, SHA-256 | `MRTD`, the TD's initial measurement |
| `sev-snp-report-v2-ecdsa-p384` | AMD SEV-SNP `ATTESTATION_REPORT` v2/v3 | NIST P-384, SHA-384 | `MEASUREMENT`, the guest's launch measurement |
| `hmac-sha256` | the development verifier, NOT a root | none | whatever the record says |

**TDX** is parsed at the real offsets: a 48-byte quote header (version, attestation
key type, `tee_type`, the QE vendor id), a 584-byte TD Quote Body (`MRSEAM`,
`TDATTRIBUTES`, `XFAM`, `MRTD`, `MRCONFIGID`, `MROWNER`, `MROWNERCONFIG`, four
`RTMR`s and a 64-byte `reportdata`), then the ECDSA-P256 authentication data. The
chain inside that data is three hops and each one is checked:

1. the 384-byte QE Report is signed by the platform (PCK) key, which is the key
   the root must name;
2. the QE Report's `report_data` is `SHA-256(attestation key || QE auth data)`,
   which is what binds the attestation key to the platform signature. Without this
   hop the attestation key is an unattached public key the peer supplied;
3. the quote body (`header || TD quote body`) is signed by that attestation key.

**SEV-SNP** is one hop: the VCEK signs the report's first `0x2A0` bytes directly.
The report is a fixed `0x4A0` bytes and its signature field holds R and S as
72-byte **little-endian** scalars, which is the detail a reader written for the
big-endian convention gets silently wrong, so the padding above the 48 bytes a
P-384 scalar occupies is checked rather than skipped.

## What the hardware proves, and what it does not

Neither format knows about a peer id, a bundle label, a region, a network posture
or a placement's challenge. The one field either gives the workload is the 64-byte
`report_data`, so that is where those claims are bound:

```
report_data = SHA-512("revl.tee-evidence.report-data/v1\0" || canonical(evidence body))
```

SHA-512's digest is exactly 64 bytes, the width both formats give the field, so no
truncation or padding decision exists to get wrong. Change any member of the
evidence and the quote stops matching it, which is what makes a captured quote
unusable under a new challenge. `tee_attestation.evidence_report_data` is the
verifier's half and `attester_report_data` is the attester's; the verifier
re-derives the digest rather than trusting one the record carries.

Two honest limits, stated here rather than implied:

* **`bundle` is a label.** No hardware format measures it. What the hardware proves
  is the MEASUREMENT, and the requirement's permitted-measurement set is where the
  operator states which measurements correspond to the approved bundle. A permitted
  set that is re-attested as builds move, rather than baked into the requirement, is
  roadmap item 469.
* **`report_data` binds what the workload SAID.** An enclave running anything at all
  can bind a body naming a permitted measurement. So the measurement the record
  states is additionally compared against the measurement register the hardware
  reported, and a record whose two halves disagree is refused. This comparison is
  load-bearing, not belt-and-braces.

## The root, and why it is not in the placement file

`tee_quote.HardwareRoot` holds what the operator pinned, and a quote is verified
against the key the ROOT names for the fingerprint the record carries. The root is
never read out of the record.

```python
from revl.tee_quote import HardwareRoot, PinnedKey

root = HardwareRoot(
    platform_keys=[PinnedKey("P-384", vcek_public_bytes, "vcek/chip-a")],
    endorsement_roots=[PinnedKey("P-384", amd_root_public_bytes, "amd-root")],
)
```

Two ways a platform key can reach the root, and neither is the peer's to choose:

* **pinned directly.** The operator pins the PCK key or the VCEK by its raw `X || Y`
  bytes. The curve is part of the pin, so peer-supplied bytes cannot ask for the
  operator's own configuration to be reinterpreted on another curve.
* **endorsed by a pinned root.** A `PlatformEndorsement` is a record, signed by a
  pinned root key, binding a platform key to a vendor and a validity window, so an
  operator can pin one root and let the vendor speak for platforms it has never
  seen. This is a one-hop stand-in for the vendor's own X.509 chain (Intel's PCK
  chain to the SGX Root CA, AMD's VCEK certificate under the ASK and ARK).
  Consuming the vendor DER directly is the remaining hop: it changes which bytes
  carry the endorsement, not where the trust terminates.

There is deliberately **no placement-file key for a root**. A root is operator
configuration, it is the thing that makes the demand mean anything, and a file a
composition author edits is the wrong place to name it. `admit_peer_for_process`
and `offer_eligible` take it as an argument and pass it through untouched, because
the verdict on it belongs to `tee_admits` and not to the placement surface.

## The development verifier stays, fenced

Removing the symmetric path would break every caller that has only a shared
secret, so it stays as `tee_quote.DevMacRoot` and is fenced three ways:

* it reports `is_production = False`;
* constructing one requires `DevMacRoot(key, acknowledged_dev_only=True)`, by name,
  so selecting it is a visible act in the source rather than a default;
* every verdict it reaches, **the admission included**, carries
  `tee_quote.DEV_ROOT_NOTE` in its own reason text, so a log line cannot be
  mistaken for a hardware-rooted admission.

`tee_admits(..., require_hardware_root=True)` refuses it outright, before anything
is read, and `tee_quote.require_production_root` is the one call that expresses
that requirement. The two verifiers can never be crossed: the record's own
`sign_alg` decides which root may judge it, a MAC record presented to a hardware
root is refused, a quote presented to the development verifier is refused, and
supplying both a root and an attester key refuses rather than letting one silently
win.

## Fail-closed: every refusal this root pins

Nothing here is admitted on a claim, and no malformed input raises out of a
verifier: `verify_quote` returns a verdict, so a caller iterating candidate peers
cannot be broken by one of them. A refused quote reports an EMPTY measurement, so
a caller comparing it against a permitted set cannot find a refused quote
acceptable.

**The root.** A platform fingerprint the root does not hold. A quote signed by a
platform the root does not endorse, even a complete and internally consistent one.
A key pinned on the wrong curve for the format. A root configured with no pinned
key at all (it would refuse everything, which is more likely a mistake than a
policy). A root configured with bare key bytes instead of a `PinnedKey`. A record
naming the peer's own offer key as the platform that signed it. An endorsement
signed by a root the operator has not pinned, one outside its validity window, one
altered after signing, one for another vendor, one certifying a key other than the
one the evidence names, and one whose `platform_key_id` does not fingerprint the
key it carries.

**The measurement.** A measurement outside the requirement's permitted set. A
record stating a permitted measurement while the quote's own register reads
another. A measurement that cannot be a hardware register's width.

**The challenge and replay.** A `report_data` that is not this placement's
challenge digest. Any claim rewritten around an otherwise valid quote (peer,
bundle, region, posture, window, nonce). A challenge already consumed for this
peer. An expired, stale or future-dated proof. A proof with no replay ledger to
check against. A refused proof does not burn the challenge, so probing with a
forgery cannot deny an honest peer its own.

**The structure.** A quote that is not bytes. A TDX quote shorter than a header
plus a body plus a length, or of an unknown version, or whose `tee_type` is SGX, or
whose `attestation_key_type` is not ECDSA-P256, or from a QE vendor that is not
Intel, or whose declared signature-data length does not account for exactly the
bytes present (both short and long, so trailing bytes cannot ride along unsigned),
or whose QE authentication or certification length overruns its own structure. A
SEV-SNP report that is not exactly 1184 bytes, of an unknown version, with a
`signature_algo` the format does not define, or whose signature scalars spill past
P-384. A non-canonical ECDSA scalar: zero, or at or above the group order, which
closes the signature-of-all-zeroes admission. A public key that is not a point on
its curve, or the point at infinity.

**The enclave's own posture.** A TD whose `TDATTRIBUTES` debug bit is set, or a
SEV-SNP guest whose policy allows debug: either is inspectable from the host that
runs it, so the confidentiality the placement is buying does not hold. Both refuse
unless the root is explicitly built with `permit_debug=True`.

## Why the curve arithmetic is here

`revl` ships with no runtime dependencies and the standard library has no
asymmetric primitive, so the alternative to about 150 lines of curve arithmetic is
a third-party dependency in the security path of a compiler. Three things keep it
honest:

* each prime is written as the FORMULA its standard defines, not a transcribed
  constant, and `tests/test_tee_quote.py` re-derives the generator's membership of
  the curve and its annihilation by the group order, so a mistyped parameter
  reddens instead of quietly verifying nothing;
* signing is deterministic (RFC 6979), which is what makes a committed fixture
  reproducible from the repository;
* scalar multiplication is not constant time, and deliberately so: every scalar
  multiplied here is public (a verification scalar, or a fixture key that signs
  nothing secret), so there is no secret to leak and no reason to hand-roll a
  hardened ladder in a compiler.

## The fixtures

`tests/fixtures/tee/` holds 23 committed quotes and a `manifest.json` recording, for
each one, the verdict it must reach and why. `tests/fixtures/tee/generate.py`
regenerates every byte, and `tests/test_tee_quote.py` asserts that the committed
bytes equal a fresh generation, so a builder change that alters the wire layout
reddens rather than quietly testing a format nothing emits.

They are not Intel- or AMD-issued quotes, and that is not a shortcut that could be
taken differently: a genuine quote is signed by a per-platform key whose
certificate chain terminates in a live vendor service, and the private half lives
inside hardware. What is committed is the part a verifier can be held to, which is
the real wire layouts at the real offsets, signed with the curves the two vendors
specify, chained to a root pinned the way an operator pins one. The forged cases
are RE-SIGNED rather than corrupted, because a test that only flips a signature
byte shows that a broken forgery is caught, not that a well-made one is.

## Still open on item 475

* **The vendor DER chain.** Consuming Intel's PCK certificate chain and AMD's KDS
  VCEK certificate directly, in place of the one-hop `PlatformEndorsement`. Same
  root, different bytes carrying the endorsement.
* **Result receipts in the composition call path.** `tee_attestation` can sign and
  check a `ResultReceipt`; wiring it into the call path, and refusing an unattested
  result there, is the offer-side gate's mirror image and is not landed.
* **An attestation root in the `lawful_retry` dispatcher.** The dispatcher has
  nowhere to hold a root, so a replay of an attested base slot refuses rather than
  choosing an attested peer. That is the safe direction and it is pinned by a test,
  but the replay path only becomes useful once the dispatcher can be configured
  with a root.
* **Quorum** (k-of-n attesters from disjoint roots) is item 471 and **drift control**
  (a permitted-measurement set that is re-attested and provably current) is item
  469. Both are referenced here, neither is this item's.

## Relates to

* `docs/attested-tee-placement.md`: the placement-file spelling of the demand.
* `docs/design/475-attested-tee-placement.md`: the design note and its deferrals.
* `docs/design/461-verifiable-private-peer-pool.md`: the peer pool this extends.
