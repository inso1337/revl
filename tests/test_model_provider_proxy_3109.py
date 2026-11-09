"""Issue #3109: an ambient proxy must not carry a model request.

`providers/transport.py` built its opener with `urllib.request.build_opener(
_NoRedirect)`, which keeps urllib's default `ProxyHandler`. That handler reads
`http_proxy`, `https_proxy` and `all_proxy` from the environment. With
`http_proxy` set and no matching `no_proxy`, an `http://127.0.0.1:...` model
endpoint was contacted through the proxy: the proxy received the prompt body
and the `Authorization` header, while `endpoint_residence` still called the
role `on_device` and `_check_url` had allowed the key over plain http because
the host was loopback.

The test below is parametrized over `http_proxy` and `HTTP_PROXY`; it starts
the endpoint and a stand-in proxy on loopback, sets the proxy variable, and
requires that the endpoint gets the request and the proxy gets nothing. On the
broken tree the proxy answers instead, so the endpoint is empty and the prompt
with its key has crossed to a host the placement check never judged.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.providers import (  # noqa: E402
    Adapter, CompletionRequest, parse_config,
)

SECRET = "sk-test-3109-Lm4pQ8xVn2Rc7Tb5Yh1"
ENV = "REVL_TEST_PROVIDER_KEY_3109"
PROMPT = "the prompt that must stay on this machine"


class Recorder:
    """A loopback server that records every request and answers in the
    OpenAI-compatible shape, so a request sent to it completes."""

    def __init__(self):
        self.requests = []
        self._closed = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.requests.append({
                    "path": self.path,
                    # urllib capitalises the names it sends
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw or b"null"),
                })
                payload = json.dumps({
                    "model": "fake",
                    "choices": [{"message": {"content": "OK"},
                                 "finish_reason": "stop"}],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def endpoint():
    server = Recorder()
    yield server
    server.close()


@pytest.fixture
def proxy():
    server = Recorder()
    yield server
    server.close()


@pytest.mark.parametrize("variable", ["http_proxy", "HTTP_PROXY"])
def test_an_ambient_proxy_never_carries_a_loopback_model_request(
        endpoint, proxy, monkeypatch, variable):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
                 "REQUEST_METHOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, proxy.base)

    binding = parse_config({"roles": {"oa": {
        "provider": "openai-compatible",
        "model": "fake",
        "base_url": endpoint.base + "/v1",
        "api_key_env": ENV,
    }}}, "test").binding("oa")

    completion = Adapter(binding, environ={ENV: SECRET}).complete(
        CompletionRequest(prompt=PROMPT, system="be brief"))

    assert completion.text == "OK"
    assert proxy.requests == [], (
        "the ambient proxy received a model request; the endpoint should have "
        "been contacted directly")
    assert len(endpoint.requests) == 1
    request = endpoint.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {SECRET}"
    assert request["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": PROMPT}]
