# Roadmap archive: Agent, interop & test surface (ranked)

Closed items moved verbatim from [docs/v2.0-roadmap.md](../v2.0-roadmap.md). The tools read this file from its "## " heading on.

## Agent, interop & test surface (ranked)

1. ✅ **`??` is unimplemented on rust, java and wasm** — a documented 2.0
   operator that is python-only today (TypeScript has it in `fn` bodies but
   not component bodies). The lowering is well-formed; three operator cases
   are simply missing.
2. ✅ **Calling a pure `fn` from a component body fails on TypeScript and
   wasm** — the `fn` call node is in neither renderer's kind set. This one
   cause is 9 of TypeScript's 12 refusals.
3. ✅ **`match` is not usable in a component or method body** — frontend-level,
   so every tier is equally affected; ADT-consuming components must call out
   to a `fn`.
4. ✅ **A bare `return` does not parse**, though the IR and two emitters already
   model it — the natural body for a void service operation.
