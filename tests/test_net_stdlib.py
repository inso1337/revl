"""The stdlib NET module - the outbound HTTPS client (plan slice R1,
stdlib/net.rvl, docs/capabilities.md for the capability scope).

The module is the missing provider-integration primitive: a typed HTTPS client
for JSON and application/x-www-form-urlencoded bodies, with custom headers, a
timeout, a bounded response, no redirect following, and the status, headers and
error returned AS DATA. What this suite pins:

  * the module imports through `use`, and its extern reaches the IR with the
    DESIGNED classification and destination: `emission` (a request leaves the
    system and cannot be reverted) with the argument-bound scope
    `net(host=host)` (issue #1985), so `revl audit` prints that the destination
    is the caller's `host` argument instead of an unnameable `*`;
  * py is the only tier with a body; emitting any other tier is refused at
    compile time and the refusal names `host_request` and the backend (the
    stdlib/auth.rvl per-tier pattern);
  * a real loopback HTTPS server (self-signed test certs, openssl) proves the
    wire contract: a JSON POST and a form POST arrive with the right method,
    headers and body; status and response headers come back as data; plain http,
    a host mismatch, a timeout and an oversized response are typed refusals
    before/without a wrong exchange; a redirect is returned, not followed; error
    text never carries the Authorization value;
  * the STATIC per-host reach (a trusted-module wrapper with
    `emission[net(host="127.0.0.1")]`, the documented pattern) is named on the
    `revl audit` surface, and its runtime destination check refuses another
    host before a socket opens;
  * the honest limit of this pin is pinned too: application code alone cannot
    mint a static host reach, because a user-origin `@py` body may not import
    the backend engine.

Every test in this file fails on the base commit for the same reason: the
module does not exist there, so the compile step cannot resolve
`stdlib/net.rvl`.
"""

from __future__ import annotations

import http.server
import json
import shutil
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import compiler as _compiler  # noqa: E402
from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import run_py  # noqa: E402

STDLIB = ROOT / "stdlib" / "net.rvl"
HTTP_STDLIB = ROOT / "stdlib" / "http.rvl"

if shutil.which("openssl") is None:  # the loopback test certs need the CLI
    pytest.skip("openssl CLI not available for loopback test certificates",
                allow_module_level=True)


# ---------------------------------------------------------------------------
# a loopback HTTPS server and its recording of what actually arrived
# ---------------------------------------------------------------------------

RECORDS: list[dict] = []


def _header(headers: dict, name: str):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _reply_header(headers: list, name: str):
    for row in headers:
        if row["name"].lower() == name.lower():
            return row["value"]
    return None


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - base signature
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, headers=()) -> None:
        try:
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # a client that timed out already hung up

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler spelling
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        body = self._read_body()
        path = self.path
        RECORDS.append({
            "method": self.command,
            "path": path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        if path == "/json":
            self._send(201, b'{"received":true}',
                       [("Content-Type", "application/json"),
                        ("X-Reply", "json-ok")])
        elif path == "/form":
            self._send(200, b'{"received":true}',
                       [("Content-Type", "application/json")])
        elif path == "/status":
            self._send(418, b"teapot", [("X-Reply", "teapot")])
        elif path == "/slow":
            time.sleep(0.6)
            self._send(200, b"slow")
        elif path == "/big":
            self._send(200, b"x" * 5000)
        elif path == "/redirect":
            self._send(302, b"moved", [("Location", "/target")])
        elif path == "/target":
            self._send(200, b"reached")
        else:
            self._send(200, b"hello")


class _TLSServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a timed-out client's broken pipe is not a test failure


class _Loopback:
    def __init__(self, port: int, ca: Path):
        self.port = port
        self.ca = ca
        self.host = "127.0.0.1"

    def url(self, path: str) -> str:
        return f"https://127.0.0.1:{self.port}{path}"


@pytest.fixture(scope="module")
def loopback(tmp_path_factory):
    from revl.placement import generate_seam_certs  # openssl CLI, test material
    certs = generate_seam_certs(tmp_path_factory.mktemp("net_certs"), ["server"])
    RECORDS.clear()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(certs["server"]["cert"], certs["server"]["key"])
    server = _TLSServer(("127.0.0.1", 0), _Handler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Loopback(port, Path(certs["server"]["ca"]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# consumers
#
# No stdlib byte-copy: `use "stdlib/net.rvl"` resolves through the compiler's
# stdlib search path to the REAL module, whose `@py` body is trusted to import
# the backends/python engine. The module file's content is pinned separately
# (test_module_file_is_the_documented_surface), so this is the same module.
# ---------------------------------------------------------------------------

def _ca_literal(path: Path) -> str:
    return json.dumps(str(path))  # a revl string literal; no quotes appear here


def _options(name: str, ca: Path, timeout_ms: int, max_bytes: int) -> str:
    return (
        f"fn {name}() -> NetOptions {{ return {{ timeout_ms: {timeout_ms}, "
        f"max_bytes: {max_bytes}, ca_file: Some({_ca_literal(ca)}) }} }}"
    )


def _live_consumer(base: str, ca: Path) -> str:
    return f'''\
use "stdlib/http.rvl" {{ Header }}
use "stdlib/net.rvl" {{
  NetField, NetError, NetOptions, NetResponse,
  get, post_json, post_form,
}}

{_options("options", ca, 2000, 1000000)}

{_options("tight_timeout", ca, 100, 1000000)}

{_options("tiny_bound", ca, 2000, 100)}

pub fn do_get(host: Str, url: Str, headers: List[Header])
    -> Result[NetResponse, NetError] {{
  return get(host, url, headers, options())
}}

pub fn do_get_with(host: Str, url: Str, headers: List[Header], opts: NetOptions)
    -> Result[NetResponse, NetError] {{
  return get(host, url, headers, opts)
}}

pub fn do_post_json(host: Str, url: Str, body: Str, headers: List[Header])
    -> Result[NetResponse, NetError] {{
  return post_json(host, url, body, headers, options())
}}

pub fn do_post_form(host: Str, url: Str, fields: List[NetField],
                    headers: List[Header])
    -> Result[NetResponse, NetError] {{
  return post_form(host, url, fields, headers, options())
}}

fn json_post_ok(host: Str, url: Str) -> Bool {{
  return match do_post_json(host, url, "123",
                            [{{ name: "X-Test", value: "one" }}]) {{
    Ok(r) => r.status == 201,
    Err(_) => false,
  }}
}}

test "a json post reaches the loopback server" {{
  assert json_post_ok("127.0.0.1", "{base}/json")
}}
'''


def _generic_audit_consumer() -> str:
    return '''\
use "stdlib/http.rvl" { Header }
use "stdlib/net.rvl" { NetError, NetOptions, NetResponse, get }

fn options() -> NetOptions {
  return { timeout_ms: 1000, max_bytes: 1000, ca_file: None }
}

// The application-level shape: the service method declares the same
// argument-bound token, with its own parameter spelled `host`.
service Ping {
  emission[net(host=host)] fn ping(host: Str, url: Str) -> Int
}

component PingClient provides ping: Ping {
  provide ping {
    fn ping(host, url) {
      let answer = emit get(host, url, [], options())
      return match answer {
        Ok(r) => r.status,
        Err(_) => 0,
      }
    }
  }
}
'''


def _compile_app(directory: Path, source: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "app.rvl"
    path.write_text(source, encoding="utf-8")
    return path, compile_files([str(path)])


def _exec_python(ir: dict) -> dict:
    emit = backend_emitter("python")
    namespace: dict = {}
    exec(compile(emit.emit(ir), "net_emitted.py", "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def live(loopback, tmp_path_factory):
    directory = tmp_path_factory.mktemp("net_live")
    path, ir = _compile_app(directory, _live_consumer(loopback.url(""), loopback.ca))
    return loopback, path, ir, _exec_python(ir)


def _ok(namespace: dict, result) -> dict:
    assert isinstance(result, namespace["Ok"]), result
    return result.value


def _err(namespace: dict, result) -> dict:
    assert isinstance(result, namespace["Err"]), result
    return result.value


# ---------------------------------------------------------------------------
# the module: classification, destination and surface
# ---------------------------------------------------------------------------

def test_module_imports_and_the_extern_binds_its_destination(tmp_path):
    _, ir = _compile_app(tmp_path / "surface", '''\
use "stdlib/http.rvl" { Header }
use "stdlib/net.rvl" { NetError, NetOptions, NetResponse, get }

fn options() -> NetOptions { return { timeout_ms: 1, max_bytes: 1, ca_file: None } }
fn f(host: Str, url: Str) -> Result[NetResponse, NetError] {
  return get(host, url, [], options())
}
''')
    externs = {e["name"]: e for e in ir["externs"]}
    assert set(externs) == {"host_request"}
    extern = externs["host_request"]
    # the DESIGNED classification: a request leaves the system, is not
    # revertible and does not read a local handle, so `emission` is the only
    # honest class (stdlib/crypto.rvl's random_token is the same argument).
    assert extern["class"] == "emission"
    # the destination is the caller's host argument, not a bare token and not a
    # private extern name (docs/capabilities.md; issue #1985).
    assert extern["capabilities"] == ["net(host=host)"]
    assert extern["returns"] == "NetReply"
    # py is the reference and the only tier with a body.
    assert set(extern["bodies"]) == {"py"}
    types = ir["types"]
    for name in ("NetOptions", "NetField", "NetResponse", "NetError",
                 "NetCall", "NetReply"):
        assert name in types, name


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    # the crossing and its destination binding: this spelling is the design.
    assert "pub extern emission[net(host=host)] fn host_request(host: Str, call: NetCall)" \
        in text
    # py is the only tier body; no other tier is silently claimed.
    assert "= @py {" in text
    assert "@ts {" not in text and "@rs {" not in text
    # the two body kinds and the trusted static-host pattern the header
    # documents (a constant destination token, a separate trusted extern).
    assert "application/json" in text
    assert "application/x-www-form-urlencoded" in text
    assert 'emission[net(host="api.example.com")]' in text
    assert "expected_host" in text
    # no em dashes (house style, docs/DOC-STATUS.md).
    assert "\u2014" not in text


def test_py_tier_emits_the_shared_engine(tmp_path):
    out = tmp_path / "net.py"
    assert main(["emit", "--backend", "python", str(STDLIB), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    # the body is the one-line bridge; the HTTP engine is shared, not copied.
    assert "from revl_net import perform" in text
    assert "expected_host=host" in text
    assert "def host_request(" in text


@pytest.mark.parametrize("backend, named", [
    ("typescript", "`host_request` has no @ts body"),
    ("rust", "`host_request` has no @rs body"),
    ("java", "`host_request` has no @java body"),
    ("go", "`host_request` has no @go body"),
])
def test_tiers_without_a_body_refuse_by_name(tmp_path, capsys, backend, named):
    out = tmp_path / f"net.{backend}"
    rc = main(["emit", "--backend", backend, str(STDLIB), "-o", str(out)])
    assert rc != 0
    assert named in capsys.readouterr().err
    assert not out.exists()


def test_wasm_refuses_at_lowering_before_this_module_is_the_reason(tmp_path,
                                                                  capsys):
    """Honest limit, pinned rather than hidden.

    The module imports stdlib/http.rvl for the shared `Header` record, and
    http.rvl itself does not lower on wasm today (an untyped empty list literal
    in its own constructors, a pre-existing frontend limit unrelated to this
    slice). So wasm is refused, but by lowering, before the missing-body
    refusal can name `host_request`. A later wasm lowering of http.rvl moves
    this test to the parametrized case above; it is not a claim that wasm can
    run the client."""
    out = tmp_path / "net.wasm"
    rc = main(["emit", "--backend", "wasm", str(STDLIB), "-o", str(out)])
    assert rc != 0
    assert not out.exists()
    # the refusal that DOES happen names the pre-existing http.rvl lowering
    # limit in front of it, not this module; on the base commit the module is
    # missing and the error is a `was not found` resolution failure instead.
    assert "empty list literal" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the py tier against the loopback server
# ---------------------------------------------------------------------------

def test_py_tier_runs_the_loopback_contract(live):
    """The canonical stdlib contract check: the module's own `test` block runs
    on the py tier through the standard runner, against the fake server."""
    _loopback, _path, ir, _ns = live
    verdict, detail = run_py(ir)
    assert verdict == "pass", detail


def test_json_post_arrives_with_method_headers_and_body(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    result = ns["do_post_json"](
        loopback.host,
        loopback.url("/json"),
        '{"hello":"world"}',
        [{"name": "X-Test", "value": "one"},
         {"name": "Authorization", "value": "Bearer abc123"}],
    )
    reply = _ok(ns, result)
    assert reply["status"] == 201
    assert reply["body"] == '{"received":true}'
    (record,) = RECORDS[mark:]
    assert record["method"] == "POST"
    assert record["path"] == "/json"
    assert record["body"] == b'{"hello":"world"}'
    assert _header(record["headers"], "content-type") == "application/json"
    assert _header(record["headers"], "x-test") == "one"
    assert _header(record["headers"], "authorization") == "Bearer abc123"


def test_form_post_arrives_urlencoded(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    result = ns["do_post_form"](
        loopback.host,
        loopback.url("/form"),
        [{"name": "a b", "value": "x&y"}, {"name": "only", "value": "1"}],
        [],
    )
    reply = _ok(ns, result)
    assert reply["status"] == 200
    (record,) = RECORDS[mark:]
    assert record["method"] == "POST"
    assert _header(record["headers"], "content-type") == \
        "application/x-www-form-urlencoded"
    # the stdlib urlencode form: space is `+`, `&` is escaped in a value.
    assert record["body"] == b"a+b=x%26y&only=1"
    assert parse_qs(record["body"].decode("utf-8")) == {"a b": ["x&y"], "only": ["1"]}


def test_status_and_response_headers_are_returned_as_data(live):
    loopback, _path, _ir, ns = live
    reply = _ok(ns, ns["do_get"](loopback.host, loopback.url("/status"), []))
    assert reply["status"] == 418
    assert reply["body"] == "teapot"
    assert _reply_header(reply["headers"], "X-Reply") == "teapot"


def test_plain_http_is_refused_before_a_socket_opens(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    refusal = _err(ns, ns["do_get"](
        loopback.host, f"http://127.0.0.1:{loopback.port}/x", []))
    assert refusal["code"] == "insecure_scheme"
    assert len(RECORDS) == mark  # nothing was sent


def test_a_url_that_does_not_match_the_declared_host_is_refused(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    refusal = _err(ns, ns["do_get"](
        "api.example.com", loopback.url("/json"), []))
    assert refusal["code"] == "host_mismatch"
    assert len(RECORDS) == mark  # nothing was sent


def test_userinfo_in_url_is_refused(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    refusal = _err(ns, ns["do_get"](
        loopback.host, f"https://user:pass@127.0.0.1:{loopback.port}/x", []))
    assert refusal["code"] == "credentials_in_url"
    assert "pass" not in refusal["message"]
    assert len(RECORDS) == mark


def test_header_injection_and_reserved_headers_are_refused(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    injected = _err(ns, ns["do_get"](loopback.host, loopback.url("/json"),
                                     [{"name": "X-Test",
                                       "value": "a\r\nInjected: 1"}]))
    assert injected["code"] == "invalid_header"
    reserved = _err(ns, ns["do_get"](loopback.host, loopback.url("/json"),
                                     [{"name": "Content-Type",
                                       "value": "text/plain"}]))
    assert reserved["code"] == "invalid_header"
    assert len(RECORDS) == mark  # neither was sent


def test_timeout_is_a_typed_error(live):
    loopback, _path, _ir, ns = live
    secret = "Bearer timeout-secret-91ba"
    refusal = _err(ns, ns["do_get_with"](
        loopback.host, loopback.url("/slow"),
        [{"name": "Authorization", "value": secret}], ns["tight_timeout"]()))
    assert refusal["code"] == "timeout"
    assert str(loopback.port) not in refusal["message"]
    assert "timeout-secret" not in json.dumps(refusal)


def test_oversized_response_is_refused(live):
    loopback, _path, _ir, ns = live
    refusal = _err(ns, ns["do_get_with"](
        loopback.host, loopback.url("/big"), [], ns["tiny_bound"]()))
    assert refusal["code"] == "response_too_large"


def test_redirect_is_returned_not_followed(live):
    loopback, _path, _ir, ns = live
    mark = len(RECORDS)
    reply = _ok(ns, ns["do_get"](loopback.host, loopback.url("/redirect"), []))
    assert reply["status"] == 302
    assert _reply_header(reply["headers"], "Location") == "/target"
    # no second request: the /target path was never hit.
    assert [record["path"] for record in RECORDS[mark:]] == ["/redirect"]


def test_error_text_never_carries_the_authorization_value(live):
    loopback, _path, _ir, ns = live
    # a port that was bound and released, so the connection is refused.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()

    secret = "Bearer DO-NOT-LEAK-7f3a"
    url = f"https://127.0.0.1:{closed_port}/private?token=DO-NOT-LEAK-7f3a"
    refusal = _err(ns, ns["do_get"](
        "127.0.0.1", url, [{"name": "Authorization", "value": secret}]))
    assert refusal["code"] in ("network_error", "protocol_error")
    # neither the header value nor the URL query is echoed into the error.
    assert "DO-NOT-LEAK" not in json.dumps(refusal)


# ---------------------------------------------------------------------------
# the STATIC named reach: a trusted-module wrapper, named on the audit surface
# ---------------------------------------------------------------------------

#: The runtime destination a static named wrapper declares. A constant
#: capability value may be any host string, including a loopback literal, which
#: lets the positive path run against the fake server.
NAMED_HOST = "127.0.0.1"

NAMED_WRAPPER = f'''\
use "stdlib/net.rvl" {{ NetCall, NetReply }}

// The documented static-per-host shape: a trusted module next to net.rvl, a
// CONSTANT destination token, and the one-line host body that enforces it with
// the shared engine.
pub extern emission[net(host="{NAMED_HOST}")] fn example_http(call: NetCall) -> NetReply
  = @py {{
    from revl_net import perform
    return perform(call, expected_host="{NAMED_HOST}")
  }}
'''


def _named_consumer(ca: Path) -> str:
    return f'''\
use "stdlib/http.rvl" {{ Header }}
use "stdlib/net.rvl" {{
  NetError, NetOptions, NetResponse, call_get, reply_to_result,
}}
use "stdlib/example_http.rvl" {{ example_http }}

{_options("options", ca, 2000, 1000000)}

pub fn example_get(url: Str, headers: List[Header])
    -> Result[NetResponse, NetError] {{
  return reply_to_result(example_http(call_get(url, headers, options())))
}}

service LocalPing {{
  emission[net(host="{NAMED_HOST}")] fn ping(url: Str) -> Int
}}

component LocalClient provides local: LocalPing {{
  provide local {{
    fn ping(url) {{
      let answer = emit example_get(url, [])
      return match answer {{
        Ok(r) => r.status,
        Err(_) => 0,
      }}
    }}
  }}
}}
'''


def _standin_install(tmp_path: Path, monkeypatch) -> Path:
    """A stand-in revl install tree whose stdlib is TRUSTED.

    A static named reach may only be declared in the trusted stdlib tree (the
    limitation the module header states), so the test builds one: net.rvl,
    http.rvl and the per-host wrapper module, with `stdlib_root()` patched at
    the compiler, the same stand-in pattern tests/test_multi_root_stdlib_ref_410
    uses. The application file stays user-origin, as a real app is."""
    install = tmp_path / "install"
    stdlib_dir = install / "stdlib"
    stdlib_dir.mkdir(parents=True)
    shutil.copy(STDLIB, stdlib_dir / "net.rvl")
    shutil.copy(HTTP_STDLIB, stdlib_dir / "http.rvl")
    (stdlib_dir / "example_http.rvl").write_text(NAMED_WRAPPER, encoding="utf-8")

    def _stdlib_root():
        return stdlib_dir

    monkeypatch.setattr(_compiler, "stdlib_root", _stdlib_root)
    return install


@pytest.fixture()
def named(loopback, tmp_path, monkeypatch):
    install = _standin_install(tmp_path, monkeypatch)
    assert install.is_dir()
    path, ir = _compile_app(tmp_path / "app", _named_consumer(loopback.ca))
    return loopback, path, ir, _exec_python(ir)


def test_static_named_wrapper_runs_and_refuses_another_host(named):
    loopback, _path, _ir, ns = named
    mark = len(RECORDS)
    reply = _ok(ns, ns["example_get"](loopback.url("/json"), []))
    assert reply["status"] == 201

    # the same server, reached through a host the constant token does not name:
    # refused BEFORE a socket opens.
    other_host_url = f"https://localhost:{loopback.port}/json"
    refusal = _err(ns, ns["example_get"](other_host_url, []))
    assert refusal["code"] == "host_mismatch"
    assert len(RECORDS) == mark + 1  # only the allowed request reached it


def test_audit_names_the_generic_argument_bound_reach(tmp_path, capsys):
    path, _ir = _compile_app(tmp_path / "generic_audit", _generic_audit_consumer())

    assert main(["audit", str(path), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    boundary = document["boundary"]["PingClient"]
    externs = {row["name"]: row for row in boundary["externs"]}
    assert externs["host_request"]["capabilities"] == ["net(host=host)"]
    assert externs["host_request"]["class"] == "emission"
    assert externs["host_request"]["backends"] == ["py"]

    assert main(["audit", str(path)]) == 0
    assert "host_request [net(host=host)]" in capsys.readouterr().out


def test_audit_names_the_host_for_the_static_wrapper(named, capsys):
    _loopback, path, _ir, _ns = named
    assert main(["audit", str(path), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    boundary = document["boundary"]["LocalClient"]
    externs = {row["name"]: row for row in boundary["externs"]}
    # the host is IN the token: an operator can bound an approval to it.
    assert externs["example_http"]["capabilities"] == \
        [f'net(host="{NAMED_HOST}")']
    assert externs["example_http"]["class"] == "emission"
    assert externs["example_http"]["backends"] == ["py"]
    # the argument-bound generic crossing is NOT in this component's reach.
    assert "host_request" not in externs

    assert main(["audit", str(path)]) == 0
    text = capsys.readouterr().out
    assert f'example_http [net(host="{NAMED_HOST}")]' in text
    assert "host_request [net" not in text


def test_application_code_cannot_mint_a_static_host_reach(tmp_path):
    """The honest limit of this pin, pinned: a static named reach needs a
    trusted module.

    Capability tokens compare by identity, so a named reach cannot be derived
    over the generic extern; and a user-origin `@py` body may not import a
    backend support module. Application code that tries is refused at compile
    time, which is exactly why the wrappers are trusted-module contributions."""
    source = '''\
use "stdlib/net.rvl" { NetCall, NetReply }

extern emission[net(host="api.example.com")] fn app_http(call: NetCall) -> NetReply
  = @py {
    from revl_net import perform
    return perform(call, expected_host="api.example.com")
  }
'''
    path = tmp_path / "app_static.rvl"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(path)])
    message = str(excinfo.value)
    assert "user-origin @py body" in message
    assert "revl_net" in message
