"""The symbol model, as the MCP server uses it (issues #1714, #1733).

It lives in `revl.symbols`, outside `revl.mcp`, so the registry's publish
check can resolve an anchor without putting the server on the compile graph
(issue #1780). This module is that same module object, with the server's
`<buffer>:` resolver installed: a loaded file's path, basename or trailing
path (`revl.mcp.edit._resolve_buffer`).
"""

import sys

from .. import symbols as _symbols


def _resolve(vs: dict, wanted: str):
    from .edit import EditError, _resolve_buffer  # noqa: PLC0415 - cycle

    try:
        return _resolve_buffer(vs, wanted)
    except EditError as error:
        raise _symbols.SymbolError(str(error)) from None


_symbols.BUFFER_RESOLVER = _resolve
sys.modules[__name__] = _symbols
