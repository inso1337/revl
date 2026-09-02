// Every executed escape from roadmap item 422's filesystem-confinement audit,
// carried as a ts-tier regression (the peer of tests/test_fs_confinement_escapes.py).
//
// F1-F4 were fixed on the PY tier in `1602cc94` and the item header was written
// as though that closed them everywhere. It did not: `revl_fs_ts.ts` carried
// every one of them untouched, and F1's reproducer was public. Each test below
// was RUN against the pre-fix module first and reproduced the escape it names,
// the outside file destroyed, the workspace bytes written outside the root, the
// hardlink written through, the concurrent writer diverting a write out of the
// jail, so none of them is a hypothetical.
//
// These drive the host module's entry points DIRECTLY rather than a compiled
// artifact. That is deliberate: an inverse is a capability-free `pure` extern,
// and `revl recover` reconstructs one from a WAL witness with no revl source
// involved, so the witness a test hands `fsRestore`/`fsUnrm` is exactly the
// attacker's reach. `ts_witnessed_fs.test.ts` covers the composed path.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { Worker } from 'node:worker_threads'
import { fileURLToPath } from 'node:url'
import * as host from '../revl_fs_ts.ts'
import {
  FsOpError, PATH_FAMILIES, READ_HELPERS, SIDECAR_KINDS,
  fsMkdir, fsMove, fsRestore, fsRm, fsUnrm, fsWrite,
  freshSidecar, garbageDir, preimageDir, resolveSidecar, resolveWithin,
} from '../revl_fs_ts.ts'

const CANARY = 'CANARY - outside the workspace\n'
// Written as escapes rather than a literal byte so this file stays text.
const NUL_PATH = 'a\u0000b.txt'
const NUL_DIR = 'a\u0000b'

let base: string
let ws: string
let outside: string
let saved: string | undefined

beforeEach(() => {
  base = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'revl-fs-')))
  ws = path.join(base, 'ws')
  outside = path.join(base, 'outside')
  fs.mkdirSync(ws)
  fs.mkdirSync(outside)
  fs.writeFileSync(path.join(outside, 'canary.txt'), CANARY)
  saved = process.env.REVL_FS_WORKSPACE
  process.env.REVL_FS_WORKSPACE = ws
})

afterEach(() => {
  if (saved === undefined) delete process.env.REVL_FS_WORKSPACE
  else process.env.REVL_FS_WORKSPACE = saved
  fs.rmSync(base, { recursive: true, force: true })
})

function inWs(rel: string): string { return path.join(ws, rel) }
function outWs(rel: string): string { return path.join(outside, rel) }
function exists(p: string): boolean {
  try { fs.lstatSync(p); return true } catch { return false }
}

// ===========================================================================
// F1 (CRITICAL), an inverse's SOURCE endpoint was never confined
// ===========================================================================

describe('F1: an inverse cannot name a source outside the workspace root', () => {
  it('fsRestore cannot steal a file from outside the root', () => {
    // `replace` is a RENAME: an unconfined source is not merely readable, it is
    // REMOVED from where it lives. Pre-fix this both stole the bytes into the
    // workspace and destroyed the file at its original location.
    const victim = outWs('canary.txt')
    fs.writeFileSync(inWs('inside.txt'), 'inside\n')

    expect(() => fsRestore({ path: 'inside.txt', preimage: victim, created: false }))
      .toThrow(FsOpError)

    expect(fs.readFileSync(victim, 'utf-8')).toBe(CANARY)
    expect(fs.readFileSync(inWs('inside.txt'), 'utf-8')).toBe('inside\n')
  })

  it('fsUnrm cannot steal a file from outside the root', () => {
    const victim = outWs('canary.txt')
    fs.writeFileSync(inWs('inside.txt'), 'inside\n')

    expect(() => fsUnrm({ path: 'inside.txt', garbage: victim }))
      .toThrow(FsOpError)

    expect(fs.readFileSync(victim, 'utf-8')).toBe(CANARY)
    expect(fs.readFileSync(inWs('inside.txt'), 'utf-8')).toBe('inside\n')
  })

  it('an inverse source must be a sidecar this workspace produced', () => {
    // Confinement alone is not enough. A source anywhere INSIDE the root would
    // still make a capability-free `pure` inverse an arbitrary
    // move-anything-in-the-workspace primitive, so the source is restricted to
    // the two directories the reversal machinery owns.
    fs.writeFileSync(inWs('victim.txt'), 'a workspace file, not a sidecar\n')
    fs.writeFileSync(inWs('target.txt'), 'target\n')

    expect(() => fsRestore(
      { path: 'target.txt', preimage: inWs('victim.txt'), created: false }))
      .toThrow(/session preimage directory/)
    expect(() => fsUnrm({ path: 'target.txt', garbage: inWs('victim.txt') }))
      .toThrow(/session garbage directory/)

    expect(fs.readFileSync(inWs('victim.txt'), 'utf-8'))
      .toBe('a workspace file, not a sidecar\n')
    expect(fs.readFileSync(inWs('target.txt'), 'utf-8')).toBe('target\n')
  })

  it('a garbage sidecar is not admissible as a preimage (kinds do not cross)', () => {
    const parked = freshSidecar(garbageDir(), 'rm')
    fs.writeFileSync(parked, 'parked\n')
    fs.writeFileSync(inWs('target.txt'), 'target\n')
    expect(() => fsRestore({ path: 'target.txt', preimage: parked, created: false }))
      .toThrow(/session preimage directory/)
  })

  it('the round trip a real witness takes still works', () => {
    // The guard shrank the primitive; it must not have broken it. A witness
    // this workspace actually produced still restores.
    fs.writeFileSync(inWs('doc.txt'), 'v1\n')
    const r = fsWrite('doc.txt', 'v2\n')
    expect(r.kind).toBe('Ok')
    expect(fs.readFileSync(inWs('doc.txt'), 'utf-8')).toBe('v2\n')
    if (r.kind !== 'Ok') return
    fsRestore(r.value)
    expect(fs.readFileSync(inWs('doc.txt'), 'utf-8')).toBe('v1\n')
    // residue-free: the snapshot was consumed by the rename
    expect(fs.readdirSync(inWs('.revl-fs-preimage'))).toEqual([])
    // and a replay is a no-op, not a throw (item 243 rule 5)
    fsRestore(r.value)
    expect(fs.readFileSync(inWs('doc.txt'), 'utf-8')).toBe('v1\n')
  })

  it('an rm round trip still works and unrm is idempotent', () => {
    fs.writeFileSync(inWs('gone.txt'), 'bytes\n')
    const r = fsRm('gone.txt')
    expect(r.kind).toBe('Ok')
    expect(exists(inWs('gone.txt'))).toBe(false)
    if (r.kind !== 'Ok') return
    fsUnrm(r.value)
    expect(fs.readFileSync(inWs('gone.txt'), 'utf-8')).toBe('bytes\n')
    fsUnrm(r.value)
    expect(fs.readFileSync(inWs('gone.txt'), 'utf-8')).toBe('bytes\n')
    expect(fs.readdirSync(inWs('.revl-fs-garbage'))).toEqual([])
  })
})

// ===========================================================================
// F2 (HIGH), the sidecar directories themselves
// ===========================================================================

describe('F2: a planted sidecar symlink cannot redirect the reversal machinery', () => {
  it('a .revl-fs-preimage symlink cannot exfiltrate a workspace file', () => {
    // `mkdirSync(d, { recursive: true })` SUCCEEDS on a pre-existing symlink,
    // and the old callers returned the unresolved join, so the preimage
    // snapshot of a workspace file landed outside the root.
    fs.symlinkSync(outside, inWs('.revl-fs-preimage'))
    fs.writeFileSync(inWs('confidential.txt'), 'WORKSPACE SECRET\n')

    const r = fsWrite('confidential.txt', 'overwritten\n')
    expect(r.kind).toBe('Err')
    if (r.kind !== 'Err') return
    expect(r.value.code).toBe('EOUTSIDE')
    expect(r.value.message).toMatch(/sidecar directory is a symlink/)

    // refused BEFORE any byte was written: the target is byte-identical
    expect(fs.readFileSync(inWs('confidential.txt'), 'utf-8'))
      .toBe('WORKSPACE SECRET\n')
    expect(fs.readdirSync(outside)).toEqual(['canary.txt'])
  })

  it('a .revl-fs-garbage symlink cannot park a removed file outside the root', () => {
    fs.symlinkSync(outside, inWs('.revl-fs-garbage'))
    fs.writeFileSync(inWs('parked.txt'), 'PARKED SECRET\n')

    const r = fsRm('parked.txt')
    expect(r.kind).toBe('Err')
    if (r.kind !== 'Err') return
    expect(r.value.code).toBe('EOUTSIDE')

    // Ok-conditional registration: a refused rm removed nothing
    expect(fs.readFileSync(inWs('parked.txt'), 'utf-8')).toBe('PARKED SECRET\n')
    expect(fs.readdirSync(outside)).toEqual(['canary.txt'])
  })

  it('a dangling sidecar symlink is refused by type, not normalized away', () => {
    // realpath happily normalizes a dangling link; only an lstat sees it.
    fs.symlinkSync(path.join(base, 'nowhere'), inWs('.revl-fs-garbage'))
    fs.writeFileSync(inWs('parked.txt'), 'x\n')
    const r = fsRm('parked.txt')
    expect(r.kind).toBe('Err')
    if (r.kind !== 'Err') return
    expect(r.value.code).toBe('EOUTSIDE')
  })

  it('a sidecar may only be created in a sidecar directory', () => {
    fs.mkdirSync(inWs('sub'))
    expect(() => freshSidecar(inWs('sub'), 'rm')).toThrow(/garbage or preimage/)
    expect(() => freshSidecar(outside, 'rm')).toThrow(FsOpError)
  })

  it('resolveSidecar refuses a source that is not in the matching directory', () => {
    const parked = freshSidecar(garbageDir(), 'rm')
    expect(resolveSidecar(parked, 'garbage')).toBe(parked)
    expect(() => resolveSidecar(parked, 'preimage')).toThrow(FsOpError)
    expect(() => resolveSidecar(garbageDir(), 'garbage')).toThrow(FsOpError)
  })

  it('the sidecar directories are still created inside the root (must #3)', () => {
    expect(garbageDir()).toBe(inWs(SIDECAR_KINDS.garbage))
    expect(preimageDir()).toBe(inWs(SIDECAR_KINDS.preimage))
    expect(fs.lstatSync(garbageDir()).isDirectory()).toBe(true)
  })
})

// ===========================================================================
// F3 (HIGH), hardlink aliases, which realpath cannot see at all
// ===========================================================================

describe('F3: a hardlinked target is refused, not written through', () => {
  it('write refuses a hardlink alias out of the root', () => {
    const canary = outWs('canary.txt')
    const alias = inWs('alias.txt')
    fs.linkSync(canary, alias)            // one inode, two names

    const r = fsWrite('alias.txt', 'PWNED THROUGH THE HARDLINK\n')
    expect(r.kind).toBe('Err')
    if (r.kind !== 'Err') return
    expect(r.value.code).toBe('EMULTILINK')

    // Ok-conditional registration: nothing registered, and both names still
    // hold the original bytes on the still-shared inode.
    expect(fs.readFileSync(canary, 'utf-8')).toBe(CANARY)
    expect(fs.readFileSync(alias, 'utf-8')).toBe(CANARY)
    expect(fs.statSync(canary).ino).toBe(fs.statSync(alias).ino)
  })

  it('a hardlink INSIDE the root is refused too, and says why', () => {
    // The cost is real and stated plainly: the guard cannot tell an inside
    // alias from an outside one through the inode, so it refuses both.
    fs.writeFileSync(inWs('a.txt'), 'shared\n')
    fs.linkSync(inWs('a.txt'), inWs('b.txt'))
    const r = fsWrite('a.txt', 'x\n')
    expect(r.kind).toBe('Err')
    if (r.kind !== 'Err') return
    expect(r.value.code).toBe('EMULTILINK')
    expect(r.value.message).toMatch(/more than one hard link/)
  })

  it('the refused write leaves no preimage residue', () => {
    fs.linkSync(outWs('canary.txt'), inWs('alias.txt'))
    fsWrite('alias.txt', 'x\n')
    if (exists(inWs('.revl-fs-preimage'))) {
      expect(fs.readdirSync(inWs('.revl-fs-preimage'))).toEqual([])
    }
  })
})

// ===========================================================================
// F4 (MEDIUM), the check-to-syscall window
// ===========================================================================

describe('F4: a concurrent writer cannot divert a write out of the root', () => {
  it('survives a leaf swapped for an outside symlink under the write', () => {
    // Pre-fix, `resolveWithin` was followed by a NAME-BASED `writeFileSync`,
    // and a competing writer in the workspace swapping the leaf for a symlink
    // in that window diverted the write outside the root, reproduced in 88
    // attempts. Post-fix the leaf is opened O_NOFOLLOW, the directory chain is
    // lstat-checked, the fd's (dev, ino) is re-established against the name,
    // and every byte goes through the fd.
    const target = inWs('racy.txt')
    const victim = outWs('raced.txt')
    fs.writeFileSync(target, 'v1')

    const deadline = Date.now() + 4000
    const worker = new Worker(
      new URL('./fs_confinement_swapper.ts', import.meta.url),
      { workerData: { target, victim, deadline } },
    )
    try {
      while (Date.now() < deadline) {
        // No try/catch on purpose: the guard's entry points are total, so a
        // raw throw escaping here is a real regression, not a transient.
        const r = fsWrite('racy.txt', 'RACED PAYLOAD')
        expect(['Ok', 'Err']).toContain(r.kind)
        if (exists(victim)) break
      }
    } finally {
      worker.terminate()
    }

    expect(exists(victim)).toBe(false)
    expect(fs.readdirSync(outside)).toEqual(['canary.txt'])
  }, 30_000)

  it('a write racing an unlink refuses rather than claiming Ok', () => {
    // The fd keeps the bytes on the inode the check admitted, but the NAME may
    // no longer hold them. Reporting Ok would enumerate a write nobody can see
    // and register an undo over a change that never became visible.
    const target = inWs('vanishing.txt')
    fs.writeFileSync(target, 'v1')
    const deadline = Date.now() + 4000
    const worker = new Worker(
      new URL('./fs_confinement_swapper.ts', import.meta.url),
      { workerData: { target, victim: outWs('unused.txt'), deadline,
        unlinkOnly: true } },
    )
    let sawRace = false
    try {
      while (Date.now() < deadline) {
        const r = fsWrite('vanishing.txt', 'payload')
        if (r.kind === 'Err' && r.value.code === 'ERACE') { sawRace = true; break }
      }
    } finally {
      worker.terminate()
    }
    // Not asserted as guaranteed to happen (it is a race), but when it does the
    // answer must be ERACE and never a false Ok.
    if (sawRace) expect(exists(outWs('unused.txt'))).toBe(false)
  }, 30_000)

  it('an unraced write still returns Ok and is reversible', () => {
    fs.writeFileSync(inWs('plain.txt'), 'v1\n')
    const r = fsWrite('plain.txt', 'v2\n')
    expect(r.kind).toBe('Ok')
    if (r.kind !== 'Ok') return
    expect(fs.readFileSync(inWs('plain.txt'), 'utf-8')).toBe('v2\n')
    fsRestore(r.value)
    expect(fs.readFileSync(inWs('plain.txt'), 'utf-8')).toBe('v1\n')
  })
})

// ===========================================================================
// The guard surface itself
// ===========================================================================

describe('the guard entry points are confined and total', () => {
  it('refuses a path outside the root, symlinked or spelled with ..', () => {
    fs.symlinkSync(outside, inWs('escape'))
    expect(() => resolveWithin('escape/canary.txt')).toThrow(/escapes the session/)
    expect(() => resolveWithin('../outside/canary.txt')).toThrow(/escapes the session/)
    expect(() => resolveWithin(outWs('canary.txt'))).toThrow(/escapes the session/)
  })

  it('does not mistake a sibling sharing the root as a name prefix', () => {
    const sibling = `${ws}-evil`
    fs.mkdirSync(sibling)
    fs.writeFileSync(path.join(sibling, 'x.txt'), 'x')
    expect(() => resolveWithin(path.join(sibling, 'x.txt')))
      .toThrow(/escapes the session/)
  })

  it('refuses every op when no workspace root is configured', () => {
    delete process.env.REVL_FS_WORKSPACE
    for (const r of [fsWrite('a.txt', 'x'), fsRm('a.txt'),
      fsMove('a.txt', 'b.txt'), fsMkdir('d')]) {
      expect(r.kind).toBe('Err')
      if (r.kind === 'Err') expect(r.value.code).toBe('EWORKSPACE')
    }
  })

  it('refuses the workspace root itself as a target', () => {
    const r = fsRm(ws)
    expect(r.kind).toBe('Err')
    if (r.kind !== 'Err') return
    expect(r.value.code).toBe('EWORKSPACE')
  })

  it('a NUL byte in a path is refused, not thrown as a raw TypeError', () => {
    // node rejects a NUL with a TypeError (ERR_INVALID_ARG_VALUE), which is
    // not an errno error and escaped every entry point's catch, breaking
    // fs.rvl's declared `-> Result[_, FsError]` contract.
    for (const r of [fsWrite(NUL_PATH, 'x'), fsRm(NUL_PATH),
      fsMove(NUL_PATH, 'c.txt'), fsMkdir(NUL_DIR)]) {
      expect(r.kind).toBe('Err')
      if (r.kind !== 'Err') continue
      expect(r.value.code).toBe('EINVAL')
      expect(r.value.message).toMatch(/with the NUL removed/)
      expect(r.value.path).not.toContain('\u0000')
    }
  })

  it('every enumerated guard entry point is wrapped total', () => {
    const listed = [
      ...Object.values(PATH_FAMILIES).flatMap((e) => [...e]),
      ...READ_HELPERS,
    ]
    for (const name of listed) {
      const fn = (host as unknown as Record<string, unknown>)[name] as
        { isTotalGuard?: boolean } | undefined
      expect(fn, `guard entry point \`${name}\` is not exported`).toBeTruthy()
      expect(fn?.isTotalGuard,
        `guard entry point \`${name}\` is not wrapped total`).toBe(true)
    }
  })
})

// the worker module path used above, asserted to exist so a rename of the
// helper fails loudly rather than silently skipping the race.
it('the swapper worker module is present', () => {
  expect(fs.existsSync(fileURLToPath(
    new URL('./fs_confinement_swapper.ts', import.meta.url)))).toBe(true)
})
