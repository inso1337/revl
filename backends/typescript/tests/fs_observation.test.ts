// The OBSERVATION half of the ts fs host — `fsResolveWithin`, `fsLexists`,
// `fsIsDir` (backends/typescript/revl_fs_ts.ts, exposed at the revl level as
// `stdlib/fs.rvl`'s `resolve_within` / `lexists` / `is_dir`).
//
// WHY THE SURFACE EXISTS. A consumer's own `@ts` body needs the confinement
// decision — "may I look at this path, and what is there?" — and had no
// supported door to it. item 396(B) jails a USER-origin `= @ts ref` to the user
// compile tree and this module lives in the install tree; item 410's
// `__REVL_STDLIB_REF_ROOT__` is install-origin only; and item 422 F1 removed the
// unconfined primitives the deprecated `globalThis.__revlFs` seam published,
// correctly. What was left was a guessed relative specifier into the install
// tree, which breaks whenever revl moves.
//
// So the door is a revl one: a consumer `use`s the three externs, gets a
// CONFINED absolute path back, and reads it with plain `node:fs` in its own
// body — importing nothing from here.
//
// This suite drives the shipped entry points directly against a real workspace.
// The cross-TIER claim (py and ts answer one corpus identically, through the
// real emitted bodies on both sides) is tests/test_fs_observation.py; the
// single-choke-point scan that proves these route through the family-1 guard
// like every mutation does is ./fs_confinement_families.test.ts, which reads the
// entry-point list out of stdlib/fs.rvl and so already covers these three.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import {
  PATH_FAMILIES, READ_HELPERS, WORKSPACE_ENV,
  fsIsDir, fsLexists, fsResolveWithin,
} from '../revl_fs_ts.ts'

let base: string
let ws: string
let outside: string
let saved: string | undefined

beforeEach(() => {
  base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'revl-fs-obs-')))
  ws = path.join(base, 'ws')
  outside = path.join(base, 'outside')
  fs.mkdirSync(path.join(ws, 'sub'), { recursive: true })
  fs.mkdirSync(outside)
  fs.writeFileSync(path.join(ws, 'a.txt'), 'alpha\n')
  fs.writeFileSync(path.join(ws, 'sub', 'b.txt'), 'beta\n')
  fs.writeFileSync(path.join(outside, 'secret.txt'), 'secret\n')
  fs.symlinkSync(path.join(ws, 'a.txt'), path.join(ws, 'link_in'))
  fs.symlinkSync(path.join(outside, 'secret.txt'), path.join(ws, 'link_out'))
  saved = process.env[WORKSPACE_ENV]
  process.env[WORKSPACE_ENV] = ws
})

afterEach(() => {
  if (saved === undefined) delete process.env[WORKSPACE_ENV]
  else process.env[WORKSPACE_ENV] = saved
  fs.rmSync(base, { recursive: true, force: true })
})

function okValue<T>(r: { kind: string; value: unknown }): T {
  expect(r.kind, `expected Ok, got ${JSON.stringify(r)}`).toBe('Ok')
  return r.value as T
}

function errCode(r: { kind: string; value: unknown }): string {
  expect(r.kind, `expected Err, got ${JSON.stringify(r)}`).toBe('Err')
  return (r.value as { code: string }).code
}

describe('the observation entry points', () => {
  it('resolve a path inside the root to its realpath', () => {
    expect(okValue(fsResolveWithin('a.txt'))).toBe(path.join(ws, 'a.txt'))
    expect(okValue(fsResolveWithin('sub/../a.txt'))).toBe(path.join(ws, 'a.txt'))
    expect(okValue(fsResolveWithin(path.join(ws, 'a.txt')))).toBe(path.join(ws, 'a.txt'))
    expect(okValue(fsResolveWithin(''))).toBe(ws)
    expect(okValue(fsResolveWithin('.'))).toBe(ws)
  })

  it('resolve a target that does not exist yet', () => {
    // a `write`/`mkdir` target must resolve BEFORE it exists, or the guard
    // could not admit the very ops it exists for. The existence question is
    // answered separately, and honestly.
    expect(okValue(fsResolveWithin('missing.txt'))).toBe(path.join(ws, 'missing.txt'))
    expect(okValue(fsResolveWithin('deep/missing.txt')))
      .toBe(path.join(ws, 'deep/missing.txt'))
    expect(okValue(fsLexists('missing.txt'))).toBe(false)
    expect(okValue(fsIsDir('missing.txt'))).toBe(false)
  })

  it('answer the existence and directory questions', () => {
    expect(okValue(fsLexists('a.txt'))).toBe(true)
    expect(okValue(fsIsDir('a.txt'))).toBe(false)
    expect(okValue(fsLexists('sub'))).toBe(true)
    expect(okValue(fsIsDir('sub'))).toBe(true)
    expect(okValue(fsIsDir('.'))).toBe(true)
    expect(okValue(fsLexists('sub/b.txt'))).toBe(true)
  })

  it('resolve a symlink BEFORE the membership test (must #1)', () => {
    // one pointing inside is admitted at its TARGET...
    expect(okValue(fsResolveWithin('link_in'))).toBe(path.join(ws, 'a.txt'))
    expect(okValue(fsLexists('link_in'))).toBe(true)
    // ...and one pointing out is REFUSED, not followed. This is the whole
    // reason a consumer asks the guard instead of calling `fs.existsSync`.
    expect(errCode(fsResolveWithin('link_out'))).toBe('EOUTSIDE')
    expect(errCode(fsLexists('link_out'))).toBe('EOUTSIDE')
    expect(errCode(fsIsDir('link_out'))).toBe('EOUTSIDE')
  })

  it('refuse every path outside the root, with the boundary code', () => {
    for (const escape of ['..', '../outside/secret.txt',
      'sub/../../outside/secret.txt', path.join(outside, 'secret.txt'),
      '/etc/hosts']) {
      expect(errCode(fsResolveWithin(escape)), escape).toBe('EOUTSIDE')
      expect(errCode(fsLexists(escape)), escape).toBe('EOUTSIDE')
      expect(errCode(fsIsDir(escape)), escape).toBe('EOUTSIDE')
    }
  })

  it('answer a refusal rather than a FACT about what is outside', () => {
    // The outside file exists and is a regular file. Neither is reported: the
    // answer is the boundary refusal, so the surface is not an oracle for the
    // filesystem beyond the jail.
    const r = fsLexists('link_out')
    expect(r.kind).toBe('Err')
    expect((r.value as { code: string; path: string }).code).toBe('EOUTSIDE')
    expect((r.value as { path: string }).path).toBe(path.join(outside, 'secret.txt'))
  })

  it('refuse a name no filesystem can hold, as EINVAL not a TypeError', () => {
    // item 422 F6 totality: node throws `ERR_INVALID_ARG_VALUE` (a TypeError)
    // for an embedded NUL rather than an errno error, which would escape the
    // `catch` and break the declared `Result` contract.
    expect(errCode(fsResolveWithin('nul\0byte'))).toBe('EINVAL')
    expect(errCode(fsLexists('nul\0byte'))).toBe('EINVAL')
    expect(errCode(fsIsDir('nul\0byte'))).toBe('EINVAL')
  })

  it('distinguish an unconfigured root from a boundary refusal', () => {
    // A consumer that cannot tell these apart reports a security refusal for a
    // missing env var. The two codes are what its two messages key off.
    delete process.env[WORKSPACE_ENV]
    expect(errCode(fsResolveWithin('a.txt'))).toBe('EWORKSPACE')
    expect(errCode(fsLexists('a.txt'))).toBe('EWORKSPACE')
    expect(errCode(fsIsDir('a.txt'))).toBe('EWORKSPACE')
  })

  it('mutate nothing — not even the sidecar directories', () => {
    // `fsWrite` creates the preimage dir on its first call; observation must
    // not. A surface that leaves residue is not an observation.
    const before = fs.readdirSync(ws).sort()
    for (const p of ['a.txt', 'sub', 'missing.txt', 'deep/missing.txt',
      'link_in', 'link_out', '..', '/etc/hosts']) {
      fsResolveWithin(p)
      fsLexists(p)
      fsIsDir(p)
    }
    expect(fs.readdirSync(ws).sort()).toEqual(before)
  })
})

describe('the jail is unchanged', () => {
  it('routes observation through the same family-1 guard as every mutation', () => {
    // Not a new door into the filesystem: the same `resolveWithin` the four
    // witnessed ops and every inverse use. Widening the jail to make
    // observation convenient would recreate what item 422 F1 removed.
    expect(PATH_FAMILIES['named-endpoint']).toEqual(['resolveWithin'])
    expect([...READ_HELPERS]).toEqual(['lexistsConfined', 'isDirConfined'])
  })

  it('exposes no write primitive on the observation surface', () => {
    // The absence IS the item 422 F1 finding. A caller that appears to need one
    // needs a witnessed op from the catalog, or a filed finding.
    const surface = { fsResolveWithin, fsLexists, fsIsDir }
    for (const fn of Object.values(surface)) {
      expect(typeof fn).toBe('function')
      expect(fn.length).toBe(1)          // one path in, one Result out
    }
  })
})
