"""The operator listener beside `revl serve --http` (issue #1553; design 569, C2).

`revl serve --http` answers the application's users (design 569, option B): the
face authenticates nobody, serves the composition's public surface, and never
serves an operator verb. Its operators reach the SAME session through this
second listener, on a second address:

    revl serve --http app.rvl --operator-listen 127.0.0.1:8471 \\
        --operator-profile ops.profile

What it is, and nothing more: the MCP Streamable HTTP transport of
`http_transport` (`HttpTransport` over the compiler server's dispatcher), pointed
at the face's session and sharing the face's dispatch lock. So an operator is
authenticated per request exactly as on `revl mcp serve --http` (a bearer secret
matching a `key sha256:` line of the live profile, or a client certificate under
mutual TLS, with the settle window and the deny-all placeholder between
requests), and the verbs an operator has are the ones that transport serves,
gated by the same profile: `revl_estop` and `revl_estop_report`, `revl_approve`
and `revl_revoke` for the tickets the face hands out, `revl_state`.

What it adds to that transport is only the wiring:

  * **One session, one lock.** The compiler server's `SESSION` is the face's
    session while the listener runs, and the face dispatches through the
    transport's `CallerBinding`, bound to `APP_CALLER` for exactly one request.
    `APP_CALLER` is a token no profile can declare, so a ticket an app request
    raises names the app as its proposer, and an app request never acts as an
    operator.
  * **E-Stop reaches the face.** The face refuses (503) while the transport's
    latch is engaged, before it waits for the lock, so a request queued behind a
    busy one is refused rather than run. The request in flight meets the latch
    at its next crossing seam (item 443).
  * **Never a browser's.** It takes no `--allow-origin`, so `http_guard` refuses
    every request that carries an `Origin` header. It refuses the app face's
    port, and like every revl listener it refuses a non-loopback address
    without TLS.
"""

from __future__ import annotations

from .http_guard import Exposure, ExposureError, check_exposure
from .http_transport import ENDPOINT, HttpTransport, ServerDispatcher, TransportError
from .live_profile import DEFAULT_SETTLE_MS

#: The operator token every app request is bound to while an operator listener
#: shares the session. Like the transport's `NO_CALLER` it is not a token a
#: profile can declare, so `operator.decide` grants it nothing.
APP_CALLER = "<app caller>"


def check_operator_exposure(operator: Exposure, app: Exposure) -> None:
    """Refuse an operator listener a browser could reach, or one on the app
    face's port. Raises `ExposureError`, like `check_exposure`."""
    if operator.allow_origins:
        raise ExposureError(
            "the operator listener is never called from a browser page: it "
            "takes no --allow-origin, and a request carrying an Origin header is "
            "refused")
    app_port, own_port = app.port_in_use, operator.port_in_use
    if app_port and own_port and app_port == own_port:
        raise ExposureError(
            f"the operator listener must not use the app face's port {app_port}: "
            f"the two serve different callers and never share a socket. Give "
            f"--operator-listen another port")
    check_exposure(operator)


class OperatorListener:
    """The MCP HTTP transport on a face's session (see the module docstring)."""

    def __init__(self, face=None, *, exposure: Exposure,
                 app_exposure: Exposure | None = None,
                 profile_path: str | None = None, registry=None,
                 auth: str = "bearer", settle_ms: int = DEFAULT_SETTLE_MS,
                 server_module=None) -> None:
        if server_module is None:
            from . import server as server_module  # noqa: PLC0415
        if not profile_path and registry is None:
            raise TransportError(
                "--operator-listen needs --operator-profile: every request on "
                "the operator listener is bound to one of its operators")
        if exposure.allow_origins:
            raise TransportError(
                "the operator listener takes no --allow-origin: it is never "
                "called from a browser page")
        self.face = face
        self.server = server_module
        self.app_exposure = app_exposure
        self.transport = HttpTransport(ServerDispatcher(server_module),
                                       registry=registry, exposure=exposure,
                                       auth=auth, server_module=server_module,
                                       profile_path=profile_path,
                                       profile_settle_ms=settle_ms)
        self._saved_session = None
        self._running = False

    @property
    def exposure(self) -> Exposure:
        return self.transport.exposure

    def start(self) -> tuple[str, int]:
        """Bind, point the compiler server at the face's session, and route the
        face's dispatch through the transport's binding. Raises
        `TransportError` when it cannot start."""
        if self.face is None:
            raise TransportError("the operator listener has no face to serve")
        if self.app_exposure is not None:
            try:
                check_operator_exposure(self.exposure, self.app_exposure)
            except ExposureError as error:
                raise TransportError(str(error)) from error
        self._saved_session = self.server.SESSION
        self.server.SESSION = self.face.session
        try:
            address = self.transport.start()
        except BaseException:
            self.server.SESSION = self._saved_session
            raise
        from .operator import Operator  # noqa: PLC0415

        self.face.attach_operator(self.transport.binding,
                                  caller=Operator(token=APP_CALLER),
                                  halted=self._latched,
                                  before=self.transport._complete_halt)
        self._running = True
        return address

    def stop(self) -> None:
        if not self._running:
            return
        # the transport first, so no operator request is left on the lock the
        # face is about to stop sharing
        self.transport.stop()
        self.face.detach_operator()
        self.server.SESSION = self._saved_session
        self._running = False

    def _latched(self) -> bool:
        return self.transport.latch.record() is not None

    def describe(self) -> list[str]:
        """The start-up lines `revl serve --http` prints for this listener."""
        scheme = "https" if self.exposure.tls else "http"
        lines = [f"operator listener: MCP on {scheme}://{self.exposure.host}:"
                 f"{self.exposure.port_in_use}{ENDPOINT} (auth: "
                 f"{self.transport.authenticator.mode}; operators only, no "
                 f"browser origin)"]
        if self.transport.latch.path:
            lines.append(f"out-of-band E-Stop: revl estop --latch "
                         f"{self.transport.latch.path}")
        return lines
