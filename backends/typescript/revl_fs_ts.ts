// Node-tier host support for the witnessed `stdlib/fs.rvl` catalog on the ts
// tier (roadmap item 369 — the ts peer of backends/python/revl_fs_workspace.py).
//
// ---------------------------------------------------- how stdlib/fs.rvl reaches it
// item 410 stage 5: `stdlib/fs.rvl`'s `@ts` externs import this module's
// per-extern entry points directly via `= @ts ref fsWrite from ".../revl_fs_ts.ts"`
// (the multi-root stdlib-ref import: origin selects the install root, the runner
// provides `__REVL_STDLIB_REF_ROOT__`, the ref is hash-pinned in the IR). The
// thunk resolves and loads this module at the extern's FIRST CALL, so no host
// code runs at artifact load. Each op's logic lives in its exported entry point
// (`fsWrite`/`fsRm`/`fsMove`/`fsMkdir` + the inverses `fsRestore`/`fsUnrm`/
// `fsUnmove`/`fsRmdirIfEmpty`), returning the `{ kind, value }` Result / Unit the
// witnessed frame reads.
//
// Historically these helpers were reached through `globalThis.__revlFs`: a
// verbatim `@ts` body cannot carry its own `import`, and these helpers need
// `node:fs` (real filesystem), which the deliberately environment-neutral
// runtime.ts must not drag in (it imports zero node builtins, so it stays
// browser-targetable). The `globalThis.__revlFs` install below is RETAINED for
// one deprecation release for out-of-tree embedders (and the harness's
// `fs_host_install`) that still pre-import this module for the global; nothing
// in-tree depends on it after the ref migration.
//
// The py analog's contract is preserved verbatim (docs/witnessed-fs.md, the
// "three musts"): symlinks resolved BEFORE the membership check, the guard
// applied to the inverse path too, and the garbage dir + preimage snapshots
// living INSIDE the workspace root so reversal can never escape.

import * as fs from 'node:fs'
import * as path from 'node:path'
import { randomUUID } from 'node:crypto'

/** The environment variable naming the session workspace root (ts tier — same
 * name and semantics as the py tier, backends/python/revl_fs_workspace.py). */
export const WORKSPACE_ENV = 'REVL_FS_WORKSPACE'

/** Subdirectory names, inside the root, that hold the reversal machinery. Both
 * are inside the workspace root (must #3), so a target under them still passes
 * `resolveWithin`. */
export const GARBAGE_DIRNAME = '.revl-fs-garbage'
export const PREIMAGE_DIRNAME = '.revl-fs-preimage'

/** The FsError record shape a forward op returns on the `Err` branch. Mirrors
 * `stdlib/fs.rvl`'s `FsError = { code, message, path }`. */
export interface FsError {
  code: string
  message: string
  path: string
}

/** A witnessed fs op (or its inverse) targeted a path outside the session
 * workspace root, or no root was configured. Carries the (code, message, path)
 * shape the `FsError` record uses, so a `@ts` body can turn it straight into an
 * `Err(...)` on the forward path — the peer of the py `ConfinementError`. */
export class ConfinementError extends Error {
  code: string
  path: string
  constructor(code: string, message: string, p = '') {
    super(`${code}: ${message} (${p})`)
    this.name = 'ConfinementError'
    this.code = code
    this.path = p
  }
  asError(): FsError {
    return { code: this.code, message: this.message, path: this.path }
  }
}

/** `os.path.realpath` for a path whose LEAF may not exist yet (a write/mkdir
 * target). node's `fs.realpathSync` throws ENOENT on a missing path, so this
 * resolves the longest existing prefix (symlinks and all) and re-appends the
 * non-existent tail, exactly as py's `os.path.realpath` normalizes a missing
 * leaf. Symlinks in every existing component are therefore resolved BEFORE the
 * membership test (must #1). */
function realpathish(p: string): string {
  const abs = path.resolve(p)
  const tail: string[] = []
  let cur = abs
  for (;;) {
    try {
      const real = fs.realpathSync.native(cur)
      if (tail.length === 0) return real
      return path.join(real, ...tail.slice().reverse())
    } catch {
      const parent = path.dirname(cur)
      if (parent === cur) return path.normalize(abs)
      tail.push(path.basename(cur))
      cur = parent
    }
  }
}

/** The configured session workspace root, realpath-resolved. Throws
 * `ConfinementError('EWORKSPACE', ...)` when unset or not a directory — an fs
 * op with no configured root is refused, never silently allowed to touch the
 * whole filesystem (peer of py `workspace_root`). */
export function workspaceRoot(): string {
  const root = process.env[WORKSPACE_ENV]
  if (!root) {
    throw new ConfinementError(
      'EWORKSPACE',
      `no session workspace root configured (set ${WORKSPACE_ENV})`,
      '',
    )
  }
  const real = fs.realpathSync.native(root)
  if (!fs.statSync(real).isDirectory()) {
    throw new ConfinementError(
      'EWORKSPACE',
      'configured session workspace root is not a directory',
      real,
    )
  }
  return real
}

/** True iff `real` is the root itself or a descendant of it. The `+ path.sep`
 * guards against a sibling that merely shares the root as a name prefix
 * (`/ws` vs `/ws-evil`) — the peer of py `_is_within`. */
function isWithin(root: string, real: string): boolean {
  return real === root || real.startsWith(root + path.sep)
}

/** Realpath `path` (must #1: symlinks resolved BEFORE the check) and refuse it
 * unless it lands inside the session workspace root. A relative path is taken
 * relative to the root. The single choke point every forward op AND every
 * inverse (must #2) routes through — peer of py `resolve_within`. */
export function resolveWithin(p: string): string {
  const root = workspaceRoot()
  const target = path.isAbsolute(p) ? p : path.join(root, p)
  const real = realpathish(target)
  if (!isWithin(root, real)) {
    throw new ConfinementError(
      'EOUTSIDE',
      'path escapes the session workspace root',
      real,
    )
  }
  return real
}

/** The session garbage directory, created inside the workspace root (must #3).
 * `rm` renames its target in here; `unrm` renames it back out. */
export function garbageDir(): string {
  const d = path.join(workspaceRoot(), GARBAGE_DIRNAME)
  fs.mkdirSync(d, { recursive: true })
  return d
}

/** The preimage-snapshot directory, created inside the workspace root (must
 * #3). `write` snapshots the target's preimage in here; `restore` reads it. */
export function preimageDir(): string {
  const d = path.join(workspaceRoot(), PREIMAGE_DIRNAME)
  fs.mkdirSync(d, { recursive: true })
  return d
}

/** A unique, non-colliding path inside `directory` for a snapshot or a parked
 * file — the uuid keeps concurrent ops and repeated same-name removals from
 * clobbering one another's sidecars (peer of py `fresh_sidecar`). */
export function freshSidecar(directory: string, tag: string): string {
  return path.join(directory, `${tag}-${randomUUID().replace(/-/g, '')}`)
}

/** Copy `src` to a fresh `dst`, preferring an APFS `clonefile()` CoW clone
 * (node's `COPYFILE_FICLONE` asks the kernel for a clone and transparently
 * falls back to a byte copy on any other filesystem/platform). Peer of py
 * `snapshot` — the CoW clone is why `write`'s preimage is cheap for a large
 * file: storage is shared until the subsequent write diverges it. */
export function snapshot(src: string, dst: string): void {
  fs.copyFileSync(src, dst, fs.constants.COPYFILE_FICLONE)
}

// ---------------------------------------------------------------- fs primitives
// Thin, synchronous wrappers over `node:fs`, named to mirror the `os.*` calls
// the py bodies make, so a `@ts` body reads almost line for line like its `@py`
// peer. Exposed on `__revlFs` because a verbatim `@ts` body cannot import
// `node:fs` itself.

/** `os.path.exists` — true iff the path exists (following symlinks). */
export function exists(p: string): boolean {
  try {
    fs.statSync(p)
    return true
  } catch {
    return false
  }
}

/** `os.path.isfile` — true iff the path is a regular file. */
export function isFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile()
  } catch {
    return false
  }
}

/** `os.path.isdir` — true iff the path is a directory. */
export function isDir(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory()
  } catch {
    return false
  }
}

/** `os.remove` — delete a file. */
export function remove(p: string): void {
  fs.rmSync(p)
}

/** `os.replace` — atomic rename that overwrites an existing destination (the
 * atomicity the inverses rely on: restore/unrm/unmove consume their sidecar in
 * one rename, residue-free). */
export function replace(src: string, dst: string): void {
  fs.renameSync(src, dst)
}

/** `os.mkdir` — create a single directory; throws if it already exists (the
 * forward `mkdir` op has already ruled that out). */
export function mkdirOne(p: string): void {
  fs.mkdirSync(p)
}

/** `os.rmdir` — remove an empty directory; throws (caught by the caller) on a
 * non-empty or missing dir, so `rmdir_if_empty` stays total. */
export function rmdir(p: string): void {
  fs.rmdirSync(p)
}

/** `open(p, "w").write(contents)` — overwrite/create a file with UTF-8 text. */
export function writeFile(p: string, contents: string): void {
  fs.writeFileSync(p, contents, { encoding: 'utf-8' })
}

/** `os.path.dirname`. */
export function dirname(p: string): string {
  return path.dirname(p)
}

// -------------------------------------------------------- per-extern entry points
// item 410 stage 5: the entry points a `stdlib/fs.rvl` `= @ts ref` thunk imports
// at first call, one per witnessed op and one per inverse. Each carries the exact
// logic the module's inline `@ts` body used to hold — the same three-musts
// contract (docs/witnessed-fs.md) — now reachable as a normal named export
// instead of through the `globalThis.__revlFs` seam. The ref thunk calls the
// export positionally with the extern's parameters and returns its value
// verbatim, so a forward op returns the `{ kind, value }` Result shape the
// witnessed frame keys off, and an inverse returns `void` (Unit).

/** `write`'s preimage witness — the ts mirror of `stdlib/fs.rvl`'s WriteWitness. */
export interface WriteWitness { path: string; preimage: string; created: boolean }
/** `rm`'s witness: the original path and where the target was parked. */
export interface RmWitness { path: string; garbage: string }
/** `move`'s witness: the resolved source and destination. */
export interface MoveWitness { from: string; to: string }
/** `mkdir`'s witness: the created directory. */
export interface MkdirWitness { path: string }

/** The `Result[Witness, FsError]` shape a forward fs op returns — the ts spelling
 * of the emitted `{ kind: 'Ok' | 'Err', value }` the witnessed runtime reads. */
export type FsResult<T> =
  | { kind: 'Ok'; value: T }
  | { kind: 'Err'; value: FsError }

/** Overwrite (or create) `path` with `contents`, snapshotting the preimage
 * first. Registers `undo fsRestore(result)` on Ok (the accumulator's binding). */
export function fsWrite(path: string, contents: string): FsResult<WriteWitness> {
  let target: string
  try {
    target = resolveWithin(path)     // must #1: realpath BEFORE the check
  } catch (e) {
    if (e instanceof ConfinementError) return { kind: 'Err', value: e.asError() }
    throw e
  }
  const existed = exists(target)
  let preimage = ''
  if (existed) {
    if (!isFile(target)) {
      return { kind: 'Err', value: { code: 'ENOTFILE',
        message: 'write target exists and is not a regular file',
        path: target } }
    }
    // snapshot the preimage INSIDE the root (must #3) before diverging it.
    preimage = freshSidecar(preimageDir(), 'pre')
    snapshot(target, preimage)
  } else {
    // require the parent to exist — we create no directories, so the
    // inverse (delete the created file) leaves zero residue.
    const parent = dirname(target)
    if (parent && !isDir(parent)) {
      return { kind: 'Err', value: { code: 'ENOENT',
        message: 'parent directory does not exist', path: target } }
    }
  }
  writeFile(target, contents)
  return { kind: 'Ok', value: { path: target, preimage, created: !existed } }
}

/** Remove `path` by parking it in the session garbage dir (inside the root).
 * Registers `undo fsUnrm(result)` on Ok. */
export function fsRm(path: string): FsResult<RmWitness> {
  let target: string
  try {
    target = resolveWithin(path)     // must #1
  } catch (e) {
    if (e instanceof ConfinementError) return { kind: 'Err', value: e.asError() }
    throw e
  }
  if (!exists(target)) {
    // Ok-conditional: an rm on a missing path registers nothing (item 243).
    return { kind: 'Err', value: { code: 'ENOENT', message: 'no such file', path: target } }
  }
  const parked = freshSidecar(garbageDir(), 'rm')   // must #3: inside root
  replace(target, parked)
  return { kind: 'Ok', value: { path: target, garbage: parked } }
}

/** Rename `from` -> `to` (both confined). Registers `undo fsUnmove(result)`. */
export function fsMove(src: string, dst: string): FsResult<MoveWitness> {
  let realFrom: string
  let realTo: string
  try {
    realFrom = resolveWithin(src)    // must #1, BOTH endpoints
    realTo = resolveWithin(dst)
  } catch (e) {
    if (e instanceof ConfinementError) return { kind: 'Err', value: e.asError() }
    throw e
  }
  if (!exists(realFrom)) {
    return { kind: 'Err', value: { code: 'ENOENT', message: 'no such source', path: realFrom } }
  }
  if (exists(realTo)) {
    return { kind: 'Err', value: { code: 'EEXIST', message: 'destination exists', path: realTo } }
  }
  replace(realFrom, realTo)
  return { kind: 'Ok', value: { from: realFrom, to: realTo } }
}

/** Create directory `path`. Registers `undo fsRmdirIfEmpty(result)` on Ok. */
export function fsMkdir(path: string): FsResult<MkdirWitness> {
  let target: string
  try {
    target = resolveWithin(path)     // must #1
  } catch (e) {
    if (e instanceof ConfinementError) return { kind: 'Err', value: e.asError() }
    throw e
  }
  if (exists(target)) {
    return { kind: 'Err', value: { code: 'EEXIST', message: 'path already exists', path: target } }
  }
  mkdirOne(target)
  return { kind: 'Ok', value: { path: target } }
}

/** Inverse of `fsWrite`: restore the preimage snapshot over the target, or
 * delete the created file. Confined (must #2), idempotent on replay. */
export function fsRestore(w: WriteWitness): void {
  // must #2: the inverse path is confined too. A throw here is caught by the
  // teardown loop as restore-residue, never a silent write outside root.
  const target = resolveWithin(w.path)
  if (w.created) {
    // the file did not exist before the write: undo == delete it.
    // idempotent — a second replay finds it already gone.
    if (exists(target)) remove(target)
    return
  }
  // restore the preimage snapshot over the target. renameSync is atomic and
  // consumes the snapshot (residue-free). idempotent — once the snapshot is
  // gone (already restored), a second replay is a no-op.
  if (exists(w.preimage)) replace(w.preimage, target)
}

/** Inverse of `fsRm`: rename the parked file back. Confined, idempotent. */
export function fsUnrm(w: RmWitness): void {
  const target = resolveWithin(w.path)   // must #2
  // rename the parked file back. idempotent — once moved back (garbage gone),
  // a second replay is a no-op.
  if (exists(w.garbage)) replace(w.garbage, target)
}

/** Inverse of `fsMove`: move it back. Both endpoints confined, idempotent. */
export function fsUnmove(w: MoveWitness): void {
  const src = resolveWithin(w.from)      // must #2: BOTH endpoints confined
  const dst = resolveWithin(w.to)
  // move it back. idempotent — once back (dst gone), a second replay no-ops.
  if (exists(dst)) replace(dst, src)
}

/** Inverse of `fsMkdir`: remove the created directory iff still empty. Total. */
export function fsRmdirIfEmpty(w: MkdirWitness): void {
  const target = resolveWithin(w.path)   // must #2
  // remove the created directory iff still empty — never delete a dir the
  // activation (or a concurrent writer) populated. idempotent + total: a
  // missing or non-empty dir is left as-is, no throw.
  try { rmdir(target) } catch (_e) { /* non-empty or missing: leave as-is */ }
}

/** The helper surface the `@ts` fs bodies reach through `globalThis.__revlFs`.
 * A flat record so a body reads `globalThis.__revlFs.resolveWithin(path)` — the
 * ts spelling of the py body's `_ws.resolve_within(path)`. */
export interface RevlFsHost {
  WORKSPACE_ENV: string
  GARBAGE_DIRNAME: string
  PREIMAGE_DIRNAME: string
  ConfinementError: typeof ConfinementError
  workspaceRoot: typeof workspaceRoot
  resolveWithin: typeof resolveWithin
  garbageDir: typeof garbageDir
  preimageDir: typeof preimageDir
  freshSidecar: typeof freshSidecar
  snapshot: typeof snapshot
  exists: typeof exists
  isFile: typeof isFile
  isDir: typeof isDir
  remove: typeof remove
  replace: typeof replace
  mkdirOne: typeof mkdirOne
  rmdir: typeof rmdir
  writeFile: typeof writeFile
  dirname: typeof dirname
}

const HOST: RevlFsHost = {
  WORKSPACE_ENV,
  GARBAGE_DIRNAME,
  PREIMAGE_DIRNAME,
  ConfinementError,
  workspaceRoot,
  resolveWithin,
  garbageDir,
  preimageDir,
  freshSidecar,
  snapshot,
  exists,
  isFile,
  isDir,
  remove,
  replace,
  mkdirOne,
  rmdir,
  writeFile,
  dirname,
}

// DEPRECATED (item 410 stage 5): install on import for out-of-tree embedders
// (and the harness `fs_host_install`) that still pre-import this module for the
// `globalThis.__revlFs` seam. `stdlib/fs.rvl` no longer reaches through it — it
// imports the per-extern entry points above via `= @ts ref`. Retained for one
// deprecation release, then removed with a note.
;(globalThis as unknown as { __revlFs?: RevlFsHost }).__revlFs = HOST
