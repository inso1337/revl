"""Issue #3111: a model endpoint's reply is bounded, and malformed JSON is a
`ProviderError`.

`providers/transport.py` read a whole reply into memory with no size limit, had
only a per-socket-read timeout, and let hostile JSON escape as raw exceptions
(`AttributeError` from a reply whose `message` is a list, `ValueError` from an
integer of thousands of digits, `RecursionError` from deep nesting).

A raw-socket loopback server plays the endpoint, so a reply can be wrong at the
byte level (a lying `Content-Length`, a byte at a time, headers that never
finish), which `http.server` would not let a test do.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.providers import (  # noqa: E402
    Adapter, CompletionRequest, ProviderError, parse_config, request_json,
)
from revl.providers.config import ProviderConfigError  # noqa: E402

SECRET = "sk-test-3111-Zq9WmP3xKd7Lr2Vb8Nt"
ENV = "REVL_TEST_PROVIDER_KEY_3111"


class Endpoint:
    """A loopback server whose reply, `script(conn)`, is raw bytes written by
    the test, one connection at a time on its own thread."""

    def __init__(self, script):
        self.script = script
        self.connections = []
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections.append(conn)
            threading.Thread(target=self._one, args=(conn,),
                             daemon=True).start()

    def _one(self, conn):
        try:
            # read the request head and body so the client is never reset
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            while len(rest) < length:
                rest += conn.recv(65536)
            self.script(conn, self.stop)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.sock.close()
        for conn in self.connections:
            try:
                conn.close()
            except OSError:
                pass


def http_200(payload: bytes, *, declared: int | None = None) -> bytes:
    n = len(payload) if declared is None else declared
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(n).encode() + b"\r\nConnection: close"
            b"\r\n\r\n" + payload)


def reply(payload: bytes):
    def script(conn, stop):
        conn.sendall(http_200(payload))
    return script


@pytest.fixture
def serve():
    started = []

    def start(script):
        endpoint = Endpoint(script)
        started.append(endpoint)
        return endpoint

    yield start
    for endpoint in started:
        endpoint.close()


def adapter(base: str, provider: str = "openai-compatible", **extra):
    entry = {"provider": provider, "model": "fake", "base_url": base,
             "api_key_env": ENV, **extra}
    if provider == "openai-compatible":
        entry["base_url"] = base + "/v1"
    binding = parse_config({"roles": {"r": entry}}, "test").binding("r")
    return Adapter(binding, environ={ENV: SECRET})


def complete(a: Adapter):
    return a.complete(CompletionRequest(prompt="hello"))


# ---- malformed JSON is a ProviderError --------------------------------------

@pytest.mark.parametrize("provider,payload", [
    ("openai-compatible", b'{"choices":[{"message":["x"]}]}'),
    ("openai-compatible", b'{"choices":[{"message":"x"}]}'),
    ("openai-compatible",
     b'{"choices":[{"message":{"content":"a"}}],"usage":["u"]}'),
    ("openai-compatible",
     b'{"choices":[{"message":{"content":"a"}}],'
     b'"usage":{"completion_tokens_details":"d"}}'),
    ("ollama", b'{"message":["x"]}'),
    ("ollama", b'{"message":"x"}'),
])
def test_a_reply_of_the_wrong_shape_is_a_provider_error(serve, provider,
                                                        payload):
    endpoint = serve(reply(payload))
    a = (_loaded_ollama(endpoint) if provider == "ollama"
         else adapter(endpoint.base))
    with pytest.raises(ProviderError) as caught:
        complete(a)
    assert "unexpected response shape" in str(caught.value)
    assert "role `r`" in str(caught.value)


def _loaded_ollama(endpoint):
    a = adapter(endpoint.base, "ollama", devices={"gpu": {}})
    a._device, a._device_options = "gpu", {}
    return a


def test_an_integer_of_thousands_of_digits_is_a_provider_error(serve):
    payload = (b'{"choices":[{"message":{"content":"a"}}],'
               b'"usage":{"prompt_tokens":' + b"9" * 6000 + b"}}")
    endpoint = serve(reply(payload))
    with pytest.raises(ProviderError, match="unparseable JSON"):
        complete(adapter(endpoint.base))


def test_deeply_nested_json_is_a_provider_error(serve):
    endpoint = serve(reply(b"[" * 200000))
    with pytest.raises(ProviderError, match="unparseable JSON"):
        complete(adapter(endpoint.base))


# ---- size bound ------------------------------------------------------------

def test_a_declared_oversize_reply_is_refused_unread(serve):
    # the head promises 1 GB and sends nothing more: refused on the header
    def script(conn, stop):
        conn.sendall(http_200(b"{", declared=10**9))
        stop.wait(5)
    endpoint = serve(script)
    started = time.monotonic()
    with pytest.raises(ProviderError, match="larger than 4096 bytes"):
        complete(adapter(endpoint.base, max_reply_bytes=4096, timeout=30))
    assert time.monotonic() - started < 5


def test_an_oversize_reply_with_no_length_is_refused_by_counting(serve):
    chunk = b"x" * 1024

    def script(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Connection: close\r\n\r\n")
        conn.sendall(b'{"choices":[{"message":{"content":"')
        sent = 0
        while not stop.is_set() and sent < 50 * 1024 * 1024:
            conn.sendall(chunk)
            sent += len(chunk)
    endpoint = serve(script)
    with pytest.raises(ProviderError, match="larger than 8192 bytes"):
        complete(adapter(endpoint.base, max_reply_bytes=8192, timeout=30))


def test_a_reply_within_the_limit_still_completes(serve):
    payload = (b'{"model":"m","choices":[{"message":{"content":"'
               + b"y" * 3000 + b'"},"finish_reason":"stop"}]}')
    endpoint = serve(reply(payload))
    done = complete(adapter(endpoint.base, max_reply_bytes=4096))
    assert done.text == "y" * 3000


def test_the_default_limit_is_a_few_megabytes():
    binding = parse_config({"roles": {"r": {
        "provider": "openai-compatible", "model": "m",
        "base_url": "http://127.0.0.1:1/v1"}}}, "test").binding("r")
    assert 1 << 20 <= binding.max_reply_bytes <= 64 << 20


@pytest.mark.parametrize("value", [0, 1023, -5, 1.5, "big", True])
def test_max_reply_bytes_must_be_a_sensible_integer(value):
    with pytest.raises(ProviderConfigError, match="max_reply_bytes"):
        parse_config({"roles": {"r": {
            "provider": "openai-compatible", "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
            "max_reply_bytes": value}}}, "test")


def test_an_error_body_is_read_only_to_the_quoted_limit(serve):
    def script(conn, stop):
        head = (b"HTTP/1.1 500 Internal Server Error\r\n"
                b"Connection: close\r\n\r\n")
        conn.sendall(head + b"E" * 10000)
        conn.close()
    endpoint = serve(script)
    with pytest.raises(ProviderError) as caught:
        request_json(endpoint.base + "/x", timeout=5)
    assert caught.value.status == 500
    assert len(str(caught.value)) < 700


# ---- time bound ------------------------------------------------------------

def test_a_server_sending_a_byte_at_a_time_is_cut_off_at_the_deadline(serve):
    def script(conn, stop):
        payload = b'{"choices":[{"message":{"content":"' + b"a" * 5000
        conn.sendall(http_200(payload, declared=len(payload) + 10))
        for i in range(len(payload)):
            if stop.is_set():
                return
            conn.sendall(payload[i:i + 1])
            time.sleep(0.1)
    endpoint = serve(script)
    started = time.monotonic()
    with pytest.raises(ProviderError, match="timed out after 1.0s"):
        complete(adapter(endpoint.base, timeout=1.0))
    assert time.monotonic() - started < 3


def test_a_server_sending_a_chunked_trickle_is_cut_off_too(serve):
    def script(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                     b"Connection: close\r\n\r\n")
        while not stop.is_set():
            conn.sendall(b"1\r\na\r\n")
            time.sleep(0.1)
    endpoint = serve(script)
    started = time.monotonic()
    with pytest.raises(ProviderError, match="timed out"):
        complete(adapter(endpoint.base, timeout=1.0))
    assert time.monotonic() - started < 3


def test_headers_that_never_finish_are_cut_off(serve):
    def script(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        i = 0
        while not stop.is_set():
            conn.sendall(b"X-Pad-%d: 1\r\n" % i)
            i += 1
            time.sleep(0.1)
    endpoint = serve(script)
    started = time.monotonic()
    with pytest.raises(ProviderError, match="timed out"):
        complete(adapter(endpoint.base, timeout=1.0))
    assert time.monotonic() - started < 3


def test_a_server_that_never_answers_still_times_out(serve):
    endpoint = serve(lambda conn, stop: stop.wait(10))
    started = time.monotonic()
    with pytest.raises(ProviderError, match="timed out"):
        complete(adapter(endpoint.base, timeout=0.5))
    assert time.monotonic() - started < 3


def test_a_prompt_reply_is_unaffected_by_the_deadline(serve):
    payload = b'{"model":"m","choices":[{"message":{"content":"ok"}}]}'
    endpoint = serve(reply(payload))
    for _ in range(3):
        assert complete(adapter(endpoint.base, timeout=2.0)).text == "ok"


def test_the_message_never_carries_the_credential(serve):
    payload = ('{"choices":[{"message":["' + SECRET + '"]}]}').encode()
    endpoint = serve(reply(payload))
    with pytest.raises(ProviderError) as caught:
        complete(adapter(endpoint.base))
    assert SECRET not in str(caught.value)
