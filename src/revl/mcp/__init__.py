"""revl ↔ MCP: the boundary bridge, and the compiler as a tool server.

`schema` — projects a composition's provided services to MCP tool
definitions (and MCP tool definitions back to a `.rvl` service + extern
skeleton). The projection is the point: an MCP tool's `readOnlyHint` /
`destructiveHint` are unverified assertions by the server author, while a
revl service carries `emission` classifications the checker *enforces*, so
a generated tool description cannot lie about side effects.

`server` — the compiler/runtime itself as an MCP server over stdio, so an
agent drives a revl system through a typed protocol whose every mutation
passes the admission gate instead of through filesystem access.
"""

from .schema import import_tools, tools_from_ir

__all__ = ["tools_from_ir", "import_tools"]
