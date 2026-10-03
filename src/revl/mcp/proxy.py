"""`revl mcp proxy`: gate an existing MCP server with no `.rvl` written (issue #1463).

The proxy sits between an MCP client and an upstream MCP server, both over
stdio. It adds no gate of its own. It turns the upstream's `tools/list` into
the same revl surface `revl mcp import` produces (`schema.classify_imported_tools`,
the one classification), loads that surface into the compiler server's live
`Session`, and routes every `tools/call` through `Session.call`. So a proxied
call meets exactly the authority a revl call meets:

  * the effect class from the import's classifier: `witnessed` (the
    operator declared an undo) or `emission` (everything else, and every tool
    that cannot be classified). An unchecked `readOnlyHint: true` is NOT
    trusted by default, so it is `emission` too, with `gated` saying why;
    only `--trust-read-only-hints` admits it as `plain` (the import's own
    rule, where a human reviews the generated source before anything runs);
  * the item-246 approval policy, always on here: plain and witnessed calls
    proceed, an emission call raises the class-(c) ticket two-step;
  * the session WAL (a policy session records) and the item-245 commit/abort:
    abort replays the declared undo of every witnessed call;
  * E-Stop, operator profiles and leases, through the compiler server's own
    verbs (`revl_approve`, `revl_estop`, ...), dispatched by `server.handle`.

What the proxy adds is the part only a proxy can see: the MCP wire. A tool that
claims `readOnlyHint: true` and then, during its own call, makes the upstream
announce a resource change is flagged, and its claim is withdrawn (fed back into
the classifier through `distrust`, never re-derived here). docs/mcp-proxy.md
says exactly what that does and does not catch.

The emitted host bodies reach the upstream through `forward`,
`forward_witnessed` and `revert` below, which read the one process-global
`_ACTIVE` proxy. It is set by `Proxy.activate` and cleared by
`Proxy.deactivate`.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading

from ..compiler import compile_source
from ..errors import RevlError
from .approval import ApprovalRequired
from .schema import (EFFECT_EMISSION, EFFECT_PLAIN, EFFECT_WITNESSED,
                     classify_imported_tools, render_imported_source)
from .server import _error  # one JSON-RPC error shape (check_vocabulary_mirrors)
from .session import SessionError

PROTOCOL_VERSION = "2025-06-18"
_KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVICE = "Upstream"
KEY = "upstream"
COMPONENT = f"{SERVICE}Provider"

# The compiler server's verbs a proxied client may call. Each is dispatched by
# `server.handle`, so the operator gate, the ticket shaping and the E-Stop are
# the server's own. The client gets no load/swap/edit verb: it cannot change
# the proxied surface, only use it and answer for it.
MANAGEMENT_VERBS = (
    "revl_approve", "revl_revoke", "revl_commit", "revl_commit_confirm",
    "revl_abort", "revl_estop", "revl_estop_report", "revl_state", "revl_lease",
)
VERDICTS_TOOL = "revl_proxy_verdicts"

# Upstream notifications that announce a change to the world the server serves.
# One of these arriving DURING a read-only-claimed call is an observed crossing.
MUTATION_SIGNALS = frozenset({
    "notifications/resources/updated",
    "notifications/resources/list_changed",
})

# Client requests forwarded to the upstream unchanged. None of them is a tool
# call, so none has an effect class; docs/mcp-proxy.md lists them as ungated.
PASSTHROUGH = frozenset({
    "resources/list", "resources/read", "resources/templates/list",
    "resources/subscribe", "resources/unsubscribe",
    "prompts/list", "prompts/get", "completion/complete", "logging/setLevel",
})

_ACTIVE: "Proxy | None" = None


class UpstreamError(RuntimeError):
    """The upstream MCP server failed, timed out, or answered with an error."""


# ---------------------------------------------------------------- host bodies

def _active() -> "Proxy":
    if _ACTIVE is None:
        raise UpstreamError("no revl mcp proxy is active in this process, so a "
                            "proxied tool has no upstream to reach")
    return _ACTIVE


def forward(op: str, arguments: str) -> str:
    """The host body of a plain or emission proxied tool."""
    return _active().forward(op, arguments)


def forward_witnessed(op: str, arguments: str) -> tuple[bool, str]:
    """The host body of a witnessed proxied tool: `(ok, witness-or-error)`."""
    return _active().forward_witnessed(op, arguments)


def revert(op: str, witness: str) -> None:
    """The host body of a witnessed tool's inverse: call the declared undo."""
    _active().revert(op, witness)


def _host_body(tool: dict, role: str) -> str:
    """The `@py` block text for one proxied extern. Only the op identifier is
    spliced in (the raw upstream name never reaches the source)."""
    op = json.dumps(tool["op"])
    lead = "\n    from revl.mcp import proxy as _revl_proxy\n"
    if role == "settled":
        return "\n    return\n"
    if role == "undo":
        return f"{lead}    _revl_proxy.revert({op}, w)\n    return\n"
    if tool["effect"] == EFFECT_WITNESSED:
        return (f"{lead}    _ok, _value = _revl_proxy.forward_witnessed({op}, arguments)\n"
                f"    return Ok(_value) if _ok else Err(_value)\n")
    return f"{lead}    return _revl_proxy.forward({op}, arguments)\n"


# ---------------------------------------------------------------- upstream

class Upstream:
    """A newline-delimited JSON-RPC client over an upstream server's stdio.

    A reader thread owns the upstream's stdout. Responses are handed to the
    waiting request; notifications are recorded against the call in flight
    (the observation window) and relayed; a request the upstream initiates is
    answered with an error, because the proxy relays none (docs/mcp-proxy.md).
    """

    def __init__(self, command: list[str], *, timeout: float = 120.0,
                 on_notification=None) -> None:
        self.command = list(command)
        self.timeout = timeout
        self.on_notification = on_notification
        self.tools_changed = False
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._observing: list | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, text=True, encoding="utf-8", bufsize=1)
        except OSError as error:
            raise UpstreamError(f"cannot start the upstream server "
                                f"{self.command!r}: {error}") from error
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="revl-mcp-proxy-upstream")
        self._reader.start()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._closed.set()
        if self._reader is not None:
            self._reader.join(timeout=5)
        if process.stdout:
            process.stdout.close()
        self._process = None

    # -- wire ----------------------------------------------------------------

    def _write(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise UpstreamError("the upstream server is not running")
        with self._write_lock:
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise UpstreamError(f"cannot write to the upstream server: {error}") \
                    from error

    def _read_loop(self) -> None:
        stream = self._process.stdout
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            with self._pending_lock:
                waiters = list(self._pending.values())
            for waiter in waiters:
                waiter.put(None)

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            with self._pending_lock:
                waiter = self._pending.get(message.get("id"))
            if waiter is not None:
                waiter.put(message)
            return
        if "id" in message:
            # a server-initiated request (sampling, roots, elicitation): the
            # proxy relays none of them, and says so rather than hanging it
            self._write({"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32601,
                "message": f"revl mcp proxy does not relay the server-initiated "
                           f"request {method!r} to its client"}})
            return
        if method == "notifications/tools/list_changed":
            self.tools_changed = True
            return
        if method in MUTATION_SIGNALS and self._observing is not None:
            self._observing.append(message)
        if self.on_notification is not None:
            self.on_notification(message)

    def request(self, method: str, params: dict | None = None, *,
                observe: list | None = None) -> dict:
        """Send one request and wait for its response. With `observe`, every
        mutation signal the upstream sends before the response is appended to
        it: the observation window of one call."""
        if self._closed.is_set():
            raise UpstreamError("the upstream server has exited")
        self._next_id += 1
        request_id = self._next_id
        waiter: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[request_id] = waiter
        self._observing = observe
        try:
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._write(message)
            try:
                response = waiter.get(timeout=self.timeout)
            except queue.Empty:
                raise UpstreamError(f"the upstream server did not answer {method!r} "
                                    f"within {self.timeout:g}s") from None
        finally:
            self._observing = None
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if response is None:
            raise UpstreamError(f"the upstream server exited during {method!r}")
        if "error" in response:
            error = response["error"] or {}
            raise UpstreamError(f"the upstream server refused {method!r}: "
                                f"{error.get('message', error)}")
        return response.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)


# ---------------------------------------------------------------- the proxy

class Proxy:
    """One gated view of one upstream server, bound to the compiler server's
    live session (`revl.mcp.server.SESSION`)."""

    def __init__(self, upstream: Upstream, *, undo: dict | None = None,
                 trust_read_only: bool = False, stdout=None) -> None:
        from . import server  # noqa: PLC0415 - the session the verbs act on

        self.server = server
        self.upstream = upstream
        self.undo = dict(undo or {})
        self.trust_read_only = trust_read_only
        self.stdout = stdout or sys.stdout
        self._out_lock = threading.Lock()
        self.upstream_info: dict = {}
        self.upstream_capabilities: dict = {}
        self.manifest: dict = {"tools": []}
        self.distrust: dict[str, str] = {}
        self.tools: list[dict] = []          # the classified, proxied surface
        self.by_name: dict[str, dict] = {}
        self.by_op: dict[str, dict] = {}
        self.excluded: list[dict] = []       # listed upstream, not proxied
        self.refused: dict[str, str] = {}    # fail-closed refusals, by tool name
        self.observations: dict[str, list] = {}
        self.source: str | None = None
        self.ir: dict | None = None
        self._last: dict | None = None
        self._pending_meta: dict | None = None
        # where client-bound notifications go instead of stdout: the HTTP
        # transport's event hub (`http_stream.EventHub.route`), which decides
        # which caller's stream may carry each one
        self.sink = None
        upstream.on_notification = self._relay

    @property
    def session(self):
        return self.server.SESSION

    # -- lifecycle -----------------------------------------------------------

    def activate(self) -> None:
        global _ACTIVE
        _ACTIVE = self

    def deactivate(self) -> None:
        global _ACTIVE
        if _ACTIVE is self:
            _ACTIVE = None

    def connect(self) -> None:
        """Initialize the upstream, list its tools, classify and load them."""
        result = self.upstream.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "revl-mcp-proxy", "version": "2.0"},
        })
        self.upstream_info = result.get("serverInfo") or {}
        self.upstream_capabilities = result.get("capabilities") or {}
        self.upstream.notify("notifications/initialized")
        self.manifest = {"tools": self._list_upstream_tools()}
        self._build()
        self.session.approval_policy = "auto"
        self._ensure_loaded()

    def _list_upstream_tools(self) -> list:
        tools: list = []
        cursor = None
        for _ in range(1000):
            result = self.upstream.request("tools/list",
                                           {"cursor": cursor} if cursor else {})
            page = result.get("tools") or []
            tools.extend(page if isinstance(page, list) else [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise UpstreamError("the upstream server's tools/list did not terminate")

    def _classify(self) -> tuple[list[dict], list[dict]]:
        classified = classify_imported_tools(
            self.manifest, undo=self.undo, distrust=self.distrust,
            trust_read_only=self.trust_read_only)
        reserved = set(MANAGEMENT_VERBS) | {VERDICTS_TOOL}
        proxied, excluded, seen = [], [], set()
        for tool in classified:
            if not tool["callable"]:
                excluded.append({**tool, "excludedBecause": "it has no usable name"})
            elif tool["name"] in reserved:
                excluded.append({**tool, "excludedBecause":
                                 "its name is a revl verb the proxy serves itself"})
            elif tool["name"] in seen:
                excluded.append({**tool, "excludedBecause":
                                 "a duplicate of a tool already proxied under this name"})
            else:
                seen.add(tool["name"])
                # A fixed prefix keeps every generated identifier clear of the
                # py backend's reserved names (`self`, Python keywords, its
                # scaffolding), which a raw upstream name can otherwise hit.
                # Only the identifier changes: the class is the classifier's.
                proxied.append({**tool, "op": f"tool_{tool['op']}"})
        return proxied, excluded

    def _render(self, proxied: list[dict]) -> str:
        return render_imported_source(proxied, service=SERVICE, key=KEY,
                                      backend="py", host_body=_host_body,
                                      signature="json")

    @staticmethod
    def _compile(source: str) -> dict:
        # `profile=None` is the operator's trust, deliberately: this source is
        # generated by the proxy from the classifier's output. The MCP client
        # authors none of it and has no verb that could (no load/swap/edit),
        # and upstream text reaches it only inside `//` comments.
        return compile_source(source, "<revl mcp proxy>.rvl", profile=None)

    def _build(self) -> None:
        proxied, excluded = self._classify()
        source = self._render(proxied)
        self.ir = self._compile(source)
        self.source = source
        self._install(proxied, excluded)

    def _install(self, proxied: list[dict], excluded: list[dict]) -> None:
        self.tools = proxied
        self.excluded = excluded
        self.by_name = {t["name"]: t for t in proxied}
        self.by_op = {t["op"]: t for t in proxied}

    def _ensure_loaded(self) -> None:
        """(Re)load the proxied surface. A commit or abort ends the session's
        generation, so the next call boots a fresh one from the same source."""
        if self.session.loaded:
            return
        self.session.load(self.ir, record=True, origin={"source": self.source})

    def reclassify(self, *, relist: bool = False) -> None:
        """Rebuild the surface after a claim was withdrawn or the upstream's
        tool list changed, and swap it in through the session.

        Fail closed: if the rebuilt surface cannot be put live (a compile
        failure, a lease the policy enforces, a refused swap), every tool whose
        class would have changed is refused by name until the proxy restarts."""
        if relist:
            self.manifest = {"tools": self._list_upstream_tools()}
        try:
            proxied, excluded = self._classify()
        except ValueError as error:
            # the upstream's list no longer carries a tool an `--undo` names:
            # no surface can be derived, so nothing proxied may run
            for tool in self.tools:
                self.refused[tool["name"]] = (
                    f"the upstream's tool list changed and no longer matches this "
                    f"proxy's --undo declarations ({error}); every tool is refused "
                    "until the proxy restarts")
            return
        source = self._render(proxied)
        if source == self.source:
            self._install(proxied, excluded)
            return
        before = {t["name"]: t["effect"] for t in self.tools}
        changed = {t["name"] for t in proxied if before.get(t["name"]) != t["effect"]}
        if not changed and set(before) == {t["name"] for t in proxied}:
            # only a verdict label moved (a gated claim now observed false):
            # the surface is the same, so there is nothing to swap
            self._install(proxied, excluded)
            return
        try:
            ir = self._compile(source)
            if self.session.loaded:
                from . import leases  # noqa: PLC0415 - the existing lease surface

                refusal = leases.check_swap(self.session, {"source": source})
                if refusal is not None:
                    raise SessionError(refusal.message)
                self.session.swap(ir, origin={"source": source})
        except (RevlError, SessionError, ApprovalRequired) as error:
            reason = (f"the proxy could not put its reclassified surface live "
                      f"({type(error).__name__}: {error}), so this tool is refused "
                      "until the proxy restarts")
            for name in changed:
                self.refused[name] = reason
            return
        self.source, self.ir = source, ir
        self._install(proxied, excluded)
        for name in changed:
            self.refused.pop(name, None)

    # -- the host-body side --------------------------------------------------

    def _upstream_call(self, tool: dict, arguments: dict, meta: dict | None) -> dict:
        params: dict = {"name": tool["name"], "arguments": arguments}
        if meta:
            params["_meta"] = meta
        observed: list = []
        result = self.upstream.request("tools/call", params, observe=observed)
        self._last = {"tool": tool["name"], "result": result, "observed": observed}
        return result

    def _tool_for(self, op: str) -> dict:
        tool = self.by_op.get(op)
        if tool is None:
            raise UpstreamError(f"no proxied tool has the identifier {op!r}")
        return tool

    def forward(self, op: str, arguments: str) -> str:
        tool = self._tool_for(op)
        result = self._upstream_call(tool, json.loads(arguments),
                                     (self._pending_meta or None))
        return _result_text(result)

    def forward_witnessed(self, op: str, arguments: str) -> tuple[bool, str]:
        tool = self._tool_for(op)
        args = json.loads(arguments)
        result = self._upstream_call(tool, args, (self._pending_meta or None))
        if result.get("isError"):
            # a mutation that did not happen schedules no undo (243 rule 6)
            return False, _result_text(result)
        if tool["undo"]["with"] == "result":
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                reason = (f"`{tool['name']}` declares an undo that takes its "
                          "result, but the upstream returned no "
                          "`structuredContent` object. The call REACHED the "
                          "upstream and its effect is NOT covered by an undo")
                self.refused[tool["name"]] = reason + "; the tool is refused from now on"
                raise UpstreamError(reason)
            witness = structured
        else:
            witness = args
        return True, json.dumps(witness, sort_keys=True)

    def revert(self, op: str, witness: str) -> None:
        tool = self._tool_for(op)
        inverse = tool["undo"]["tool"]
        result = self.upstream.request("tools/call", {
            "name": inverse, "arguments": json.loads(witness)})
        if result.get("isError"):
            raise UpstreamError(f"the undo `{inverse}` of `{tool['name']}` "
                                f"failed: {_result_text(result)}")

    # -- the client side -----------------------------------------------------

    def call_tool(self, name: str, arguments, meta: dict | None = None) -> dict:
        """One proxied `tools/call`: an MCP CallToolResult."""
        tool = self.by_name.get(name)
        if tool is None:
            return _error_result(f"unknown tool: {name}")
        if name in self.refused:
            return _payload_result(self.server._session_error(self.refused[name]))
        if not isinstance(arguments, dict):
            return _error_result("`arguments` must be an object")
        # item 55: a proxied tool is a call on the proxied composition, so it
        # answers to the operator profile's `call` verb, like `revl_call`. With
        # no profile bound this is ungated (stdio without --operator-profile).
        from . import operator as _operator  # noqa: PLC0415

        decision = _operator.decide(self.session, "revl_call",
                                    {"key": KEY, "method": tool["op"]})
        if decision.gated and not decision.allowed:
            return _payload_result(self.server._refused_by_operator(decision))
        self._last = None
        self._pending_meta = meta
        try:
            self._ensure_loaded()
            self.session.call(KEY, tool["op"], [json.dumps(arguments, sort_keys=True)])
        except ApprovalRequired as exc:
            return _payload_result(self.server._approval_required(exc))
        except SessionError as error:
            return _payload_result(self.server._session_error(str(error)))
        except Exception as exc:  # the upstream or a host body raised
            return _payload_result(self.server._session_error(
                f"{type(exc).__name__}: {exc}", raised=True))
        finally:
            self._pending_meta = None
        last = self._last
        if last is None or last["tool"] != name:
            return _payload_result(self.server._session_error(
                "the proxied call completed without reaching the upstream"))
        result = dict(last["result"])
        verdict = self._observe(tool, last["observed"])
        meta_out = dict(result.get("_meta") or {})
        meta_out["revl/proxy"] = verdict
        result["_meta"] = meta_out
        return result

    def _observe(self, tool: dict, observed: list) -> dict:
        verdict = _verdict(tool)
        if not observed:
            return verdict
        methods = sorted({m.get("method") for m in observed})
        self.observations.setdefault(tool["name"], []).extend(methods)
        if tool["readOnlyClaim"] == "unchecked":
            # plain under --trust-read-only-hints, or gated and approved by
            # default: either way the claim is now refuted, not just unchecked
            reason = (f"during a call it claimed was read-only, the upstream sent "
                      f"{', '.join(methods)}")
            self.distrust[tool["name"]] = reason
            self.reclassify()
            flagged = self.by_name.get(tool["name"], tool)
            verdict = _verdict(flagged)
            verdict["flagged"] = reason
            self._send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return verdict

    def verdicts(self) -> dict:
        return {
            "ok": True,
            "upstream": self.upstream_info,
            "tools": [_verdict(t) | ({"refused": self.refused[t["name"]]}
                                     if t["name"] in self.refused else {})
                      for t in self.tools],
            "excluded": [{"name": t["name"], "index": t["index"],
                          "reason": t["excludedBecause"]} for t in self.excluded],
            "observations": self.observations,
        }

    def _advertised(self) -> list[dict]:
        upstream_tools = {}
        for raw in self.manifest.get("tools") or []:
            if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                upstream_tools.setdefault(raw["name"], raw)
        listed = []
        for tool in self.tools:
            raw = upstream_tools.get(tool["name"], {})
            entry = {k: v for k, v in raw.items() if k != "annotations"}
            entry["name"] = tool["name"]
            entry.setdefault("inputSchema", {"type": "object"})
            annotations = dict(raw.get("annotations") or {}) \
                if isinstance(raw.get("annotations"), dict) else {}
            # the annotations the proxy ENFORCES, not the ones the server claimed
            annotations["readOnlyHint"] = tool["effect"] == EFFECT_PLAIN
            annotations["destructiveHint"] = tool["effect"] == EFFECT_EMISSION
            entry["annotations"] = annotations
            entry["x-revl"] = _verdict(tool)
            listed.append(entry)
        server_tools = {t["name"]: t for t in self.server._ADVERTISED}
        listed.extend(server_tools[name] for name in MANAGEMENT_VERBS
                      if name in server_tools)
        listed.append({
            "name": VERDICTS_TOOL,
            "description": "The proxy's verdict on every upstream tool: the effect "
                           "class it enforces, the honesty of its read-only claim, "
                           "why, and any tool listed upstream but not proxied.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        })
        return listed

    def _relay(self, message: dict) -> None:
        self._send(message)

    def _send(self, message: dict) -> None:
        sink = self.sink
        if sink is not None:
            sink(message)
            return
        with self._out_lock:
            self.stdout.write(json.dumps(message) + "\n")
            self.stdout.flush()

    def describe(self, requested: str | None = None) -> dict:
        """The `initialize` result. Pure: it reads the proxy and touches neither
        the session nor the upstream, so the HTTP transport may read it without
        the dispatch lock (`server/discover`, `subscriptions/listen`)."""
        capabilities = {"tools": {"listChanged": True}}
        for passthrough in ("resources", "prompts", "logging", "completions"):
            if passthrough in self.upstream_capabilities:
                capabilities[passthrough] = self.upstream_capabilities[passthrough]
        name = self.upstream_info.get("name") or "the upstream server"
        return {
            "protocolVersion": requested if requested in _KNOWN_PROTOCOLS
            else PROTOCOL_VERSION,
            "capabilities": capabilities,
            "serverInfo": {"name": "revl-mcp-proxy", "version": "2.0"},
            "instructions": (
                f"Every tool of {name} is gated by revl. A call to a tool "
                "revl cannot show to be safe returns `approvalRequired` with a ticket "
                "instead of running: relay the ticket to a human, who answers "
                "it with revl_approve. revl_proxy_verdicts lists each tool's "
                "class and why."),
        }

    def handle(self, message: dict) -> dict | None:
        """One client JSON-RPC message -> one response (None for a notification)."""
        if self.upstream.tools_changed:
            self.upstream.tools_changed = False
            try:
                self.reclassify(relist=True)
            except UpstreamError:
                pass
            self._send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            result = self.describe(params.get("protocolVersion"))
        elif method == "tools/list":
            result = {"tools": self._advertised()}
        elif method == "tools/call":
            name = params.get("name")
            if name in MANAGEMENT_VERBS:
                response = self.server.handle(message)
                if name in ("revl_commit_confirm", "revl_abort"):
                    self._ensure_loaded_quietly()
                return response
            if name == VERDICTS_TOOL:
                result = _payload_result(self.verdicts())
            elif name not in self.by_name:
                return _error(request_id, -32602, f"unknown tool: {name}")
            else:
                result = self.call_tool(name, params.get("arguments") or {},
                                        params.get("_meta"))
        elif method in PASSTHROUGH:
            try:
                result = self.upstream.request(method, params)
            except UpstreamError as error:
                return None if request_id is None else _error(request_id, -32603,
                                                              str(error))
        elif method == "ping":
            result = {}
        elif method in ("notifications/initialized", "initialized"):
            return None
        else:
            if request_id is None:
                return None
            return _error(request_id, -32601, f"method not found: {method}")
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _ensure_loaded_quietly(self) -> None:
        """After a commit or abort, boot the next generation so the client's
        next call finds a live surface. A refusal here (an E-Stopped session)
        is left for that next call to report."""
        try:
            self._ensure_loaded()
        except (SessionError, ApprovalRequired):
            pass

    def serve(self, stdin=None, before=None) -> int:
        """The stdio loop. `before` is `server.serve`'s: None to go on, or a
        reason to refuse the message (the live operator profile)."""
        stdin = stdin or sys.stdin
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._send(_error(None, -32700, "parse error"))
                continue
            if not isinstance(message, dict):
                self._send(_error(None, -32600, "invalid request"))
                continue
            refusal = before(message) if before is not None else None
            if refusal is not None:
                if message.get("id") is not None:
                    self._send(_error(message["id"], -32603, refusal))
                continue
            response = self.handle(message)
            if response is not None:
                self._send(response)
        return 0


# ---------------------------------------------------------------- shaping

def _verdict(tool: dict) -> dict:
    action_class = {EFFECT_PLAIN: None, EFFECT_WITNESSED: "a",
                    EFFECT_EMISSION: "c"}[tool["effect"]]
    verdict = {
        "tool": tool["name"],
        "classification": tool["effect"],
        "actionClass": action_class,
        "readOnlyClaim": tool["readOnlyClaim"],
        "reasons": list(tool["reasons"]),
        "approval": {EFFECT_PLAIN: "proceeds: no crossing is declared",
                     EFFECT_WITNESSED: "proceeds: its declared undo runs on abort",
                     EFFECT_EMISSION: "a human yes per call (class c ticket)"}
        [tool["effect"]],
        "classifiedBy": "revl mcp import",
    }
    if tool.get("gated") is not None:
        verdict["gated"] = tool["gated"]
        verdict["approval"] = ("a human yes per call (class c ticket): the "
                               "read-only claim is unchecked, and this proxy does "
                               "not trust an unchecked claim without "
                               "--trust-read-only-hints")
    elif tool["effect"] == EFFECT_PLAIN:
        verdict["trusted"] = ("an unchecked read-only claim, admitted because the "
                              "operator passed --trust-read-only-hints")
    if tool["undo"] is not None:
        verdict["undo"] = dict(tool["undo"])
    if tool["unclassifiable"] is not None:
        verdict["unclassifiable"] = tool["unclassifiable"]
    return verdict


def _result_text(result: dict) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    if parts:
        return "\n".join(parts)
    return json.dumps(result.get("structuredContent", result.get("content", [])))


def _payload_result(payload: dict) -> dict:
    """A revl verdict as a CallToolResult, the shape `revl mcp serve` uses."""
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": not payload.get("ok", False),
            "structuredContent": payload}


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class _Discard:
    """The proxy's stdout under HTTP, where nothing is written to stdout: the
    transport routes every client-bound notification through `Proxy.sink`
    instead (docs/mcp-http-transport.md)."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass


def run(command: list[str], *, undo: dict | None = None,
        trust_read_only: bool = False, timeout: float = 120.0,
        stdin=None, stdout=None, stderr=None, http: dict | None = None,
        live=None) -> int:
    """`revl mcp proxy -- COMMAND...`: serve until the client closes stdin, or,
    with `http` (`{"exposure", "auth", and "profile_path" or "registry"}`),
    until interrupted. `live` is the stdio loop's per-message hook
    (`live_profile.StdioBinding`)."""
    stderr = stderr or sys.stderr
    upstream = Upstream(command, timeout=timeout)
    proxy = Proxy(upstream, undo=undo, trust_read_only=trust_read_only,
                  stdout=_Discard() if http is not None else stdout)
    proxy.activate()
    try:
        upstream.start()
        try:
            proxy.connect()
        except (UpstreamError, RevlError, SessionError, ApprovalRequired) as error:
            print(f"error: revl mcp proxy cannot gate {command!r}: {error}",
                  file=stderr)
            return 1
        for verdict in proxy.verdicts()["tools"]:
            state = (f"gated: {verdict['gated']}" if "gated" in verdict
                     else f"read-only claim: {verdict['readOnlyClaim']}")
            print(f"revl mcp proxy: {verdict['tool']}: {verdict['classification']} "
                  f"({state})", file=stderr)
        if trust_read_only:
            print("revl mcp proxy: --trust-read-only-hints: every unchecked "
                  "read-only claim above is TRUSTED, not verified", file=stderr)
        for excluded in proxy.excluded:
            print(f"revl mcp proxy: not proxied: tool #{excluded['index']} "
                  f"{excluded['name']!r}: {excluded['excludedBecause']}", file=stderr)
        if http is not None:
            from .http_transport import HttpTransport, ProxyDispatcher, TransportError  # noqa: PLC0415

            try:
                transport = HttpTransport(ProxyDispatcher(proxy),
                                          registry=http.get("registry"),
                                          profile_path=http.get("profile_path"),
                                          profile_settle_ms=http.get("profile_settle_ms",
                                                                     1000),
                                          exposure=http["exposure"],
                                          auth=http.get("auth", "bearer"),
                                          server_module=proxy.server)
            except TransportError as error:
                print(f"error: {error}", file=stderr)
                return 1
            code = transport.serve_forever(stderr=stderr)
        else:
            code = proxy.serve(stdin, before=live)
        _abort_at_exit(proxy, stderr)
        return code
    finally:
        proxy.deactivate()
        upstream.close()


def _abort_at_exit(proxy: Proxy, stderr) -> None:
    """The client closed the connection without a verdict. Treat it as the
    crash it is to the transaction: abort, which replays the declared undo of
    every witnessed call not yet committed. Done here because this is the last
    moment the upstream is reachable; `revl recover` later could only record
    those undos as residue."""
    session = proxy.session
    if not session.loaded or session.halted:
        return
    try:
        result = session.abort()
    except SessionError as error:
        print(f"revl mcp proxy: could not abort the uncommitted session at exit: "
              f"{error}", file=stderr)
        return
    print(f"revl mcp proxy: the session ended without a commit; aborted, "
          f"replayed {len(result.get('replayed') or [])} declared undo(s), "
          f"residue-free: {bool(result.get('noResidue'))}", file=stderr)
