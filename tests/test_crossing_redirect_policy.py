"""The redirect policy an EMITTED crossing carries (`revl.crossing_redirect`).

Two constructs in this tree generate a host body that talks HTTP to a peer
named in a declaration: `revl import a2a` (the A2A `message/send` crossing) and
the composition `remote` row. Both used to hand the request to their tier's
default HTTP client, and every default follows: `urllib.request.urlopen`
follows and re-issues a 301/302/303 POST as a GET with the body dropped;
`fetch` defaults to `redirect: "follow"` and the Fetch standard says the same
about the method.

That default contradicts the declaration. The endpoint is part of what the
generated file DECLARES and what the composition ADMITS — the A2A importer
derives its `net.<host>` reach bound from the endpoint's host alone, and a
`remote` row writes `at host("h:port")` so an operator can read where the
crossing goes. So a redirect is a REFUSAL CONDITION rather than a transport
detail, and this file executes that against real loopback servers rather than
asserting it about the emitted text.

Every test runs the crossing the compiler actually emits, extracted from the
IR, against two loopback peers: the DECLARED one and a second standing in for
somewhere else. Three claims per tier:

  * a 302 to the second peer is refused, the diagnostic names the rule, and
    the second peer is never contacted at all;
  * a POST is never retried as a GET — a 301/302/303 is refused even when it
    stays on the declared origin, and even when following is declared;
  * a declared follow (`--follow-redirects`, `redirect(same_origin)`) is
    bounded to the declared origin and to the two method-preserving codes,
    and re-issues the SAME method with the SAME body.

Non-vacuity is executed, not argued. `_following_restored` puts each tier's
old default back into the emitted body by the smallest edit that expresses it
(`_opener.open` -> `_req.urlopen`; `redirect: "manual"` -> `"follow"`) and the
paired test asserts the crossing then does reach the second peer and consume
its answer. If the fixture ever stopped redirecting, those tests would fail
and say so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.import_a2a import import_a2a  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.synthesize import synthesize_provider  # noqa: E402

# The reply both peers give when they answer rather than redirect. The `text`
# is how a test tells WHICH peer's answer was consumed.
_A2A_OK = {"jsonrpc": "2.0", "id": "1", "result": {
    "kind": "message", "role": "agent", "messageId": "m-1",
    "parts": [{"kind": "text", "text": "PEER"}]}}


# ------------------------------------------------------------------ two peers

class _Peer:
    """A loopback HTTP peer that records every request it is asked to serve.

    `respond(peer, method, path, body)` returns `(status, headers, payload)`.
    The recording is the load-bearing part: "the second peer was never
    contacted" is the claim, and it is only checkable if the peer would have
    noticed.
    """

    def __init__(self, respond):
        self.seen: list[tuple[str, str, bytes]] = []
        peer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                peer.seen.append((self.command, self.path, body))
                status, headers, payload = respond(
                    peer, self.command, self.path, body)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = _serve
            do_POST = _serve
            do_PUT = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _answer(text: str):
    """Answer a JSON-RPC `message/send` with a direct message reply."""
    def respond(peer, method, path, body):
        reply = json.loads(json.dumps(_A2A_OK))
        reply["result"]["parts"][0]["text"] = text
        return 200, {"content-type": "application/json"}, json.dumps(reply).encode()
    return respond


def _bridge_answer(value):
    """Answer the composition bridge envelope (`{"ok","value"}`)."""
    def respond(peer, method, path, body):
        payload = json.dumps({"ok": True, "value": value}).encode()
        return 200, {"content-type": "application/json"}, payload
    return respond


def _redirect_to(code: int, target: str, otherwise):
    """Redirect the FIRST request with `code`, then behave like `otherwise`.

    Redirecting only the first request is what lets a declared same-origin
    follow land somewhere that answers, and it keeps a restored-default run
    from looping.
    """
    def respond(peer, method, path, body):
        if len(peer.seen) == 1:
            return code, {"location": target}, b""
        return otherwise(peer, method, path, body)
    return respond


@pytest.fixture
def peers():
    made: list[_Peer] = []

    def make(respond) -> _Peer:
        peer = _Peer(respond)
        made.append(peer)
        return peer

    try:
        yield make
    finally:
        for peer in made:
            peer.close()


# ----------------------------------------------------- the emitted py crossing

def _a2a_py_body(endpoint: str, *, follow: bool) -> str:
    """The py host body `revl import a2a` emits for `endpoint`, out of the IR.

    Nothing here is hand-written: the card goes through the importer and the
    compiler, and what is executed below is the body an operator would ship.
    """
    card = {
        "protocolVersion": "1.0.0",
        "name": "Redirect Probe",
        "url": endpoint,
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "probe", "name": "probe",
                    "description": "one crossing",
                    "tags": ["probe"]}],
    }
    source = import_a2a(card, filename="probe.json", backend="py",
                        allow_plaintext=True, follow_redirects=follow)
    ir = compile_source(source, "probe.rvl")
    return ir["externs"][0]["bodies"]["py"]


_REMOTE_SERVICE = """
service Probe {
  emission fn ping(note: Str) -> Str
}
"""


def _remote_py_body(host: str, *, redirect: str) -> str:
    """The py host body a composition `remote` row synthesizes, out of the IR.

    Only the URL's SCHEME is rewritten afterwards, from `https` to `http`, so
    the crossing can reach a loopback server without a certificate. The policy
    under test is byte-for-byte the emitted one, and it is scheme-agnostic —
    the same-origin comparison includes the scheme either way.
    """
    service = Parser(_REMOTE_SERVICE, "probe.rvl").parse().services[0]
    _component, text = synthesize_provider(service, "remote", {
        "label": "probe", "key": "probe", "host": host, "realm": None,
        "capability": "net.probe", "on_failure": "withdraw",
        "transport": None, "redirect": redirect,
        "doc": "probe.rvl", "line": 1,
    })
    ir = compile_source(_REMOTE_SERVICE + "\n" + text, "probe.rvl")
    body = ir["externs"][0]["bodies"]["py"]
    assert f'"https://{host}/probe/ping"' in body
    return body.replace(f'"https://{host}/probe/ping"',
                        f'"http://{host}/probe/ping"')


def _run_py(body: str, **params: object):
    """Execute an emitted py body as its own function, for real, over TCP.

    The parameter names are the ones the emitter chose (`message` for an A2A
    skill, the method's own parameter for a `remote` row), so they are passed
    by keyword rather than guessed.
    """
    names = ", ".join(params)
    indented = textwrap.indent(textwrap.dedent(body), "    ")
    namespace: dict = {"__name__": "generated"}
    exec(compile(f"def _crossing({names}):\n{indented}\n", "<body>", "exec"),
         namespace)
    return namespace["_crossing"](**params)


def _following_restored(body: str) -> str:
    """The pre-fix body: urllib's own opener, which follows by default.

    Used only to prove the fixture is not vacuous. One substitution, and it is
    exactly the line the fix changed.
    """
    restored = body.replace("_opener.open(_r, ", "_req.urlopen(_r, ")
    assert restored != body, "the emitted body no longer opens through _opener"
    return restored


# ------------------------------------------- py: refusal, and its non-vacuity

def test_py_a2a_refuses_a_302_to_another_host_and_never_contacts_it(peers):
    elsewhere = peers(_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/elsewhere",
                                  _answer("DECLARED")))

    body = _a2a_py_body(f"{declared.origin}/a2a", follow=False)
    with pytest.raises(RuntimeError) as excinfo:
        _run_py(body, message="ping")

    message = str(excinfo.value)
    assert "a2a: redirect refused" in message
    assert "HTTP 302" in message
    assert elsewhere.origin in message, "the refusal names where it refused to go"
    # The whole point: the crossing declared `net.<declared-host>` and the
    # process must not have reached anywhere else.
    assert elsewhere.seen == []
    assert [m for m, _p, _b in declared.seen] == ["POST"]


def test_py_a2a_would_have_followed_before_the_fix(peers):
    """Non-vacuity. The same two peers, with urllib's default put back.

    This is the defect, executed: the POST is re-issued as a GET, the body is
    dropped, it lands on a host the declaration never named, and the extern
    consumes that host's reply as the A2A result.
    """
    elsewhere = peers(_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/elsewhere",
                                  _answer("DECLARED")))

    body = _following_restored(_a2a_py_body(f"{declared.origin}/a2a",
                                            follow=False))
    assert _run_py(body, message="ping") == "ELSEWHERE"
    assert [(m, b) for m, _p, b in elsewhere.seen] == [("GET", b"")]


@pytest.mark.parametrize("code", [301, 302, 303])
def test_py_a2a_refuses_a_method_changing_redirect_even_same_origin(peers, code):
    """A POST is never silently retried as a GET, whatever the flag says."""
    declared = peers(_redirect_to(code, "/moved", _answer("DECLARED")))
    for follow in (False, True):
        body = _a2a_py_body(f"{declared.origin}/a2a", follow=follow)
        declared.seen.clear()
        with pytest.raises(RuntimeError) as excinfo:
            _run_py(body, message="ping")
        message = str(excinfo.value)
        assert "a2a: redirect refused" in message
        assert f"HTTP {code}" in message
        assert [m for m, _p, _b in declared.seen] == ["POST"], (
            "the crossing must not have been re-issued at all")


def test_py_a2a_undeclared_follow_refuses_even_a_same_origin_307(peers):
    declared = peers(_redirect_to(307, "/moved", _answer("DECLARED")))
    body = _a2a_py_body(f"{declared.origin}/a2a", follow=False)
    with pytest.raises(RuntimeError) as excinfo:
        _run_py(body, message="ping")
    assert "not declared on this crossing" in str(excinfo.value)
    assert len(declared.seen) == 1


def test_py_a2a_declared_follow_is_same_origin_only(peers):
    elsewhere = peers(_answer("ELSEWHERE"))
    declared = peers(_redirect_to(307, f"{elsewhere.origin}/elsewhere",
                                  _answer("DECLARED")))
    body = _a2a_py_body(f"{declared.origin}/a2a", follow=True)
    with pytest.raises(RuntimeError) as excinfo:
        _run_py(body, message="ping")
    assert "SAME-ORIGIN only" in str(excinfo.value)
    assert elsewhere.seen == []


def test_py_a2a_declared_follow_keeps_the_method_and_the_body(peers):
    """The one case that IS followed, and the two properties it must keep."""
    declared = peers(_redirect_to(307, "/moved", _answer("DECLARED")))
    body = _a2a_py_body(f"{declared.origin}/a2a", follow=True)
    assert _run_py(body, message="ping") == "DECLARED"

    assert [(m, p) for m, p, _b in declared.seen] == [
        ("POST", "/a2a"), ("POST", "/moved")]
    first, second = declared.seen[0][2], declared.seen[1][2]
    assert second == first and json.loads(second)["method"] == "message/send"


# ------------------------------------------------ py: the composition `remote` row

def test_py_remote_row_refuses_a_302_to_another_host_and_never_contacts_it(peers):
    elsewhere = peers(_bridge_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/elsewhere",
                                  _bridge_answer("DECLARED")))

    body = _remote_py_body(f"127.0.0.1:{declared.port}", redirect="refuse")
    with pytest.raises(RuntimeError) as excinfo:
        _run_py(body, note="note")

    message = str(excinfo.value)
    assert "remote: redirect refused" in message
    assert "HTTP 302" in message
    assert elsewhere.origin in message
    assert elsewhere.seen == []
    # NOT flattened into the row's `on_failure` transport diagnostic.
    assert "transport failure" not in message


def test_py_remote_row_would_have_followed_before_the_fix(peers):
    """Non-vacuity for the `remote` row, same shape."""
    elsewhere = peers(_bridge_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/elsewhere",
                                  _bridge_answer("DECLARED")))

    body = _following_restored(
        _remote_py_body(f"127.0.0.1:{declared.port}", redirect="refuse"))
    assert _run_py(body, note="note") == "ELSEWHERE"
    assert [(m, b) for m, _p, b in elsewhere.seen] == [("GET", b"")]


def test_py_remote_row_declared_same_origin_follow_keeps_the_method(peers):
    declared = peers(_redirect_to(308, "/moved", _bridge_answer("DECLARED")))
    body = _remote_py_body(f"127.0.0.1:{declared.port}",
                           redirect="same_origin")
    assert _run_py(body, note="note") == "DECLARED"
    assert [m for m, _p, _b in declared.seen] == ["POST", "POST"]
    assert declared.seen[0][2] == declared.seen[1][2]


def test_py_remote_row_refusal_is_a_fault_under_on_failure_result(peers):
    """`on_failure(result)` classifies the DECLARED crossing's failure. A
    redirect is the peer declining to be the declared endpoint, so it stays a
    fault rather than becoming a quiet `Err` a caller can ignore."""
    elsewhere = peers(_bridge_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/x",
                                  _bridge_answer("DECLARED")))
    service = Parser("""
service Probe {
  emission fn ping(note: Str) -> Result[Str, Str]
}
""", "probe.rvl").parse().services[0]
    host = f"127.0.0.1:{declared.port}"
    _component, text = synthesize_provider(service, "remote", {
        "label": "probe", "key": "probe", "host": host, "realm": None,
        "capability": "net.probe", "on_failure": "result", "transport": None,
        "redirect": "refuse", "doc": "probe.rvl", "line": 1,
    })
    ir = compile_source(
        "service Probe {\n  emission fn ping(note: Str) -> Result[Str, Str]\n}\n"
        + text, "probe.rvl")
    body = ir["externs"][0]["bodies"]["py"].replace(
        f'"https://{host}/probe/ping"', f'"http://{host}/probe/ping"')
    with pytest.raises(RuntimeError) as excinfo:
        _run_py(body, note="note")
    assert "remote: redirect refused" in str(excinfo.value)
    assert elsewhere.seen == []


# ----------------------------------------------------- the emitted ts crossing

_TS_DRIVER = """
async function crossing(message: string): Promise<string> {
%s
}

crossing("ping").then(
  (text) => console.log(JSON.stringify({ ok: true, text })),
  (error) => console.log(JSON.stringify({
    ok: false, error: String((error && error.message) || error) })),
);
"""


def _run_ts(body: str, tmp_path: Path) -> dict:
    module = tmp_path / "crossing.ts"
    module.write_text(_TS_DRIVER % textwrap.indent(textwrap.dedent(body), "  "),
                      encoding="utf-8")
    # Node strips the type annotations natively; nothing here is transpiled,
    # so what runs is the emitted body.
    done = subprocess.run([shutil.which("node"), str(module)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _a2a_ts_body(endpoint: str, *, follow: bool) -> str:
    card = {
        "protocolVersion": "1.0.0",
        "name": "Redirect Probe",
        "url": endpoint,
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "probe", "name": "probe",
                    "description": "one crossing",
                    "tags": ["probe"]}],
    }
    source = import_a2a(card, filename="probe.json", backend="ts",
                        allow_plaintext=True, follow_redirects=follow)
    ir = compile_source(source, "probe.rvl")
    return ir["externs"][0]["bodies"]["ts"]


pytestmark_node = pytest.mark.skipif(shutil.which("node") is None,
                                     reason="node not on PATH")


@pytestmark_node
def test_ts_a2a_refuses_a_302_to_another_host_and_never_contacts_it(peers, tmp_path):
    elsewhere = peers(_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/elsewhere",
                                  _answer("DECLARED")))

    got = _run_ts(_a2a_ts_body(f"{declared.origin}/a2a", follow=False), tmp_path)
    assert got["ok"] is False
    assert "a2a: redirect refused" in got["error"]
    assert "HTTP 302" in got["error"]
    assert elsewhere.seen == []
    assert [m for m, _p, _b in declared.seen] == ["POST"]


@pytestmark_node
def test_ts_a2a_would_have_followed_before_the_fix(peers, tmp_path):
    """Non-vacuity, ts. `redirect: "manual"` back to the Fetch default.

    With `follow` restored the runtime chases the `Location` itself, the loop
    below never sees a 3xx, and the crossing consumes the other host's reply —
    the POST having become a GET on the way.
    """
    elsewhere = peers(_answer("ELSEWHERE"))
    declared = peers(_redirect_to(302, f"{elsewhere.origin}/elsewhere",
                                  _answer("DECLARED")))

    body = _a2a_ts_body(f"{declared.origin}/a2a", follow=False)
    restored = body.replace('redirect: "manual"', 'redirect: "follow"')
    assert restored != body, "the emitted body no longer pins redirect handling"

    got = _run_ts(restored, tmp_path)
    assert got == {"ok": True, "text": "ELSEWHERE"}
    assert [(m, b) for m, _p, b in elsewhere.seen] == [("GET", b"")]


@pytestmark_node
@pytest.mark.parametrize("code", [301, 302, 303])
def test_ts_a2a_refuses_a_method_changing_redirect_even_same_origin(
        peers, tmp_path, code):
    declared = peers(_redirect_to(code, "/moved", _answer("DECLARED")))
    for follow in (False, True):
        declared.seen.clear()
        got = _run_ts(_a2a_ts_body(f"{declared.origin}/a2a", follow=follow),
                      tmp_path)
        assert got["ok"] is False
        assert "a2a: redirect refused" in got["error"]
        assert f"HTTP {code}" in got["error"]
        assert [m for m, _p, _b in declared.seen] == ["POST"]


@pytestmark_node
def test_ts_a2a_declared_follow_is_same_origin_only(peers, tmp_path):
    elsewhere = peers(_answer("ELSEWHERE"))
    declared = peers(_redirect_to(307, f"{elsewhere.origin}/elsewhere",
                                  _answer("DECLARED")))
    got = _run_ts(_a2a_ts_body(f"{declared.origin}/a2a", follow=True), tmp_path)
    assert got["ok"] is False
    assert "SAME-ORIGIN only" in got["error"]
    assert elsewhere.seen == []


@pytestmark_node
def test_ts_a2a_declared_follow_keeps_the_method_and_the_body(peers, tmp_path):
    declared = peers(_redirect_to(307, "/moved", _answer("DECLARED")))
    got = _run_ts(_a2a_ts_body(f"{declared.origin}/a2a", follow=True), tmp_path)
    assert got == {"ok": True, "text": "DECLARED"}
    assert [(m, p) for m, p, _b in declared.seen] == [
        ("POST", "/a2a"), ("POST", "/moved")]
    assert declared.seen[0][2] == declared.seen[1][2]


# ------------------------------------------------- the surface, and the defaults

def test_the_default_is_refuse_on_every_construct_that_emits_a_crossing():
    """Nothing follows unless it was DECLARED, on either construct.

    A tier that gains an emitted crossing later gets its answer from
    `revl.crossing_redirect`, which is why the policy lives there rather than
    twice in two emitters.
    """
    from revl import crossing_redirect

    assert "_follow_redirects = False" in crossing_redirect.py_policy(
        "x", follow=False)
    assert "xFollowRedirects = false" in crossing_redirect.ts_policy(
        "x", follow=False, url_expr='"u"', send="send")

    endpoint = "http://127.0.0.1:1/a2a"
    assert "_follow_redirects = False" in _a2a_py_body(endpoint, follow=False)
    assert "a2aFollowRedirects = false" in _a2a_ts_body(endpoint, follow=False)

    service = Parser(_REMOTE_SERVICE, "probe.rvl").parse().services[0]
    _component, text = synthesize_provider(service, "remote", {
        "label": "probe", "key": "probe", "host": "peer.internal:8443",
        "realm": None, "capability": "net.probe", "on_failure": "withdraw",
        "transport": None, "doc": "probe.rvl", "line": 1,
    })
    # No `redirect` key at all: the synthesizer must not fall open.
    assert "_follow_redirects = False" in text
    assert "Redirect: `refuse`" in text


def test_every_tier_that_emits_an_a2a_crossing_carries_the_policy():
    """A tier added to `_BODIES` without a redirect policy fails here.

    The defect was not that one body was wrong, it was that each body reached
    for its tier's default and every default follows. `go` (`http.Client`,
    follows, `CheckRedirect` to stop) and `rust` (`reqwest`, follows, ten hops)
    would both arrive with the same hole, so the check is over the body table
    rather than over the two bodies that exist today.
    """
    from revl.import_a2a import _BODIES

    endpoint = "http://127.0.0.1:1/a2a"
    for backend, emit in _BODIES.items():
        body = emit(endpoint, "probe", follow_redirects=False)
        assert "redirect refused" in body, backend
        assert "same-origin" in body.lower(), backend


def test_the_remote_row_declares_the_policy_and_the_row_table_carries_it(tmp_path):
    from revl.composition import resolve_file

    (tmp_path / "services.rvl").write_text(_REMOTE_SERVICE, encoding="utf-8")
    (tmp_path / "base.rvl").write_text("""
composition App {
  use "services.rvl"
  remote @a provides probe: Probe at host("peer.internal:8443")
  remote @b provides probe2: Probe at host("peer.internal:8443")
    redirect(same_origin)
}
""", encoding="utf-8")
    table = resolve_file(str(tmp_path / "base.rvl"), str(tmp_path))
    policy = {row.label: row.remote["redirect"] for row in table.rows}
    assert policy == {"a": "refuse", "b": "same_origin"}

    text = table.sources[
        next(r.source for r in table.rows if r.label == "b")]
    assert "_follow_redirects = True" in text
    assert "Redirect: `same_origin`" in text


def test_the_remote_row_takes_only_the_two_spellings():
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition App {
  remote @a provides probe: Probe at host("peer.internal:8443") redirect(follow)
}
""", "base.rvl").parse()
    message = str(excinfo.value)
    assert "`redirect` takes `refuse` or `same_origin`" in message
    assert "301/302/303" in message
