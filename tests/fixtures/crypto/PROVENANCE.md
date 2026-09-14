# Committed ECDSA test vectors, and where they came from

`src/revl/tee_quote.py` implements ECDSA over NIST P-256 and P-384 in pure
Python integers, because `revl` ships with no runtime dependencies and the
standard library has no asymmetric primitive. That code decides whether a TEE
attestation quote is genuine, so it is checked here against authority written
by somebody else rather than against itself.

The vectors are COMMITTED, not fetched. A gate that needs the network is a gate
that skips on the day the network is down, and a skip reports the same colour as
a pass (issue #266, roadmap item 445). Nothing under `tests/` downloads these.

## `wycheproof/`

Project Wycheproof ECDSA verification vectors, IEEE P1363 encoding, taken
verbatim from the upstream repository at
<https://github.com/C2SP/wycheproof>, path `testvectors_v1/`.

P1363 is the encoding this module uses: a signature is raw `R || S`, each
integer big-endian and padded to the order length, with no ASN.1 wrapper. The
DER suites in the same upstream directory are mostly tests of a parser this
module does not have, which is why they are not here.

| file | curve | hash | cases |
| --- | --- | --- | --- |
| `ecdsa_secp256r1_sha256_p1363_test.json` | P-256 | SHA-256 | 262 |
| `ecdsa_secp256r1_sha512_p1363_test.json` | P-256 | SHA-512 | 332 |
| `ecdsa_secp384r1_sha384_p1363_test.json` | P-384 | SHA-384 | 280 |
| `ecdsa_secp384r1_sha512_p1363_test.json` | P-384 | SHA-512 | 318 |

The first and third are the pairings the two quote formats actually use (Intel
TDX signs with P-256/SHA-256, AMD SEV-SNP with P-384/SHA-384). The two SHA-512
suites are there for the truncation path: a digest wider than the group order
has to be cut to the leftmost `qlen` bits, and an implementation that reduces
modulo `n` instead, or forgets to cut at all, passes the matched-hash suites and
fails these.

Each case carries Wycheproof's own `flags`, which name the bug family it was
built to catch: `InvalidSignature` (r=0/s=0 admission, CVE-2022-21449),
`RangeCheck` (r or s at or beyond the order), `PointDuplication`,
`EdgeCaseShamirMultiplication` (an intermediate point at infinity),
`ModularInverse`, `ArithmeticError`, `SignatureSize`, `EdgeCasePublicKey`.
`tests/test_ecdsa_vectors.py` asserts that the flags it expects are still
present, so a future vector refresh that drops a family is visible.

The four files are vendored UNMODIFIED, byte for byte, so they can be diffed
against upstream. Copyright the Wycheproof authors, licensed under the Apache
License 2.0; see <https://github.com/C2SP/wycheproof/blob/main/LICENSE>. The
rest of this repository is MIT (see `LICENSE` at the root).

## `rfc6979_ecdsa_p256_p384.json`

The deterministic-nonce reference values of RFC 6979, Appendix A.2.5 (P-256) and
A.2.6 (P-384), <https://www.rfc-editor.org/rfc/rfc6979.txt>. Extracted
mechanically from the RFC text rather than retyped.

Each group carries the group order `q`, the private scalar `x`, the public point
`Ux`/`Uy`, and ten `(hash, message, k, r, s)` cases: five hashes (SHA-1,
SHA-224, SHA-256, SHA-384, SHA-512) over each of the two messages `sample` and
`test`. All values are big-endian hex except `message`, which is the literal
ASCII string.

The five hashes matter more than the two messages. `_bits2int` and `_rfc6979_k`
have to behave differently when the hash is narrower than the order (SHA-1 and
SHA-224 against both curves, SHA-256 against P-384: the HMAC drum has to be
turned more than once and the result is not truncated), equal to it, and wider
than it (SHA-384 and SHA-512 against P-256: the digest is cut to the leftmost
`qlen` bits). `k` is asserted directly, not just the resulting signature, so a
nonce derivation that is wrong but self-consistent cannot pass.
