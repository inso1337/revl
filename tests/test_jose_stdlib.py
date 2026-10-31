"""The stdlib JOSE module (stdlib/jose.rvl): local JWT/JWKS verification.

An OAuth/OIDC id token is a signed JWT: a compact JWS whose payload is a JSON
claims object. Accepting one is a client-side question ("did the issuer's key
sign these exact bytes, and do the claims say what I asked for?"), and this
module is that verification as a primitive kit: compact-JWS parse, strict
base64url, algorithm pinning to RS256/ES256, JWK Set parse and key selection by
`kid`/`alg`/`use`/key type, local signature verification, and the registered
claim checks (`iss`, `aud`, `exp`, `nbf`) as typed results against a
caller-supplied clock and leeway.

The module is pure revl except the two signature bodies, which are `@py` extern
bodies built on Python's integer arithmetic and `hashlib` (no third-party
package). This suite executes them on the py tier and pins that every other tier
REFUSES THE MODULE AT EMIT TIME BY NAME, rather than silently skipping the
signature check.

Checked here:
  * the module imports through `use`, its public names reach the IR, exactly two
    `pure` externs exist, each with a `py` body only, and the file is the
    documented public surface;
  * the published vectors, named in every failure: RFC 7515 Appendix A.2 (RS256),
    Appendix A.3 (ES256), Appendix A.1 (HS256, refused as `unsupported-alg`),
    and RFC 7517 Appendix A.1 (a JWK Set with an EC `use: "enc"` key and an RSA
    signing key);
  * a tampered signature, a tampered payload, a re-encoded header and a
    signature byte flip are all refused;
  * JWKS parsing, `kid`/alg/`use`/key-type selection, `key-not-found`,
    `ambiguous-key` and `bad-key`;
  * the JSON strictness: duplicate header/claim/JWK members, wrong claim types,
    trailing content, bad escapes, lone surrogates and excessive nesting;
  * the claim checks: expired, not-yet-valid, leeway boundaries, wrong issuer,
    wrong audience (string and array forms), absent `exp`/`aud`;
  * claim-check verdicts on published and on MINTED tokens. Minted tokens name
    their helper `minted (not published)`: they are signed in the test with the
    RFC 7515 A.2 private key at fixed instants, never by the module.
"""

import base64
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from _builtin_cases import serve_builtin_cases  # noqa: E402

MODULE = ROOT / "stdlib" / "jose.rvl"

# ------------------------------------------------------------ published vectors
# RFC 7515 Appendix A.2, "Example JWS Using RSASSA-PKCS1-v1_5 SHA-256". The
# header b64 is the RFC's; the payload b64 is the RFC's (the payload JSON carries
# the RFC's own line breaks); the signature is the RFC's.
RFC7515_A2_HEADER = "eyJhbGciOiJSUzI1NiJ9"
RFC7515_A2_PAYLOAD = (
    "eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFt"
    "cGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
)
RFC7515_A2_SIGNATURE = (
    "cC4hiUPoj9Eetdgtv3hF80EGrhuB__dzERat0XF9g2VtQgr9PJbu3XOiZj5RZmh7"
    "AAuHIm4Bh-0Qc_lF5YKt_O8W2Fp5jujGbds9uJdbF9CUAr7t1dnZcAcQjbKBYNX4"
    "BAynRFdiuB--f_nZLgrnbyTyWzO75vRK5h6xBArLIARNPvkSjtQBMHlb1L07Qe7K"
    "0GarZRmB_eSN9383LcOLn6_dO--xi12jzDwusC-eOkHWEsqtFZESc6BfI7noOPqv"
    "hJ1phCnvWh6IeYI2w9QOYEUipUTI8np6LbgGY9Fs98rqVt5AXLIhWkWywlVmtVrB"
    "p0igcN_IoypGlUPQGe77Rw"
)
RFC7515_A2_N = (
    "ofgWCuLjybRlzo0tZWJjNiuSfb4p4fAkd_wWJcyQoTbji9k0l8W26mPddx"
    "HmfHQp-Vaw-4qPCJrcS2mJPMEzP1Pt0Bm4d4QlL-yRT-SFd2lZS-pCgNMs"
    "D1W_YpRPEwOWvG6b32690r2jZ47soMZo9wGzjb_7OMg0LOL-bSf63kpaSH"
    "SXndS5z5rexMdbBYUsLA9e-KXBdQOS-UTo7WTBEMa2R2CapHg665xsmtdV"
    "MTBQY4uDZlxvb3qCo5ZwKh9kG4LT6_I5IhlJH7aGhyxXFvUK-DWNmoudF8"
    "NAco9_h9iaGNj8q2ethFkMLs91kzk2PAcDTW9gb54h4FRWyuXpoQ"
)
RFC7515_A2_E = "AQAB"
# the RFC 7515 A.2 private exponent, used ONLY by the test-side minting helper
RFC7515_A2_D = (
    "Eq5xpGnNCivDflJsRQBXHx1hdR1k6Ulwe2JZD50LpXyWPEAeP88vLNO97I"
    "jlA7_GQ5sLKMgvfTeXZx9SE-7YwVol2NXOoAJe46sui395IW_GO-pWJ1O0"
    "BkTGoVEn2bKVRUCgu-GjBVaYLU6f3l9kJfFNS3E0QbVdxzubSu3Mkqzjkn"
    "439X0M_V51gfpRLI9JYanrC4D4qAdGcopV_0ZHHzQlBjudU2QvXt4ehNYT"
    "CBr6XCLQUShb1juUO1ZdiYoFaFQT5Tw8bGUl_x_jTj3ccPDVZFD9pIuhLh"
    "BOneufuBiB4cS98l2SR_RQyGWSeWjnczT0QU91p1DhOVRuOopznQ"
)

# RFC 7515 Appendix A.3, "Example JWS Using ECDSA P-256 SHA-256".
RFC7515_A3_HEADER = "eyJhbGciOiJFUzI1NiJ9"
RFC7515_A3_PAYLOAD = RFC7515_A2_PAYLOAD  # A.3 shares A.1/A.2's payload
RFC7515_A3_SIGNATURE = (
    "DtEhU3ljbEg8L38VWAfUAqOyKAM6-Xx-F4GawxaepmXFCgfTjDxw5djxLa8ISlSA"
    "pmWQxfKTUJqPP3-Kg6NU1Q"
)
RFC7515_A3_X = "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU"
RFC7515_A3_Y = "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"

# RFC 7515 Appendix A.1, "Example JWS Using HMAC SHA-256": used only to prove
# that an HS256 token is refused by name before any key is considered.
RFC7515_A1_HEADER = "eyJ0eXAiOiJKV1QiLA0KICJhbGciOiJIUzI1NiJ9"
RFC7515_A1_PAYLOAD = RFC7515_A2_PAYLOAD
RFC7515_A1_SIGNATURE = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

# RFC 7517 Appendix A.1, "Example Public Keys": a published JWK Set with one EC
# key marked `use: "enc"` and one RSA signing key.
RFC7517_A1_EC_X = "MKBCTNIcKUSDii11ySs3526iDZ8AiTo7Tu6KPAqv7D4"
RFC7517_A1_EC_Y = "4Etl6SRW2YiLUrN5vfvVHuhp7x8PxltmWWlbbM4IFyM"
RFC7517_A1_RSA_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx"
    "4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMs"
    "tn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2"
    "QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbI"
    "SD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqb"
    "w0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)
RFC7517_A1_RSA_E = "AQAB"

RFC7515_A2_TOKEN = f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.{RFC7515_A2_SIGNATURE}"
RFC7515_A3_TOKEN = f"{RFC7515_A3_HEADER}.{RFC7515_A3_PAYLOAD}.{RFC7515_A3_SIGNATURE}"
RFC7515_A1_TOKEN = f"{RFC7515_A1_HEADER}.{RFC7515_A1_PAYLOAD}.{RFC7515_A1_SIGNATURE}"

RFC7515_A2_PAYLOAD_JSON = (
    '{"iss":"joe",\r\n "exp":1300819380,\r\n "http://example.com/is_root":true}'
)

# ------------------------------------------------------------- jwks documents
JWKS_A2 = json.dumps({"keys": [{
    "kty": "RSA", "kid": "rfc7515-a2", "alg": "RS256",
    "n": RFC7515_A2_N, "e": RFC7515_A2_E,
}]})
JWKS_A3 = json.dumps({"keys": [{
    "kty": "EC", "kid": "rfc7515-a3", "alg": "ES256", "crv": "P-256",
    "x": RFC7515_A3_X, "y": RFC7515_A3_Y,
}]})
JWKS_7517 = json.dumps({"keys": [
    {"kty": "EC", "crv": "P-256", "x": RFC7517_A1_EC_X, "y": RFC7517_A1_EC_Y,
     "use": "enc", "kid": "1"},
    {"kty": "RSA", "n": RFC7517_A1_RSA_N, "e": RFC7517_A1_RSA_E,
     "alg": "RS256", "kid": "2011-04-29"},
]})

#: the instant every published-vector test uses: before the A.2/A.3 `exp`.
NOW = 1300819000
#: the instant at which the published vectors are exactly expired (`exp`).
NOW_EXPIRED = 1300819380


# ------------------------------------------------------------ test-side helpers

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _jwks(*keys) -> str:
    return json.dumps({"keys": list(keys)})


def _hdr(text: str) -> str:
    return _b64e(text.encode("utf-8"))


def _minted_rs256(header: dict, payload_text: str, *, key=None) -> str:
    """A token MINTED (not published) for one claim-check case.

    Signed with the RFC 7515 A.2 RSA private key by integer arithmetic in this
    test, at fixed instants only; the module never signs and never reads a
    clock. `payload_text` is signed verbatim, so a case can carry a payload that
    is not produced by `json.dumps` (a duplicated claim, to name one).
    """
    key = key or (_rsa_n(), _rsa_d())
    header_b64 = _hdr(json.dumps(header, separators=(",", ":")))
    payload_b64 = _b64e(payload_text.encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}"
    digest = hashlib.sha256(signing_input.encode("utf-8")).digest()
    t = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    k = 256
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    s = pow(int.from_bytes(em, "big"), key[1], key[0])
    return f"{signing_input}.{_b64e(s.to_bytes(k, 'big'))}"


def _rsa_n() -> int:
    return int.from_bytes(_b64d(RFC7515_A2_N), "big")


def _rsa_d() -> int:
    return int.from_bytes(_b64d(RFC7515_A2_D), "big")


def _minted_rs256_bytes(header: dict, payload_raw: bytes) -> str:
    """Mint with a payload that is NOT valid UTF-8 (the signature still covers
    it, so only the UTF-8 read can refuse it)."""
    key = (_rsa_n(), _rsa_d())
    header_b64 = _hdr(json.dumps(header, separators=(",", ":")))
    payload_b64 = _b64e(payload_raw)
    signing_input = f"{header_b64}.{payload_b64}"
    digest = hashlib.sha256(signing_input.encode("utf-8")).digest()
    t = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    em = b"\x00\x01" + b"\xff" * (256 - len(t) - 3) + b"\x00" + t
    s = pow(int.from_bytes(em, "big"), key[1], key[0])
    return f"{signing_input}.{_b64e(s.to_bytes(256, 'big'))}"


def _flip_signature(token: str) -> str:
    """Flip one byte of the raw signature and re-encode it canonically."""
    header, payload, signature = token.split(".")
    raw = bytearray(_b64d(signature))
    raw[len(raw) // 2] ^= 0x01
    return f"{header}.{payload}.{_b64e(bytes(raw))}"


#: the consumer reaches every public name through one `use`, and every wrapper
#: hands a typed verdict back to Python so the assertions stay in one language.
CONSUMER = """\
use "stdlib/jose.rvl" {
  JoseError, JwsHeader, Claims, VerifyOptions, Jwk, Verified,
  parse_jwks, header_of, claims_of, check_claims, select_key, verify,
}

fn opts(issuer: Str, audience: Str, leeway: Int) -> VerifyOptions {
  return { issuer: issuer, audience: audience, leeway_seconds: leeway, require_exp: true }
}

fn opts_lax(issuer: Str, audience: Str, leeway: Int) -> VerifyOptions {
  return { issuer: issuer, audience: audience, leeway_seconds: leeway, require_exp: false }
}

fn code_of(r: Result[Verified, JoseError]) -> Str {
  return match r { Ok(v) => "ok", Err(e) => e.code }
}

fn bool_str(b: Bool) -> Str {
  if (b) { return "true" }
  return "false"
}

fn verify_code(token: Str, jwks: Str, issuer: Str, audience: Str, leeway: Int, now: Int) -> Str {
  return code_of(verify(token, jwks, opts(issuer, audience, leeway), now))
}

fn verify_code_lax(token: Str, jwks: Str, issuer: Str, audience: Str, leeway: Int, now: Int) -> Str {
  return code_of(verify(token, jwks, opts_lax(issuer, audience, leeway), now))
}

fn verify_payload(token: Str, jwks: Str, issuer: Str, audience: Str, leeway: Int, now: Int) -> Str {
  return match verify(token, jwks, opts(issuer, audience, leeway), now) {
    Ok(v) => v.payload,
    Err(e) => e.code,
  }
}

fn aud_summary(aud: List[Str]) -> Str {
  var out = ""
  var i = 0
  while (i < aud.length()) {
    if (i > 0) { out = out.concat(",") }
    out = out.concat(aud[i])
    i += 1
  }
  return out
}

fn claim_summary(c: Claims) -> Str {
  var out = c.iss
  out = out.concat("|")
  out = out.concat(c.sub)
  out = out.concat("|")
  out = out.concat(aud_summary(c.aud))
  out = out.concat("|")
  out = out.concat(c.exp.to_str())
  out = out.concat(":")
  out = out.concat(bool_str(c.has_exp))
  out = out.concat("|")
  out = out.concat(c.nbf.to_str())
  out = out.concat(":")
  out = out.concat(bool_str(c.has_nbf))
  out = out.concat("|")
  out = out.concat(c.iat.to_str())
  out = out.concat(":")
  out = out.concat(bool_str(c.has_iat))
  return out
}

fn claims_summary(payload_json: Str) -> Str {
  return match claims_of(payload_json) {
    Ok(c) => claim_summary(c),
    Err(e) => e.code,
  }
}

fn claims_check(payload_json: Str, issuer: Str, audience: Str, leeway: Int, now: Int) -> Str {
  return match claims_of(payload_json) {
    Ok(c) => match check_claims(c, opts(issuer, audience, leeway), now) {
      Ok(v) => "ok",
      Err(e) => e.code,
    },
    Err(e) => e.code,
  }
}

fn claims_check_lax(payload_json: Str, issuer: Str, audience: Str, leeway: Int, now: Int) -> Str {
  return match claims_of(payload_json) {
    Ok(c) => match check_claims(c, opts_lax(issuer, audience, leeway), now) {
      Ok(v) => "ok",
      Err(e) => e.code,
    },
    Err(e) => e.code,
  }
}

fn header_summary(token: Str) -> Str {
  return match header_of(token) {
    Ok(h) => h.alg.concat("|").concat(h.typ).concat("|").concat(h.kid),
    Err(e) => e.code,
  }
}

fn jwks_count(jwks: Str) -> Int {
  return match parse_jwks(jwks) {
    Ok(keys) => keys.length(),
    Err(e) => 0 - 1,
  }
}

fn jwks_message(jwks: Str) -> Str {
  return match parse_jwks(jwks) {
    Ok(keys) => "",
    Err(e) => e.message,
  }
}

fn jwks_code(jwks: Str) -> Str {
  return match parse_jwks(jwks) {
    Ok(keys) => "ok",
    Err(e) => e.code,
  }
}

fn select_summary(jwks: Str, alg: Str, kid: Str) -> Str {
  return match parse_jwks(jwks) {
    Ok(keys) => match select_key(keys, { alg: alg, typ: "JWT", kid: kid }) {
      Ok(k) => "ok:".concat(k.kid),
      Err(e) => e.code,
    },
    Err(e) => "parse:".concat(e.code),
  }
}

"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    d = tmp_path_factory.mktemp("jose_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "jose.rvl").write_text(MODULE.read_text(encoding="utf-8"),
                                           encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_public_names_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"verify_code", "verify_payload", "claims_summary", "claims_check",
            "header_summary", "jwks_count", "jwks_code",
            "select_summary"} <= names


def test_module_externs_are_two_pure_py_bodies(consumer_ir):
    externs = {e["name"]: e for e in consumer_ir["externs"]}
    assert set(externs) == {"rs256_verify", "es256_verify"}
    for name, extern in externs.items():
        assert extern["class"] == "pure", name
        assert extern["returns"] == "Bool", name
        # the py tier is the executed one; no other tier may pretend
        assert set(extern["bodies"]) == {"py"}, name


def test_module_file_is_the_documented_surface():
    text = MODULE.read_text(encoding="utf-8")
    assert "pub type JoseError = { code: Str, message: Str }" in text
    assert "pub type JwsHeader = { alg: Str, typ: Str, kid: Str }" in text
    assert "pub type VerifyOptions = {" in text
    assert "require_exp: Bool," in text
    assert "missing-exp" in text
    assert "pub fn verify(token: Str, jwks_json: Str, options: VerifyOptions, now: Int) -> Result[Verified, JoseError]" in text
    assert "pub fn parse_jwks(jwks_json: Str) -> Result[List[Jwk], JoseError]" in text
    assert "pub fn select_key(keys: List[Jwk], header: JwsHeader) -> Result[Jwk, JoseError]" in text
    assert "pub fn check_claims(claims: Claims, options: VerifyOptions, now: Int) -> Result[Claims, JoseError]" in text
    assert "pub fn claims_of(payload_json: Str) -> Result[Claims, JoseError]" in text
    assert "pub fn header_of(token: Str) -> Result[JwsHeader, JoseError]" in text


def test_module_is_pure_revl_except_the_two_verify_bodies():
    text = MODULE.read_text(encoding="utf-8")
    # exactly the two declared externs carry a host body; the rest is pure revl
    assert text.count("= @py {") == 2
    for marker in ("= @ts", "= @rs", "= @go", "= @java", "= @wasm"):
        assert marker not in text, marker
    assert "extern emission" not in text and "extern acquire" not in text


def test_other_tiers_refuse_the_module_by_name(consumer_ir):
    # The refusal is the contract: a tier with no verify body must fail the
    # emit with a message naming the extern, never skip the signature check.
    spec = importlib.util.spec_from_file_location(
        "rust_emit_jose", ROOT / "backends" / "rust" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(Exception) as caught:
        module.emit(consumer_ir)
    message = str(caught.value)
    assert "no @rs body" in message
    assert "rs256_verify" in message


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_jose", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    serve_builtin_cases(stub)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "jose.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


def _verify(ns, token, jwks, issuer="", audience="", leeway=0, now=NOW):
    return ns["verify_code"](token, jwks, issuer, audience, leeway, now)


# ---- the published vectors ------------------------------------------------

def test_rfc7515_a2_rs256_vector_verifies(ns):
    assert _verify(ns, RFC7515_A2_TOKEN, JWKS_A2, issuer="joe") == "ok"


def test_rfc7515_a2_payload_and_claims(ns):
    payload = ns["verify_payload"](RFC7515_A2_TOKEN, JWKS_A2, "joe", "", 0, NOW)
    assert payload == RFC7515_A2_PAYLOAD_JSON
    assert ns["claims_summary"](payload) == "joe|||1300819380:true|0:false|0:false"


def test_rfc7515_a2_header(ns):
    assert ns["header_summary"](RFC7515_A2_TOKEN) == "RS256||"


def test_rfc7515_a3_es256_vector_verifies(ns):
    assert _verify(ns, RFC7515_A3_TOKEN, JWKS_A3, issuer="joe") == "ok"


def test_rfc7515_a3_header_and_claims(ns):
    assert ns["header_summary"](RFC7515_A3_TOKEN) == "ES256||"
    payload = ns["verify_payload"](RFC7515_A3_TOKEN, JWKS_A3, "joe", "", 0, NOW)
    assert ns["claims_summary"](payload) == "joe|||1300819380:true|0:false|0:false"


def test_rfc7515_a1_hs256_is_refused_by_name(ns):
    # the sibling HMAC verifier lives in crypto.rvl; this kit must never accept
    # an HS256 token, because that is the algorithm-confusion door
    assert _verify(ns, RFC7515_A1_TOKEN, JWKS_A2, issuer="joe") == "unsupported-alg"


def test_rfc7517_a1_jwks_parses(ns):
    assert ns["jwks_code"](JWKS_7517) == "ok"
    assert ns["jwks_count"](JWKS_7517) == 2


def test_rfc7517_a1_key_selection(ns):
    # no kid: the single RSA signing key is chosen ...
    assert ns["select_summary"](JWKS_7517, "RS256", "") == "ok:2011-04-29"
    # ... and the EC key marked use "enc" is never an ES256 signature key
    assert ns["select_summary"](JWKS_7517, "ES256", "") == "key-not-found"


def test_rfc7517_a1_wrong_key_refuses_the_a2_token(ns):
    # the RFC 7517 A.1 RSA key is a DIFFERENT key; a valid signature for the A.2
    # key must not verify under it
    assert _verify(ns, RFC7515_A2_TOKEN, JWKS_7517, issuer="joe") == "signature-invalid"


def test_rfc7515_a3_token_does_not_verify_under_rsa_jwks(ns):
    assert _verify(ns, RFC7515_A3_TOKEN, JWKS_A2, issuer="joe") == "key-not-found"


# ---- tampering ------------------------------------------------------------

def test_tampered_signature_character_refused(ns):
    signature = RFC7515_A2_SIGNATURE
    middle = len(signature) // 2
    replacement = "A" if signature[middle] != "A" else "B"
    bad = f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.{signature[:middle]}{replacement}{signature[middle + 1:]}"
    assert _verify(ns, bad, JWKS_A2, issuer="joe") == "signature-invalid"


def test_flipped_signature_byte_refused(ns):
    assert _verify(ns, _flip_signature(RFC7515_A2_TOKEN), JWKS_A2, issuer="joe") == "signature-invalid"


def test_es256_flipped_signature_byte_refused(ns):
    assert _verify(ns, _flip_signature(RFC7515_A3_TOKEN), JWKS_A3, issuer="joe") == "signature-invalid"


def test_tampered_payload_refused(ns):
    # a payload that is still canonical base64url of a plausible JSON object,
    # but is not the bytes that were signed
    payload = _b64e(b'{"iss":"joe","exp":1300819380}')
    bad = f"{RFC7515_A2_HEADER}.{payload}.{RFC7515_A2_SIGNATURE}"
    assert _verify(ns, bad, JWKS_A2, issuer="joe") == "signature-invalid"


def test_reencoded_header_refused(ns):
    # same JSON value, different (still canonical) spelling: the signature
    # covers the exact segment text, and a verifier that re-serialized the
    # header before verifying would accept this
    header = _b64e(b'{"alg": "RS256"}')
    bad = f"{header}.{RFC7515_A2_PAYLOAD}.{RFC7515_A2_SIGNATURE}"
    assert _verify(ns, bad, JWKS_A2, issuer="joe") == "signature-invalid"


def test_non_canonical_signature_segment_refused(ns):
    # the published signature's last character has zero trailing bits; `x` is a
    # second spelling of the same bytes, and strict canonical decoding refuses it
    signature = RFC7515_A2_SIGNATURE
    assert signature[-1] == "w"
    bad = f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.{signature[:-1]}x"
    assert _verify(ns, bad, JWKS_A2, issuer="joe") == "invalid-base64url"


# ---- minted tokens (not published): claim checks under a valid signature ----

MINT_HEADER = {"alg": "RS256", "typ": "JWT", "kid": "rfc7515-a2"}


def test_minted_token_verifies_and_reads_header(ns):
    token = _minted_rs256({"alg": "RS256", "typ": "JWT", "kid": "rfc7515-a2", "cty": "x"},
                          '{"iss":"minted","aud":"app","sub":"acct-1","exp":1400000000,"nbf":1300000000,"iat":1300000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", audience="app",
                   now=1350000000) == "ok"
    payload = ns["verify_payload"](token, JWKS_A2, "minted", "app", 0, 1350000000)
    assert ns["claims_summary"](payload) == "minted|acct-1|app|1400000000:true|1300000000:true|1300000000:true"


def test_minted_unknown_header_members_are_skipped(ns):
    token = _minted_rs256({"alg": "RS256", "jwk_extra": {"nested": [1, 2, {"x": None}]}},
                          '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1350000000) == "ok"


def test_minted_expired_token_refused(ns):
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1400000000) == "token-expired"


def test_minted_not_yet_valid_token_refused(ns):
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted","nbf":1400000000,"exp":1500000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1300000000) == "token-not-yet-valid"


def test_minted_wrong_issuer_refused(ns):
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="evil", now=1350000000) == "wrong-issuer"


def test_minted_audience_string_and_array(ns):
    as_string = _minted_rs256(MINT_HEADER, '{"iss":"minted","aud":"app","exp":1400000000}')
    assert _verify(ns, as_string, JWKS_A2, issuer="minted", audience="app", now=1350000000) == "ok"
    assert _verify(ns, as_string, JWKS_A2, issuer="minted", audience="other", now=1350000000) == "wrong-audience"
    as_array = _minted_rs256(MINT_HEADER, '{"iss":"minted","aud":["a","app","b"],"exp":1400000000}')
    assert _verify(ns, as_array, JWKS_A2, issuer="minted", audience="app", now=1350000000) == "ok"
    assert _verify(ns, as_array, JWKS_A2, issuer="minted", audience="c", now=1350000000) == "wrong-audience"


def test_minted_absent_audience_claim_refused_when_expected(ns):
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", audience="app", now=1350000000) == "wrong-audience"


def test_minted_duplicate_claim_refused(ns):
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted","iss":"evil","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1350000000) == "malformed-claims"


def test_minted_payload_not_utf8_refused(ns):
    token = _minted_rs256_bytes(MINT_HEADER, b"\xff\xfe")
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1350000000) == "invalid-utf8"


def test_minted_wrong_kid_refused(ns):
    token = _minted_rs256({"alg": "RS256", "kid": "nope"}, '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1350000000) == "key-not-found"


def test_minted_missing_kid_uses_the_single_key(ns):
    token = _minted_rs256({"alg": "RS256"}, '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1350000000) == "ok"


def test_verify_refuses_a_token_without_exp(ns):
    # through the authenticating door, not just check_claims: an id token with a
    # valid signature but no `exp` must be refused by name
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted"}')
    assert _verify(ns, token, JWKS_A2, issuer="minted", now=1350000000) == "missing-exp"


def test_verify_require_exp_false_accepts_a_timeless_token(ns):
    token = _minted_rs256(MINT_HEADER, '{"iss":"minted","nbf":100}')
    assert ns["verify_code_lax"](token, JWKS_A2, "minted", "", 0, 1350000000) == "ok"


# ---- JWK key material must be canonical base64url too ----------------------

JWK_NON_CANONICAL = [
    # padding, the shape a lenient host decoder would strip
    ("n", RFC7515_A2_N + "=", "n"),
    # non-zero trailing bits: the last `Q` of n is canonical (value 16); `R` is
    # the same bytes with two non-canonical extra bits
    ("n", RFC7515_A2_N[:-1] + "R", "n"),
    ("e", RFC7515_A2_E + "==", "e"),
    ("x", RFC7515_A3_X[:-1] + "B", "x"),
]


@pytest.mark.parametrize("member,value,name", JWK_NON_CANONICAL)
def test_jwk_key_material_must_be_canonical(ns, member, value, name):
    if member == "x":
        key = {"kty": "EC", "kid": "k", "crv": "P-256",
               "x": RFC7515_A3_X, "y": RFC7515_A3_Y}
    else:
        key = {"kty": "RSA", "kid": "k", "n": RFC7515_A2_N, "e": RFC7515_A2_E}
    key[member] = value
    assert ns["jwks_code"](_jwks(key)) == "malformed-json"
    # the refusal names the offending member, so a bad JWK document is debuggable
    assert f"`{name}`" in ns["jwks_message"](_jwks(key))


def test_jwk_member_standard_base64_alphabet_refused(ns):
    key = {"kty": "RSA", "kid": "k", "n": RFC7515_A2_N[:-1] + "+",
           "e": RFC7515_A2_E}
    assert ns["jwks_code"](_jwks(key)) == "malformed-json"


# ---- malformed compact serializations -------------------------------------

MALFORMED_TOKENS = [
    ("abc", "malformed-token"),
    ("a.b", "malformed-token"),
    ("a.b.c.d", "malformed-token"),
    (".b.c", "malformed-token"),
    (f"{RFC7515_A2_HEADER}..{RFC7515_A2_SIGNATURE}", "malformed-token"),
    (f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.", "malformed-token"),
    ("A." + RFC7515_A2_PAYLOAD + "." + RFC7515_A2_SIGNATURE, "invalid-base64url"),
    (f"{RFC7515_A2_HEADER}=.{RFC7515_A2_PAYLOAD}.{RFC7515_A2_SIGNATURE}", "invalid-base64url"),
    (f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.+{RFC7515_A2_SIGNATURE}", "invalid-base64url"),
    # valid header/payload, signature with a character outside the alphabet
    (f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.!!", "invalid-base64url"),
]


@pytest.mark.parametrize("token,code", MALFORMED_TOKENS)
def test_malformed_compact_serialization_refused(ns, token, code):
    assert _verify(ns, token, JWKS_A2, issuer="joe") == code


# ---- protected header refusals --------------------------------------------

def _token_with_header_json(ns, header_text: str) -> str:
    """Wrap arbitrary header JSON in a token whose later segments are valid."""
    return _hdr(header_text) + f".{RFC7515_A2_PAYLOAD}.{RFC7515_A2_SIGNATURE}"


HEADER_REFUSALS = [
    ("[]", "malformed-json"),
    ("not json", "malformed-json"),
    ('{"typ":"JWT"}', "malformed-json"),
    ('{"alg":5}', "malformed-json"),
    ('{"alg":"RS256","alg":"none"}', "malformed-json"),
    ('{"alg":"RS256"} trailing', "malformed-json"),
    ('{"alg":"none"}', "unsupported-alg"),
    ('{"alg":"RS384"}', "unsupported-alg"),
    ('{"alg":"RS256","kid":5}', "malformed-json"),
]


@pytest.mark.parametrize("header_text,code", HEADER_REFUSALS)
def test_protected_header_refusals(ns, header_text, code):
    assert _verify(ns, _token_with_header_json(ns, header_text), JWKS_A2,
                   issuer="joe") == code


# ---- JWKS parse refusals --------------------------------------------------

JWKS_REFUSALS = [
    ("", "malformed-json"),
    ("{}", "malformed-json"),
    ('{"keys":{}}', "malformed-json"),
    ('{"keys":[1]}', "malformed-json"),
    ('{"keys":[]} trailing', "malformed-json"),
    ('{"keys":[],"keys":[]}', "malformed-json"),
    ('{"keys":[{"kty":"RSA","n":5}]}', "malformed-json"),
    ('{"keys":[{"kty":"RSA","n":"' + RFC7517_A1_RSA_N + '","n":"x"}]}', "malformed-json"),
    ('{"keys":', "malformed-json"),
    ('{"keys":[{"kty":"RSA",}]}', "malformed-json"),
]


@pytest.mark.parametrize("jwks,code", JWKS_REFUSALS)
def test_jwks_parse_refusals(ns, jwks, code):
    assert ns["jwks_code"](jwks) == code


def test_jwks_unknown_members_and_escaped_names(ns):
    # a JWK Set is extensible; an unknown member of ANY shape is skipped, and a
    # member name spelled with \u escapes is the same member
    jwks = ('{"issuer_hint":{"nested":[true,false,null,1.5]},'
            '"keys":[{"kty":"RSA","\\u006b\\u0069\\u0064":"escaped",'
            '"n":"' + RFC7515_A2_N + '","e":"' + RFC7515_A2_E + '","extra":[{"a":1}]}]}')
    assert ns["jwks_count"](jwks) == 1
    assert ns["select_summary"](jwks, "RS256", "escaped") == "ok:escaped"


# ---- key selection --------------------------------------------------------

JWKS_TWO_RSA = _jwks(
    {"kty": "RSA", "kid": "one", "alg": "RS256", "n": RFC7515_A2_N, "e": RFC7515_A2_E},
    {"kty": "RSA", "kid": "two", "alg": "RS256", "n": RFC7515_A2_N, "e": RFC7515_A2_E},
)
JWKS_RSA_AND_EC = _jwks(
    {"kty": "RSA", "kid": "rsa", "alg": "RS256", "n": RFC7515_A2_N, "e": RFC7515_A2_E},
    {"kty": "EC", "kid": "ec", "alg": "ES256", "crv": "P-256",
     "x": RFC7515_A3_X, "y": RFC7515_A3_Y},
)


def test_key_selection_by_kid(ns):
    assert ns["select_summary"](JWKS_TWO_RSA, "RS256", "one") == "ok:one"
    assert ns["select_summary"](JWKS_TWO_RSA, "RS256", "two") == "ok:two"
    assert ns["select_summary"](JWKS_TWO_RSA, "RS256", "missing") == "key-not-found"


def test_key_selection_refuses_ambiguity(ns):
    # two usable keys and no kid: picking the document's first would make the
    # verified key a function of JWKS ordering
    assert ns["select_summary"](JWKS_TWO_RSA, "RS256", "") == "ambiguous-key"
    assert ns["select_summary"](JWKS_RSA_AND_EC, "RS256", "shared") == "key-not-found"


def test_key_selection_duplicate_kid_is_ambiguous(ns):
    jwks = _jwks(
        {"kty": "RSA", "kid": "dup", "alg": "RS256", "n": RFC7515_A2_N, "e": RFC7515_A2_E},
        {"kty": "RSA", "kid": "dup", "alg": "RS256", "n": RFC7515_A2_N, "e": RFC7515_A2_E},
    )
    assert ns["select_summary"](jwks, "RS256", "dup") == "ambiguous-key"


def test_key_selection_filters_by_kty_curve_alg_and_use(ns):
    cases = [
        ({"kty": "EC", "kid": "k", "crv": "P-256", "alg": "ES256",
          "x": RFC7515_A3_X, "y": RFC7515_A3_Y}, "ES256", "ok:k"),
        ({"kty": "EC", "kid": "k", "crv": "P-384", "alg": "ES256",
          "x": RFC7515_A3_X, "y": RFC7515_A3_Y}, "ES256", "key-not-found"),
        ({"kty": "EC", "kid": "k", "alg": "ES256",
          "x": RFC7515_A3_X, "y": RFC7515_A3_Y}, "ES256", "key-not-found"),
        ({"kty": "EC", "kid": "k", "crv": "P-256", "use": "enc", "alg": "ES256",
          "x": RFC7515_A3_X, "y": RFC7515_A3_Y}, "ES256", "key-not-found"),
        ({"kty": "RSA", "kid": "k", "alg": "RS384", "n": RFC7515_A2_N,
          "e": RFC7515_A2_E}, "RS256", "key-not-found"),
        ({"kty": "RSA", "kid": "k", "n": RFC7515_A2_N, "e": RFC7515_A2_E},
         "ES256", "key-not-found"),
        ({"kty": "RSA", "kid": "k", "use": "sig", "n": RFC7515_A2_N,
          "e": RFC7515_A2_E}, "RS256", "ok:k"),
    ]
    for key, alg, expected in cases:
        assert ns["select_summary"](_jwks(key), alg, "k") == expected, (key, alg)


# ---- bad key material -----------------------------------------------------

def test_rsa_short_modulus_refused_as_bad_key(ns):
    # a 1024-bit modulus, base64url of a 128-byte integer
    short_n = _b64e(b"\x80" + b"\x01" * 127)
    jwks = _jwks({"kty": "RSA", "kid": "short", "alg": "RS256", "n": short_n,
                  "e": RFC7515_A2_E})
    token = _minted_rs256({"alg": "RS256", "kid": "short"},
                          '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, jwks, issuer="minted", now=1350000000) == "bad-key"


def test_rsa_missing_key_material_refused_as_bad_key(ns):
    jwks = _jwks({"kty": "RSA", "kid": "empty", "alg": "RS256", "e": RFC7515_A2_E})
    token = _minted_rs256({"alg": "RS256", "kid": "empty"},
                          '{"iss":"minted","exp":1400000000}')
    assert _verify(ns, token, jwks, issuer="minted", now=1350000000) == "bad-key"


def test_p256_coordinate_width_refused_as_bad_key(ns):
    jwks = _jwks({"kty": "EC", "kid": "ec", "alg": "ES256", "crv": "P-256",
                  "x": _b64e(b"\x01" * 31), "y": RFC7515_A3_Y})
    token = f"{_hdr('{\"alg\":\"ES256\",\"kid\":\"ec\"}')}.{RFC7515_A3_PAYLOAD}.{RFC7515_A3_SIGNATURE}"
    assert _verify(ns, token, jwks, issuer="joe") == "bad-key"


# ---- signature width checks ----------------------------------------------

def test_es256_wrong_signature_width_refused(ns):
    raw = _b64d(RFC7515_A3_SIGNATURE)[:63]
    token = f"{RFC7515_A3_HEADER}.{RFC7515_A3_PAYLOAD}.{_b64e(raw)}"
    assert _verify(ns, token, JWKS_A3, issuer="joe") == "signature-invalid"


def test_rs256_wrong_signature_width_refused(ns):
    raw = _b64d(RFC7515_A2_SIGNATURE)[:255]
    token = f"{RFC7515_A2_HEADER}.{RFC7515_A2_PAYLOAD}.{_b64e(raw)}"
    assert _verify(ns, token, JWKS_A2, issuer="joe") == "signature-invalid"


# ---- claims parsing -------------------------------------------------------

CLAIMS_CASES = [
    ('{"iss":"joe","sub":"s","aud":["a","b"],"exp":1,"nbf":2,"iat":3}',
     "joe|s|a,b|1:true|2:true|3:true"),
    ('{"aud":"only"}', "||only|0:false|0:false|0:false"),
    ("{}", "|||0:false|0:false|0:false"),
    # unknown members of every JSON shape are skipped
    ('{"iss":"joe","nested":{"a":[1,2.5,true,null,"x"]},"flag":false,"num":1e3}',
     "joe|||0:false|0:false|0:false"),
    # \u escapes decode, including a surrogate pair (U+1F600)
    ('{"iss":"j\\u00f6e","sub":"\\ud83d\\ude00"}', "jöe|😀||0:false|0:false|0:false"),
]


@pytest.mark.parametrize("payload,summary", CLAIMS_CASES)
def test_claims_parse_and_normalise(ns, payload, summary):
    assert ns["claims_summary"](payload) == summary


CLAIMS_PARSE_REFUSALS = [
    ("[]", "malformed-json"),
    ("not json", "malformed-json"),
    ('{"iss":"joe"} trailing', "malformed-json"),
    ('{"exp":"1300819380"}', "malformed-claims"),
    ('{"exp":1300819380.5}', "malformed-claims"),
    ('{"exp":1e3}', "malformed-claims"),
    ('{"aud":5}', "malformed-claims"),
    ('{"aud":["a",5]}', "malformed-claims"),
    ('{"iss":["a"]}', "malformed-claims"),
    ('{"iss":"a","iss":"b"}', "malformed-claims"),
    ('{"exp":1,"exp":2}', "malformed-claims"),
    ('{"aud":"a","aud":"b"}', "malformed-claims"),
    # a raw control character inside a JSON string, a bad escape, and a lone
    # surrogate are each refusals, not lenient decodes
    ('{"iss":"a\x01b"}', "malformed-json"),
    ('{"iss":"a\\qb"}', "malformed-json"),
    ('{"iss":"\\ud83d"}', "malformed-json"),
    ('{"iss":"\\ud83d\\u0041"}', "malformed-json"),
    # an integer wider than a NumericDate must not silently wrap
    ('{"exp":99999999999999999999999}', "malformed-json"),
]


@pytest.mark.parametrize("payload,code", CLAIMS_PARSE_REFUSALS)
def test_claims_parse_refusals(ns, payload, code):
    assert ns["claims_summary"](payload) == code


def test_deep_nesting_refused(ns):
    payload = '{"x":' + "[" * 40 + "]" * 40 + "}"
    assert ns["claims_summary"](payload) == "malformed-json"


# ---- claim checks (published claims JSON, no signature needed here) --------

def test_claims_check_expiry_boundaries(ns):
    payload = '{"iss":"joe","exp":100}'
    assert ns["claims_check"](payload, "", "", 0, 99) == "ok"
    assert ns["claims_check"](payload, "", "", 0, 100) == "token-expired"
    assert ns["claims_check"](payload, "", "", 10, 109) == "ok"
    assert ns["claims_check"](payload, "", "", 10, 110) == "token-expired"


def test_claims_check_not_before_boundaries(ns):
    payload = '{"nbf":100,"exp":200}'
    assert ns["claims_check"](payload, "", "", 0, 100) == "ok"
    assert ns["claims_check"](payload, "", "", 0, 99) == "token-not-yet-valid"
    assert ns["claims_check"](payload, "", "", 10, 90) == "ok"


def test_claims_check_requires_exp_by_default(ns):
    # an OIDC id token must expire: a verifier that only checks a PRESENT `exp`
    # accepts a never-expiring token, so the default door refuses absence
    assert ns["claims_check"]('{"iss":"joe"}', "joe", "", 0, 0) == "missing-exp"
    # the opt-out is explicit, for a deliberately timeless non-OIDC token
    assert ns["claims_check_lax"]('{"iss":"joe"}', "joe", "", 0, 0) == "ok"
    # `nbf` stays optional, and an absent nbf is not a refusal
    assert ns["claims_check"]('{"exp":100}', "", "", 0, 0) == "ok"


def test_claims_check_issuer_and_audience(ns):
    payload = '{"iss":"joe","aud":["app","other"],"exp":200}'
    assert ns["claims_check"](payload, "joe", "other", 0, 0) == "ok"
    assert ns["claims_check"](payload, "joe", "app", 0, 0) == "ok"
    assert ns["claims_check"](payload, "evil", "app", 0, 0) == "wrong-issuer"
    assert ns["claims_check"](payload, "joe", "absent", 0, 0) == "wrong-audience"
    # "" means "not checked", which is a caller choice documented in the module
    assert ns["claims_check"](payload, "", "", 0, 0) == "ok"


def test_claims_check_leeway_bounds(ns):
    assert ns["claims_check"]('{"exp":100}', "", "", -1, 0) == "bad-options"
    # a leeway past one day is not clock skew and would extend every token
    assert ns["claims_check"]('{"exp":100}', "", "", 86401, 0) == "bad-options"
    assert ns["claims_check"]('{"exp":100}', "", "", 86400, 100 + 86399) == "ok"


def test_claims_exp_zero_is_expired_not_absent(ns):
    # the `has_exp` flag exists so a numeric zero is a real instant, not "no exp"
    assert ns["claims_check"]('{"exp":0}', "", "", 0, 1) == "token-expired"
