"""`stdlib/auth.rvl`'s `host_validate` is a real token validator (issue #1554).

The accepted `revl serve --http` design (B + C2,
docs/design/569-serve-http-caller-identity.md) authenticates nobody at the
server: the composition's `Auth.validate` is the authentication, and only it
mints a `Principal`. Before this issue the shipped py body accepted ANY
non-blank token as that subject, so `Authorization: Bearer alice` was alice.

Pinned here:
  * the validator verifies an HS256 JWT (RFC 7519 / RFC 7515 / RFC 7518) and
    checks the signature, `exp`, `nbf`, `iss`, `aud` and `sub`;
  * forged, tampered, unsigned, wrong-algorithm, expired, wrong-audience,
    wrong-issuer and malformed tokens are refused with a 401;
  * a valid token mints the Principal of its `sub`;
  * the any-token stub runs only behind REVL_AUTH_INSECURE_DEV_STUB=1; without a
    configured validator and without the flag every call is refused by name;
  * the key comes from the environment only and reaches no IR, refusal, log
    line or recorded trace;
  * the tiers without a body refuse by name at compile time.

The unit half runs the SHIPPED body text, taken from the compiled IR, so it
needs no runtime. The live half stands a composition up over the HTTP face and
needs cordis-py.
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402

AUTH = str(ROOT / "stdlib" / "auth.rvl")

# A distinctive key, so a leak anywhere is a substring hit.
KEY = "k3y-1554-" + "S" * 40
ISSUER = "https://issuer.example"
AUDIENCE = "notes-api"
DEV_FLAG = "REVL_AUTH_INSECURE_DEV_STUB"
AUTH_ENV = ("REVL_AUTH_HS256_SECRET", "REVL_AUTH_HS256_SECRET_FILE",
            "REVL_AUTH_ISSUER", "REVL_AUTH_AUDIENCE", "REVL_AUTH_LEEWAY",
            DEV_FLAG)


# ---------------------------------------------------------------- helpers

class Ok:
    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


class Err:
    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(obj) -> str:
    return _b64(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _sign(head: str, body: str, key: str = KEY) -> str:
    mac = hmac.new(key.encode("utf-8"), f"{head}.{body}".encode("ascii"),
                   hashlib.sha256)
    return _b64(mac.digest())


def mint(claims=None, *, key: str = KEY, header=None, **overrides) -> str:
    """An HS256 JWT. `claims` defaults to a valid one for alice."""
    now = int(time.time())
    body = {"sub": "alice", "iss": ISSUER, "aud": AUDIENCE,
            "iat": now, "exp": now + 600} if claims is None else dict(claims)
    body.update(overrides)
    head = _segment(header or {"alg": "HS256", "typ": "JWT"})
    payload = _segment(body)
    return f"{head}.{payload}.{_sign(head, payload, key)}"


def _body_of_host_validate() -> str:
    ir = compile_files([AUTH])
    (ext,) = [e for e in ir["externs"] if e["name"] == "host_validate"]
    return ext["bodies"]["py"]


@pytest.fixture(scope="module")
def host_validate():
    """The shipped `@py` body, executed the way the py emitter renders it."""
    namespace = {"Ok": Ok, "Err": Err}
    exec("def host_validate(b):" + _body_of_host_validate(), namespace)
    return namespace["host_validate"]


@pytest.fixture
def clean_env(monkeypatch):
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def configured(clean_env):
    clean_env.setenv("REVL_AUTH_HS256_SECRET", KEY)
    clean_env.setenv("REVL_AUTH_ISSUER", ISSUER)
    clean_env.setenv("REVL_AUTH_AUDIENCE", AUDIENCE)
    return clean_env


def _validate(fn, token):
    return fn({"token": token})


def _refused(result, status: int = 401) -> str:
    assert isinstance(result, Err), f"expected a refusal, got {result.value!r}"
    assert result.value["status"] == status
    return result.value["message"]


# ---------------------------------------------------------------- valid

def test_a_valid_token_mints_the_principal_of_its_subject(host_validate,
                                                         configured):
    result = _validate(host_validate, mint())
    assert isinstance(result, Ok)
    assert result.value == {"subject": "alice"}
    # the subject is the signed `sub`, never the token text
    other = _validate(host_validate, mint(sub="bob"))
    assert other.value == {"subject": "bob"}


def test_an_audience_list_containing_ours_is_accepted(host_validate,
                                                      configured):
    token = mint(aud=["billing", AUDIENCE])
    assert _validate(host_validate, token).value == {"subject": "alice"}


def test_leeway_admits_a_token_just_past_expiry(host_validate, configured):
    token = mint(exp=int(time.time()) - 5)
    _refused(_validate(host_validate, token))
    configured.setenv("REVL_AUTH_LEEWAY", "60")
    assert isinstance(_validate(host_validate, token), Ok)


def test_a_key_file_is_read_and_its_trailing_newline_dropped(host_validate,
                                                             configured,
                                                             tmp_path):
    key_file = tmp_path / "hs256.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    configured.delenv("REVL_AUTH_HS256_SECRET")
    configured.setenv("REVL_AUTH_HS256_SECRET_FILE", str(key_file))
    assert _validate(host_validate, mint()).value == {"subject": "alice"}


# ---------------------------------------------------------------- refused

def test_a_forged_token_is_refused(host_validate, configured):
    forged = mint(key="an-attacker-key-" + "x" * 32)
    assert "signature" in _refused(_validate(host_validate, forged))


def test_tampered_claims_are_refused(host_validate, configured):
    head, _, sig = mint().split(".")
    now = int(time.time())
    evil = _segment({"sub": "mallory", "iss": ISSUER, "aud": AUDIENCE,
                     "exp": now + 600})
    assert "signature" in _refused(
        _validate(host_validate, f"{head}.{evil}.{sig}"))


@pytest.mark.parametrize("header", [
    {"alg": "none", "typ": "JWT"},
    {"alg": "None"},
    {"typ": "JWT"},
])
def test_an_unsigned_token_is_refused(host_validate, configured, header):
    now = int(time.time())
    body = _segment({"sub": "alice", "iss": ISSUER, "aud": AUDIENCE,
                     "exp": now + 600})
    for sig in ("", _sign(_segment(header), body)):
        token = f"{_segment(header)}.{body}.{sig}"
        assert "alg" in _refused(_validate(host_validate, token))


@pytest.mark.parametrize("header", [
    {"alg": "HS512", "typ": "JWT"},
    {"alg": "RS256", "typ": "JWT"},
    {"alg": "HS256", "typ": "JWT", "crit": ["exp"]},
    {"alg": "HS256", "typ": "at+jwt-but-not-jwt"},
])
def test_other_algorithms_and_headers_are_refused(host_validate, configured,
                                                   header):
    _refused(_validate(host_validate, mint(header=header)))


def test_an_expired_token_is_refused(host_validate, configured):
    token = mint(exp=int(time.time()) - 1)
    assert "expired" in _refused(_validate(host_validate, token))


def test_a_not_yet_valid_token_is_refused(host_validate, configured):
    token = mint(nbf=int(time.time()) + 600)
    assert "not yet valid" in _refused(_validate(host_validate, token))


@pytest.mark.parametrize("aud", ["other-api", ["other-api"], [], None, 7])
def test_a_wrong_audience_token_is_refused(host_validate, configured, aud):
    claims = {"sub": "alice", "iss": ISSUER, "exp": int(time.time()) + 600}
    if aud is not None:
        claims["aud"] = aud
    assert "audience" in _refused(_validate(host_validate, mint(claims)))


@pytest.mark.parametrize("iss", ["https://evil.example", "", None])
def test_a_wrong_issuer_token_is_refused(host_validate, configured, iss):
    claims = {"sub": "alice", "aud": AUDIENCE, "exp": int(time.time()) + 600}
    if iss is not None:
        claims["iss"] = iss
    assert "issuer" in _refused(_validate(host_validate, mint(claims)))


@pytest.mark.parametrize("drop, value", [
    ("exp", None), ("exp", "tomorrow"), ("exp", True),
    ("sub", None), ("sub", ""), ("sub", 42),
])
def test_required_claims_are_enforced(host_validate, configured, drop, value):
    now = int(time.time())
    claims = {"sub": "alice", "iss": ISSUER, "aud": AUDIENCE, "exp": now + 600}
    if value is None:
        del claims[drop]
    else:
        claims[drop] = value
    _refused(_validate(host_validate, mint(claims)))


def _duplicate_member_token() -> str:
    head = _segment({"alg": "HS256", "typ": "JWT"})
    now = int(time.time())
    raw = (f'{{"sub":"mallory","sub":"alice","iss":"{ISSUER}",'
           f'"aud":"{AUDIENCE}","exp":{now + 600}}}')
    body = _b64(raw.encode("utf-8"))
    return f"{head}.{body}.{_sign(head, body)}"


def _non_canonical_signature() -> str:
    head, body, sig = mint().split(".")
    # the last base64url char of a 32-byte MAC carries 2 unused bits; setting
    # them decodes to the same bytes, so a lax decoder would accept it
    alphabet = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                "0123456789-_")
    last = alphabet.index(sig[-1])
    return f"{head}.{body}.{sig[:-1]}{alphabet[last | 1]}"


@pytest.mark.parametrize("token", [
    "alice",
    "a.b",
    "a.b.c.d",
    "!!!.???.***",
    "e30.e30.",
    "e30.\u00e9t\u00e9.AAAA",
    "eyJhbGciOiJIUzI1NiJ9.\u00e9t\u00e9.AAAA",
    "W10.e30.AAAA",
    "x" * 9000,
])
def test_malformed_tokens_are_refused_not_raised(host_validate, configured,
                                                 token):
    _refused(_validate(host_validate, token))


def test_duplicate_members_are_refused(host_validate, configured):
    _refused(_validate(host_validate, _duplicate_member_token()))


def test_a_non_canonical_signature_is_refused(host_validate, configured):
    _refused(_validate(host_validate, _non_canonical_signature()))


@pytest.mark.parametrize("bearer", [{"token": None}, {"token": "   "}, None])
def test_no_credential_is_refused(host_validate, configured, bearer):
    assert "missing" in _refused(host_validate(bearer))


# ---------------------------------------------------------------- the dev stub

def test_the_stub_is_refused_without_the_dev_flag(host_validate, clean_env):
    """No validator configured and no flag: every call is refused BY NAME, with
    a 503 that names the variables to set and the flag, never a silent Ok."""
    for token in ("alice", mint()):
        message = _refused(_validate(host_validate, token), status=503)
        assert "REVL_AUTH_HS256_SECRET" in message
        assert DEV_FLAG in message
    result = _validate(host_validate, "alice")
    assert result.value["code"] == "auth_not_configured"


def test_the_stub_runs_only_behind_the_dev_flag(host_validate, clean_env):
    clean_env.setenv(DEV_FLAG, "1")
    assert _validate(host_validate, "alice").value == {"subject": "alice"}
    _refused(host_validate({"token": None}))


@pytest.mark.parametrize("value", ["true", "yes", "0", " 1"])
def test_only_the_exact_dev_flag_enables_the_stub(host_validate, clean_env,
                                                  value):
    clean_env.setenv(DEV_FLAG, value)
    assert DEV_FLAG in _refused(_validate(host_validate, "alice"), status=503)


def test_the_dev_flag_next_to_a_real_key_is_refused(host_validate,
                                                    configured):
    configured.setenv(DEV_FLAG, "1")
    assert DEV_FLAG in _refused(_validate(host_validate, "alice"), status=503)
    _refused(_validate(host_validate, mint()), status=503)


@pytest.mark.parametrize("setup", [
    "short-key", "both-key-sources", "unreadable-key-file", "no-issuer",
    "no-audience", "negative-leeway", "non-ascii-leeway", "large-leeway",
])
def test_an_incomplete_configuration_is_refused_by_name(host_validate,
                                                        configured, tmp_path,
                                                        setup):
    if setup == "short-key":
        configured.setenv("REVL_AUTH_HS256_SECRET", "too-short")
    elif setup == "both-key-sources":
        configured.setenv("REVL_AUTH_HS256_SECRET_FILE", str(tmp_path / "k"))
    elif setup == "unreadable-key-file":
        configured.delenv("REVL_AUTH_HS256_SECRET")
        configured.setenv("REVL_AUTH_HS256_SECRET_FILE",
                          str(tmp_path / "missing"))
    elif setup == "no-issuer":
        configured.delenv("REVL_AUTH_ISSUER")
    elif setup == "no-audience":
        configured.delenv("REVL_AUTH_AUDIENCE")
    else:
        leeway = {"negative-leeway": "-5", "non-ascii-leeway": "\u00b2",
                  "large-leeway": "301"}[setup]
        configured.setenv("REVL_AUTH_LEEWAY", leeway)
    message = _refused(_validate(host_validate, mint()), status=503)
    assert message.startswith("stdlib/auth: ")


# ---------------------------------------------------------------- no key leaks

def _every_path(fn) -> list:
    """One call down every branch the validator has, valid and refused."""
    tokens = [mint(), mint(key="forged-" + "f" * 40),
              mint(exp=int(time.time()) - 1), mint(aud="other"),
              mint(iss="other"), mint(header={"alg": "none"}), "alice",
              _duplicate_member_token(), _non_canonical_signature()]
    return [fn({"token": t}) for t in tokens] + [fn({"token": None})]


@pytest.mark.parametrize("source", ["env", "file"])
def test_the_key_reaches_no_ir_refusal_or_log(host_validate, configured,
                                              tmp_path, caplog, capsys,
                                              source):
    if source == "file":
        key_file = tmp_path / "hs256.key"
        key_file.write_text(KEY, encoding="utf-8")
        configured.delenv("REVL_AUTH_HS256_SECRET")
        configured.setenv("REVL_AUTH_HS256_SECRET_FILE", str(key_file))
    caplog.set_level(logging.DEBUG)
    ir_text = json.dumps(compile_files([AUTH]))
    results = _every_path(host_validate)
    # misconfigured paths too: a short key must not be echoed either
    configured.setenv(DEV_FLAG, "1")
    results += _every_path(host_validate)
    rendered = json.dumps([r.value for r in results])
    captured = capsys.readouterr()
    for text in (ir_text, rendered, caplog.text, captured.out, captured.err):
        assert KEY not in text
        assert KEY[:16] not in text


def test_a_short_key_is_not_echoed_in_its_refusal(host_validate, configured):
    configured.setenv("REVL_AUTH_HS256_SECRET", "short-secret-1554")
    message = _refused(_validate(host_validate, mint()), status=503)
    assert "short-secret-1554" not in message


# ---------------------------------------------------------------- tiers

NOTES_APP = """\
use "stdlib/http.rvl" { ApiError }
use "stdlib/auth.rvl" { Auth, Bearer }

type Note = { id: Str, owner: Str, body: Str }

extern pure fn owned_note(who: Principal, id: Str) -> Result[Note, ApiError] = @py {
    subject = who.get("subject") if isinstance(who, dict) else None
    notes = {"n1": {"id": "n1", "owner": "alice", "body": "alice's note"}}
    n = notes.get(id)
    if n is None or n["owner"] != subject:
        return Err({"status": 404, "code": "not_found", "message": "no such note"})
    return Ok(n)
}

service NoteStore {
  fn get(who: Principal, id: Str) -> Result[Note, ApiError]
}

component Store provides store: NoteStore {
  provide store {
    fn get(who, id) = owned_note(who, id)
  }
}

service NotesApi {
  route get "/notes/{id}"
  fn get_note(bearer: Bearer, id: Str) -> Result[Note, ApiError]
}

component NotesHttp requires auth: Auth, store: NoteStore
                    provides notes_api: NotesApi {
  provide notes_api {
    fn get_note(bearer, id) {
      return match auth.validate(bearer) {
        Err(e) => Err(e),
        Ok(who) => store.get(who, id),
      }
    }
  }
}
"""


def _notes_app(tmp_path) -> str:
    app = tmp_path / "notes.rvl"
    app.write_text(NOTES_APP, encoding="utf-8")
    return str(app)


# The tier checks emit `stdlib/auth.rvl` on its own, so the only extern that
# can be named is the module's own `host_validate`.

def test_py_is_the_tier_that_ships_the_validator(tmp_path):
    out = tmp_path / "out.py"
    assert main(["emit", "--backend", "python", AUTH, "-o", str(out)]) == 0
    assert "hmac.compare_digest" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("backend, named", [
    ("typescript", "`host_validate` has no @ts body"),
    ("rust", "`host_validate` has no @rs body"),
    ("java", "`host_validate` has no @java body"),
    ("go", "`host_validate` has no @go body"),
    ("wasm", "type 'Principal' is not lowerable"),
])
def test_tiers_without_a_validator_refuse_by_name(tmp_path, capsys, backend,
                                                  named):
    out = tmp_path / f"out.{backend}"
    rc = main(["emit", "--backend", backend, AUTH, "-o", str(out)])
    assert rc != 0
    assert named in capsys.readouterr().err
    assert not out.exists()


# ---------------------------------------------------------------- live

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="standing a live composition up needs the cordis-py runtime; install "
           "it with `sh backends/python/setup.sh`")


def _live_face(tmp_path):
    from revl.mcp.http_face import HttpComposedServer
    from revl.mcp.session import Session
    from revl.mcp.surface import declared_param_types

    ir = compile_files([_notes_app(tmp_path), AUTH])
    session = Session()
    session.load(ir, {}, record=True, origin=None)
    return session, HttpComposedServer(
        session, declared=declared_param_types([_notes_app(tmp_path), AUTH]))


def _get(face, token=None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return face.dispatch_http("GET", "/notes/n1", b"", headers)


@needs_runtime
def test_live_route_serves_only_a_verified_subject(tmp_path, configured):
    session, face = _live_face(tmp_path)
    try:
        owner = _get(face, mint())
        assert owner.status == 200
        assert json.loads(owner.body)["owner"] == "alice"
        # the old stub's answer: a bare name is no longer that user
        assert _get(face, "alice").status == 401
        assert _get(face, mint(key="forged-" + "f" * 40)).status == 401
        assert _get(face, mint(exp=int(time.time()) - 1)).status == 401
        assert _get(face, mint(aud="other")).status == 401
        assert _get(face, mint(sub="mallory")).status == 404
        assert _get(face).status == 401
        timeline = json.dumps(session.timeline(), default=str)
        assert KEY not in timeline
    finally:
        session.unload()


@needs_runtime
def test_live_route_refuses_by_name_when_unconfigured(tmp_path, clean_env):
    session, face = _live_face(tmp_path)
    try:
        reply = _get(face, "alice")
        assert reply.status == 503
        assert DEV_FLAG in reply.body.decode("utf-8")
    finally:
        session.unload()


# ---------------------------------------------------------------- startup

# Design 569 question 5, decided for issue #1554: `revl serve --http` refuses to
# START with the dev stub flag set on a non-loopback bind, naming the flag and
# the address, before anything is loaded or bound. A per-call refusal would
# still leave an any-token face listening on the network.

class _FakeSession:
    loads: list = []

    def __init__(self):
        self.ir = None

    def load(self, ir, config, origin=None, **kwargs):
        self.ir = ir
        _FakeSession.loads.append(ir)


class _FakeHttpd:
    def __init__(self, host, port):
        self.server_address = (host, port)

    def serve_forever(self):
        return None

    def server_close(self):
        return None


@pytest.fixture
def no_network(monkeypatch):
    """Stand `revl serve --http` up with no runtime and no socket, recording
    whether it got as far as loading and binding."""
    binds: list = []
    _FakeSession.loads = []

    def fake_bind(face, host, port):
        binds.append(host)
        return _FakeHttpd(host, port)

    monkeypatch.setattr("revl.mcp.session.Session", _FakeSession)
    monkeypatch.setattr("revl.mcp.http_face.build_http_server", fake_bind)
    return binds


def _serve(host: str) -> int:
    return main(["serve", "--http", "--host", host, "--port", "0", AUTH])


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10",
                                  "no-such-host.invalid"])
def test_the_dev_stub_refuses_to_start_off_loopback(clean_env, no_network,
                                                    capsys, host):
    clean_env.setenv(DEV_FLAG, "1")
    assert _serve(host) != 0
    err = capsys.readouterr().err
    assert DEV_FLAG in err and repr(host) in err
    # refused before the composition was loaded or anything was bound
    assert no_network == [] and _FakeSession.loads == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1",
                                  "localhost"])
def test_the_dev_stub_still_starts_on_loopback(clean_env, no_network, host):
    clean_env.setenv(DEV_FLAG, "1")
    assert _serve(host) == 0
    assert no_network == [host] and len(_FakeSession.loads) == 1


def test_off_loopback_starts_without_the_dev_flag(configured, no_network):
    assert _serve("0.0.0.0") == 0
    assert no_network == ["0.0.0.0"]


def test_any_value_of_the_flag_refuses_off_loopback(clean_env, no_network):
    # auth.rvl refuses a flag other than "1" per call; the bind check fails
    # closed on any value at all
    clean_env.setenv(DEV_FLAG, "true")
    assert _serve("0.0.0.0") != 0
    assert no_network == []
