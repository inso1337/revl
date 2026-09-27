"""The exposure rules every revl HTTP listener shares (issue #1463).

Two listeners use this module: the MCP Streamable HTTP transport
(`revl.mcp.http_transport`, for `revl mcp serve --http` and `revl mcp proxy
--http`) and the composition's own HTTP face (`revl.mcp.http_face`, for `revl
serve --http`). Both answer the same two questions the same way.

**Where may it listen?** A loopback address, or any address with TLS. A
listener on a non-loopback address without TLS refuses to start
(`check_exposure`). A wildcard bind (`0.0.0.0`, `::`) names no host, so it also
needs the host names it answers to (`--allow-host`).

**Whose request is this?** A request is refused with 403 before anything reads
its body when:

  * its `Host` is not a name this listener answers to. On a loopback bind that is
    `127.0.0.1`, `localhost` and `[::1]` (with or without the port) plus any
    `--allow-host`. This is what defeats DNS rebinding: a page that re-points its
    own name at 127.0.0.1 still sends its own name as `Host`;
  * it carries an `Origin` that is not listed in `--allow-origin`. A request with
    no `Origin` (every non-browser client) passes this check. The MCP transport
    spec requires exactly this (`Origin` present and invalid: 403);
  * it carries either header more than once.

It also holds the one dispatch lock both listeners use (`DispatchLock`): a
live `Session` runs one asyncio loop with `run_until_complete`, so two request
threads calling it at once fail with "This event loop is already running"
(issue #1488).

This module decides nothing about WHO the caller is; authentication is the
listener's own (`http_transport.Authenticator`).
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]", "::1")


class ExposureError(ValueError):
    """A listener configuration that would expose the server unsafely."""


def is_loopback(host: str) -> bool:
    """Whether `host` names only this machine."""
    name = (host or "").strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def is_wildcard(host: str) -> bool:
    name = (host or "").strip().strip("[]")
    return name in ("", "0.0.0.0", "::")


@dataclass
class Exposure:
    """Where a listener binds and which requests it will even look at.
    `bound_port` is filled in once the socket is bound (a requested port of 0
    picks a free one), so the Host check compares against the real port."""

    host: str = "127.0.0.1"
    port: int = 0
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_client_ca: str | None = None
    allow_hosts: tuple[str, ...] = ()
    allow_origins: tuple[str, ...] = ()
    bound_port: int | None = None

    @property
    def tls(self) -> bool:
        return bool(self.tls_cert)

    @property
    def port_in_use(self) -> int:
        return self.bound_port if self.bound_port is not None else self.port


def check_exposure(exposure: Exposure) -> None:
    """Refuse a listener that would be reachable off this machine in clear text,
    or that could not tell which `Host` values are its own."""
    if bool(exposure.tls_cert) != bool(exposure.tls_key):
        raise ExposureError("--tls-cert and --tls-key go together: give both or "
                            "neither")
    if exposure.tls_client_ca and not exposure.tls_cert:
        raise ExposureError("--tls-client-ca (mutual TLS) needs --tls-cert and "
                            "--tls-key: a client certificate is only checked "
                            "inside a TLS connection")
    if not is_loopback(exposure.host) and not exposure.tls:
        raise ExposureError(
            f"refusing to listen on {exposure.host!r} without TLS: an address "
            f"other than loopback is reachable from other machines, and this "
            f"listener carries credentials and state-changing calls. Bind "
            f"127.0.0.1, or pass --tls-cert and --tls-key")
    if is_wildcard(exposure.host) and not exposure.allow_hosts:
        raise ExposureError(
            f"a wildcard bind ({exposure.host!r}) names no host, so the Host "
            f"check that stops DNS rebinding would have nothing to compare "
            f"against. Name the host names clients use with --allow-host")
    for origin in exposure.allow_origins:
        if origin.strip() in ("", "*", "null"):
            raise ExposureError(f"--allow-origin {origin!r} would admit every "
                                f"page; name an exact origin such as "
                                f"https://app.example.com")


def allowed_hosts(exposure: Exposure) -> frozenset[str]:
    """Every `Host` header value this listener answers to, lower-cased."""
    port = exposure.port_in_use
    names: set[str] = {h.strip().lower() for h in exposure.allow_hosts if h.strip()}
    if is_loopback(exposure.host):
        names |= {"127.0.0.1", "localhost", "[::1]"}
    if not is_wildcard(exposure.host):
        host = exposure.host.strip().lower()
        names.add(f"[{host}]" if ":" in host and not host.startswith("[") else host)
    out: set[str] = set()
    for name in names:
        out.add(name)
        if ":" not in name.split("]")[-1]:
            out.add(f"{name}:{port}")
    return frozenset(out)


def _header_values(headers, name: str) -> list[str]:
    getter = getattr(headers, "get_all", None)
    if getter is not None:
        return list(getter(name) or [])
    value = headers.get(name)
    return [] if value is None else [value]


def request_refusal(headers, exposure: Exposure) -> str | None:
    """Why this request is refused before it is read, or None to go on."""
    hosts = _header_values(headers, "Host")
    if len(hosts) != 1:
        return "the request must carry exactly one Host header"
    host = hosts[0].strip().lower()
    if host not in allowed_hosts(exposure):
        return (f"Host {hosts[0]!r} is not a name this server answers to "
                f"(a DNS-rebinding request carries the attacker's name); pass "
                f"--allow-host to add one")
    origins = _header_values(headers, "Origin")
    if len(origins) > 1:
        return "the request carries more than one Origin header"
    if origins:
        origin = origins[0].strip()
        if origin not in exposure.allow_origins:
            return (f"Origin {origin!r} is not allowed; a browser page may call "
                    f"this server only from an origin named with --allow-origin")
    return None


def server_tls_context(exposure: Exposure) -> ssl.SSLContext | None:
    """The TLS context for a listener, or None when it serves clear text."""
    if not exposure.tls:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(exposure.tls_cert, exposure.tls_key)
    if exposure.tls_client_ca:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(exposure.tls_client_ca)
    return context


#: How long a connection may take to complete its TLS handshake, and to send a
#: request once connected. A client that stalls is dropped rather than holding a
#: worker thread forever.
CONNECTION_TIMEOUT = 30.0


class Listener(ThreadingHTTPServer):
    """A threaded HTTP listener that applies `Exposure`: TLS (and mutual TLS)
    when configured, with the handshake done in the connection's own worker
    thread so one slow client cannot stall `accept`."""

    daemon_threads = True

    def __init__(self, exposure: Exposure, handler_class) -> None:
        check_exposure(exposure)
        family = socket.AF_INET6 if ":" in exposure.host.strip("[]") else socket.AF_INET
        self.address_family = family
        self.exposure = exposure
        self.tls_context = server_tls_context(exposure)
        super().__init__((exposure.host.strip("[]"), exposure.port), handler_class)
        exposure.bound_port = self.server_address[1]

    def finish_request(self, request, client_address) -> None:
        request.settimeout(CONNECTION_TIMEOUT)
        if self.tls_context is None:
            super().finish_request(request, client_address)
            return
        try:
            wrapped = self.tls_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # no handshake, no request: the caller never got in
        try:
            super().finish_request(wrapped, client_address)
        finally:
            try:
                wrapped.close()
            except OSError:
                pass


class DispatchLock:
    """One request at a time against one live session (issue #1488).

    A `Session` drives a single asyncio loop with `run_until_complete`, so it
    can serve one call at a time; a threaded HTTP listener that dispatched two
    requests into it at once had all but one fail with "This event loop is
    already running". Every revl HTTP listener that fronts a session takes this
    lock around dispatch: the MCP transport (`http_transport.CallerBinding`) and
    the composition's own face (`http_face`).

    The cost is throughput: requests are served one after another, so one slow
    call delays every request behind it. Usable as `with lock:` like a
    `threading.Lock`, or with `hold(blocking=False)` to try without waiting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True) -> bool:
        return self._lock.acquire(blocking)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> "DispatchLock":
        self._lock.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.release()

    @contextmanager
    def hold(self, *, blocking: bool = True):
        """Yield True while holding the lock, or False at once when
        `blocking=False` and another request holds it."""
        if not self._lock.acquire(blocking):
            yield False
            return
        try:
            yield True
        finally:
            self._lock.release()
