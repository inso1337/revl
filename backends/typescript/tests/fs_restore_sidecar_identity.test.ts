// `fsRestore` re-checks the sidecar it is about to install (issue #1016), the
// ts peer of tests/test_fs_restore_sidecar_identity_1016.py.
//
// `resolveSidecar` proves the SLOT is a preimage sidecar this workspace owns.
// It proves nothing about the FILE in that slot, and a snapshot sits there for
// the whole life of an activation, so a same-UID writer inside the workspace
// had that window to rewrite it, swap it for another inode, or hardlink it out.
// The inverse then renamed the result over the target and reported a clean,
// residue-free reversal.
//
// This is the inverse-path member of the family the forward path already has:
// `openConfinedWrite` binds its target's identity on the held fd and
// `confirmLanded` re-establishes it after the write. `snapshotPreimage` now
// captures the sidecar's own identity, the witness carries it, and
// `installCapturedSidecar` re-checks it on the way in and re-checks the
// installed result on the way out.
//
// Both directions are pinned: a sidecar that cannot be shown to be the captured
// one throws (which the teardown loop records as restore-residue) and is never
// installed, and an untampered restore stays exactly as quiet as before.
//
// Issue #2652 added a content comparison on both install passes: the sha256 of
// the sidecar's bytes, recorded at snapshot time and re-read through the same fd
// the stat came from. The seven stat fields alone do not see a same-size
// in-place rewrite that lands inside one inode timestamp tick; the digest does,
// because it does not depend on clock resolution. The same-tick test pins the
// recorded stat tuple to the tampered file's own tuple before the inverse runs,
// so only the digest can refuse and the result does not depend on the clock. A
// capture with no `sha256` at all (an older WAL) is compared stat-only and
// still restores. A capture that CARRIES the key must carry a bare
// 64-lowercase-hex digest: a present `null`, number or malformed string is
// refused `EIDENTITY`, not read as an absent key.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { FsOpError, fsRestore, fsWrite } from '../revl_fs_ts.ts'
import type { WriteWitness } from '../revl_fs_ts.ts'

// The second install pass can only be forced through the rename, a digest that
// cannot be re-read can only be forced through the read, and a failure between
// creating the snapshot sidecar and handing it to the handle can only be forced
// at one of the syscalls in that window, so this file wraps those three
// `node:fs` calls. All hooks are empty unless a test sets one, and the wrappers
// delegate to the real calls otherwise.
const hooks = vi.hoisted(() => ({
  beforeRename: undefined as (() => void) | undefined,
  failRead: undefined as (() => never) | undefined,
  failFchmod: undefined as (() => never) | undefined,
}))

vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:fs')>()
  return {
    ...actual,
    renameSync: (src: fs.PathLike, dst: fs.PathLike): void => {
      const hook = hooks.beforeRename
      hooks.beforeRename = undefined
      if (hook !== undefined) hook()
      actual.renameSync(src, dst)
    },
    readSync: ((...args: Parameters<typeof actual.readSync>) => {
      const fail = hooks.failRead
      hooks.failRead = undefined
      if (fail !== undefined) fail()
      return actual.readSync(...args)
    }) as typeof actual.readSync,
    fchmodSync: ((...args: Parameters<typeof actual.fchmodSync>) => {
      const fail = hooks.failFchmod
      hooks.failFchmod = undefined
      if (fail !== undefined) fail()
      return actual.fchmodSync(...args)
    }) as typeof actual.fchmodSync,
  }
})

let base: string
let ws: string
let saved: string | undefined

beforeEach(() => {
  base = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'revl-fs-')))
  ws = path.join(base, 'ws')
  fs.mkdirSync(ws)
  fs.writeFileSync(path.join(ws, 'artifact.txt'), 'v1')
  saved = process.env.REVL_FS_WORKSPACE
  process.env.REVL_FS_WORKSPACE = ws
})

afterEach(() => {
  // a hook a test set but did not consume (a test that fails early) must not
  // leak into the next one.
  hooks.beforeRename = undefined
  hooks.failRead = undefined
  hooks.failFchmod = undefined
  if (saved === undefined) delete process.env.REVL_FS_WORKSPACE
  else process.env.REVL_FS_WORKSPACE = saved
  fs.rmSync(base, { recursive: true, force: true })
})

/** Perform the witnessed write and hand back its witness plus the two paths. */
function written(): { witness: WriteWitness; target: string; sidecar: string } {
  const result = fsWrite('artifact.txt', 'v2')
  expect(result.kind).toBe('Ok')
  const witness = result.value as WriteWitness
  expect(fs.readFileSync(witness.preimage, 'utf-8')).toBe('v1')
  return { witness, target: path.join(ws, 'artifact.txt'), sidecar: witness.preimage }
}

function sha256(text: string): string {
  return createHash('sha256').update(text).digest('hex')
}

/** Pin the witness's recorded stat fields to `sidecar`'s CURRENT tuple.
 *
 * This is the same-tick case of issue #2652, forced rather than raced: after a
 * same-size in-place rewrite inside one inode timestamp tick, every captured
 * stat field equals the tampered file's own tuple. Overwriting the recorded
 * fields with that tuple therefore removes the stat comparison from the test
 * without removing the tamper, so anything that still refuses can only be the
 * content digest. On a filesystem whose clock did tick this leaves a test that
 * passes for one reason instead of a test that flips with the tick. */
function asIfSameTick(witness: WriteWitness, sidecar: string): void {
  const capture = witness.capture
  if (capture === null || capture === undefined) {
    throw new Error('the witness carries no capture to pin')
  }
  const st = fs.statSync(sidecar, { bigint: true })
  const current: Record<string, string> = {
    dev: String(st.dev),
    ino: String(st.ino),
    mode: String(st.mode),
    nlink: String(st.nlink),
    size: String(st.size),
    mtimeNs: String(st.mtimeNs),
    ctimeNs: String(st.ctimeNs),
  }
  Object.assign(capture as unknown as Record<string, string>, current)
}

function refusalOf(fn: () => void): FsOpError {
  try {
    fn()
  } catch (e) {
    expect(e).toBeInstanceOf(FsOpError)
    return e as FsOpError
  }
  throw new Error('the call was expected to refuse, and did not')
}

function codeOf(fn: () => void): string {
  return refusalOf(fn).code
}

// ===========================================================================
// The non-vacuity control: an honest restore is unchanged and stays quiet.
// It passes before this change as well as after it, so the refusals below
// cannot be satisfied by an inverse that simply refuses always.
// ===========================================================================

describe('an untampered restore is unchanged', () => {
  it('installs the preimage silently and leaves no residue', () => {
    const { witness, target, sidecar } = written()
    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')

    fsRestore(witness)

    expect(fs.readFileSync(target, 'utf-8')).toBe('v1')
    expect(fs.existsSync(sidecar)).toBe(false)
    expect(fs.readdirSync(path.join(ws, '.revl-fs-preimage'))).toEqual([])

    fsRestore(witness)                       // still idempotent on replay
    expect(fs.readFileSync(target, 'utf-8')).toBe('v1')
  })

  it('a witness that carries no capture still restores', () => {
    // A durable record written before the capture key existed (an older
    // install's WAL). Refusing it would strand a recoverable WAL.
    const { witness, target, sidecar } = written()
    fsRestore({ path: witness.path, preimage: witness.preimage,
      created: witness.created })
    expect(fs.readFileSync(target, 'utf-8')).toBe('v1')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('records the sidecar bytes digest alongside the stat fields', () => {
    // The positive direction of the digest check: the capture binds the bytes
    // that were on disk at snapshot time, and a restore nobody touched still
    // installs them. Without this, the refusal below could be satisfied by an
    // inverse that simply refuses every restore.
    const { witness, target, sidecar } = written()
    const recorded = witness.capture?.sha256
    expect(recorded).toMatch(/^[0-9a-f]{64}$/)
    expect(recorded).toBe(sha256(fs.readFileSync(sidecar, 'utf-8')))

    fsRestore(witness)

    expect(fs.readFileSync(target, 'utf-8')).toBe('v1')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('a capture recorded before the digest still restores', () => {
    // A capture with the stat fields but no `sha256` predates the digest (an
    // older install's WAL). It is compared stat-only and still restores:
    // adding the key must not turn a recoverable WAL into a refusal.
    const { witness, target, sidecar } = written()
    const recorded = witness.capture
    if (recorded === null || recorded === undefined) {
      throw new Error('the witness carries no capture to strip the digest from')
    }
    const legacy = { ...recorded }
    delete legacy.sha256
    expect(legacy.sha256).toBeUndefined()

    fsRestore({ ...witness, capture: legacy })

    expect(fs.readFileSync(target, 'utf-8')).toBe('v1')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('a created file is still deleted by its inverse', () => {
    const result = fsWrite('fresh.txt', 'hello')
    expect(result.kind).toBe('Ok')
    fsRestore(result.value as WriteWitness)
    expect(fs.existsSync(path.join(ws, 'fresh.txt'))).toBe(false)
    fsRestore(result.value as WriteWitness)  // idempotent
  })
})

// ===========================================================================
// A capture that CARRIES the digest key must carry a bare 64-lowercase-hex
// digest. A present `null` is NOT "no digest": reading it as absent was the
// fail-open this pins, because a writer who can rewrite the record to null
// would downgrade the preimage check to stat-only, which is the check the key
// exists to keep off the clock. Only an ABSENT key is legacy. The absent-key
// legacy control and the valid-digest control already live above
// ("a capture recorded before the digest still restores" and the test that
// records the sidecar bytes digest).
// ===========================================================================

describe('a present recorded digest must be a bare 64-lowercase-hex string', () => {
  it('refuses a present null digest and leaves the target untouched', () => {
    const { witness, target, sidecar } = written()
    if (witness.capture === null || witness.capture === undefined) {
      throw new Error('the witness carries no capture')
    }
    ;(witness.capture as unknown as Record<string, unknown>).sha256 = null

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('sha256')
    expect(refusal.message).toContain('malformed')

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('v1')
  })

  it('refuses a recorded digest that is not 64 lowercase hex', () => {
    const { witness, target, sidecar } = written()
    if (witness.capture === null || witness.capture === undefined) {
      throw new Error('the witness carries no capture')
    }
    ;(witness.capture as unknown as Record<string, unknown>).sha256 = 'z'.repeat(64)

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('sha256')
    expect(refusal.message).toContain('malformed')

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('v1')
  })
})

// ===========================================================================
// The tampered sidecars. Each of these installed the attacker's file before.
// ===========================================================================

describe('a sidecar that is not the captured snapshot is refused', () => {
  it('refuses one rewritten in place inside one timestamp tick', () => {
    // Same inode, same length. `(dev, ino)` alone cannot see this, and on a
    // filesystem whose inode clock is coarse neither can `size`/`mtimeNs`/
    // `ctimeNs`: the rewrite lands in the same timestamp tick and every
    // captured stat field stays equal (issue #2652). The recorded stat tuple is
    // therefore pinned to the tampered file's own tuple before the inverse
    // runs, so the tick is out of the test and the only comparison left that
    // can refuse is the recorded sha256.
    const { witness, target, sidecar } = written()
    const before = fs.statSync(sidecar, { bigint: true })
    const fd = fs.openSync(sidecar, 'r+')
    fs.writeSync(fd, Buffer.from('zz'), 0, 2, 0)
    fs.closeSync(fd)
    const after = fs.statSync(sidecar, { bigint: true })
    expect([after.dev, after.ino]).toEqual([before.dev, before.ino])
    expect(after.size).toBe(before.size)
    expect(witness.capture?.sha256).toBe(sha256('v1'))
    asIfSameTick(witness, sidecar)

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('sha256')

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('zz')
  })

  it('refuses a forged recorded digest on an honest sidecar', () => {
    // The record is forged, the file is not: the sidecar still holds the bytes
    // the snapshot wrote, and only the recorded sha256 is changed to another
    // well-formed digest. A compare path that trusted the record, or that
    // treated a well-formed digest as unverifiable, would install the honest
    // bytes and report a clean reversal. The forged value is 64 lowercase hex,
    // so it takes the mismatch branch rather than the malformed-record one.
    const { witness, target, sidecar } = written()
    const recorded = witness.capture
    if (recorded === null || recorded === undefined) {
      throw new Error('the witness carries no capture to forge')
    }
    const forged = { ...recorded, sha256: '0'.repeat(64) }
    expect(forged.sha256).not.toBe(recorded.sha256)
    expect(recorded.sha256).toBe(sha256('v1'))

    const refusal = refusalOf(() => fsRestore({ ...witness, capture: forged }))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('sha256')
    // a well-formed forged digest must take the mismatch branch, not the
    // malformed-record branch
    expect(refusal.message).toContain('sha256 differ')

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('v1')
  })

  it('refuses a rewrite between the pre-rename read and the rename', () => {
    // The post-install pass exists for the window between the first read and
    // the rename, which is by name. A writer can rewrite the sidecar in place
    // after its bytes were hashed; that is forced here inside the rename, so
    // the pre-rename comparison sees the captured bytes and the installed
    // result does not. The recorded tuple is pinned to the rewritten file's own
    // tuple, so only the post-install digest can refuse.
    const { witness, target, sidecar } = written()
    hooks.beforeRename = () => {
      const fd = fs.openSync(sidecar, 'r+')
      fs.writeSync(fd, Buffer.from('zz'), 0, 2, 0)
      fs.closeSync(fd)
      asIfSameTick(witness, sidecar)
    }

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('sha256')
    expect(refusal.message).toContain('the restored target')
    expect(hooks.beforeRename).toBeUndefined()
    // the pre-rename pass saw the captured bytes, so the rename did install the
    // tampered file; the post-install pass is what refused, and the residue is
    // the target holding the tampered bytes rather than the sidecar slot.
    expect(fs.readFileSync(target, 'utf-8')).toBe('zz')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('refuses one re-permissioned between the pre-rename check and the rename', () => {
    // The digest cannot see a re-permissioning: the bytes, the size and the
    // inode are all unchanged. The window is forced inside the rename, after
    // the pre-rename pass has compared both the stat tuple and the digest, so
    // the installed result is the only pass that can see it.
    // `INSTALLED_CAPTURE_FIELDS` keeps `mode` precisely so the digest does not
    // replace the permission control, and the refusal must name the field that
    // drifted rather than the digest: the bytes are honest.
    //
    // The rename is by name and runs before the installed result is checked, so
    // the target ends at the old version's bytes (the captured preimage) with
    // the re-permissioned mode; the residue the refusal names is that mode,
    // never the forward mutation or a tampered file.
    const { witness, target, sidecar } = written()
    const capture = witness.capture
    if (capture === null || capture === undefined) {
      throw new Error('the witness carries no capture to re-permission')
    }
    const capturedMode = Number(capture.mode) & 0o777
    const rePermissioned = capturedMode === 0o640 ? 0o600 : 0o640
    hooks.beforeRename = () => {
      fs.chmodSync(sidecar, rePermissioned)
    }

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('mode differ')
    expect(refusal.message).not.toContain('sha256')
    expect(refusal.message).toContain('the restored target')
    expect(hooks.beforeRename).toBeUndefined()
    expect(fs.readFileSync(target, 'utf-8')).toBe('v1')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('refuses when the recorded digest cannot be re-read', () => {
    // The digest is read through the same descriptor the stat comes from, so a
    // recorded digest that cannot be read must refuse the restore rather than
    // fall back to the stat comparison. Fail closed: the target is untouched
    // and the sidecar is not consumed.
    const { witness, target, sidecar } = written()
    hooks.failRead = () => {
      throw new Error('simulated read failure')
    }

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('ERACE')
    expect(refusal.message).toContain('content digest')

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('v1')
  })

  it('refuses a grown sidecar before reading it', () => {
    // A sidecar that grew no longer matches the captured size, which the
    // recorded capture always carries. Refusing on that field BEFORE the digest
    // read keeps a grown sidecar from being read in full on every restore. The
    // read hook would blow up if the observation reached the read; the refusal
    // must instead be `EIDENTITY` and the hook must still be armed, which is
    // what proves the read was never attempted.
    const { witness, target, sidecar } = written()
    fs.appendFileSync(sidecar, 'x')
    hooks.failRead = () => {
      throw new Error('the grown sidecar was read')
    }

    const refusal = refusalOf(() => fsRestore(witness))
    expect(refusal.code).toBe('EIDENTITY')
    expect(refusal.message).toContain('size')
    expect(hooks.failRead).toBeDefined()

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.existsSync(sidecar)).toBe(true)
  })

  it('refuses one swapped for another inode', () => {
    const { witness, target, sidecar } = written()
    const decoy = path.join(ws, 'decoy')
    fs.writeFileSync(decoy, 'v1')            // the same BYTES as the snapshot
    fs.renameSync(decoy, sidecar)

    expect(codeOf(() => fsRestore(witness))).toBe('EIDENTITY')
    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.existsSync(sidecar)).toBe(true)
  })

  it('refuses one hardlinked from a second name', () => {
    // Installing it would hand the caller a file another name still points at,
    // the hazard the forward write refuses with `EMULTILINK`.
    const { witness, target, sidecar } = written()
    const alias = path.join(ws, 'alias')
    fs.linkSync(sidecar, alias)
    expect(Number(fs.statSync(sidecar, { bigint: true }).nlink)).toBe(2)

    expect(codeOf(() => fsRestore(witness))).toBe('EMULTILINK')
    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.existsSync(alias)).toBe(true)
  })

  it('refuses one replaced by a directory', () => {
    const { witness, target, sidecar } = written()
    fs.unlinkSync(sidecar)
    fs.mkdirSync(sidecar)

    expect(codeOf(() => fsRestore(witness))).toBe('EOUTSIDE')
    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
  })
})

// ===========================================================================
// A snapshot that fails after it created its sidecar removes that sidecar.
// ===========================================================================

describe('a failed snapshot leaves no sidecar behind', () => {
  it('removes the sidecar it created when the copy fails', () => {
    // `snapshotPreimage` creates the sidecar, then may throw at the copy, the
    // mode/mtime restoration, the stat, or the digest. At that point the handle
    // does not carry the preimage yet, so `discardWrite` (which removes what
    // the handle records) removes nothing and the file would outlive the
    // session inside the workspace. Forced here at `fchmod`, after the copy and
    // before the capture, the same window an ENOSPC, EPERM, EIO or ESTALE
    // throw lands in.
    hooks.failFchmod = () => {
      throw new Error('simulated fchmod failure')
    }

    const result = fsWrite('artifact.txt', 'v2')
    if (result.kind !== 'Err') {
      throw new Error('the snapshot failure was expected to refuse the write')
    }
    expect(result.value.code).toBe('ESNAPSHOT')

    expect(fs.readFileSync(path.join(ws, 'artifact.txt'), 'utf-8')).toBe('v1')
    expect(fs.readdirSync(path.join(ws, '.revl-fs-preimage'))).toEqual([])
  })
})
