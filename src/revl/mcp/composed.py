"""Serve a composition's OWN operations as MCP tools — the fourth quadrant.

`revl mcp serve` serves the *compiler's* tools (revl_check, revl_swap …): an
agent operates the toolchain. This is the mirror image: boot one composition
and put *its provided operations* on the wire, so an agent (or any MCP client)
calls the running system directly — `cache.get`, `bus.send` — instead of
driving the compiler.

Why this is the only trustworthy `readOnlyHint` in MCP. Everywhere else a
tool's `readOnlyHint`/`destructiveHint` is an assertion by the server author,
and nothing checks it — the tool-poisoning trust gap. Here every tool is
projected by :func:`revl.mcp.schema.tools_from_ir` from the *checked* emission
classification: a tool is read-only because the compiler refused to compile
its provider otherwise, a destructive tool names the boundary it crosses, and
the implementation behind the tool surface can only change through the
admission gate. The hints are compiler-derived, not author-asserted.

The plumbing already exists: `tools_from_ir` is the projection, and a
:class:`revl.mcp.session.Session` already boots a composition and drives a
provided operation via ``Session.call``. This module is the wiring — projected
tools onto the JSON-RPC wire, each ``tools/call`` routed to the live session —
plus a config-to-boot story (:func:`serve_composition`) so a standalone
``revl serve --mcp app.rvl`` can stand a composition up on its own.
"""

from __future__ import annotations

import json
import sys

from .schema import tools_from_ir
from .session import Session, SessionError

PROTOCOL_VERSION = "2024-11-05"


def _positional(arguments: dict, param_names: list[str]) -> list:
    """Named MCP arguments -> the positional list ``Session.call`` wants.

    The projected `inputSchema.properties` is insertion-ordered in the
    operation's declared parameter order (schema.py builds it by walking
    `params`), so the property keys ARE the call signature. A missing optional
    parameter becomes a `None` in place; trailing omitted parameters are
    dropped so the provider's own default applies rather than a forced `None`.
    """
    args = [arguments.get(name) for name in param_names]
    while args and param_names[len(args) - 1] not in arguments:
        args.pop()
    return args


class ComposedServer:
    """A booted composition, its provided operations advertised as MCP tools.

    One live :class:`Session` drives the composition; each advertised tool
    resolves to a ``(key, method, param_names)`` route, and a ``tools/call``
    lands on ``Session.call`` against the running system.
    """

    def __init__(self, session: Session, composition: str = "revl") -> None:
        self.session = session
        self.composition = composition
        self._advertised: list[dict] = []
        self._routes: dict[str, tuple[str, str, list[str]]] = {}
        self._project(session.ir or {})

    def _project(self, ir: dict) -> None:
        """Reuse the schema projection, and remember how each tool name maps
        back onto a provided key + operation for dispatch."""
        self._advertised = []
        self._routes = {}
        for tool in tools_from_ir(ir, composition=self.composition):
            provenance = tool.get("x-revl") or {}
            key = provenance.get("key")
            method = provenance.get("operation")
            params = list((tool.get("inputSchema") or {}).get("properties") or {})
            self._routes[tool["name"]] = (key, method, params)
            self._advertised.append(tool)

    # -- protocol ----------------------------------------------------------

    def _call_tool(self, name: str, arguments: dict) -> dict:
        key, method, param_names = self._routes[name]
        args = _positional(arguments or {}, param_names)
        try:
            return {"ok": True, **self.session.call(key, method, args)}
        except SessionError as error:
            return {"ok": False, "diagnostics": [{
                "severity": "error", "code": "REVL", "category": "session",
                "message": str(error),
            }]}
        except Exception as exc:  # the callee raised — a result, not a crash
            return {"ok": False, "raised": True, "diagnostics": [{
                "severity": "error", "code": "REVL", "category": "runtime",
                "message": f"{type(exc).__name__}: {exc}",
            }], "trace": self.session.state().get("trace", [])}

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            manifest = (self.session.ir or {}).get("manifest") or {}
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"revl:{self.composition}", "version": "2.0"},
                "instructions": (
                    "These tools ARE a running revl composition "
                    f"({' -> '.join(manifest.get('loadOrder') or []) or 'empty'}). "
                    "Every readOnlyHint/destructiveHint is derived by the compiler "
                    "from the checked emission classification, not asserted by an "
                    "author: a tool is read-only only where the checker refused "
                    "unreverted mutation."),
            }
        elif method == "tools/list":
            result = {"tools": self._advertised}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name not in self._routes:
                return _error(request_id, -32602, f"unknown tool: {name}")
            payload = self._call_tool(name, params.get("arguments") or {})
            result = {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": not payload.get("ok", False),
                "structuredContent": payload,
            }
        elif method in ("notifications/initialized", "initialized"):
            return None
        elif method == "ping":
            result = {}
        else:
            if request_id is None:
                return None
            return _error(request_id, -32601, f"method not found: {method}")

        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, stdin=None, stdout=None) -> int:
        """Read newline-delimited JSON-RPC from stdin until EOF."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
                stdout.flush()
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
        return 0


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def serve_composition(ir: dict, config: dict | None = None, *,
                      composition: str = "revl", stdin=None, stdout=None) -> int:
    """Boot `ir` into a live session and serve its provided operations.

    Booting is admission: a composition is loaded through the same
    `Session.load` a live `revl_load` runs, so open typed holes are refused
    here exactly as they are there. The caller (the CLI entry) owns the config
    preflight, mirroring `revl run` — config reaches each component at boot.
    """
    session = Session()
    # origin is left None: this served composition exposes no snapshot tool, so
    # there is nothing to replay through the gate later (session.py documents
    # None as "this session cannot be snapshotted").
    session.load(ir, config or {}, origin=None)
    return ComposedServer(session, composition=composition).serve(stdin, stdout)
