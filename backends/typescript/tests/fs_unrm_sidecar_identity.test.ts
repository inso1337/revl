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
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { FsOpError, fsRm, fsUnrm } from '../revl_fs_ts.ts'
import type { RmWitness } from '../revl_fs_ts.ts'

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

// ===========================================================================
// The non-vacuity controls: an honest unrm is unchanged and stays quiet. They
// pass before this change as well as after it, so the refusals below cannot be
// satisfied by an inverse that simply refuses always.
// ===========================================================================

describe('an untampered unrm is unchanged', () => {
  it('puts the parked file back silently and leaves no residue', () => {
    const { witness, target, sidecar } = removed()

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

  it('an honest rm of a directory still reverses', () => {
    // The over-refusal control, and why this inverse cannot copy the preimage
    // side's flat "regular file" assertion: `fsRm` parks a directory too.
    fs.mkdirSync(path.join(ws, 'tree'))
    fs.writeFileSync(path.join(ws, 'tree', 'leaf'), 'inside')
    const { witness, target, sidecar } = removed('tree')
    expect(fs.statSync(sidecar).isDirectory()).toBe(true)

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
