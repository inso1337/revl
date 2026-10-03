"""The operator profile as a live file (issue #1463, the #1491 follow-ups).

One mechanism for every MCP transport: the HTTP transport authenticates each
request against it, and the stdio servers (`revl mcp serve`, `revl mcp proxy`)
re-bind their session to it before each message. So a `revoked` line, an
`until`, a removed operator or a narrowed grant takes effect on the next
request or message, with no restart.

Three rules, all failing CLOSED (no registry, every request refused, naming why):

  * **Cheap when nothing changed.** One `stat` per request. The file is read and
    hashed only when its stat signature (mtime, ctime, size, inode, device)
    changed, or its mtime is within `RACY_NS` of the last read, where two writes
    in one timestamp tick could leave the signature unchanged (git's "racy
    clean" rule).
  * **Adopt only settled content.** New content is adopted once it has read
    IDENTICAL twice, at least `SETTLE_NS` apart. A profile caught mid-write can
    still parse, and one cut off just before a `may not` line would widen a
    grant. Until the content settles, requests are refused ("profile changing");
    they are NOT served under the previous profile either, because the edit in
    progress may be a revocation, and serving the old grants would delay it.
  * **A profile that cannot be read or parsed refuses every request** until the
    file is fixed.

One exception to both refusals: **E-Stop is never fenced**. While the profile is
settling or broken, `revl_estop` is still accepted from an operator authorized
for it under the LAST ADOPTED profile (`last_adopted`). An E-Stop only stops
things, so honouring it under the prior profile cannot widen authority. With no
profile ever adopted, the E-Stop latch file is the only way to halt.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time


DEFAULT_SETTLE_MS = 1000


class ProfileUnavailable(RuntimeError):
    """The operator profile could not be loaded when the server started."""


def is_estop(message) -> bool:
    """Is this JSON-RPC message a `revl_estop` tool call?"""
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return False
    params = message.get("params")
    return isinstance(params, dict) and params.get("name") == "revl_estop"


class ProfileSource:
    """The operator profile file, re-read when it changes."""

    RACY_NS = 2_000_000_000
    START_TIMEOUT_S = 10.0

    def __init__(self, path: str, *, settle_ms: int = DEFAULT_SETTLE_MS) -> None:
        if settle_ms < 0:
            raise ValueError("--profile-settle-ms must be 0 or more")
        self.SETTLE_NS = int(settle_ms) * 1_000_000
        self.START_TIMEOUT_S = max(self.START_TIMEOUT_S, 5 * settle_ms / 1000)
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._signature = None
        self._read_at_ns = 0
        self._adopted_digest: str | None = None
        self._pending_digest: str | None = None
        self._pending_since_ns = 0
        self.registry = None
        # the last registry this source adopted, kept through settling and
        # breakage for the one verb that is never fenced (E-Stop)
        self.last_adopted = None
        self.error: str | None = None
        self.changing = False
        self._load_at_start()

    def _load_at_start(self) -> None:
        deadline = time.monotonic() + self.START_TIMEOUT_S
        self.refresh()
        while self.changing and time.monotonic() < deadline:
            time.sleep(self.SETTLE_NS / 1e9)
            self.refresh()
        if self.registry is None:
            raise ProfileUnavailable(self.error or f"cannot load {self.path}")

    # -- the three rules -----------------------------------------------------

    def refresh(self) -> None:
        from .operator import ProfileError, parse_profile  # noqa: PLC0415

        with self._lock:
            try:
                st = os.stat(self.path)
            except OSError as error:
                self._refuse(f"cannot read the operator profile {self.path}: {error}")
                self._signature = self._adopted_digest = self._pending_digest = None
                return
            signature = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_dev)
            racy = st.st_mtime_ns >= self._read_at_ns - self.RACY_NS
            if signature == self._signature and not racy \
                    and self._pending_digest is None:
                return  # unchanged, and adopted or refused already
            try:
                with open(self.path, "rb") as handle:
                    data = handle.read()
            except OSError as error:
                self._refuse(f"cannot read the operator profile {self.path}: {error}")
                self._signature = self._adopted_digest = self._pending_digest = None
                return
            self._signature = signature
            self._read_at_ns = time.time_ns()
            digest = hashlib.sha256(data).hexdigest()
            now = time.monotonic_ns()
            if digest == self._adopted_digest and self._pending_digest is None:
                return  # the content in force, re-read (a touch, a racy re-read)
            if digest != self._pending_digest:
                if digest == self._adopted_digest:
                    # an edit that was reverted before it settled
                    self._pending_digest = None
                    self._adopt(data, digest, parse_profile, ProfileError)
                    return
                self._pending_digest, self._pending_since_ns = digest, now
                self._settling()
                return
            if now - self._pending_since_ns < self.SETTLE_NS:
                self._settling()
                return
            self._pending_digest = None
            self._adopt(data, digest, parse_profile, ProfileError)

    def _adopt(self, data: bytes, digest: str, parse_profile, profile_error) -> None:
        self._adopted_digest = digest
        self.changing = False
        try:
            registry = parse_profile(data.decode("utf-8"), source=self.path)
        except (UnicodeDecodeError, profile_error) as error:
            self._refuse(f"the operator profile {self.path} does not parse, so every "
                         f"request is refused until it does: {error}")
            return
        self.registry, self.error = registry, None
        self.last_adopted = registry

    def _settling(self) -> None:
        self.changing = True
        self.registry = None
        self.error = (f"the operator profile {self.path} is changing: new content is "
                      f"adopted once it reads the same twice, "
                      f"{self.SETTLE_NS // 1_000_000} ms apart, and until then every "
                      f"request is refused rather than served under either version")

    def _refuse(self, message: str) -> None:
        self.changing = False
        self.registry, self.error = None, message

    def current(self):
        """`(registry, None)`, or `(None, why every request is refused)`."""
        self.refresh()
        return self.registry, self.error


class StdioBinding:
    """Re-binds a stdio server's session to the live profile before each message.

    The session runs as one serve-time operator token. Before each message its
    `operator` becomes that token's CURRENT entry and its `operator_registry`
    the current registry, so the operator gate and the quorum-cast checks read
    the file as it is now.

    Refused, on every verb except `revl_estop`: a profile that is changing or
    broken, and a serve-time operator the profile no longer declares, has
    `revoked`, or whose `until` has passed. A revocation that left management
    verbs working would not be one. An E-Stop is judged against the current
    profile, or, while it is settling or broken, the last adopted one."""

    def __init__(self, source: ProfileSource, server_module, token: str) -> None:
        self.source = source
        self.server = server_module
        self.token = token

    def __call__(self, message=None) -> str | None:
        from .quorum import _lifetime_refusal  # noqa: PLC0415 - one lifetime rule

        estop = is_estop(message)
        registry, broken = self.source.current()
        if registry is None:
            if not estop or self.source.last_adopted is None:
                return broken
            registry = self.source.last_adopted
        operator = registry.get(self.token)
        if operator is None:
            return (f"the operator this session runs as, `{self.token}`, is no longer "
                    f"declared in {self.source.path}, so every request is refused")
        if not estop:
            lapsed = _lifetime_refusal(operator, int(time.time() * 1000))
            if lapsed is not None:
                return (f"the operator this session runs as is no longer in force, so "
                        f"every request but revl_estop is refused: {lapsed.message}")
        session = self.server.SESSION
        session.operator = operator
        session.operator_registry = registry
        return None
