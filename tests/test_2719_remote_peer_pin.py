"""Issue #2719: a `remote` row can pin the peer's TLS leaf SPKI.

The pin binds the key the WIRE already presents. It adds no signature to the
canonical envelope and no revocation source: the ordinary TLS chain and
hostname check runs first, and only then does the emitted body compare the
leaf certificate's SubjectPublicKeyInfo (SPKI) with the pin. A mismatch is a
`TransportFault` naming both fingerprints, so the row's `on_failure` settles
it (withdrawal by default, an in-band `Err` under `result`).

The fingerprint is the FULL SHA-256 hex of the SPKI. The expected values in
this file are computed with the `openssl` CLI, not with `revl.tls_pin`, so the
runtime path is checked against an independent encoder rather than against the
same walker twice.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source, tls_pin  # noqa: E402
from revl.composition import resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.placement import generate_seam_certs  # noqa: E402
from revl.synthesize import synthesize_provider  # noqa: E402

if shutil.which("openssl") is None:  # the cert minting needs the openssl CLI
    pytest.skip("openssl CLI not available for test cert generation",
                allow_module_level=True)

PROBE = """
service Probe {
  emission fn ping(note: Str) -> Str
}
"""

PROBE_RESULT = """
service ProbeResult {
  emission fn ping(note: Str) -> Result[Str, Str]
}
"""

PIN_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90" * 2


def write(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / f"{name}.rvl").write_text(text)
    return tmp_path


def composition(pin: str | None, *, label: str = "probe",
                service: str = "Probe") -> str:
    clause = f'\n    pinned "{pin}"' if pin is not None else ""
    return f"""
composition Shop {{
  use "services.rvl"
  remote @{label} provides {label}: {service}
    at host("127.0.0.1:8443"){clause}
}}
"""


def resolve(tmp_path: Path, doc: str = "base"):
    return resolve_file(str(tmp_path / f"{doc}.rvl"), str(tmp_path))


# ------------------------------------------------------------------- admission

def test_a_pinned_clause_parses_and_is_an_admission_fact(tmp_path):
    program = Parser("""
composition Shop {
  remote @probe provides probe: Probe
    at host("127.0.0.1:8443")
    pinned "A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F90"
}
""", "t.rvl").parse()
    row = program.compositions[0].remotes[0]
    assert row.pin == PIN_HEX.upper()
    assert row.pin_line > 0

    write(tmp_path, services=PROBE, base=composition(PIN_HEX))
    table = resolve(tmp_path)
    assert table.rows[0].remote["pin"] == PIN_HEX
    # The fact is in the IR and the manifest, which is what `revl audit` and a
    # consumer of the document read; an upper-case spelling is normalised.
    assert table.to_ir()["rows"][0]["remote"]["pin"] == PIN_HEX
    assert f"PINNED PEER IDENTITY: sha256:{PIN_HEX}" in table.sources[
        ".revl/synthesized/_project/probe.remote.rvl"]


@pytest.mark.parametrize("bad", [
    "",                              # an empty digest is not a digest
    "a1b2c3d4",                       # short: must not be padded
    "zz" * 32,                        # not hex
    "a1b2c3d4e5f60718293a4b5c6d7e8f90" * 2 + "00",  # too long
])
def test_a_malformed_pin_is_refused_at_admission(tmp_path, bad):
    write(tmp_path, services=PROBE, base=composition(bad))
    with pytest.raises(RevlError) as raised:
        resolve(tmp_path)
    message = str(raised.value)
    assert "malformed" in message
    assert "64 hex characters" in message


def test_a_duplicate_pinned_clause_is_refused():
    with pytest.raises(RevlError) as raised:
        Parser(f"""
composition Shop {{
  remote @probe provides probe: Probe
    at host("127.0.0.1:8443")
    pinned "{PIN_HEX}"
    pinned "{PIN_HEX}"
}}
""", "t.rvl").parse()
    assert "duplicate `pinned`" in str(raised.value)


def test_a_row_without_a_pin_carries_no_pin_fact_and_no_handler(tmp_path):
    write(tmp_path, services=PROBE, base=composition(None))
    table = resolve(tmp_path)
    assert "pin" not in table.rows[0].remote
    assert "_PinnedHTTPSHandler" not in table.sources[
        ".revl/synthesized/_project/probe.remote.rvl"]
    assert "pin" not in table.to_ir()["rows"][0]["remote"]


# --------------------------------------------------------- the SPKI digest

def test_the_openssl_and_in_tree_spki_walks_agree(certs):
    """The emitted walker must hash exactly what `openssl` calls the SPKI.

    If these disagreed, the pin an operator computes from a certificate would
    never match the one the body checks, and every pinned row would refuse.
    """
    for name in ("peer_a", "peer_b"):
        cert = certs[name]["cert"]
        der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text(encoding="utf-8"))
        assert tls_pin.spki_sha256(der) == openssl_pin(cert)
        assert len(openssl_pin(cert)) == 64


def test_the_pin_normaliser_accepts_case_and_refuses_a_short_digest():
    assert tls_pin.normalize_pin(PIN_HEX.upper()) == PIN_HEX
    with pytest.raises(ValueError):
        tls_pin.normalize_pin(PIN_HEX[:16])


# ------------------------------------------------------------ the runtime

class _Ok:
    def __init__(self, value):
        self.value = value


class _Err:
    def __init__(self, error):
        self.error = error


class _TlsPeer:
    """One loopback HTTPS peer, with its own certificate and key."""

    def __init__(self, cert: str, key: str, value: str):
        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                self.server.seen.append((self.path, body))
                payload = json.dumps({"ok": True, "value": value}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.seen = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.httpd.socket = context.wrap_socket(self.httpd.socket,
                                                server_side=True)
        self.port = self.httpd.server_port
        self.host = f"127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    out = tmp_path_factory.mktemp("revl_pin_certs")
    return generate_seam_certs(out, ["peer_a", "peer_b"])


def openssl_pin(cert: str) -> str:
    """The full SHA-256 hex of a certificate's SPKI, through openssl alone."""
    pub = subprocess.run(
        ["openssl", "x509", "-in", cert, "-pubkey", "-noout"],
        capture_output=True, check=True).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input=pub, capture_output=True, check=True).stdout
    return hashlib.sha256(der).hexdigest()


def body(host: str, pin: str | None, *, service: str = PROBE,
         on_failure: str = "withdraw", transport: str | None = None) -> str:
    svc = Parser(service, "probe.rvl").parse().services[0]
    _component, text = synthesize_provider(svc, "remote", {
        "label": "probe", "key": "probe", "host": host, "realm": None,
        "capability": "net.probe", "on_failure": on_failure,
        "transport": transport, "redirect": "refuse", "pin": pin,
        "doc": "probe.rvl", "line": 1,
    })
    ir = compile_source(service + "\n" + text, "probe.rvl")
    return ir["externs"][0]["bodies"]["py"]


def run(body_src: str, extra: dict | None = None, **params):
    names = ", ".join(params)
    indented = textwrap.indent(textwrap.dedent(body_src), "    ")
    namespace: dict = {"__name__": "generated"}
    namespace.update(extra or {})
    exec(compile(f"def _crossing({names}):\n{indented}\n", "<body>", "exec"),
         namespace)
    return namespace["_crossing"](**params)


def test_a_matching_key_is_admitted(certs, monkeypatch):
    pin = openssl_pin(certs["peer_a"]["cert"])
    peer = _TlsPeer(certs["peer_a"]["cert"], certs["peer_a"]["key"], "pong-a")
    # The generated body keeps the ordinary chain check, so the test trusts the
    # throwaway CA exactly as a deployment trusts its own.
    monkeypatch.setenv("SSL_CERT_FILE", certs["peer_a"]["ca"])
    try:
        assert run(body(peer.host, pin), note="hi") == "pong-a"
        assert peer.httpd.seen, "the crossing never reached the peer"
    finally:
        peer.close()


def test_another_key_is_refused_naming_both_fingerprints(certs, monkeypatch):
    expected = openssl_pin(certs["peer_a"]["cert"])
    presented = openssl_pin(certs["peer_b"]["cert"])
    assert expected != presented
    peer = _TlsPeer(certs["peer_b"]["cert"], certs["peer_b"]["key"], "pong-b")
    monkeypatch.setenv("SSL_CERT_FILE", certs["peer_b"]["ca"])
    try:
        with pytest.raises(RuntimeError) as raised:
            run(body(peer.host, expected), note="hi")
    finally:
        peer.close()
    message = str(raised.value)
    assert "expected sha256:" + expected in message
    assert "presented sha256:" + presented in message
    assert getattr(raised.value, "_revl_transport_fault", False) is True
    # Chain and hostname passed (same CA, same name); the PIN is what refused,
    # and it refused before the request was dispatched.
    assert peer.httpd.seen == []


def test_on_failure_result_returns_the_pin_mismatch_in_band(certs, monkeypatch):
    expected = openssl_pin(certs["peer_a"]["cert"])
    presented = openssl_pin(certs["peer_b"]["cert"])
    peer = _TlsPeer(certs["peer_b"]["cert"], certs["peer_b"]["key"], "pong-b")
    monkeypatch.setenv("SSL_CERT_FILE", certs["peer_b"]["ca"])
    try:
        result = run(
            body(peer.host, expected, service=PROBE_RESULT,
                 on_failure="result"),
            extra={"Ok": _Ok, "Err": _Err}, note="hi")
    finally:
        peer.close()
    assert isinstance(result, _Err)
    assert expected in result.error
    assert presented in result.error
    assert peer.httpd.seen == []


def test_a_row_without_a_pin_accepts_any_trusted_key(certs, monkeypatch):
    peer = _TlsPeer(certs["peer_b"]["cert"], certs["peer_b"]["key"], "pong-b")
    monkeypatch.setenv("SSL_CERT_FILE", certs["peer_b"]["ca"])
    try:
        assert run(body(peer.host, None), note="hi") == "pong-b"
    finally:
        peer.close()


def test_the_handler_is_installed_only_when_the_row_is_pinned(certs):
    pinned = body("127.0.0.1:8443", PIN_HEX)
    unpinned = body("127.0.0.1:8443", None)
    assert "_PinnedHTTPSHandler" in pinned
    assert f"_revl_pin_expected = {PIN_HEX!r}" in pinned
    assert "_PinnedHTTPSHandler" not in unpinned
    assert "_revl_pin_expected" not in unpinned
    assert "http.client as _http_client" in pinned


def test_the_a2a_wire_carries_the_pin_too(certs):
    """A pin on a `through a2a` row must not be silently ignored: the A2A body
    installs the same handler over its own opener."""
    source = body("127.0.0.1:8443", PIN_HEX, transport="a2a")
    assert "_PinnedHTTPSHandler" in source
    assert "_revl_pin_expected" in source


def test_the_a2a_wire_refuses_another_key_too(certs, monkeypatch):
    """The A2A body's wiring, executed: same refusal, before the request."""
    expected = openssl_pin(certs["peer_a"]["cert"])
    presented = openssl_pin(certs["peer_b"]["cert"])
    peer = _TlsPeer(certs["peer_b"]["cert"], certs["peer_b"]["key"], "pong-b")
    monkeypatch.setenv("SSL_CERT_FILE", certs["peer_b"]["ca"])
    try:
        with pytest.raises(RuntimeError) as raised:
            run(body(peer.host, expected, transport="a2a"), note="hi")
    finally:
        peer.close()
    message = str(raised.value)
    assert "expected sha256:" + expected in message
    assert "presented sha256:" + presented in message
    assert peer.httpd.seen == []


def test_the_pin_is_what_refused_not_the_chain(certs, monkeypatch):
    """Non-vacuity: with the handler removed by the smallest edit that
    expresses it, the SAME trusted peer answers. So the refusal above is the
    pin's, not a chain or hostname failure."""
    expected = openssl_pin(certs["peer_a"]["cert"])
    peer = _TlsPeer(certs["peer_b"]["cert"], certs["peer_b"]["key"], "pong-b")
    monkeypatch.setenv("SSL_CERT_FILE", certs["peer_b"]["ca"])
    source = body(peer.host, expected)
    restored = source.replace(", _PinnedHTTPSHandler()", "")
    assert restored != source, "the emitted body no longer installs the pin"
    try:
        assert run(restored, note="hi") == "pong-b"
        assert peer.httpd.seen
    finally:
        peer.close()
