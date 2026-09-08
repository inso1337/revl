// Node-tier host shim for `stdlib/server.rvl`'s `Server` service (roadmap item
// 457 S3, docs/design/457-endpoint-one-definition.md). The ts peer of the
// pattern `backends/typescript/revl_fs_ts.ts` and `revl_shell_ts.ts` establish:
// a shipped module a stdlib `@ts ref` extern imports, so a revl component reaches
// an ambient host service (here Cordis `@cordisjs/server`'s `ctx.server`) through
// the REVIEWED host-reference door (item 396 option B) rather than a `globalThis`
// bridge.
//
// ---------------------------------------------------- how stdlib/server.rvl reaches it
// A `host @server provides server: Server` row synthesizes a provider whose
// externs are `= @ts ref <verb> from "../backends/typescript/revl_server_ts.ts"`
// (src/revl/synthesize.py `_host_source`). The multi-root stdlib-ref import
// (item 410) resolves and loads this module at the extern's FIRST CALL, so no
// host code runs at artifact load; the ref is hash-pinned in the IR and jailed to
// the install root.
//
// Each exported entry point registers one route method on the server. The Cordis
// server is reached from the fiber's Context, which the caller threads in as the
// first argument (the route-registration EMITTER wires the actual handler — item
// 457 S3's emitted-`ctx.server` half; this shim is the reviewed door the ref
// points at, and the handler-value contract is finalized against the exemplary
// app, item 462). Each entry point is a thin, side-effecting registration over
// `ctx.server[verb](path, handler)`, matching `@cordisjs/server`'s method set
// (`packages/core/src/index.ts`).
//
// node builtins are reached with `process.getBuiltinModule(...)` and never a
// bare `require`/top-level `import` of a node module, per the emitted-ts runtime
// contract (a `@ts ref` module runs under plain node ESM).

// The Cordis Context shape this shim touches — narrowed to `ctx.server`'s route
// registration surface so the shim needs no dependency on `@cordisjs/server`'s
// own types at build time (the running host provides the real object).
type RouteHandler = (req: unknown, res: unknown) => unknown;
interface CordisServer {
  get(path: string, handler: RouteHandler): void;
  post(path: string, handler: RouteHandler): void;
  put(path: string, handler: RouteHandler): void;
  patch(path: string, handler: RouteHandler): void;
  delete(path: string, handler: RouteHandler): void;
  head(path: string, handler: RouteHandler): void;
}
interface HostContext {
  server: CordisServer;
}

// The handler the route-registration emitter binds (item 457 S3, emitted half).
// Until that lands, a registered-but-unbound route answers `501 Not Implemented`
// rather than silently 404-ing, so a mis-wired composition is loud, not quiet.
function pendingHandler(_req: unknown, res: unknown): void {
  const r = res as { status?: number; body?: unknown };
  r.status = 501;
  r.body = "route handler not yet bound (item 457 S3 emitted half)";
}

function register(
  ctx: HostContext,
  verb: keyof CordisServer,
  path: string,
  handler: RouteHandler = pendingHandler,
): void {
  ctx.server[verb](path, handler);
}

export function get(ctx: HostContext, path: string): void {
  register(ctx, "get", path);
}
export function post(ctx: HostContext, path: string): void {
  register(ctx, "post", path);
}
export function put(ctx: HostContext, path: string): void {
  register(ctx, "put", path);
}
export function patch(ctx: HostContext, path: string): void {
  register(ctx, "patch", path);
}
function del(ctx: HostContext, path: string): void {
  register(ctx, "delete", path);
}
export { del as delete };
export function head(ctx: HostContext, path: string): void {
  register(ctx, "head", path);
}
