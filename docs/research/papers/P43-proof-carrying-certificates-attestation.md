---
id: P43
title: "Signing What Is Not Proved: Admission Attestations, Coverage Certificates and Reproducible Bundles"
theme: security
kind: full
maturity: shipped-evidence
status: The HMAC envelope pair and the reproducible bundle are shipped with tests; the drift monitor and asymmetric signatures are designed and not built, and no systematic evaluation exists.
fused_with: P40
---

## Pitch
A compiler's "admitted" stamp says nothing about how far the guarantees it names are proved, so revl signs a second envelope that states, per guarantee, the proof status the formal ledger actually gives, and a verifier re-derives that status from the artifacts instead of trusting the signer.

## Abstract
Supply-chain attestation systems log that a build or a check happened. They say little about what the check is worth. A compiler that admits a composition under nine named safety guarantees, of which one is only partly proved and several are proved only over a lattice or with a weak shape statement, produces the same green stamp for all nine. We describe two signed envelopes in revl. The first, an admission attestation, was originally a claim: it signed a constant list of guarantee codes without compiling anything. We changed it into a measurement: signing now requires a gate verdict produced for the same composition hash, the listed codes are those the shipped ruleset cites, the checker identity is bound into the signed body, and a receiver may, when it turns the option on (off by default), re-run its own frontend over the staged source before accepting. The second, a component certificate, states per guarantee whether it is proved, partial or unproved, carries the ledger's own gap sentence, and accounts for the whole diagnostics catalogue exactly once through three lists. Its verifier re-derives every status, caveat, requirement, artifact digest and proof-model pin from the formal package on the verifying machine, so a key holder who re-signs an edited certificate is still caught, relative to the formal package on the verifying machine. A reproducible bundle rebuilds bit-for-bit from source, except for its timestamped evidence, which is verified by signature. We are explicit about what remains out of reach: both envelopes use a shared-secret HMAC, so the key proves authorship and not honesty, and the verifier trusts the formal package on disk. We report the forgery classes caught by the test suite. We report no deployment numbers, because none exist yet.

## Problem and claim
Existing attestation formats bind a subject to an event ("this builder produced these bytes", "this check passed"). A consumer who does not trust the checker needs more: which properties the check covers, and how strongly each is established. A single green stamp collapses a full proof, a lattice-qualified proof, a marked-weak statement and an unproved coverage claim into one bit.

The claim is narrow. Signing the admission and signing the coverage are two different claims and need two envelopes under domain-separated MACs. The coverage envelope can be made verifiable by re-derivation: the verifier never reads a status out of the document it checks, it rebuilds the status from the ledger and compares member by member. Under that rule, a re-signed forgery that promotes a status or drops a caveat fails, even with the key.

We do not claim the proofs are correct, that the ledger prose matches the Lean development, or that the HMAC gives third-party verifiability. The docs state each of these as out of scope.

## Contributions
1. Attestation as measurement: `make_attestation` refuses to sign without a `GateVerdict` from `run_gate`, refuses a verdict whose composition hash differs from the IR being signed, and binds checker identity (compiler version, ruleset digest). A receiver can re-run its own frontend (`TrustStore.recheck_source`).
2. A second envelope, the component certificate (`revl.component-certificate`, v1.1), carrying per-guarantee status (proved, partial, unproved) separate from the qualification text, read back from `formal/STATUS.md`, `formal/scripts/nonvacuity.tsv` and `formal/scripts/run_gate.sh`.
3. A closed-catalogue accounting rule: every catalogued diagnostic code appears exactly once in `statuses`, `named_statuses` or the third list, enforced by builder and verifier, so stated silence is distinguished from absence.
4. Verification by re-derivation, with a forgery test matrix (promoted status, dropped caveat, forged digest, forged pin, non-ASCII signature, cross-protocol replay).
5. `revl bundle` / `revl verify`: a directory of source, IR, lock, per-backend artifacts, policy and evidence that rebuilds bit-for-bit.
6. An explicit trust-boundary statement (HMAC proves authorship, not honesty) and a policy-drift module that gives the post-admission half a verdict vocabulary.

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| in-toto attestations and SLSA provenance | Signed statements about how an artifact was produced; predicate types are open | The coverage certificate is a predicate about how far the check is proved, and the verifier re-derives it from the proof ledger rather than trusting the statement |
| Sigstore (Fulcio, Rekor) | Keyless signing and a transparency log for identity | revl uses a shared-secret HMAC; it has no identity or log layer. This is a weakness, see limits |
| CompCert, CakeML | Compiler correctness proved once, in the compiler | revl's gate is not proved end to end; the certificate says which rows are partial or unproved and which proofs are contentless |
| Proof-carrying code (Necula) | The producer ships a proof the consumer checks | Here the consumer re-derives a coverage statement and can re-run the gate; it does not check a proof term for the composition |
| Reproducible builds (Debian) | Same inputs give bit-identical outputs | `revl verify` recompiles bundled source and compares each emitted backend artifact; evidence carrying timestamps is verified by signature, not by byte reproduction (`docs/bundle.md:155-158`) |
| TUF | Key roles and rollback protection for update metadata | Not addressed; asymmetric upgrade deferred |

Other citations to be checked: in-toto (Torres-Arias et al., USENIX Security 2019); SLSA spec; Necula POPL 1997.

CompCert's checked primary sources are in the [related-work table](../related-work.md).

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Attestation requires a gate verdict for the same composition | `src/revl/attest.py:806-870` (`make_attestation`), `:676-750` (`GateVerdict`, `run_gate`); `tests/test_attest_truthfulness.py::test_a_g2_violating_composition_cannot_be_attested` | shipped | The module docstring (`src/revl/attest.py:1-60`) notes that v1 signed a constant list without compiling |
| Guarantee list is read from the shipped ruleset, not the docs | `src/revl/attest.py:582` (`discharged_guarantees`); `test_the_guarantee_list_is_derived_from_the_shipped_ruleset`, `test_a_ruleset_that_stops_citing_a_rule_stops_attesting_it` | shipped | The list is per ruleset, not per composition; it is the same for every composition the ruleset admits |
| Checker identity bound; sign_alg checked | `src/revl/attest.py:603-625`, `:121` (`SIGN_ALG`); `test_a_relabelled_sign_alg_is_refused`, `test_the_signed_body_names_which_checker_asserted_it` | shipped | |
| Domain separation between attestation, receipt and certificate | `src/revl/attest.py:129` (`SIGN_DOMAIN`); `src/revl/cert.py:94` (`CERT_SIGN_DOMAIN`); `test_the_certificate_is_a_second_signature_domain`, `test_an_admission_receipt_does_not_verify_as_an_attestation` | shipped | |
| Receiver may re-run its own frontend | `src/revl/deploy.py:586-610` and `:752-770` (`recheck_source`); `test_a_receiver_can_re_run_the_gate_itself` | shipped, off by default | Docstring: off, "admission trusts the SIGNER's gate run" |
| Certificate with per-guarantee status | `src/revl/cert.py:1047` (`make_certificate`), `:921` (`certifiable`), `:1461` (`verify_certificate`); `docs/design/474-component-certificates.md:11-59` | shipped (slices 1 and 2) | Envelope v1.1; a v1.0 certificate is refused by version |
| Status and qualification are separate facts | `docs/design/474-component-certificates.md` "What the item text asked for"; `formal/STATUS.md:48-56`; `test_a_proved_row_that_is_qualified_keeps_its_qualification` | shipped | G4, G5, G8 are "full over the lattice" with weak or contentless shape statements |
| Contentless theorems named, not counted | `test_a_contentless_theorem_is_named_rather_than_counted_as_proof` | shipped | |
| Whole catalogue accounted for once | `docs/design/474-component-certificates.md` "The coverage boundary, and why it is three lists"; `test_the_whole_catalogue_is_accounted_for_exactly_once`, `test_a_code_the_ledger_says_nothing_about_is_named_rather_than_dropped` | shipped | The doc says 22 codes. On main, the `GUARANTEES` table in `src/revl/diagnostics.py:25-67` has 25 keys (9 numbered G, 5 named G, 7 A, 4 T), and `OTHER_CODES` (`:170`) adds codes to `ALL_CODES`. The 21-with-ledger-status figure was not checked |
| Re-signed forgeries are caught | `test_a_ledger_edit_changes_the_certificate` (parametrized), `test_a_dependency_pin_the_manifest_does_not_name_is_refused` | shipped | Test file has 45 test functions; the matrix is parametrized so the case count is higher; not counted |
| Hostile input gives a reason, not a traceback | `test_a_signature_that_is_not_ascii_is_a_reason_and_not_a_raise`, `test_the_verifier_process_refuses_a_non_ascii_signature` | shipped | |
| Oracle census is run, not read from prose | `src/revl/cert.py:503` (`oracle_census`); `test_the_oracle_census_is_the_harness_run_not_a_document` | shipped | |
| Dev HMAC is not a trust root | `docs/revl-attest.md:345-372`; `docs/design/474-component-certificates.md` "The trust boundary" | shipped (stated) | "The key proves authorship, not honesty" |
| Reproducible bundle | `docs/bundle.md:1-70`; `tests/test_bundle.py::test_verify_is_green_end_to_end`, `test_tampered_emitted_artifact_is_the_exact_backend_mismatch`, `test_pack_is_deterministic` | shipped | Evidence with timestamps is excluded from byte comparison by design |
| Policy-drift control | `src/revl/drift.py:1-40`, `detect` at `:232`; `tests/test_821_drift_control.py` (27 test functions) | partial | Module docstring: it "re-hashes nothing"; the monitor that re-measures live subjects is "the follow-up" |
| Asymmetric signatures | `src/revl/attest.py:60-70` docstring; `docs/design/474-component-certificates.md` "Deferred" | designed | Not built |
| Drift between `as_of_commit` and tree, confidence scalar, certificate over an observed run | `docs/design/474-component-certificates.md` "Deferred, with preconditions" | designed | Confidence scalar is deliberately refused |

## Numbers that exist
- Test function counts (grep of `def test`, parametrization not expanded): `tests/test_826_component_certificate.py` 45, `tests/test_attest.py` 26, `tests/test_attest_truthfulness.py` 28, `tests/test_bundle.py` 32, `tests/test_821_drift_control.py` 27. These measure suite size, not forgery-detection rate.
- Catalogue size: the design doc says 22 diagnostics codes, 21 with a ledger status (`docs/design/474-component-certificates.md`, "The coverage boundary"). On main, `GUARANTEES` in `src/revl/diagnostics.py:25-67` has 25 keys, so the 22 is stale. The ledger-status count is not verified; define the count before citing it.
- Status distribution, read off `formal/STATUS.md:48-56` at the time of the doc: G1 partial; G2, G3 full; G4, G5, G8 full over the lattice with weak or contentless shape statements; G6 full at head granularity; G7 full; G9 full for the rule at the sink and for the coverage of the walk, since issue #2108 (`formal/STATUS.md:56`, `:99-101`). The design doc's "rule proved; coverage unproved" reading (`docs/design/474-component-certificates.md`, lines 36-42) was measured at `16c2be6c` and is stale on main.
- No detection-rate, overhead, or user-study numbers exist.

## Paper outline
1. Introduction: the single-bit stamp problem, with the G1-G9 example.
2. Threat model: who the verifier is, what the key holder can do, what is out of scope.
3. The admission attestation: from claim to measurement (the v1 defect, the v2 fix, recheck at the receiver).
4. The coverage certificate: three lists, status separate from qualification, re-derivation.
5. Reproducible bundle and the drift verdict vocabulary.
6. Evaluation: forgery matrix, ledger-history replay, cost.
7. Limits and related work.

## Threats to validity and limits
- The verifier trusts the `formal/` package on disk. The design doc says it "does not establish that the Lean development has no mistakes, and it does not establish that the prose in `formal/STATUS.md` accurately describes the development" (`docs/design/474-component-certificates.md`, "What is not covered").
- Symmetric HMAC: "every verifier is a key holder and a key holder can sign any claim." Third-party verification is not offered.
- The attestation's guarantee list is a property of the ruleset, not of the composition; it says which rules the frontend enforces, and the module docstring says it "is NOT a proof of the G-rules themselves" (`src/revl/attest.py:55-58`).
- `recheck_source` is off by default, so the default receiver still trusts the signer's gate run.
- The certificate builder keeps an asymmetry: deleting the "What G9 does NOT cover" section still builds. A verifier holding the signed revision catches it; a builder-only consumer does not (`docs/design/474-component-certificates.md`, "certifiable" paragraph).
- `timestamp` and `signer` are stated, not re-derived (`cert.UNVERIFIABLE`).
- Drift detection is a pure decision function. A continuous-monitoring story is not built.
- The ledger contains contentless statements and marked-weak shape statements; the certificate reports them, which is the point, but a reader may see a partially verified system.
- Single project, single formal package; generality of the coverage-certificate pattern is not shown.
- Not yet done: a systematic forgery mutation study per body member, a replay of the ledger history, verification cost, bundle reproducibility across machines, and a decision on asymmetric signing.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 40 checked, 6 corrected.
