// Node-tier host support for the witnessed `stdlib/fs.rvl` catalog on the ts
// tier (roadmap item 369 — the ts peer of backends/python/revl_fs_workspace.py).
//
// ---------------------------------------------------------------- why a global
// A `@ts` extern body is spliced VERBATIM into `export function name(...) {...}`
// (backends/typescript/emit.py `_emit_ts_externs`), so — unlike a `@py` body,
// which can `import revl_fs_workspace` at function scope — it cannot carry its
// own `import`. And these helpers need `node:fs` (real filesystem), which the
// deliberately environment-neutral runtime.ts must not drag in (it imports zero
// node builtins, so it stays browser-targetable). So this module installs the
// helpers on `globalThis.__revlFs` at import time and the `@ts` fs bodies reach
// them there — the same `globalThis` seam the witnessed teardown fixture uses
// for host state (`__revlWitnessBox`, tests/fixtures/_gen_witnessed_teardown.py).
//
// The py analog's contract is preserved verbatim (docs/witnessed-fs.md, the
// "three musts"): symlinks resolved BEFORE the membership check, the guard
// applied to the inverse path too, and the garbage dir + preimage snapshots
// living INSIDE the workspace root so reversal can never escape.
//
// Loading: a node entrypoint (a test harness, or a future ts session runner —
// the analog of `backends/python` being on `sys.path`) imports this module for
// its install side effect before any witnessed fs body runs.

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

// Install on import (the side effect the fs bodies depend on).
;(globalThis as unknown as { __revlFs?: RevlFsHost }).__revlFs = HOST
