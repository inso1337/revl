"""`revl_context`: the declarations an edit may depend on, as an MCP tool.

The MCP twin of `revl context` (docs/context.md). The answer is `revl.context`
verbatim: `pack` is the text to put in the prompt, `services` the same content
as rows, `tokens` the pack's size beside the files it was cut from.
"""

from __future__ import annotations

from ..context import context
from ..diagnostics import report
from ..errors import RevlError
from ..parser import Parser
from .query_tools import _SOURCE_INPUT

_INLINE = "<context>.rvl"


def _usage(message: str) -> dict:
    # the server's refusal shape, not a copy of it; imported here because the
    # server imports this module
    from .server import _session_error  # noqa: PLC0415
    return _session_error(message, category="usage")


def _tool_context(arguments: dict) -> dict:
    source = arguments.get("source")
    modules = dict(arguments.get("modules") or {})
    budget = arguments.get("budget")
    if budget is not None and (type(budget) is not int or budget < 1):
        return _usage("`budget` must be a positive integer (a token ceiling)")
    try:
        if source is not None:
            # a bare source reads nothing from the disk, as `compile_source`
            # holds it: its imports come in `modules` or not at all
            if not modules and Parser(source, _INLINE).parse().uses:
                return _usage("`use` declarations need `modules` (in-memory "
                              "sources) or `files` with a real source path")
            paths, sources = [_INLINE], {**modules, _INLINE: source}
        elif arguments.get("files"):
            paths, sources = list(arguments["files"]), modules
        else:
            return _usage("provide `source` or `files`")
        return context(paths, sources=sources or None,
                       component=arguments.get("component"), budget=budget)
    except RevlError as error:
        return report(error)


CONTEXT_TOOL = {
    "name": "revl_context",
    "description":
        "WHAT CAN THIS COMPONENT SEE? The service declarations a component "
        "must provide and may require, cut verbatim from the source with no "
        "provider body: operations, types, `emission` markers, the "
        "`requires`/`provides` clause to write, the `use` line when the "
        "service lives in another module, and a `hash` that moves on a "
        "signature edit and never on a doc edit. Read it instead of the "
        "composition before writing a component. Builds from the parse, so a "
        "component whose body does not check yet still grounds. `budget` "
        "shrinks the pack in a fixed order (docs off, then collapse, from the "
        "last service backwards) and never drops a service. `tokens` gives "
        "the pack beside the files it was cut from: on a tiny file the raw "
        "file can be the cheaper read.",
    "inputSchema": {
        "type": "object",
        "properties": {
            **_SOURCE_INPUT,
            "component": {"type": "string",
                          "description": "the component to ground (default: "
                                         "every declared service)"},
            "budget": {"type": "integer", "minimum": 1,
                       "description": "the pack's token ceiling"},
        },
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
    "handler": _tool_context,
}
