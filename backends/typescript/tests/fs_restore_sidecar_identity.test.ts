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
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { FsOpError, fsRestore, fsWrite } from '../revl_fs_ts.ts'
import type { WriteWitness } from '../revl_fs_ts.ts'

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

function codeOf(fn: () => void): string {
  try {
    fn()
  } catch (e) {
    expect(e).toBeInstanceOf(FsOpError)
    return (e as FsOpError).code
  }
  throw new Error('the call was expected to refuse, and did not')
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

  it('a created file is still deleted by its inverse', () => {
    const result = fsWrite('fresh.txt', 'hello')
    expect(result.kind).toBe('Ok')
    fsRestore(result.value as WriteWitness)
    expect(fs.existsSync(path.join(ws, 'fresh.txt'))).toBe(false)
    fsRestore(result.value as WriteWitness)  // idempotent
  })
})

// ===========================================================================
// The tampered sidecars. Each of these installed the attacker's file before.
// ===========================================================================

describe('a sidecar that is not the captured snapshot is refused', () => {
  it('refuses one rewritten in place', () => {
    // Same inode, same length: `(dev, ino)` alone cannot see this, which is why
    // the capture records the stamps too.
    const { witness, target, sidecar } = written()
    const before = fs.statSync(sidecar, { bigint: true })
    const fd = fs.openSync(sidecar, 'r+')
    fs.writeSync(fd, Buffer.from('zz'), 0, 2, 0)
    fs.closeSync(fd)
    const after = fs.statSync(sidecar, { bigint: true })
    expect([after.dev, after.ino]).toEqual([before.dev, before.ino])

    expect(codeOf(() => fsRestore(witness))).toBe('EIDENTITY')

    expect(fs.readFileSync(target, 'utf-8')).toBe('v2')
    expect(fs.readFileSync(sidecar, 'utf-8')).toBe('zz')
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
