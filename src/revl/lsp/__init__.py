"""A human-facing Language Server for revl (roadmap item 126, slice 1).

The MCP server (`revl.mcp`) is the agent-facing language server; this package
is its counterpart for a person in an editor — inline diagnostics, hover, and
go-to-definition over the same compiler surfaces, spoken as LSP.

Slice 1 is deliberately small: three capabilities, each reusing machinery
`revl` already runs (`compile_source` for diagnostics, `diagnostics.explain`
for hover text, the parser AST for symbols). Run it as `revl lsp`; the
absolute-interpreter fallback is `python -P -m revl.lsp` (the `-P` is the
PYTHONSAFEPATH safety bit, issue #317).
"""

from __future__ import annotations

from .analysis import (
    compute_code_actions,
    compute_definition,
    compute_diagnostics,
    compute_hover,
)
from .document import Position
from .fixgen import Fix, fix_code, generate_fix
from .server import LspServer, serve

__all__ = [
    "LspServer",
    "serve",
    "Position",
    "compute_diagnostics",
    "compute_hover",
    "compute_definition",
    "compute_code_actions",
    "Fix",
    "fix_code",
    "generate_fix",
]
