"""The symbol model, as the MCP server uses it (issues #1714, #1733).

It lives in `revl.symbols`, outside `revl.mcp`, so the registry's publish
check can resolve an anchor without putting the server on the compile graph
(issue #1780). This module is that same module object, with the server's
`<buffer>:` resolver installed: a loaded file's path, basename or trailing
path (`revl.mcp.edit._resolve_buffer`), and its canonical-form hook for a body
fragment (`canonical`, issue #1700).
"""

import sys

from .. import symbols as _symbols


def _resolve(vs: dict, wanted: str):
    from .edit import EditError, _resolve_buffer  # noqa: PLC0415 - cycle

    try:
        return _resolve_buffer(vs, wanted)
    except EditError as error:
        read_only = _read_only(vs, wanted)
        if read_only is not None:
            return read_only
        raise _symbols.SymbolError(str(error)) from None


def _read_only(vs: dict, wanted: str):
    """The read-only dependency buffer `wanted` names, or None (issue #1779).

    `_resolve_buffer` refuses a dependency buffer by name, because nothing may
    WRITE one. Addressing a declaration inside one by `<buffer>:Name` is a read,
    though, and `revl_source` has to be able to reach a truc reached through
    `use` the same way it reaches a loaded file — so the refusal is answered
    with the buffer itself. An edit that resolves through here still cannot
    write it: `_set_text` refuses the same buffer by name."""
    from .edit import _readonly_match  # noqa: PLC0415 - cycle

    path = _readonly_match(vs, wanted)
    return ("dependency", path) if path is not None else None


def canonical(fragment: str) -> str:
    """`fragment` as `revl fmt` would write it (issue #1700): an agent may send
    terse text and the server stores the canonical form, under the formatter's
    IR-equivalence gate. A fragment the formatter cannot read, or a rewrite the
    gate refuses, is returned unchanged, so the compile reports the real
    error. Here, beside the server, because `revl.mcp.canonical` is not on the
    compile graph (issue #1780); `revl.symbols` calls it through its
    `CANONICALISE` hook."""
    from .canonical import canonicalise  # noqa: PLC0415
    return canonicalise(fragment, "<edit>").text


_symbols.BUFFER_RESOLVER = _resolve
_symbols.canonical = canonical
_symbols.CANONICALISE = canonical
sys.modules[__name__] = _symbols
