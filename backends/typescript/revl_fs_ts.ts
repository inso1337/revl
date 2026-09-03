// Node-tier host support for the witnessed `stdlib/fs.rvl` catalog on the ts
// tier (roadmap item 369, the ts peer of backends/python/revl_fs_workspace.py).
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
// Three more entry points carry the OBSERVATION half of the surface
// (`fsResolveWithin`/`fsLexists`/`fsIsDir`, near the bottom of this file). They
// are what lets a CONSUMER's own `@ts` body reach the jail without importing
// this module at all: `stdlib/fs.rvl` re-exposes them as `pure` revl externs, so
// a consumer asks revl for a confined path and then reads it with plain
// `node:fs`. Observation only — the write primitives an earlier seam exported
// are item 422 F1 itself and stay gone.
//
// Historically these helpers were reached through `globalThis.__revlFs`: a
// verbatim `@ts` body cannot carry its own `import`, and these helpers need
// `node:fs` (real filesystem), which the deliberately environment-neutral
// runtime.ts must not drag in (it imports zero node builtins, so it stays
// browser-targetable). The `globalThis.__revlFs` install below is RETAINED for
// one deprecation release for out-of-tree embedders (and the harness's
// `fs_host_install`) that still pre-import this module for the global; nothing
// in-tree depends on it after the ref migration. It now carries the GUARD
// surface only: the unconfined primitives it used to publish (`replace`,
// `remove`, `writeFile`, `mkdirOne`, `rmdir`, `snapshot`) were themselves the
// escape described below, so they are gone rather than deprecated.
//
// ============================================================================
// The four path families (roadmap item 422 F1-F4, ported from the py tier)
// ============================================================================
//
// An earlier revision of this module claimed the py analog's contract was
// "preserved verbatim": symlinks resolved before the membership check, the
// guard applied to the inverse path too, the sidecars inside the root. It was
// not. `resolveWithin` guarded the paths that routed through it and nothing
// else, and three families of paths reached a filesystem syscall around it.
// The py tier closed the same four in `1602cc94`; this is that fix ported, not
// redesigned, and the claim is maintained here as the same EXPLICIT enumeration
// (`PATH_FAMILIES` below) with one guard entry point per family.
//
// 1. `named-endpoint` (`resolveWithin`), a path an op was HANDED: `write`'s
//    target, `rm`'s target, `move`'s two endpoints, `mkdir`'s target, and every
//    inverse's TARGET. Realpathed (must #1: symlinks resolved BEFORE the
//    membership test) and refused unless it lands inside the root.
// 2. `sidecar-directory` (`garbageDir`, `preimageDir`, `freshSidecar`), the
//    reversal machinery's OWN directories and the files made inside them (must
//    #3). Previously `path.join` + `fs.mkdirSync(d, { recursive: true })`,
//    returned unresolved. `mkdirSync` with `recursive` SUCCEEDS on a
//    pre-existing symlink, so one planted `.revl-fs-garbage` /
//    `.revl-fs-preimage` link inside the workspace redirected every preimage
//    snapshot and every parked `rm` OUT of the root, exfiltrating workspace
//    bytes and giving the attacker the file an inverse would later rename back
//    in. The directory is now created non-recursively, `lstat`-checked to be a
//    real directory rather than a link, and returned only after `resolveWithin`
//    confirms it; `freshSidecar` re-resolves its directory, requires it to BE
//    one of the two, and guards the leaf.
// 3. `inverse-source` (`resolveSidecar`), an inverse's SOURCE endpoint:
//    `fsRestore`'s `preimage`, `fsUnrm`'s `garbage`. Previously handed straight
//    to `replace()`, which is a RENAME, so the source is REMOVED from where it
//    lives: a witness naming a file outside the root both STOLE it and
//    DESTROYED it at its original location, from a capability-free `pure`
//    inverse callable from any pure position (and reconstructible by `revl
//    recover` from a forged WAL witness, with no revl source involved). The
//    source now goes through `resolveSidecar`, which confines it AND requires
//    it to be a sidecar this workspace itself produced.
// 4. `syscall-time` (`openConfinedWrite`, `writeThrough`, `snapshotPreimage`,
//    `confirmLanded`, `replaceConfined`, `removeConfined`, `mkdirConfined`,
//    `rmdirConfined`, `closeHandle`, `discardWrite`), the mutation itself.
//    `resolveWithin` followed by a separate NAME-BASED `fs.writeFileSync`
//    leaves a check-to-syscall window, and a competing writer in the workspace
//    that swapped the leaf for a symlink won it (measured: diverted a witnessed
//    write outside the root within 88 attempts). See "what node cannot express"
//    for exactly how far this is closed here and where it is only narrowed.
//
// # HARDLINKS, which realpath cannot see at all
//
// An inside name and an outside name can be two directory entries for the SAME
// inode. Every path-based check passes while the write mutates the file outside
// the root, and the undo's rename then replaces the NAME and breaks the link,
// so the inside file reads as reverted while the outside file keeps the
// attacker's bytes, residue no discharge descriptor enumerates. Writing
// through an fd does not help (the fd names the same shared inode), so the
// honest control is REFUSAL: `openConfinedWrite` `fstat`s the fd it opened and
// returns `EMULTILINK` when `nlink > 1`. A refused write is an `Err`, which
// registers no inverse, so there is no undo to mislead. The cost is stated
// plainly: a legitimately hardlinked file inside the workspace cannot be
// witnessed-written.
//
// # A write never lies (the py tier's item 431(b), ported)
//
// `openConfinedWrite` holds an fd, so a competing writer that unlinks the leaf
// between the open and the write does not divert the bytes, they reach the
// inode the check admitted, which by then is an ORPHAN with no directory entry.
// Reporting `Ok` would then claim a mutation at a path that does not hold it.
// So `confirmLanded` compares `(st_dev, st_ino)` on the leaf against the fd
// that was written, and a parted name is `Err(ERACE)`, which registers no
// inverse. `discardWrite` removes the preimage sidecar and removes the created
// leaf ONLY while it still holds our inode, removing by name after a lost race
// would delete the competing writer's file, which is not ours to remove.
//
// # What node CANNOT express, stated rather than silently weakened
//
// The py guard closes the check-to-syscall window by walking down from the root
// one component at a time with `O_NOFOLLOW` and performing every syscall
// through a directory fd (`os.open(..., dir_fd=)`, `os.replace(...,
// src_dir_fd=)`, `os.unlink(..., dir_fd=)`). Node's `fs` exposes no `*at()`
// family at all: there is no `openat`, `renameat`, `unlinkat`, `mkdirat` or
// `fstatat`, and `fs.opendirSync` hands back no usable directory fd. Without a
// native addon the dirfd walk is not expressible here. What IS expressible, and
// what this module therefore does:
//
//   * `assertRealDirChain` lstats every component from the root down to the
//     parent immediately before each syscall and refuses a symlinked or
//     non-directory component by TYPE (a dangling link, which realpath
//     normalizes happily, is caught too);
//   * the write's leaf is opened `O_NOFOLLOW`, so the leaf itself cannot be
//     swapped for a symlink in the window that remains;
//   * the open is re-verified against the name (`assertLeafIsFd`): the chain is
//     re-walked and the leaf's `(dev, ino)` must equal the opened fd's. A swap
//     of an intermediate component during the open therefore has to be undone
//     so precisely that the same inode is still reachable at the inside name,
//     which means a hardlink, and `nlink > 1` is refused;
//   * every byte goes through the verified fd, never by name, and the preimage
//     is read back out of that same fd;
//   * `confirmLanded` re-establishes the same `(dev, ino)` identity afterwards.
//
// The residual, named: the rename-based ops (`rm`, `move`, and the inverses)
// have only `fs.renameSync`, which is by name. POSIX `rename` does not follow a
// symlink at either LEAF (it moves the link itself), so the leaf is not the
// exposure; an intermediate DIRECTORY component swapped between
// `assertRealDirChain` and the `renameSync` is, and node cannot close that.
// The py tier can and does. Two things bound it: the inverses can only ever
// consume a sidecar this workspace produced (family 3), and both endpoints are
// re-checked. A ts tier that needs the py tier's guarantee needs an `*at()`
// binding, which is a native-addon decision this module does not get to make.
//
// The APFS CoW clone is the other casualty: `fs.copyFileSync` with
// `COPYFILE_FICLONE` clones BY NAME, and reopening the target by name is
// exactly the race the fd exists to avoid, so `snapshotPreimage` copies through
// the fd instead. Snapshots of a large file are no longer O(1) on APFS; a
// correct snapshot is worth more than a cheap one.
//
// # Why the inverses stay `pure`
//
// Unchanged from the py tier and forced, not chosen: item 243 rule 3 requires a
// witnessed extern's declared inverse to be non-emitting and non-witnessed, and
// the parser refuses a `[caps]` bracket on `pure` and `acquire` alike, so there
// is no capability-scoped spelling of an inverse in the surface. The fix is to
// shrink the primitive instead of gating it. After this change an inverse's
// entire authority is: move a sidecar THIS WORKSPACE PRODUCED back over a path
// inside the same workspace, or delete a path inside the workspace.

import * as fs from 'node:fs'
import * as path from 'node:path'
import { randomUUID } from 'node:crypto'

/** The environment variable naming the session workspace root (ts tier, same
 * name and semantics as the py tier, backends/python/revl_fs_workspace.py). */
export const WORKSPACE_ENV = 'REVL_FS_WORKSPACE'

/** Subdirectory names, inside the root, that hold the reversal machinery. Both
 * are inside the workspace root (must #3), so a target under them still passes
 * `resolveWithin`. */
export const GARBAGE_DIRNAME = '.revl-fs-garbage'
export const PREIMAGE_DIRNAME = '.revl-fs-preimage'

/** The sidecar `kind` tokens `freshSidecar`/`resolveSidecar` speak, mapped to
 * their directory name. `rm` parks in `garbage`; `write` snapshots in
 * `preimage`. Peer of py `SIDECAR_KINDS`. */
export const SIDECAR_KINDS: Record<string, string> = {
  garbage: GARBAGE_DIRNAME,
  preimage: PREIMAGE_DIRNAME,
}

/** Every family of paths in this slice that can reach a filesystem syscall, and
 * the guard entry points that family must route through. This table IS the
 * single-choke-point claim, stated so it can be tested:
 * `tests/fs_confinement_families.test.ts` asserts the family set and scans the
 * per-extern entry points of this very module to prove every mutating call is
 * one of the `syscall-time` entry points and every path handed to one was bound
 * from a family 1-3 guard. Adding a family means editing this table and that
 * test. Peer of py `PATH_FAMILIES`. */
export const PATH_FAMILIES: Record<string, readonly string[]> = {
  'named-endpoint': ['resolveWithin'],
  'sidecar-directory': ['garbageDir', 'preimageDir', 'freshSidecar'],
  'inverse-source': ['resolveSidecar'],
  'syscall-time': ['openConfinedWrite', 'writeThrough', 'snapshotPreimage',
    'confirmLanded', 'replaceConfined', 'removeConfined', 'mkdirConfined',
    'rmdirConfined', 'closeHandle', 'discardWrite'],
}

/** Read-only helpers an entry point may call. They observe and mutate nothing,
 * so they belong to no family; listing them here is what lets the family scan
 * refuse EVERYTHING else instead of maintaining a hand-kept exception list.
 *
 * They are also what the OBSERVATION entry points (`fsResolveWithin`,
 * `fsLexists`, `fsIsDir`) are built from. Observation is the half of this module
 * a consumer's own `@ts` body legitimately needs, and before those entry points
 * existed the only way to reach it from a user-origin body was to guess a
 * relative specifier into the install tree. Widening this list is still a
 * deliberate edit: a read helper is a new way to LOOK at the filesystem through
 * the jail. Peer of py `READ_HELPERS`. */
export const READ_HELPERS: readonly string[] = ['lexistsConfined', 'isDirConfined']

/** Which positional arguments of a `syscall-time` entry point are PATHS (and so
 * must have come from a family 1-3 guard). The rest are handles or data. Peer
 * of py `SYSCALL_PATH_ARGS`. */
export const SYSCALL_PATH_ARGS: Record<string, readonly number[]> = {
  openConfinedWrite: [0],
  writeThrough: [],
  snapshotPreimage: [],
  confirmLanded: [],
  replaceConfined: [0, 1],
  removeConfined: [0],
  mkdirConfined: [0],
  rmdirConfined: [0],
  closeHandle: [],
  discardWrite: [],
}

const O_NOFOLLOW = fs.constants.O_NOFOLLOW ?? 0
const O_NONBLOCK = fs.constants.O_NONBLOCK ?? 0

/** The FsError record shape a forward op returns on the `Err` branch. Mirrors
 * `stdlib/fs.rvl`'s `FsError = { code, message, path }`. */
export interface FsError {
  code: string
  message: string
  path: string
}

/** A witnessed fs op (or its inverse) could not run. Carries the same
 * (code, message, path) shape the `FsError` record uses, so an entry point can
 * turn it straight into an `Err(...)` on the forward path, the peer of the py
 * `FsOpError`. */
export class FsOpError extends Error {
  code: string
  detail: string
  path: string
  constructor(code: string, message: string, p = '') {
    super(`${code}: ${message} (${p})`)
    this.name = 'FsOpError'
    this.code = code
    this.detail = message
    this.path = p
  }
  asError(): FsError {
    return { code: this.code, message: this.detail, path: this.path }
  }
}

/** The boundary refusal specifically: a path (a target, a sidecar, or an
 * inverse's source) did not resolve inside the session workspace root, or no
 * root was configured. A subclass of `FsOpError` so an entry point needs one
 * `catch`, and a distinct type so a caller (and the test suite) can tell a
 * confinement refusal from an ordinary `ENOENT`. */
export class ConfinementError extends FsOpError {
  constructor(code: string, message: string, p = '') {
    super(code, message, p)
    this.name = 'ConfinementError'
  }
}

// ---------------------------------------------------------------------------
// totality: every guard entry point throws FsOpError or nothing (py 422 F6)
// ---------------------------------------------------------------------------

/** A path safe to put in a refusal's `path` field. A NUL cannot survive into a
 * log line or a WAL witness verbatim, so it is escaped; anything that is not a
 * string at all is stringified rather than crashing the refusal. */
function sanitized(p: unknown): string {
  if (typeof p !== 'string') return String(p)
  return p.replace(/\0/g, '\\x00')
}

/** Refuse a path no filesystem name can hold, BEFORE any syscall sees it. The
 * only member today is the embedded NUL, which node rejects with a `TypeError`
 * (`ERR_INVALID_ARG_VALUE`) rather than an errno error, so it escaped the
 * entry points' `catch` and broke fs.rvl's `-> Result[_, FsError]` contract.
 * The message names what the author can do (item 274). */
function refuseUnusablePath(p: unknown): void {
  if (typeof p === 'string' && p.indexOf('\0') !== -1) {
    throw new FsOpError(
      'EINVAL',
      'path contains a NUL byte, which no filesystem name can hold; pass the '
      + 'same path with the NUL removed. A witnessed fs path is a plain name, '
      + 'relative to the session workspace root or absolute inside it, never '
      + 'raw bytes or a length-prefixed buffer',
      sanitized(p),
    )
  }
}

/** `ENOENT`, `ELOOP`, ... for an errno node attached to a thrown `Error`. Falls
 * back to `EIO` for anything carrying no recognisable code, so the
 * `FsError.code` tag is always a short machine token. */
function errnoCode(e: unknown): string {
  const code = (e as { code?: unknown })?.code
  return typeof code === 'string' && /^E[A-Z0-9]+$/.test(code) ? code : 'EIO'
}

/** Wrap one guard entry point so it throws `FsOpError` or nothing.
 *
 * Applied over `PATH_FAMILIES` + `READ_HELPERS` at module init, so the
 * enumeration that states the choke point is also what states the totality: a
 * fifth entry point is total the moment it is listed, and cannot be added to
 * the table without gaining the property. An `FsOpError` (`ConfinementError`
 * included) passes through untouched, the guard's own refusals already carry a
 * code, a sentence and a path, and re-wrapping them would flatten the
 * confinement refusal a caller and the test suite distinguish by type. */
function makeTotal<F extends (...a: never[]) => unknown>(name: string, fn: F): F {
  const total = ((...args: never[]) => {
    try {
      return fn(...args)
    } catch (e) {
      if (e instanceof FsOpError) throw e
      const first = args.length > 0 ? args[0] : ''
      const message = (e as { message?: string })?.message ?? String(e)
      const where = (e as { path?: unknown })?.path ?? first
      throw new FsOpError(errnoCode(e), `${name} failed (${message})`,
        sanitized(where))
    }
  }) as F
  ;(total as { isTotalGuard?: boolean }).isTotalGuard = true
  return total
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
 * `ConfinementError('EWORKSPACE', ...)` when unset or not a directory, an fs
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
 * (`/ws` vs `/ws-evil`), the peer of py `_is_within`. */
function isWithin(root: string, real: string): boolean {
  return real === root || real.startsWith(root + path.sep)
}

// ---------------------------------------------------------------------------
// family 1: named endpoints
// ---------------------------------------------------------------------------

/** Realpath `p` (must #1: symlinks resolved BEFORE the check) and refuse it
 * unless it lands inside the session workspace root. A relative path is taken
 * relative to the root.
 *
 * This is the `named-endpoint` family's guard: every path an op is HANDED
 * routes through it, forward ops and inverses alike (must #2). It is not the
 * whole story, see `PATH_FAMILIES` for the other three families. A resolved
 * path is a CHECKED path, not yet a safe syscall: the `syscall-time` helpers
 * are what reach the filesystem, and they re-establish what they can rather
 * than trusting this string a second time. */
function rawResolveWithin(p: string): string {
  // refused BEFORE realpath, whose `lstat` would throw a `TypeError` no caller
  // catches. Family 1 is where every caller-supplied path enters, so one check
  // here covers the four forward ops and every inverse endpoint
  // (`resolveSidecar` routes through here too).
  refuseUnusablePath(p)
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

// ---------------------------------------------------------------------------
// how a checked path becomes as safe a syscall as node allows
// ---------------------------------------------------------------------------

/** Refuse `realDir` unless every component from the workspace root down to it
 * is a REAL directory, checked by `lstat` so a symlink is refused by TYPE and a
 * dangling link (which realpath normalizes happily) is caught too.
 *
 * This is node's stand-in for the py guard's `O_NOFOLLOW` directory-fd walk.
 * The py version resolves each component exactly once and performs the syscall
 * relative to the fd it ends up holding, so no component can be re-pointed
 * underneath it. Node exposes no `*at()` syscall, so this re-walks by name and
 * the syscall re-walks by name again: a component swapped between the two is a
 * window the py tier does not have. See "what node CANNOT express" at the top.
 * Every caller pairs it with the strongest fd-side check its syscall allows
 * (`O_NOFOLLOW` at the leaf, an `(dev, ino)` identity check against the fd). */
function assertRealDirChain(realDir: string): void {
  const root = workspaceRoot()
  if (!isWithin(root, realDir)) {
    throw new ConfinementError(
      'EOUTSIDE', 'path escapes the session workspace root', realDir)
  }
  const rel = path.relative(root, realDir)
  const parts = rel === '' ? [] : rel.split(path.sep)
  let cur = root
  for (const part of parts) {
    cur = path.join(cur, part)
    const st = fs.lstatSync(cur)   // ENOENT/ELOOP propagate; callers translate
    if (st.isSymbolicLink()) {
      throw new ConfinementError(
        'EOUTSIDE',
        'a directory component of the path is a symlink, so the syscall would '
        + 'leave the session workspace root',
        cur,
      )
    }
    if (!st.isDirectory()) {
      throw new FsOpError('ENOTDIR', 'a path component is not a directory', cur)
    }
  }
}

/** A resolved path as (parent directory, leaf name), refusing the root itself,
 * no op may replace or remove the workspace root. Peer of py `_split`. */
function splitLeaf(real: string): [string, string] {
  const root = workspaceRoot()
  if (real === root) {
    throw new ConfinementError(
      'EWORKSPACE', 'the workspace root itself is not a valid target', real)
  }
  return [path.dirname(real), path.basename(real)]
}

/** `(dev, ino)` of an open fd, as a comparable string. `bigint: true` is what
 * makes this exact: the default `Stats` carries `ino` as a double, which loses
 * precision on a large inode number and would make two different files compare
 * equal. */
function fdIdentity(fd: number): string {
  const st = fs.fstatSync(fd, { bigint: true })
  return `${st.dev}:${st.ino}`
}

/** `(dev, ino)` of a NAME, without following a symlink at the leaf, or `null`
 * when the name does not resolve. */
function nameIdentity(real: string): string | null {
  try {
    const st = fs.lstatSync(real, { bigint: true })
    return `${st.dev}:${st.ino}`
  } catch {
    return null
  }
}

/** Does `real` still name the very inode `fd` holds?
 *
 * The chain is re-walked first, so neither a swapped directory component nor a
 * symlink planted at the leaf can make a different inode answer yes, and the
 * comparison is on `(dev, ino)` rather than on the path string. `false` for a
 * vanished leaf, a replaced one, or a walk that no longer reaches the parent:
 * every "the name and the fd have parted" case, which is exactly what all three
 * callers need to know. Peer of py `_leaf_is_handle`. */
function leafIsFd(fd: number, real: string): boolean {
  try {
    const [parent] = splitLeaf(real)
    assertRealDirChain(parent)
  } catch {
    return false
  }
  const named = nameIdentity(real)
  return named !== null && named === fdIdentity(fd)
}

// ---------------------------------------------------------------------------
// family 2: the sidecar directories and the files made inside them
// ---------------------------------------------------------------------------

/** The resolved path of a sidecar directory, refusing a symlink.
 *
 * `fs.mkdirSync(d, { recursive: true })`, what this used to do, SUCCEEDS on a
 * pre-existing symlink, because its existence check follows links, and the old
 * callers then returned the unresolved `d`. One symlink named `.revl-fs-garbage`
 * or `.revl-fs-preimage` inside the workspace therefore redirected the whole
 * reversal machinery outside the root. Here the directory is created
 * NON-recursively (so an existing name is an `EEXIST` we then inspect) and
 * `lstat`ed: a symlink is refused by TYPE, not merely resolved and compared, so
 * a dangling link is caught too. `resolveWithin` then confirms containment. */
function sidecarDirReal(kind: string, create: boolean): string {
  const name = SIDECAR_KINDS[kind]
  if (name === undefined) {
    throw new ConfinementError('EWORKSPACE', `unknown sidecar kind \`${kind}\``, '')
  }
  const root = workspaceRoot()
  const p = path.join(root, name)
  if (create) {
    try {
      fs.mkdirSync(p, 0o700)
    } catch (e) {
      if (errnoCode(e) !== 'EEXIST') throw e
    }
  }
  let st: fs.Stats
  try {
    st = fs.lstatSync(p)
  } catch (e) {
    if (errnoCode(e) === 'ENOENT') {
      // not created yet and we were told not to create it: nothing can
      // legitimately live inside it, so name the (absent) directory.
      return p
    }
    throw e
  }
  if (st.isSymbolicLink()) {
    throw new ConfinementError(
      'EOUTSIDE',
      `the \`${name}\` sidecar directory is a symlink; the reversal machinery `
      + 'must live inside the session workspace root',
      realpathish(p),
    )
  }
  if (!st.isDirectory()) {
    throw new ConfinementError(
      'EWORKSPACE', `the \`${name}\` sidecar path is not a directory`, p)
  }
  return resolveWithin(p)
}

/** The session garbage directory, created inside the workspace root (must #3)
 * and returned RESOLVED. `rm` renames its target in here; `unrm` renames it
 * back out. */
function rawGarbageDir(): string {
  return sidecarDirReal('garbage', true)
}

/** The preimage-snapshot directory, created inside the workspace root (must #3)
 * and returned RESOLVED. `write` snapshots the target's preimage in here;
 * `restore` reads it. */
function rawPreimageDir(): string {
  return sidecarDirReal('preimage', true)
}

/** The sidecar `kind` whose directory is `realDir`, or a refusal. Keeps
 * `freshSidecar` from being handed an arbitrary directory: the only places a
 * sidecar may be made are the two the reversal machinery owns. */
function sidecarKindOf(realDir: string): string {
  for (const kind of Object.keys(SIDECAR_KINDS)) {
    if (realDir === sidecarDirReal(kind, false)) return kind
  }
  throw new ConfinementError(
    'EOUTSIDE',
    'a sidecar may only be created in the session garbage or preimage directory',
    realDir,
  )
}

/** A unique, non-colliding path inside `directory` for a snapshot or a parked
 * file, the uuid keeps concurrent ops and repeated same-name removals from
 * clobbering one another's sidecars.
 *
 * `directory` is re-resolved and required to BE one of the two sidecar
 * directories, and the returned leaf is itself put through `resolveWithin`, so
 * the sidecar path is a guarded path and not merely a string joined onto a
 * guarded one. Peer of py `fresh_sidecar`. */
function rawFreshSidecar(directory: string, tag: string): string {
  const realDir = resolveWithin(directory)
  sidecarKindOf(realDir)
  return resolveWithin(
    path.join(realDir, `${tag}-${randomUUID().replace(/-/g, '')}`))
}

// ---------------------------------------------------------------------------
// family 3: an inverse's source endpoint
// ---------------------------------------------------------------------------

/** The guard for an inverse's SOURCE endpoint: `fsRestore`'s `preimage`,
 * `fsUnrm`'s `garbage`.
 *
 * Confinement to the root is necessary but not sufficient. The inverses are
 * `pure` (item 243 rule 3 leaves no capability-scoped spelling for them), so
 * this is a capability-free primitive callable from any pure position, and
 * `rename` REMOVES what it names as a source. Admitting any confined path would
 * still make the inverses an arbitrary move-anything-inside-the-workspace
 * primitive, and admitting an unconfined one made them a steal-and-destroy
 * primitive for the whole filesystem (item 422 F1).
 *
 * So the source is restricted to the matching sidecar directory: an inverse may
 * consume only a sidecar this workspace itself produced. Returns the resolved
 * path, which may not exist, the caller checks, because a replayed inverse
 * whose sidecar is already consumed must no-op (item 243 rule 5). */
function rawResolveSidecar(p: string, kind: string): string {
  const real = resolveWithin(p)
  const realDir = sidecarDirReal(kind, false)
  if (path.dirname(real) !== realDir || real === realDir) {
    throw new ConfinementError(
      'EOUTSIDE',
      `an inverse may only consume a sidecar from the session ${kind} directory`,
      real,
    )
  }
  return real
}

// ---------------------------------------------------------------------------
// family 4: the mutations themselves
// ---------------------------------------------------------------------------

/** An open, verified write fd plus what the caller needs to finish the op.
 * Holding the fd across the snapshot and the write is the point: containment,
 * file type and link count were established ON THIS FD. Peer of py
 * `WriteHandle`. */
export class WriteHandle {
  fd: number
  real: string
  created: boolean
  mode: number
  atimeMs: number
  mtimeMs: number
  /** the preimage sidecar `snapshotPreimage` took, so `discardWrite` can remove
   * it when the write is abandoned after the snapshot. The entry point cannot
   * clean it up itself: the family scan requires every path reaching a mutation
   * to be bound from a family 1-3 guard, and a snapshot's return value is not
   * one. */
  preimage = ''
  constructor(fd: number, real: string, created: boolean, st: fs.BigIntStats) {
    this.fd = fd
    this.real = real
    this.created = created
    this.mode = Number(st.mode) & 0o7777
    this.atimeMs = Number(st.atimeMs)
    this.mtimeMs = Number(st.mtimeMs)
  }
}

/** How many times the open/create pair is retried before giving up. Only a
 * writer racing the very same leaf can consume an attempt, and each attempt is
 * one syscall, so a small bound keeps an honest concurrent creation from
 * failing while refusing to spin against a hostile one. */
const OPEN_ATTEMPTS = 8

/** Open the leaf of `real`, creating it if it does not exist, and report which
 * happened.
 *
 * `O_NOFOLLOW` is the point: `resolveWithin` saw a real file (or nothing), so a
 * symlink here means the leaf was swapped after the check, refused as a
 * confinement failure, never followed. The open/create pair races a concurrent
 * writer of the same name in both directions (the file can appear between the
 * open and the create, or vanish between the create and the retry), so it
 * retries a bounded number of times rather than surfacing the raw errno.
 *
 * The fd is `O_RDWR`, not `O_WRONLY`, because the preimage snapshot is read
 * back out of this very fd rather than reopening the target by name, reading
 * the name a second time is exactly the race the fd exists to avoid. A target
 * the process may write but not read is therefore refused (`EACCES`), which is
 * the honest outcome: without read access there is no preimage, and without a
 * preimage the write is not reversible. */
function openLeaf(real: string): [number, boolean] {
  for (let attempt = 0; attempt < OPEN_ATTEMPTS; attempt++) {
    try {
      return [fs.openSync(real, fs.constants.O_RDWR | O_NOFOLLOW | O_NONBLOCK),
        false]
    } catch (e) {
      const code = errnoCode(e)
      if (code === 'EISDIR') {
        throw new FsOpError(
          'ENOTFILE', 'write target exists and is not a regular file', real)
      }
      if (code !== 'ENOENT') {
        // ELOOP: the leaf is a symlink now, though `resolveWithin` saw a real
        // file. That is exactly the swapped-leaf race, refused.
        throw new ConfinementError(
          'EOUTSIDE',
          `the write target changed under the confinement check (${code})`,
          real,
        )
      }
    }
    try {
      return [fs.openSync(
        real, fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_EXCL,
        0o666), true]
    } catch (e) {
      if (errnoCode(e) !== 'EEXIST') throw e
    }
  }
  throw new FsOpError(
    'ERACE',
    'the write target kept appearing and vanishing under a concurrent writer, '
    + 'so no open could be verified',
    real,
  )
}

/** Open `p` for writing, inside the root, with the narrowest check-to-syscall
 * window node allows and without a hardlink write-through.
 *
 * Order matters: resolve (family 1), assert every directory component from the
 * root down is a real directory, open the leaf `O_NOFOLLOW`, re-assert the
 * chain and require the leaf's `(dev, ino)` to be the fd's, and only THEN
 * `fstat` to decide whether the file is writable at all. Nothing is truncated
 * by the open, so a refusal leaves the target byte-identical.
 *
 * Refusals, all as `FsOpError` (an `Err` on the forward path, which registers
 * no inverse):
 *   * `EOUTSIDE`   - outside the root, a symlinked directory component, or a
 *     symlink where the check saw a real file (a lost race, refused rather
 *     than followed);
 *   * `ENOENT`     - the parent directory does not exist (we create no
 *     directories, so the inverse leaves zero residue);
 *   * `ENOTFILE`   - the target exists and is not a regular file;
 *   * `EMULTILINK` - the target has more than one link. `realpath` cannot see
 *     through a hardlink and writing through the fd would still mutate the
 *     shared inode, so a multiply-linked target is refused outright;
 *   * `ERACE`      - a concurrent writer kept the leaf appearing and vanishing,
 *     or parted the name from the inode the open admitted. */
function rawOpenConfinedWrite(p: string): WriteHandle {
  const real = resolveWithin(p)
  const [parent] = splitLeaf(real)
  try {
    assertRealDirChain(parent)
  } catch (e) {
    if (e instanceof FsOpError) throw e
    const code = errnoCode(e)
    if (code === 'ENOENT' || code === 'ENOTDIR') {
      throw new FsOpError('ENOENT', 'parent directory does not exist', real)
    }
    throw new ConfinementError(
      'EOUTSIDE',
      `the path to the write target changed under the confinement check (${code})`,
      real,
    )
  }
  const [fd, created] = openLeaf(real)
  try {
    if (!leafIsFd(fd, real)) {
      throw new FsOpError(
        'ERACE',
        'the write target was replaced by a concurrent writer while it was '
        + 'being opened, so the fd and the name have parted',
        real,
      )
    }
    const st = fs.fstatSync(fd, { bigint: true })
    if (!st.isFile()) {
      throw new FsOpError(
        'ENOTFILE', 'write target exists and is not a regular file', real)
    }
    if (st.nlink > 1n) {
      throw new FsOpError(
        'EMULTILINK',
        'write target has more than one hard link, so the write would reach an '
        + 'inode the workspace boundary cannot confine',
        real,
      )
    }
    return new WriteHandle(fd, real, created, st)
  } catch (e) {
    fs.closeSync(fd)
    throw e
  }
}

/** Release a `WriteHandle`'s fd. Idempotent. */
function rawCloseHandle(handle: WriteHandle): void {
  if (handle.fd >= 0) {
    fs.closeSync(handle.fd)
    handle.fd = -1
  }
}

/** Refuse the write unless `handle.real` still names the inode that was written
 * (the py tier's item 431(b), ported).
 *
 * Confinement is not in question here: the bytes went through a verified fd and
 * reached the inode the check admitted, inside the root, whatever the name did
 * afterwards. The question is whether the WITNESS is true. A competing writer
 * that unlinks the leaf mid-call leaves that inode an orphan, and reporting
 * `Ok` then claims a mutation at a path that does not hold it, the discharge
 * descriptor would enumerate a write nobody can see, and the registered undo
 * would restore a preimage over a forward change that never became visible. So
 * a parted name is `ERACE`, and `Err` registers no inverse. */
function rawConfirmLanded(handle: WriteHandle): void {
  if (!leafIsFd(handle.fd, handle.real)) {
    throw new FsOpError(
      'ERACE',
      'the write target was removed or replaced by a concurrent writer while '
      + 'the write was in flight, so this path does not hold the bytes '
      + 'written; nothing outside the session workspace root was reached and no '
      + 'undo was registered. Retry the write, or serialize with the other '
      + 'writer before retrying',
      handle.real,
    )
  }
}

/** Abandon a write that failed after the open: remove the preimage sidecar it
 * snapshotted, and, if the open CREATED the target and the name still holds
 * that very inode, remove the target again.
 *
 * A forward op that returns `Err` registers no inverse (Ok-conditional
 * registration, item 243), so anything the failed attempt left behind would be
 * residue nothing enumerates. An existing target is left alone, the open does
 * not truncate, so it still holds its original bytes.
 *
 * The inode check on the created branch is not decoration. Removing BY NAME
 * after a lost race would delete whatever the competing writer put at that
 * name, which is neither ours to remove nor residue of ours: the file we
 * created is by then an unlinked orphan that vanishes when the fd closes. */
function rawDiscardWrite(handle: WriteHandle): void {
  if (handle.preimage) {
    removeConfined(handle.preimage)
    handle.preimage = ''
  }
  if (handle.created && leafIsFd(handle.fd, handle.real)) {
    removeConfined(handle.real)
  }
}

/** Truncate and write `contents` THROUGH the verified fd, never by name, so
 * the bytes reach the inode `openConfinedWrite` admitted and no other. */
function rawWriteThrough(handle: WriteHandle, contents: string): void {
  fs.ftruncateSync(handle.fd, 0)
  const data = Buffer.from(contents, 'utf-8')
  let offset = 0
  while (offset < data.length) {
    offset += fs.writeSync(handle.fd, data, offset, data.length - offset, offset)
  }
}

/** Snapshot the open target into a fresh preimage sidecar, and return the
 * sidecar's path (the witness's `preimage`).
 *
 * The source is the verified fd, not a name, so the snapshot cannot be raced
 * into copying some other file; the destination is a guarded sidecar path
 * (family 2) opened `O_CREAT|O_EXCL|O_NOFOLLOW` inside a chain-checked
 * directory. The py tier takes an APFS `fclonefileat` CoW clone from the fd;
 * node's only clone is `copyFileSync(COPYFILE_FICLONE)`, which is BY NAME, so
 * the portable fd-to-fd copy is the only honest option here and the O(1)
 * snapshot is lost. Mode and mtime are restored so a restored preimage is not
 * silently re-permissioned.
 *
 * A snapshot that cannot be taken is an `ESNAPSHOT` refusal, never a raw throw:
 * a write with no preimage is not reversible, so the honest outcome is to
 * refuse before a byte is written. The sidecar is recorded on the handle so
 * `discardWrite` can remove it when a LATER step refuses. */
function rawSnapshotPreimage(handle: WriteHandle): string {
  const directory = preimageDir()
  const dst = freshSidecar(directory, 'pre')
  let out = -1
  try {
    assertRealDirChain(directory)
    out = fs.openSync(
      dst,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL
      | O_NOFOLLOW,
      0o600,
    )
    const buffer = Buffer.allocUnsafe(1 << 20)
    let offset = 0
    for (;;) {
      const read = fs.readSync(handle.fd, buffer, 0, buffer.length, offset)
      if (read === 0) break
      let written = 0
      while (written < read) {
        written += fs.writeSync(out, buffer, written, read - written,
          offset + written)
      }
      offset += read
    }
    fs.fchmodSync(out, handle.mode)
    fs.futimesSync(out, handle.atimeMs / 1000, handle.mtimeMs / 1000)
  } catch (e) {
    if (out >= 0) fs.closeSync(out)
    const message = (e as { message?: string })?.message ?? String(e)
    throw new FsOpError(
      'ESNAPSHOT',
      `could not snapshot the write target's preimage (${message}), so the `
      + 'write would not be reversible and was refused before any byte was '
      + 'written; retry, or write to a path no other writer is racing',
      handle.real,
    )
  }
  fs.closeSync(out)
  handle.preimage = dst
  return dst
}

/** `rename(src, dst)` with both parents chain-checked immediately before the
 * call. Both endpoints must already have come from a family 1-3 guard.
 *
 * POSIX `rename` does not follow a symlink at either LEAF, so the leaves are
 * safe; an intermediate directory component swapped between the chain check and
 * the call is the residual node cannot close (no `renameat`). A missing source
 * is `ENOENT`, the caller decides whether that is an error or an idempotent
 * replay. */
function rawReplaceConfined(srcReal: string, dstReal: string): void {
  const [srcParent] = splitLeaf(srcReal)
  const [dstParent] = splitLeaf(dstReal)
  try {
    assertRealDirChain(srcParent)
  } catch (e) {
    if (e instanceof FsOpError) throw e
    throw new FsOpError('ENOENT', 'no such file', srcReal)
  }
  try {
    assertRealDirChain(dstParent)
  } catch (e) {
    if (e instanceof FsOpError) throw e
    throw new FsOpError('ENOENT', 'parent directory does not exist', dstReal)
  }
  try {
    fs.renameSync(srcReal, dstReal)
  } catch (e) {
    if (errnoCode(e) === 'ENOENT') {
      throw new FsOpError('ENOENT', 'no such file', srcReal)
    }
    throw e
  }
}

/** `unlink` with the parent chain-checked. A missing target is a no-op: an
 * inverse must be idempotent on replay (item 243 rule 5). `unlink` does not
 * follow a symlink at the leaf, so a planted link is removed, never traversed. */
function rawRemoveConfined(real: string): void {
  const [parent] = splitLeaf(real)
  try {
    assertRealDirChain(parent)
  } catch (e) {
    if (e instanceof ConfinementError) throw e
    return
  }
  try {
    fs.unlinkSync(real)
  } catch (e) {
    const code = errnoCode(e)
    if (code !== 'ENOENT' && code !== 'ENOTDIR') throw e
  }
}

/** `mkdir` with the parent chain-checked. */
function rawMkdirConfined(real: string): void {
  const [parent] = splitLeaf(real)
  try {
    assertRealDirChain(parent)
  } catch (e) {
    if (e instanceof FsOpError) throw e
    throw new FsOpError('ENOENT', 'parent directory does not exist', real)
  }
  try {
    fs.mkdirSync(real)
  } catch (e) {
    if (errnoCode(e) === 'EEXIST') {
      throw new FsOpError('EEXIST', 'path already exists', real)
    }
    throw e
  }
}

/** `rmdir` with the parent chain-checked, iff the directory is still empty.
 * Total and idempotent: a missing or non-empty directory is left as-is, never a
 * throw, `rmdir_if_empty` must never delete a directory the activation (or a
 * concurrent writer) populated. */
function rawRmdirConfined(real: string): void {
  const [parent] = splitLeaf(real)
  try {
    assertRealDirChain(parent)
  } catch (e) {
    if (e instanceof ConfinementError) throw e
    return
  }
  try {
    fs.rmdirSync(real)
  } catch {
    /* non-empty, missing, or not a directory: leave as-is */
  }
}

/** Does `real` name something (a dangling symlink included)? A READ, used by
 * the entry points for their `ENOENT`/`EEXIST` pre-checks and for an inverse's
 * idempotent no-op. It decides nothing about confinement, the mutation that
 * follows re-establishes what it can regardless of what this said, so a lost
 * race here costs an error message, never an escape. */
function rawLexistsConfined(real: string): boolean {
  return nameIdentity(real) !== null
}

/** Is `real` a directory? A READ, the peer of `rawLexistsConfined`, and the only
 * observation `stdlib/fs.rvl`'s `is_dir` needs beyond it.
 *
 * `real` is a path a family 1-3 guard already resolved, so this follows no
 * symlink the membership test has not already seen: `resolveWithin` realpaths
 * every existing component INCLUDING the leaf, so what arrives here is
 * symlink-canonical and inside the root. Like `rawLexistsConfined` it decides
 * nothing about confinement, and a lost race costs a stale answer, never an
 * escape.
 *
 * `statSync(...).isDirectory()` follows symlinks, which is what py's
 * `os.path.isdir` does; the two tiers must answer the same question. Total on
 * its own (a stat error answers false) and total again through `makeTotal`. */
function rawIsDirConfined(real: string): boolean {
  try {
    return fs.statSync(real).isDirectory()
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// apply totality over the enumeration, then export the wrapped entry points
// ---------------------------------------------------------------------------
// Driven by the tables rather than by a wrapper per function, so the
// single-choke-point enumeration and the totality guarantee cannot drift apart:
// `tests/fs_confinement_families.test.ts` asserts every listed name is wrapped.
// Everything above and below calls the WRAPPED binding (module-scope `const`s
// initialised here, before any op can run), so an internal call
// (`openConfinedWrite` -> `resolveWithin`, `discardWrite` -> `removeConfined`)
// is total too.

const RAW: Record<string, (...a: never[]) => unknown> = {
  resolveWithin: rawResolveWithin as (...a: never[]) => unknown,
  garbageDir: rawGarbageDir as (...a: never[]) => unknown,
  preimageDir: rawPreimageDir as (...a: never[]) => unknown,
  freshSidecar: rawFreshSidecar as (...a: never[]) => unknown,
  resolveSidecar: rawResolveSidecar as (...a: never[]) => unknown,
  openConfinedWrite: rawOpenConfinedWrite as (...a: never[]) => unknown,
  writeThrough: rawWriteThrough as (...a: never[]) => unknown,
  snapshotPreimage: rawSnapshotPreimage as (...a: never[]) => unknown,
  confirmLanded: rawConfirmLanded as (...a: never[]) => unknown,
  replaceConfined: rawReplaceConfined as (...a: never[]) => unknown,
  removeConfined: rawRemoveConfined as (...a: never[]) => unknown,
  mkdirConfined: rawMkdirConfined as (...a: never[]) => unknown,
  rmdirConfined: rawRmdirConfined as (...a: never[]) => unknown,
  closeHandle: rawCloseHandle as (...a: never[]) => unknown,
  discardWrite: rawDiscardWrite as (...a: never[]) => unknown,
  lexistsConfined: rawLexistsConfined as (...a: never[]) => unknown,
  isDirConfined: rawIsDirConfined as (...a: never[]) => unknown,
}

const GUARD: Record<string, (...a: never[]) => unknown> = {}
for (const entries of Object.values(PATH_FAMILIES)) {
  for (const entry of entries) {
    if (RAW[entry] === undefined) {
      throw new Error(`PATH_FAMILIES names \`${entry}\`, which has no implementation`)
    }
    GUARD[entry] = makeTotal(entry, RAW[entry])
  }
}
for (const entry of READ_HELPERS) {
  if (RAW[entry] === undefined) {
    throw new Error(`READ_HELPERS names \`${entry}\`, which has no implementation`)
  }
  GUARD[entry] = makeTotal(entry, RAW[entry])
}

/** family 1, see `rawResolveWithin`. */
export const resolveWithin = GUARD.resolveWithin as typeof rawResolveWithin
/** family 2, see `rawGarbageDir`. */
export const garbageDir = GUARD.garbageDir as typeof rawGarbageDir
/** family 2, see `rawPreimageDir`. */
export const preimageDir = GUARD.preimageDir as typeof rawPreimageDir
/** family 2, see `rawFreshSidecar`. */
export const freshSidecar = GUARD.freshSidecar as typeof rawFreshSidecar
/** family 3, see `rawResolveSidecar`. */
export const resolveSidecar = GUARD.resolveSidecar as typeof rawResolveSidecar
/** family 4, see `rawOpenConfinedWrite`. */
export const openConfinedWrite =
  GUARD.openConfinedWrite as typeof rawOpenConfinedWrite
/** family 4, see `rawWriteThrough`. */
export const writeThrough = GUARD.writeThrough as typeof rawWriteThrough
/** family 4, see `rawSnapshotPreimage`. */
export const snapshotPreimage =
  GUARD.snapshotPreimage as typeof rawSnapshotPreimage
/** family 4, see `rawConfirmLanded`. */
export const confirmLanded = GUARD.confirmLanded as typeof rawConfirmLanded
/** family 4, see `rawReplaceConfined`. */
export const replaceConfined = GUARD.replaceConfined as typeof rawReplaceConfined
/** family 4, see `rawRemoveConfined`. */
export const removeConfined = GUARD.removeConfined as typeof rawRemoveConfined
/** family 4, see `rawMkdirConfined`. */
export const mkdirConfined = GUARD.mkdirConfined as typeof rawMkdirConfined
/** family 4, see `rawRmdirConfined`. */
export const rmdirConfined = GUARD.rmdirConfined as typeof rawRmdirConfined
/** family 4, see `rawCloseHandle`. */
export const closeHandle = GUARD.closeHandle as typeof rawCloseHandle
/** family 4, see `rawDiscardWrite`. */
export const discardWrite = GUARD.discardWrite as typeof rawDiscardWrite
/** read helper, see `rawLexistsConfined`. */
export const lexistsConfined = GUARD.lexistsConfined as typeof rawLexistsConfined
/** read helper, see `rawIsDirConfined`. */
export const isDirConfined = GUARD.isDirConfined as typeof rawIsDirConfined

// -------------------------------------------------------- per-extern entry points
// item 410 stage 5: the entry points a `stdlib/fs.rvl` `= @ts ref` thunk imports
// at first call, one per witnessed op and one per inverse. Each mirrors its `@py`
// peer in `stdlib/fs.rvl` line for line, and reaches the filesystem ONLY through
// the guard entry points enumerated in `PATH_FAMILIES`, which
// `tests/fs_confinement_families.test.ts` scans this file to prove. The ref thunk
// calls the export positionally with the extern's parameters and returns its
// value verbatim, so a forward op returns the `{ kind, value }` Result shape the
// witnessed frame keys off, and an inverse returns `void` (Unit).

/** `write`'s preimage witness, the ts mirror of `stdlib/fs.rvl`'s WriteWitness. */
export interface WriteWitness { path: string; preimage: string; created: boolean }
/** `rm`'s witness: the original path and where the target was parked. */
export interface RmWitness { path: string; garbage: string }
/** `move`'s witness: the resolved source and destination. */
export interface MoveWitness { from: string; to: string }
/** `mkdir`'s witness: the created directory. */
export interface MkdirWitness { path: string }

/** The `Result[Witness, FsError]` shape a forward fs op returns, the ts spelling
 * of the emitted `{ kind: 'Ok' | 'Err', value }` the witnessed runtime reads. */
export type FsResult<T> =
  | { kind: 'Ok'; value: T }
  | { kind: 'Err'; value: FsError }

/** Overwrite (or create) `p` with `contents`, snapshotting the preimage first.
 * Registers `undo fsRestore(result)` on Ok (the accumulator's binding). */
export function fsWrite(p: string, contents: string): FsResult<WriteWitness> {
  let handle: WriteHandle
  try {
    const target = resolveWithin(p)      // must #1: realpath BEFORE the check
    // ...and then the syscall-time guard, which is what actually opens the
    // file: it chain-checks every directory component, opens the leaf
    // O_NOFOLLOW, and re-establishes the fd's identity against the name. A
    // non-regular target is ENOTFILE, a missing parent ENOENT (we create no
    // directories, so the inverse leaves zero residue), and a HARDLINKED target
    // is EMULTILINK, realpath cannot see through a hard link, so a
    // multiply-linked name is refused rather than written through. The open
    // does not truncate: a refusal leaves the target byte-identical, and
    // Ok-conditional registration means no inverse.
    handle = openConfinedWrite(target)
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
  try {
    let preimage = ''
    if (!handle.created) {
      // snapshot the preimage INSIDE the root (must #3) before diverging it,
      // taken from the verified fd, into a guarded sidecar path. A refused
      // sidecar (a planted `.revl-fs-preimage` symlink) fails HERE, before any
      // byte is written, so the target is untouched.
      preimage = snapshotPreimage(handle)
    }
    writeThrough(handle, contents)
    // the bytes reached the inode the check admitted, but a competing writer
    // that unlinked the leaf mid-call left that inode an ORPHAN, and Ok would
    // then claim a mutation at a path which does not hold it. Confinement was
    // never in question here; the RESULT was. A parted name is an Err, so no
    // witness, no inverse, and discardWrite clears the preimage sidecar.
    confirmLanded(handle)
    return {
      kind: 'Ok',
      value: { path: handle.real, preimage, created: handle.created },
    }
  } catch (e) {
    discardWrite(handle)   // residue-free: an Err registers no inverse
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  } finally {
    closeHandle(handle)
  }
}

/** Remove `p` by parking it in the session garbage dir (inside the root).
 * Registers `undo fsUnrm(result)` on Ok. */
export function fsRm(p: string): FsResult<RmWitness> {
  try {
    const target = resolveWithin(p)      // must #1
    if (!lexistsConfined(target)) {
      // Ok-conditional: an rm on a missing path registers nothing (item 243).
      return { kind: 'Err', value: { code: 'ENOENT', message: 'no such file', path: target } }
    }
    // must #3: inside root, and the garbage dir is now itself resolved and
    // refused if it is a symlink, so a planted `.revl-fs-garbage` link can no
    // longer park the removed file outside the boundary.
    const parked = freshSidecar(garbageDir(), 'rm')
    replaceConfined(target, parked)
    return { kind: 'Ok', value: { path: target, garbage: parked } }
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
}

/** Rename `src` -> `dst` (both confined). Registers `undo fsUnmove(result)`. */
export function fsMove(src: string, dst: string): FsResult<MoveWitness> {
  try {
    const realFrom = resolveWithin(src)  // must #1, BOTH endpoints
    const realTo = resolveWithin(dst)
    if (!lexistsConfined(realFrom)) {
      return { kind: 'Err', value: { code: 'ENOENT', message: 'no such source', path: realFrom } }
    }
    if (lexistsConfined(realTo)) {
      return { kind: 'Err', value: { code: 'EEXIST', message: 'destination exists', path: realTo } }
    }
    replaceConfined(realFrom, realTo)
    return { kind: 'Ok', value: { from: realFrom, to: realTo } }
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
}

/** Create directory `p`. Registers `undo fsRmdirIfEmpty(result)` on Ok. */
export function fsMkdir(p: string): FsResult<MkdirWitness> {
  try {
    const target = resolveWithin(p)      // must #1
    if (lexistsConfined(target)) {
      return { kind: 'Err', value: { code: 'EEXIST', message: 'path already exists', path: target } }
    }
    mkdirConfined(target)
    return { kind: 'Ok', value: { path: target } }
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
}

/** Inverse of `fsWrite`: restore the preimage snapshot over the target, or
 * delete the created file. Confined (must #2), idempotent on replay. */
export function fsRestore(w: WriteWitness): void {
  // must #2: BOTH endpoints are confined, not just the target. The target is a
  // `named-endpoint`; the preimage is the SOURCE of a rename, and a rename
  // REMOVES what it names, so an unguarded source made this capability-free
  // inverse a steal-and-destroy primitive for any file the process can reach
  // (item 422 F1). It routes through the `inverse-source` guard, which confines
  // it AND requires it to be a sidecar this workspace itself produced. A throw
  // here is recorded by the teardown loop as restore-residue, never a silent
  // write (or a silent theft) outside the root.
  const target = resolveWithin(w.path)
  if (w.created) {
    // the file did not exist before the write: undo == delete it.
    // idempotent, a second replay finds it already gone.
    removeConfined(target)
    return
  }
  // restore the preimage snapshot over the target. the rename is atomic and
  // consumes the snapshot (residue-free). idempotent, once the snapshot is
  // gone (already restored), a second replay is a no-op.
  const preimage = resolveSidecar(w.preimage, 'preimage')
  if (lexistsConfined(preimage)) replaceConfined(preimage, target)
}

/** Inverse of `fsRm`: rename the parked file back. Confined, idempotent. */
export function fsUnrm(w: RmWitness): void {
  const target = resolveWithin(w.path)   // must #2, the TARGET endpoint
  // ...and the SOURCE endpoint, the same rename-steals-what-it-names hole
  // `fsRestore` had: only a sidecar from the session garbage dir is admissible.
  const parked = resolveSidecar(w.garbage, 'garbage')
  // rename the parked file back. idempotent, once moved back (garbage gone),
  // a second replay is a no-op.
  if (lexistsConfined(parked)) replaceConfined(parked, target)
}

/** Inverse of `fsMove`: move it back. Both endpoints confined, idempotent. */
export function fsUnmove(w: MoveWitness): void {
  const src = resolveWithin(w.from)      // must #2: BOTH endpoints confined
  const dst = resolveWithin(w.to)
  // move it back. idempotent, once back (dst gone), a second replay no-ops.
  if (lexistsConfined(dst)) replaceConfined(dst, src)
}

/** Inverse of `fsMkdir`: remove the created directory iff still empty. Total. */
export function fsRmdirIfEmpty(w: MkdirWitness): void {
  const target = resolveWithin(w.path)   // must #2
  // remove the created directory iff still empty, never delete a dir the
  // activation (or a concurrent writer) populated. idempotent + total: a
  // missing or non-empty dir is left as-is, no throw.
  rmdirConfined(target)
}

// ------------------------------------------------- the observation entry points
// The OTHER half of this module, and the one a consumer's own host body needs.
//
// The four mutations above are witnessed and revertible; these three only LOOK.
// They exist because "may I touch this path, and what is there?" had no
// supported door for a USER-origin `@ts` body: 396(B) jails a user ref to the
// user compile tree, item 410's `__REVL_STDLIB_REF_ROOT__` is stdlib-origin
// only, and item 422 F1 removed the unconfined primitives the deprecated
// `globalThis.__revlFs` seam published. What was left was a guessed relative
// specifier into the install tree, which breaks whenever revl moves.
//
// So the door is a revl one: `stdlib/fs.rvl` exposes `resolve_within`, `lexists`
// and `is_dir` as ordinary `pure` externs a consumer `use`s, and these are their
// ts bodies. A consumer's own body never imports this module — it receives an
// already-CONFINED absolute path from `resolve_within` and reads it with plain
// `node:fs`, exactly as its `@py` peer would with `os`.
//
// OBSERVATION ONLY, and that is the point rather than an omission. The write
// primitives an earlier seam exported are item 422 F1 itself; they stay gone. A
// consumer that appears to need one needs a witnessed op from the catalog above,
// or a finding filed — never a new primitive here.

/** Realpath `p` and refuse it unless it lands inside the session workspace root,
 * handing the resolved absolute path back as `Ok`. The whole confinement
 * decision, and nothing else: the SAME family-1 guard every mutation routes
 * through, so a path this refuses is a path no op on this surface would touch,
 * and a path it admits is symlink-canonical and inside the root.
 *
 * `Err` carries the guard's own refusal verbatim — `EWORKSPACE` (no root
 * configured), `EOUTSIDE` (the boundary refusal), `EINVAL` (a name no filesystem
 * can hold) — so a caller distinguishes an operator mistake from a security
 * refusal without a second vocabulary. */
export function fsResolveWithin(p: string): FsResult<string> {
  try {
    return { kind: 'Ok', value: resolveWithin(p) }
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
}

/** Does `p` name something, once confined? `Ok(true)`/`Ok(false)` for a path
 * inside the root, `Err` with the same code `fsResolveWithin` gives for one that
 * is not — a path outside the boundary answers with a refusal, never with a
 * fact about what is out there. */
export function fsLexists(p: string): FsResult<boolean> {
  try {
    const target = resolveWithin(p)
    return { kind: 'Ok', value: lexistsConfined(target) }
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
}

/** Is `p` a directory, once confined? Same shape and same refusals as
 * `fsLexists`. */
export function fsIsDir(p: string): FsResult<boolean> {
  try {
    const target = resolveWithin(p)
    return { kind: 'Ok', value: isDirConfined(target) }
  } catch (e) {
    if (e instanceof FsOpError) return { kind: 'Err', value: e.asError() }
    throw e
  }
}

/** The helper surface the DEPRECATED `globalThis.__revlFs` seam publishes.
 *
 * It carries the GUARD entry points and nothing else. The previous shape
 * exported the raw primitives (`replace`, `remove`, `writeFile`, `mkdirOne`,
 * `rmdir`, `snapshot`) unconfined, which is item 422 F1 itself: an exported
 * `replace` is a steal-and-destroy primitive for the whole filesystem. They are
 * removed rather than deprecated. */
export interface RevlFsHost {
  WORKSPACE_ENV: string
  GARBAGE_DIRNAME: string
  PREIMAGE_DIRNAME: string
  FsOpError: typeof FsOpError
  ConfinementError: typeof ConfinementError
  PATH_FAMILIES: typeof PATH_FAMILIES
  READ_HELPERS: typeof READ_HELPERS
  workspaceRoot: typeof workspaceRoot
  resolveWithin: typeof resolveWithin
  resolveSidecar: typeof resolveSidecar
  garbageDir: typeof garbageDir
  preimageDir: typeof preimageDir
  freshSidecar: typeof freshSidecar
  openConfinedWrite: typeof openConfinedWrite
  writeThrough: typeof writeThrough
  snapshotPreimage: typeof snapshotPreimage
  confirmLanded: typeof confirmLanded
  discardWrite: typeof discardWrite
  closeHandle: typeof closeHandle
  replaceConfined: typeof replaceConfined
  removeConfined: typeof removeConfined
  mkdirConfined: typeof mkdirConfined
  rmdirConfined: typeof rmdirConfined
  lexistsConfined: typeof lexistsConfined
}

const HOST: RevlFsHost = {
  WORKSPACE_ENV,
  GARBAGE_DIRNAME,
  PREIMAGE_DIRNAME,
  FsOpError,
  ConfinementError,
  PATH_FAMILIES,
  READ_HELPERS,
  workspaceRoot,
  resolveWithin,
  resolveSidecar,
  garbageDir,
  preimageDir,
  freshSidecar,
  openConfinedWrite,
  writeThrough,
  snapshotPreimage,
  confirmLanded,
  discardWrite,
  closeHandle,
  replaceConfined,
  removeConfined,
  mkdirConfined,
  rmdirConfined,
  lexistsConfined,
}

// DEPRECATED (item 410 stage 5): install on import for out-of-tree embedders
// (and the harness `fs_host_install`) that still pre-import this module for the
// `globalThis.__revlFs` seam. `stdlib/fs.rvl` no longer reaches through it, it
// imports the per-extern entry points above via `= @ts ref`. Retained for one
// deprecation release, then removed with a note.
;(globalThis as unknown as { __revlFs?: RevlFsHost }).__revlFs = HOST
