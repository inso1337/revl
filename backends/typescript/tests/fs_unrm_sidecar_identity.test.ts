// `fsUnrm` re-checks the sidecar it is about to install (issue #1038), the ts
// peer of tests/test_fs_unrm_sidecar_identity_1038.py and the `unrm` half of
// what issue #1016 closed for `fsRestore`.
//
// `resolveSidecar` proves the SLOT is a garbage sidecar this workspace owns. It
// proves nothing about the FILE in that slot, and a parked file sits there for
// the whole life of an activation, so a same-UID writer inside the workspace had
// that window to rewrite it in place, swap it for another inode with the same
// bytes, link a second name to it, or replace it with something that is not a
// file. The inverse then renamed the result back over the original path and
// reported a clean, residue-free reversal.
//
// `parkCapturedSidecar` now records the parked file's identity, the witness
// carries it as a superset `capture` key, and `installParkedSidecar` re-checks
// it on the way in and re-checks the installed result on the way out, with the
// codes the preimage side already spells.
//
// Both directions are pinned: a sidecar that cannot be shown to be the parked
// one throws (which the teardown loop records as restore-residue) and is never
// installed, and an untampered unrm stays exactly as quiet as before, including
// for the two cases the preimage side may refuse outright and this one must not
// (a parked directory, and a file that already carried a second hardlink).
//
// Issue #2666 added a content comparison on both install passes: the sha256 of
// the parked file's bytes, recorded at park time when the file opens read-only
// and re-read through an O_NOFOLLOW descriptor on install. The seven stat fields
// alone do not see a same-size in-place rewrite that lands inside one inode
// timestamp tick; the digest does, because it does not depend on clock
// resolution. The same-tick test pins the recorded stat tuple to the tampered
// file's own tuple before the inverse runs, so only the digest can refuse and
// the result does not depend on the clock. A capture with no `sha256` (an older
// WAL) is compared stat-only and still restores, and a parked file that could
// not be read at park time stays stat-only too.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { FsOpError, fsRm, fsUnrm } from '../revl_fs_ts.ts'
import type { RmWitness } from '../revl_fs_ts.ts'

// The post-rename install pass can only be forced through the rename, so this
// file wraps that one `node:fs` call. The hook is empty unless a test sets it,
// and the wrapper delegates to the real call otherwise.
const hooks = vi.hoisted(() => ({
  afterRename: undefined as (() => void) | undefined,
}))

vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:fs')>()
  return {
    ...actual,
    renameSync: (src: fs.PathLike, dst: fs.PathLike): void => {
      actual.renameSync(src, dst)
      const hook = hooks.afterRename
      hooks.afterRename = undefined
      if (hook !== undefined) hook()
    },
  }
})

let base: string
let ws: string
let saved: string | undefined

beforeEach(() => {
  base = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'revl-fs-')))
  ws = path.join(base, 'ws')
  fs.mkdirSync(ws)
  fs.writeFileSync(path.join(ws, 'doomed.txt'), 'payload')
  saved = process.env.REVL_FS_WORKSPACE
  process.env.REVL_FS_WORKSPACE = ws
})

afterEach(() => {
  // a hook a test set but did not consume (a test that fails early) must not
  // leak into the next one.
  hooks.afterRename = undefined
  if (saved === undefined) delete process.env.REVL_FS_WORKSPACE
  else process.env.REVL_FS_WORKSPACE = saved
  fs.rmSync(base, { recursive: true, force: true })
})

/** Perform the witnessed rm and hand back its witness plus the two paths. */
function removed(name = 'doomed.txt'):
{ witness: RmWitness; target: string; sidecar: string } {
  const result = fsRm(name)
  expect(result.kind).toBe('Ok')
  const witness = result.value as RmWitness
  expect(path.dirname(witness.garbage)).toBe(path.join(ws, '.revl-fs-garbage'))
  expect(fs.existsSync(path.join(ws, name))).toBe(false)
  return { witness, target: path.join(ws, name), sidecar: witness.garbage }
}

function codeOf(fn: () => void): string {
  try {
    fn()
  } catch (e) {
    expect(e).toBeInstanceOf(FsOpError)
    return (e as FsOpError).code
  }
  throw new Error('the call was expected to refuse, and did not')
}

function sha256(text: string): string {
  return createHash('sha256').update(text).digest('hex')
}

/** Run `fn`, returning the `FsOpError` it refused with, or null when it did
 * not refuse. The same-tick tests use this to report the bytes a base build
 * installed instead of only saying that no error was raised. */
function refusalOf(fn: () => void): FsOpError | null {
  try {
    fn()
    return null
  } catch (e) {
    if (e instanceof FsOpError) return e
    throw e
  }
}

/** Pin the witness's recorded stat fields to `sidecar`'s CURRENT tuple.
 *
 * This is the same-tick case of issue #2666, forced rather than raced: after a
 * same-size in-place rewrite inside one inode timestamp tick, every captured
 * stat field equals the tampered file's own tuple. Overwriting the recorded
 * fields with that tuple therefore removes the stat comparison from the test
 * without removing the tamper, so anything that still refuses can only be the
 * content digest. On a filesystem whose clock did tick this leaves a test that
 * passes for one reason instead of a test that flips with the tick. */
function asIfSameTick(witness: RmWitness, sidecar: string): void {
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

function readable(real: string): boolean {
  try {
    fs.accessSync(real, fs.constants.R_OK)
    return true
  } catch {
    return false
  }
}

// ===========================================================================
// The non-vacuity controls: an honest unrm is unchanged and stays quiet. The
// restore itself passes before this change as well as after it, so the refusals
// below cannot be satisfied by an inverse that simply refuses always. The first
// control also asserts the positive direction of the digest, which is the one
// assertion here that needs the new park capture.
// ===========================================================================

describe('an untampered unrm is unchanged', () => {
  it('puts the parked file back silently and leaves no residue', () => {
    const { witness, target, sidecar } = removed()
    // the capture binds the parked bytes, not only their stat tuple
    expect(witness.capture?.sha256).toBe(sha256('payload'))

    fsUnrm(witness)

    expect(fs.readFileSync(target, 'utf-8')).toBe('payload')
    expect(fs.existsSync(sidecar)).toBe(false)
    expect(fs.readdirSync(path.join(ws, '.revl-fs-garbage'))).toEqual([])

    fsUnrm(witness)                          // still idempotent on replay
    expect(fs.readFileSync(target, 'utf-8')).toBe('payload')
  })

  it('a witness that carries no capture still unrms', () => {
    // A durable record written before the capture key existed (an older
    // install's WAL). Refusing it would strand a recoverable WAL.
    const { witness, target, sidecar } = removed()
    fsUnrm({ path: witness.path, garbage: witness.garbage })
    expect(fs.readFileSync(target, 'utf-8')).toBe('payload')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('a capture with the digest key dropped still unrms', () => {
    // A capture written after the identity key existed but before the content
    // digest (an older WAL, still replayable). Dropping the key here pins both
    // directions: the capture stays stat-only and still restores, and a missing
    // digest is never treated as a failed content check.
    const { witness, target, sidecar } = removed()
    if (witness.capture === null || witness.capture === undefined) {
      throw new Error('the witness carries no capture')
    }
    delete witness.capture.sha256
    expect(witness.capture.sha256).toBeUndefined()

    fsUnrm(witness)

    expect(fs.readFileSync(target, 'utf-8')).toBe('payload')
    expect(fs.existsSync(sidecar)).toBe(false)
  })

  it('an honest rm of a write-only file still reverses stat-only', (ctx) => {
    // The capture limitation: `parkedStat` deliberately never opens the parked
    // inode, because `fsRm` accepts a file whose mode denies read. Such a file
    // cannot be digested at park, so the capture omits the key and the reversal
    // proceeds on the stat check alone, exactly as before (issue #2666).
    fs.chmodSync(path.join(ws, 'doomed.txt'), 0o200)
    if (readable(path.join(ws, 'doomed.txt'))) {
      ctx.skip('cannot revoke read permission (running as root)')
    }
    const { witness, target, sidecar } = removed()
    expect(witness.capture?.sha256).toBeUndefined()
    expect(fs.statSync(sidecar, { bigint: true }).mode & 0o777n).toBe(0o200n)

    fsUnrm(witness)

    expect(fs.statSync(target, { bigint: true }).mode & 0o777n).toBe(0o200n)
    fs.chmodSync(target, 0o600)
    expect(fs.readFileSync(target, 'utf-8')).toBe('payload')
  })

  it('an honest rm of a directory still reverses', () => {
    // The over-refusal control, and why this inverse cannot copy the preimage
    // side's flat "regular file" assertion: `fsRm` parks a directory too.
    fs.mkdirSync(path.join(ws, 'tree'))
    fs.writeFileSync(path.join(ws, 'tree', 'leaf'), 'inside')
    const { witness, target, sidecar } = removed('tree')
    expect(fs.statSync(sidecar).isDirectory()).toBe(true)
    expect(witness.capture?.sha256).toBeUndefined()

    fsUnrm(witness)

    expect(fs.statSync(target).isDirectory()).toBe(true)
    expect(fs.readFileSync(path.join(target, 'leaf'), 'utf-8')).toBe('inside')
  })

  it('an honest rm of an already-hardlinked file still reverses', () => {
    // The other over-refusal control: a flat `nlink === 1` demand would refuse
    // an honest reversal. What is refused is a link ADDED since the park.
    fs.writeFileSync(path.join(ws, 'shared.txt'), 'two names')
    fs.linkSync(path.join(ws, 'shared.txt'), path.join(ws, 'alias.txt'))
    const { witness, target, sidecar } = removed('shared.txt')
    expect(Number(fs.statSync(sidecar, { bigint: true }).nlink)).toBe(2)

    fsUnrm(witness)

    expect(fs.readFileSync(target, 'utf-8')).toBe('two names')
  })
})

// ===========================================================================
// The tampered sidecars. Each of these installed the attacker's file before.
// ===========================================================================

describe('a sidecar that is not the parked file is refused', () => {
  it('refuses one rewritten in place', () => {
    // Same inode, same length: `(dev, ino)` alone cannot see this, which is why
    // the capture records the stamps too.
    const { witness, target, sidecar } = removed()
    const before = fs.statSync(sidecar, { bigint: true })
    const fd = fs.openSync(sidecar, 'r+')
    fs.writeSync(fd, Buffer.from('POISON!'), 0, 7, 0)
    fs.closeSync(fd)
    const after = fs.statSync(sidecar, { bigint: true })
    expect([after.dev, after.ino]).toEqual([before.dev, before.ino])

    expect(codeOf(() => fsUnrm(witness))).toBe('EIDENTITY')

    expect(fs.existsSync(target)).toBe(false)
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('POISON!')
  })

  it('refuses one swapped for another inode', () => {
    const { witness, target, sidecar } = removed()
    const decoy = path.join(ws, 'decoy')
    fs.writeFileSync(decoy, 'payload')       // the same BYTES as the parked file
    fs.renameSync(decoy, sidecar)

    expect(codeOf(() => fsUnrm(witness))).toBe('EIDENTITY')
    expect(fs.existsSync(target)).toBe(false)
    expect(fs.existsSync(sidecar)).toBe(true)
  })

  it('refuses one hardlinked from a second name since the park', () => {
    // Installing it would hand the caller a file another name still points at,
    // the hazard the forward write refuses with `EMULTILINK`.
    const { witness, target, sidecar } = removed()
    expect(Number(fs.statSync(sidecar, { bigint: true }).nlink)).toBe(1)
    const alias = path.join(ws, 'alias')
    fs.linkSync(sidecar, alias)

    expect(codeOf(() => fsUnrm(witness))).toBe('EMULTILINK')
    expect(fs.existsSync(target)).toBe(false)
    expect(fs.existsSync(alias)).toBe(true)
  })

  it('refuses one replaced by a directory', () => {
    const { witness, target, sidecar } = removed()
    fs.unlinkSync(sidecar)
    fs.mkdirSync(sidecar)

    expect(codeOf(() => fsUnrm(witness))).toBe('EOUTSIDE')
    expect(fs.existsSync(target)).toBe(false)
  })
})

// ===========================================================================
// The content digest (issue #2666). The stat tuple cannot see a same-size
// in-place rewrite inside one inode timestamp tick; the recorded bytes can.
// ===========================================================================

describe('a parked file whose bytes changed is refused', () => {
  it('refuses a same-tick same-length in-place rewrite', () => {
    // The collision the seven stat fields cannot see. The parked file is
    // rewritten in place at the same length, so `dev`, `ino`, `mode`, `nlink`
    // and `size` are unchanged, and the capture's stat keys are overwritten
    // from the POST-rewrite stat, so `mtimeNs`/`ctimeNs` cannot drift either.
    // That is what a filesystem whose inode clock has not ticked between the
    // park and the rewrite shows this check. Only the recorded bytes still tell
    // the two files apart, so only the digest refuses it. Before the digest
    // existed the base installs `POISON!` over the original path.
    const { witness, target, sidecar } = removed()
    if (witness.capture === null || witness.capture === undefined) {
      throw new Error('the witness carries no capture')
    }

    const fd = fs.openSync(sidecar, 'r+')
    fs.writeSync(fd, Buffer.from('POISON!'), 0, 7, 0)
    fs.closeSync(fd)
    asIfSameTick(witness, sidecar)
    expect(witness.capture.size).toBe('7')

    const refused = refusalOf(() => fsUnrm(witness))
    if (refused === null) {
      throw new Error(
        `the same-tick rewrite was installed: ${fs.readFileSync(target, 'utf-8')}`)
    }
    expect(refused.code).toBe('EIDENTITY')
    expect(refused.message).toContain('sha256')

    expect(fs.existsSync(target)).toBe(false)
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('POISON!')
  })

  it('refuses when a recorded digest cannot be re-read', (ctx) => {
    // A digest was recorded, so the parked file was readable at park. If it is
    // not readable at install, the content check cannot run; falling back to
    // stat-only would let exactly the rewrite it exists for pass, so the
    // reversal fails closed instead (issue #2666).
    const { witness, target, sidecar } = removed()
    fs.chmodSync(sidecar, 0o200)             // readable at park, not at install
    if (readable(sidecar)) {
      ctx.skip('cannot revoke read permission (running as root)')
    }
    // the chmod's own stat drift is not the point, the unreadability is
    asIfSameTick(witness, sidecar)

    const refused = refusalOf(() => fsUnrm(witness))
    if (refused === null) {
      throw new Error(
        'the recorded digest could not be re-read and the parked file was '
        + 'installed anyway')
    }
    expect(refused.code).toBe('EIDENTITY')
    expect(refused.message).toContain('could not be re-read')

    expect(fs.existsSync(target)).toBe(false)
    expect(fs.existsSync(sidecar)).toBe(true)
  })

  it('re-checks the installed digest after the rename', () => {
    // The digest is compared on BOTH passes. A writer that wins the window
    // between the pre-rename check and the rename has its file installed under
    // a check that passed on the parked one. The post-install pass re-reads the
    // installed bytes; the stat keys are pinned here too, so the digest is the
    // only thing left that can refuse it.
    const { witness, target, sidecar } = removed()
    if (witness.capture === null || witness.capture === undefined) {
      throw new Error('the witness carries no capture')
    }
    const capture = witness.capture
    hooks.afterRename = () => {
      const fd = fs.openSync(target, 'r+')
      fs.writeSync(fd, Buffer.from('POISON!'), 0, 7, 0)
      fs.closeSync(fd)
      asIfSameTick(witness, target)
    }

    const refused = refusalOf(() => fsUnrm(witness))
    if (refused === null) {
      throw new Error(
        `the post-rename rewrite was installed: ${fs.readFileSync(target, 'utf-8')}`)
    }
    expect(refused.code).toBe('EIDENTITY')

    expect(fs.readFileSync(target, 'utf-8')).toBe('POISON!')
    expect(fs.existsSync(sidecar)).toBe(false)
    expect(capture.sha256).toBe(sha256('payload'))
  })
})
