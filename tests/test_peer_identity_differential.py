"""`peer_identity`'s signed records against OpenSSL, on inputs neither chose.

``tests/test_ecdsa_differential.py`` already holds the curve arithmetic to an
independent implementation. This file holds the LAYER above it: the claim that a
signed peer record is an ECDSA signature over exactly the bytes this module says
it covers, and that :func:`revl.peer_identity.verify_record` and OpenSSL reach
the same verdict about every record, including the mutated ones.

That is a separate claim and a defect in it would not show up downstairs. A
wiring layer can sign the wrong message, cover the wrong members, or accept a
record whose bytes changed, with a perfectly correct curve underneath. So:

  * every signature ``sign_record`` produces verifies under OpenSSL, over the
    message ``signed_bytes`` names and no other;
  * every signature OpenSSL produces over that message verifies under
    ``verify_record``, so the verifier is not merely agreeing with its own
    signer's nonce choice;
  * the two agree about records that are WRONG - a flipped bit in r or s, a
    member removed, a member edited, a substituted public key, a signature moved
    to another record - which is the half that catches an over-permissive
    verifier rather than a broken signer.

``cryptography`` is a HARD import and a declared test dependency, not an
``importorskip``. The repository has been bitten twice by a suite that was
correct, gated on something nobody installed, and therefore reported SKIPPED
forever (item 430, item 433, issue #266), and a differential that vanishes when
the crypto library is absent vanishes precisely on the machines least likely to
have one. ``test_this_differential_cannot_skip_itself`` is the guard, mirroring
``tests/test_ecdsa_differential.py``'s.

Deterministic inputs: the draws come from a seeded ``random.Random``, so a
disagreement is reproducible from the file. OpenSSL picks its own nonce, so the
signatures it contributes are fresh on every run even though the keys and
bodies are not.
"""

from __future__ import annotations

import ast
import random
import re
import sys
from pathlib import Path

import pytest

# Hard, deliberately. See the module docstring.
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl.tee_quote import derive_public_key  # noqa: E402

#: Rounds per domain. Pure-Python P-256 arithmetic is the cost; 12 keeps the
#: file inside a few seconds while putting 36 independent keys and well over a
#: hundred signatures through both implementations.
ROUNDS = 12

#: The three protocols a peer record travels under. Signing the same body under
#: two of them must produce signatures that do not cross-verify, and that is
#: checked here rather than asserted in prose.
DOMAINS = [
    ("join", b"revl.pool-join/v1\x00"),
    ("offer", b"revl.peer-offer/v1\x00"),
    ("withdrawal", b"revl.pool-withdrawal/v1\x00"),
]
DOMAIN_IDS = [name for name, _ in DOMAINS]

_TALLY: dict[str, int] = {}


def _tally(name: str, count: int = 1) -> None:
    _TALLY[name] = _TALLY.get(name, 0) + count


def _library_key(scalar: int):
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _library_public(public_key: bytes):
    width = pi.IDENTITY_CURVE.coord_len
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(public_key[:width], "big"),
        int.from_bytes(public_key[width:], "big"),
        ec.SECP256R1()).public_key()


def _to_der(signature: bytes) -> bytes:
    width = pi.IDENTITY_CURVE.order_len
    return asym_utils.encode_dss_signature(
        int.from_bytes(signature[:width], "big"),
        int.from_bytes(signature[width:], "big"))


def _from_der(der: bytes) -> bytes:
    r, s = asym_utils.decode_dss_signature(der)
    width = pi.IDENTITY_CURVE.order_len
    return r.to_bytes(width, "big") + s.to_bytes(width, "big")


def _library_verifies(public, der: bytes, message: bytes) -> bool:
    try:
        public.verify(der, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def _draws(seed: int):
    """Deterministic ``(identity, body)`` pairs.

    The bodies are shaped like the records this module actually signs - a join,
    an offer, a withdrawal - with random values, nesting and member counts, so
    the canonicalisation is exercised over more than one fixed layout."""
    rng = random.Random(seed)
    for index in range(ROUNDS):
        scalar = rng.randrange(1, pi.IDENTITY_CURVE.n)
        identity = pi.PeerIdentity(peer_id=f"peer-{index}",
                                   private_key=scalar)
        body = {
            "kind": rng.choice(["revl.pool-join", "revl.peer-offer"]),
            "version": "1.0",
            "pool_id": f"pool-{rng.randrange(1000)}",
            "peer_id": identity.peer_id,
            "nonce": rng.randbytes(rng.choice([0, 1, 16])).hex(),
            "issued_at": f"2026-09-{rng.randrange(10, 29)}T00:00:00+00:00",
            "count": rng.randrange(-5, 5),
            "flag": rng.choice([True, False]),
            "nested": {"caps": sorted(f"cap-{rng.randrange(50)}"
                                      for _ in range(rng.randrange(0, 4))),
                       "depth": {"inner": rng.randrange(100)}},
        }
        if rng.random() < 0.5:
            body["optional"] = None
        yield identity, body, index


# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,domain", DOMAINS, ids=DOMAIN_IDS)
def test_every_record_this_module_signs_verifies_under_openssl(name, domain):
    """The signer, checked by somebody else, over the message this module
    NAMES as covered.

    If ``sign_record`` signed anything other than ``signed_bytes(domain,
    record)`` - a different serialisation, a different domain, a subset of the
    members - OpenSSL would reject here, because OpenSSL is handed the message
    the module documents rather than the one it happened to build."""
    for identity, body, index in _draws(seed=0xA11CE + len(name)):
        record = pi.sign_record(domain, body, identity)
        message = pi.signed_bytes(domain, record)
        public = _library_public(identity.public_key)
        signature = bytes.fromhex(record[pi.SIGNATURE_FIELD])
        assert len(signature) == 2 * pi.IDENTITY_CURVE.order_len
        assert _library_verifies(public, _to_der(signature), message), (
            f"{name} round {index}: OpenSSL rejects a signature sign_record "
            f"produced over the bytes signed_bytes names as covered")
        _tally("mine verified by openssl")


@pytest.mark.parametrize("name,domain", DOMAINS, ids=DOMAIN_IDS)
def test_every_record_openssl_signs_verifies_here(name, domain):
    """The verifier, fed signatures it did not produce.

    OpenSSL chooses its nonce at random, so these are fresh values on every run
    and sweep the r/s space in a way a deterministic signer cannot. A verifier
    that only ever agreed with its own signer would pass the test above and fail
    this one."""
    for identity, body, index in _draws(seed=0xB0B + len(name)):
        record = pi.sign_record(domain, body, identity)
        message = pi.signed_bytes(domain, record)
        private = _library_key(identity.private_key)
        for _ in range(2):
            theirs = _from_der(private.sign(message,
                                            ec.ECDSA(hashes.SHA256())))
            swapped = dict(record)
            swapped[pi.SIGNATURE_FIELD] = theirs.hex()
            ok, reason = pi.verify_record(domain, swapped,
                                          identity.public_key)
            assert ok, (
                f"{name} round {index}: verify_record rejects a signature "
                f"OpenSSL produced over the same message: {reason}")
            _tally("openssl verified by mine")


@pytest.mark.parametrize("name,domain", DOMAINS, ids=DOMAIN_IDS)
def test_the_two_agree_about_records_that_are_wrong(name, domain):
    """Agreement on REJECTION, which is the direction that matters.

    A verifier that accepts everything passes every positive test ever written.
    Each round takes a valid record and breaks it the ways an attacker would,
    at both levels: the signature scalars, and the record bytes underneath
    them."""
    width = pi.IDENTITY_CURVE.order_len
    disagreements: list[str] = []
    for identity, body, index in _draws(seed=0xDEAD + len(name)):
        record = pi.sign_record(domain, body, identity)
        signature = bytes.fromhex(record[pi.SIGNATURE_FIELD])
        r = int.from_bytes(signature[:width], "big")
        s = int.from_bytes(signature[width:], "big")

        other = pi.PeerIdentity(
            peer_id=identity.peer_id,
            private_key=(identity.private_key % (pi.IDENTITY_CURVE.n - 1)) + 1)

        def broken(label, mutate, scalars=None):
            candidate = dict(record)
            mutate(candidate)
            rr, ss = scalars if scalars else (r, s)
            candidate[pi.SIGNATURE_FIELD] = (
                rr.to_bytes(width, "big") + ss.to_bytes(width, "big")).hex() \
                if 0 <= rr < (1 << (8 * width)) and 0 <= ss < (1 << (8 * width)) \
                else "00" * width * 2
            return label, candidate, (rr, ss)

        def drop_a_member(candidate):
            candidate.pop("nonce", None)

        def edit_a_member(candidate):
            candidate["pool_id"] = "somewhere-else"

        def add_a_member(candidate):
            candidate["tier"] = "durable"

        def substitute_the_public_key(candidate):
            candidate[pi.PUBLIC_KEY_FIELD] = other.public_key.hex()

        def change_the_algorithm(candidate):
            candidate["sign_alg"] = "hmac-sha256"

        def untouched(candidate):
            return None

        candidates = [
            broken("bit flipped in r", untouched, (r ^ 1, s)),
            broken("bit flipped in s", untouched, (r, s ^ 1)),
            broken("high bit flipped in r", untouched, (r ^ (1 << 200), s)),
            broken("r and s swapped", untouched, (s, r)),
            broken("r zeroed", untouched, (0, s)),
            broken("s zeroed", untouched, (r, 0)),
            broken("r at the order", untouched, (pi.IDENTITY_CURVE.n, s)),
            broken("s at the order", untouched, (r, pi.IDENTITY_CURVE.n)),
            broken("member dropped", drop_a_member),
            broken("member edited", edit_a_member),
            broken("member added", add_a_member),
            broken("public key substituted", substitute_the_public_key),
            broken("algorithm changed", change_the_algorithm),
        ]

        for label, candidate, (rr, ss) in candidates:
            mine, _ = pi.verify_record(domain, candidate, identity.public_key)
            # What OpenSSL says about the SAME two things: the bytes this
            # record now covers, and the scalars it now carries.
            message = pi.signed_bytes(domain, candidate)
            try:
                der = asym_utils.encode_dss_signature(rr, ss)
            except ValueError:
                theirs = False
            else:
                theirs = _library_verifies(
                    _library_public(identity.public_key), der, message)
            _tally("rejection agreement")
            if bool(mine) is not bool(theirs):
                disagreements.append(
                    f"{name} round {index} [{label}]: verify_record says "
                    f"{mine}, OpenSSL says {theirs}")
    assert not disagreements, (
        f"{len(disagreements)} verdict disagreements with OpenSSL:\n  "
        + "\n  ".join(disagreements[:20]))


def test_a_record_signed_under_one_domain_does_not_verify_under_another():
    """Domain separation, checked by OpenSSL as well as by this module.

    A verifier that dropped the domain prefix would still pass every
    single-protocol test in this file, so the cross-protocol case is its own
    check. Both implementations must call it a rejection."""
    join_domain = DOMAINS[0][1]
    offer_domain = DOMAINS[1][1]
    for identity, body, index in _draws(seed=0xD0A1):
        record = pi.sign_record(join_domain, body, identity)
        ok, _ = pi.verify_record(offer_domain, record, identity.public_key)
        assert not ok, (
            f"round {index}: a join record verified as an offer, so the "
            f"domain prefix is not reaching the signed message")
        signature = bytes.fromhex(record[pi.SIGNATURE_FIELD])
        assert not _library_verifies(
            _library_public(identity.public_key), _to_der(signature),
            pi.signed_bytes(offer_domain, record)), (
            "OpenSSL disagrees: it accepts the cross-domain message")
        _tally("cross-domain rejection")


def test_a_signature_does_not_move_between_records():
    """A valid signature lifted onto a different record is a rejection under
    both, which is the replay this covered-set design exists to stop."""
    pairs = list(_draws(seed=0x5EED))
    domain = DOMAINS[0][1]
    for (identity, body, index), (_other_id, other_body, _) in zip(
            pairs, pairs[1:] + pairs[:1]):
        mine_record = pi.sign_record(domain, body, identity)
        other_record = pi.sign_record(domain, other_body, identity)
        if mine_record[pi.SIGNATURE_FIELD] == other_record[pi.SIGNATURE_FIELD]:
            continue  # the two bodies canonicalised the same; not a transplant
        transplanted = dict(other_record)
        transplanted[pi.SIGNATURE_FIELD] = mine_record[pi.SIGNATURE_FIELD]
        ok, _ = pi.verify_record(domain, transplanted, identity.public_key)
        assert not ok, f"round {index}: a signature moved between records"
        assert not _library_verifies(
            _library_public(identity.public_key),
            _to_der(bytes.fromhex(mine_record[pi.SIGNATURE_FIELD])),
            pi.signed_bytes(domain, transplanted))
        _tally("transplant rejection")


def test_public_keys_agree():
    """`derive_public_key` lands on the point OpenSSL lands on, for the scalars
    this file's identities are built from. Everything else here is meaningless
    if the two disagree about which key a scalar names."""
    for identity, _body, index in _draws(seed=0xFACE):
        theirs = _library_key(identity.private_key).public_key().public_numbers()
        width = pi.IDENTITY_CURVE.coord_len
        mine = derive_public_key(pi.IDENTITY_CURVE, identity.private_key)
        assert int.from_bytes(mine[:width], "big") == theirs.x, index
        assert int.from_bytes(mine[width:], "big") == theirs.y, index
        assert identity.public_key == mine
        _tally("public keys agreed")


def test_this_differential_is_not_vacuous():
    """The counts, asserted. A differential whose loops silently ran zero times
    reports the same colour as one that checked everything, and this repository
    has shipped exactly that shape before."""
    assert _TALLY.get("mine verified by openssl", 0) >= ROUNDS * len(DOMAINS)
    assert _TALLY.get("openssl verified by mine", 0) >= 2 * ROUNDS * len(DOMAINS)
    assert _TALLY.get("rejection agreement", 0) >= 13 * ROUNDS * len(DOMAINS)
    assert _TALLY.get("cross-domain rejection", 0) >= ROUNDS
    assert _TALLY.get("transplant rejection", 0) >= 1
    assert _TALLY.get("public keys agreed", 0) >= ROUNDS


def test_this_differential_cannot_skip_itself():
    """No `skipif`, no `importorskip`, nowhere in this file.

    A differential test that disappears when the library it differs against is
    missing provides its coverage exactly where the coverage is least needed.
    The import is hard; keep it hard, and keep `cryptography` in the `test`
    extra so `pip install -e '.[test]'` is enough to collect this file.

    Same rule and same guard as `tests/test_ecdsa_differential.py
    ::test_this_differential_cannot_skip_itself`, which is where it comes
    from."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {"importorskip", "skip", "xfail", "skipif"}
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            used.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in banned:
            used.add(node.func.id)
    assert not used, (
        f"this file uses {sorted(used)}, so its checks can report SKIPPED, "
        "which is the same colour as a pass. Make the dependency hard.")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"^test = \[[^\]]*cryptography", pyproject, re.MULTILINE), (
        "`cryptography` is imported at the top of this file but is not in the "
        "`test` extra, so a plain `pip install -e '.[test]'` job cannot collect "
        "it and the only cross-implementation check on the peer-identity "
        "signing layer stops running.")

    setup = (ROOT / "backends" / "python" / "setup.sh").read_text(
        encoding="utf-8")
    assert "cryptography" in setup, (
        "backends/python/setup.sh does not install `cryptography`, so the "
        "`frontend-cordis` job (which runs the whole root suite out of that "
        "venv) cannot collect this file.")


def test_the_module_under_test_is_not_the_library_being_tested():
    """`src/revl/peer_identity.py` must not have grown an import of
    `cryptography`.

    revl ships with no runtime dependencies, and this file's whole value is that
    a second implementation checks the first. Both stop being true in the same
    instant if the module imports the library, and this differential would then
    be comparing OpenSSL against itself and passing forever."""
    source = (ROOT / "src" / "revl" / "peer_identity.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.split(".")[0] not in {"cryptography", "ecdsa", "nacl"}, (
                f"peer_identity.py imports {name}. It is meant to be the "
                "pure-Python layer this file differs OpenSSL against.")
