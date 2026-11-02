"""The HTTP listener's listen backlog (the connection-storm availability fix).

`socketserver.TCPServer.request_queue_size` is 5, so the kernel keeps at most
five completed handshakes unaccepted. A load test of the face
(`ab -n 2000 -c 64`) measured ~2 s maximum connect time while processing stayed
at ~119 ms. `http_guard.Listener` now binds with `REVL_HTTP_BACKLOG` (default
`DEFAULT_BACKLOG` = 128), read before `TCPServer.__init__` binds and listens.
"""

from __future__ import annotations

import errno
import select
import socket
import socketserver
import time
from http.server import BaseHTTPRequestHandler

import pytest

from revl.mcp import http_guard
from revl.mcp.http_guard import Exposure, Listener


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_ambient_backlog(monkeypatch):
    """The default-value tests must not inherit a setting from the machine."""
    monkeypatch.delenv(http_guard.BACKLOG_ENV, raising=False)


def _listener() -> Listener:
    return Listener(Exposure(host="127.0.0.1", port=0), _Handler)


def _connected(socks, deadline: float) -> int:
    """How many of `socks` completed their non-blocking connect before the
    deadline (a failed connect becomes writable with a non-zero SO_ERROR)."""
    done = 0
    pending = list(socks)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _, writable, _ = select.select([], pending, [], remaining)
        if not writable:
            break
        for sock in writable:
            pending.remove(sock)
            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                done += 1
    return done


def test_the_listener_does_not_bind_with_socketservers_default_of_five():
    assert socketserver.TCPServer.request_queue_size == 5
    httpd = _listener()
    try:
        assert httpd.request_queue_size == http_guard.DEFAULT_BACKLOG == 128
    finally:
        httpd.server_close()


def test_the_backlog_is_configurable_through_the_environment(monkeypatch):
    monkeypatch.setenv(http_guard.BACKLOG_ENV, "256")
    httpd = _listener()
    try:
        assert httpd.request_queue_size == 256
    finally:
        httpd.server_close()


@pytest.mark.parametrize("raw", ["", "0", "-4", "many", "64x"])
def test_a_bad_backlog_setting_keeps_the_documented_default(monkeypatch, raw):
    monkeypatch.setenv(http_guard.BACKLOG_ENV, raw)
    httpd = _listener()
    try:
        assert httpd.request_queue_size == http_guard.DEFAULT_BACKLOG
    finally:
        httpd.server_close()


def test_a_backlog_above_the_cap_is_clamped(monkeypatch):
    monkeypatch.setenv(http_guard.BACKLOG_ENV, "1000000000")
    httpd = _listener()
    try:
        assert httpd.request_queue_size == http_guard.MAX_BACKLOG
    finally:
        httpd.server_close()


def test_a_burst_of_sixty_four_connects_is_queued_with_no_accept_loop():
    """A 64-connection burst must fit the kernel queue before any request is
    accepted. With socketserver's 5, only five connect: the rest wait for a
    retransmission the idle accept loop never unlocks, which is the measured
    ~2 s connect latency. No serve loop runs here, so no request is processed
    and no latency is asserted; only how many handshakes the queue holds."""
    httpd = _listener()
    pending: list[socket.socket] = []
    try:
        assert httpd.request_queue_size >= 64
        port = httpd.server_address[1]
        for _ in range(64):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            code = sock.connect_ex(("127.0.0.1", port))
            assert code in (0, errno.EINPROGRESS, errno.EWOULDBLOCK,
                            errno.EALREADY), code
            pending.append(sock)
        assert _connected(pending, time.monotonic() + 3.0) == 64
    finally:
        for sock in pending:
            sock.close()
        httpd.server_close()
