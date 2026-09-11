# 474: proof-carrying component certificates

**Roadmap:** item 474. **Issue:** #826. **Landed:** slice 1, `revl attest
COMPOSITION --certificate` and `revl attest CERT --verify-certificate` over
`src/revl/cert.py`, with `tests/test_826_component_certificate.py`. **Status:**
DECISION, slice 1 landed; the confidence and drift items below are designed and
deferred behind named preconditions.

## The decision

**A composition's admission record and its coverage record are two different
claims, and the tool should be able to sign the second one.** `revl attest`
(item 127, `src/revl/attest.py`) already signs the first: the gate admitted this
exact IR, under this checker, at this instant. What it does not carry is how far
the guarantees the verdict names are actually proved, which parts of the model
those proofs cover, and which rows of `formal/STATUS.md` say "this one is only
partial". A reader who sees nine guarantee codes and a valid signature has to
open the formal package and reconcile two documents by hand to find out that one
of the nine is unproved and unstatable.

Slice 1 lands that reconciliation as a signed document. It is a second envelope
under the same key (`revl.component-certificate` v1.0) rather than a member added
to the attestation, because the attestation's v2 envelope is what deploy, bundle
and publish verify against and widening it would move the meaning of every
record already in flight.

## What the item text asked for, against the tree

The item text is materially stale in two directions and accurate in its exit
criterion.

Its per-guarantee list, "G2/G3/G7 full, G4/G5/G6/G8 lattice- or head-qualified,
G1 partial, G9 coverage unproved", under-counts the document. Measured on
`267862fb`, the map rows at `formal/STATUS.md:48-56` read: G1 `partial`; G2 and
G3 `full`; G4 `full over the lattice` with a marked-weak shape-level statement;
G5 `full over the lattice` with a **contentless** shape-level statement,
registered as a finding; G6 `full at head granularity`; G7 `full` for which
entries run, in what order, under which verdict; G8 `full over the lattice` with
a marked-weak marker-level statement; G9 `rule proved; coverage unproved and
unstatable`. Three of the eight rows the item calls "lattice- or
head-qualified" are qualified *and* full, so a binary full/partial/unproved
status cannot represent the map: the status and the qualification are separate
facts and the certificate carries both.

It also implies that green-check-only reporting is the current state. It is not.
`attest.run_gate` refuses to produce a `GateVerdict` for a composition the
frontend rejects, `make_attestation` refuses to sign without one, and
`attest.verify_attestation` already refuses an envelope naming a guarantee code
the catalogue does not define. `formal/STATUS.md` already carries the honest
caveat map, including a G9 section headed "What G9 does NOT cover". The gap the
item names that is real is the machine-readable half: the honest prose exists
and nothing signed it, re-derived it, or failed closed when it went missing.

The item's date (2026-09-09) is not evidence of anything: authored commit
timestamps in this repository run months ahead of it.

## The shape of the certificate

`cert.make_certificate(ir, key, *, verdict, source_path, formal=None, now=None,
signer=None)` builds the body and signs it. The members are:

| member | source |
|---|---|
| `kind`, `version` | `CERT_KIND`, `CERT_VERSION` |
| `verdict`, `hash_alg`, `sign_alg` | constants, checked at verify time rather than merely recorded |
| `subject` | the file the gate ran over (`subject.filename`), its sha256 (`cert.source_digest`), the canonical IR hash (`attest.canonical_hash`) and the guarantee codes the verdict cites (`attest.discharged_guarantees`) |
| `proof_model` | `cert.proof_model(root)`: the pinned `formal/lean-toolchain`, a sha256 of `formal/lake-manifest.json`, and the pinned manifest package names |
| `as_of_commit` | `cert.tree_commit(root)`, the commit the formal package sits in, or `null` when the package is not in a git work tree |
| `artifacts` | one sha256 per formal artifact, over the three files the requirements are read from |
| `statuses` | `cert.formal_state(root)`: one row per catalogued code with the status this build derives, the map's status cell verbatim, the map's gap cell verbatim, the registered theorem names and the contentless findings |
| `requirements` | what each status rests on, one row per derived claim |
| `caveats` | the qualifications the artifacts record, flattened to one line each |
| `checker`, `timestamp`, `key_id`, `signer` | the attestation members, unchanged in meaning |
| `signature` | `HMAC-SHA256(key, CERT_SIGN_DOMAIN + canonical_bytes(body))` |

The signature reuses `attest._canonical_bytes`, so the signed bytes are a pure
function of the body members and not of dict insertion order. `CERT_SIGN_DOMAIN`
is `b"revl.component-certificate/v1\x00"`, distinct from the attestation's
`b"revl.attestation/v2\x00"`: one key signs two protocols and neither document
may verify as the other.

## Where the state is read from

The certificate is a reading of documents that already exist, and each reading
is a named function so that the assertion "the coverage matches
`formal/STATUS.md`" has something to be checked against.

- `cert.guarantee_map(text, *, source)` reads the table under `## What the layer
  covers, and what it does not`, keyed by the bolded guarantee code in the first
  cell. A document with no such section is a refusal, and so is a map with no
  rows: neither is evidence of coverage.
- `cert.status_of(cell, *, source, code)` places a status cell. The reading is
  the weakest thing the cell will admit rather than the strongest. A cell
  beginning `full` is `proved`, with everything after the first semicolon
  carried verbatim as the qualification. A cell naming `partial`, or one naming
  `unproved` beside a word-bounded `proved` or `unstatable` (G9's cell), is
  `partial`. A cell the reading cannot place is a `CertError`, not a guess.
- `cert.registry(text, *, source)` reads `formal/scripts/nonvacuity.tsv`, whose
  `kind` column is what makes a contentless statement visible. `_registered`
  then answers "which registered theorems does this guarantee's row cite", by
  the name prefixes `RevL.<code>.` and `RevL.<code>Classified.`.
- `cert.gate_steps` and `cert.gate_theorems` read the numbered steps and the
  `RevL.*` / `RevLOracle.*` theorem lists of `formal/scripts/run_gate.sh`, plus
  the axiom policy the gate states.
- `cert.oracle_census`, `cert.injection_proofs` and `cert.injection_sweep` read
  the oracle census, the injection table and the mutation sweep paragraph out of
  `formal/STATUS.md`.

`cert.certifiable(state, guarantees)` is the gate on the build side. It refuses
to produce a certificate whose statuses are asserted rather than checked: a
guarantee whose status is `proved` needs at least one theorem registered under
its name, a guarantee that is not `proved` needs the map's own gap sentence,
and every catalogued code needs a row. That is what makes "the certificate
cannot claim a guarantee the artifacts do not back" a property of the builder
rather than a promise in a comment. The asymmetry to know about: the rows and
the registered theorems are required, but the unstatable-obligation section is
optional. Deleting the whole "What G9 does NOT cover" section from
`formal/STATUS.md` therefore still builds, silently dropping `REQ_UNSTATABLE`
and one requirement (26 to 25), so a certificate can be produced over a package
that stopped admitting the unstatable obligation at all. It is not silent to a
*verifier*: the deleted text moves the `formal/STATUS.md` digest, and a reader
holding the revision the certificate was signed over catches it as an artifact
mismatch.

## The trust boundary

The certificate is an attestation, so the boundary is the attestation's boundary
plus one step, and the user-facing statement of it lives in
`docs/revl-attest.md` section 7.

- **The key proves authorship, not honesty.** Symmetric HMAC over a shared
  secret: every verifier is a key holder and a key holder can sign any claim.
  The signature is checked first and what the record says is checked after,
  because authenticity is not authority.
- **The evidence is the formal artifacts, not the certificate.**
  `cert.verify_certificate` re-derives the evidence from the package on the
  verifying machine and compares it with what was signed, member by member: the
  artifact digests; every member of every status row (the status, the status
  cell it was read out of, that cell's name, the gap, the theorem count, the
  oracle cell, the registered theorem names, the contentless findings); every
  caveat; every requirement with both the `detail` and the `check` recorded
  behind it; all three members of the proof model — the toolchain pin, the
  manifest digest and the dependency pins the manifest names; the checker
  identity; the
  subject's source digest; the commit, as a revision of this history; and
  `key_id`, against the key in hand. It never reads a status out of the document
  it is checking. An attacker who holds the key can edit the record and re-sign
  it, and the re-derivation is what survives that: a `proved` status promoted
  over a `partial` one is caught as an evidence mismatch, a dropped status row is
  caught because the catalogue is closed, a dropped caveat is caught because the
  caveat set is re-derived, and a forged artifact digest is caught because the
  digests are re-derived too. The same sweep catches a rewritten status cell, a
  rewritten or emptied gap, a renamed guarantee, an unregistered theorem, a
  rewritten theorem count or oracle cell, a dropped contentless finding, a
  rewritten requirement `detail` or `check`, a forged toolchain pin, a forged
  manifest digest or a dependency pin the manifest does not name, a rewritten
  checker version or ruleset digest, a commit this history does
  not contain, a forged subject digest and a forged key fingerprint.
- **It fails closed.** `verify_certificate` never raises: a malformed document
  is `(False, reason)` with the reason naming which of the key, the envelope and
  the evidence failed. Unknown guarantee names, unknown requirement kinds,
  duplicated or unsorted rows, a missing status row, a signed member dropped from
  the record, a truncated subject hash, a
  non-instant timestamp, a missing signature and a signature that is not ASCII
  are all refusals. The last one is the defect class this verifier has to be
  immune to by construction: `hmac.compare_digest` raises `TypeError` on two
  strings that are not both ASCII, so a peer-supplied record must be refused
  before it reaches the comparison, or the failure mode of a hostile certificate
  is a traceback rather than a reason.
- **Two members are recorded, not re-derived.** `timestamp` and `signer` are
  statements about the signing event rather than about the tree, so there is
  nothing on the verifying machine to compare them with (`cert.UNVERIFIABLE`).
  They stay inside the MAC and every successful verification names them in its
  reason, so a green result is never read as a claim about them.
- **The secret never appears in `argv`.** `--key PATH` names a file,
  `REVL_ATTEST_KEY_FILE` names one, `REVL_ATTEST_KEY` carries the bytes in the
  environment. There is no default key.
- **`--formal DIR` is explicit and does not fall back.** A named directory with
  no `STATUS.md` is a refusal rather than a silent search, because certifying
  against a package the caller did not name would be a coverage claim about the
  wrong document.
- **What is not covered.** The verifier trusts the `formal/` package on disk; it
  only establishes that it is the revision this certificate was signed over, by
  digest. It does not establish that the Lean development has no mistakes, and
  it does not establish that the prose in `formal/STATUS.md` accurately
  describes the development. It records what the status document says,
  including the rows that say the coverage is not there. `timestamp` and
  `signer` are not checked against anything: they describe the signing event,
  not the tree. `subject.filename` is checked through the digest of the file it
  names rather than as a path, so the same bytes under another path verify, and
  `as_of_commit` is checked as a revision of this repository's history rather
  than as an equality with `HEAD`, so a checkout that moved forward is not a
  false refusal and a commit this history does not contain is.

## Seams touched

| file | change |
|---|---|
| `src/revl/cert.py` | new; 1413 lines, the model, the readers, the builder, the verifier and the renders |
| `src/revl/cli/parser.py` | three flags on the existing `attest` subparser: `--certificate`, `--verify-certificate`, `--formal DIR` |
| `src/revl/cli/observe.py` | `_run_attest` dispatches to `_run_certificate`, and refuses `--verify` combined with either certificate flag so the two protocols cannot be confused |
| `tests/test_826_component_certificate.py` | new; the exit criterion, the readers, the trust boundary, the re-signed forgeries and the artifact mutations |
| `docs/revl-attest.md` | section 7, the certificate and its trust boundary |
| `docs/commands-reference.md` | the three flags |

No subcommand is added, so the `cli-verbs` block does not move. The certificate
documentation goes in `revl-attest.md` and this file, so the `doc-status`
inventory does not move either, which was deliberate: regenerating a shared
block makes every open PR pay for this one.

## Deferred, with preconditions

- **Per-guarantee drift between `as_of_commit` and the working tree.** The
  `artifacts` digests already detect a moved document, and the verifier names
  which one. What is not built is a report of *what* moved between two commits.
  Precondition: a caller that has two revisions of the package at once, which is
  `revl formal-diff` and not this item.
- **Confidence above the guarantee table.** The item's phrase "coverage" could
  be read as a number. It is deliberately not one: a status read out of prose
  that has been flattened to a scalar is exactly the invented formula the honest
  reporting exists to avoid. Coverage stays a per-guarantee status plus the rows
  that back it, and any future scalar has to be derived from the registry rather
  than from the table.
- **A certificate for the runtime evidence of a deployed composition.** The
  attestation already carries optional `evidence_bindings` over the item-293
  evidence bundle and the certificate accepts them through the same member.
  Binding a certificate to a *recorded run* is the policy-diff and blast-radius
  lane (item 468), not this one.
- **Asymmetric signatures.** Unchanged from item 127 and noted there: an Ed25519
  envelope would let an untrusted verifier check with a public key. The
  `sign_alg` member exists for the migration and the verifier refuses any value
  other than `hmac-sha256`.
