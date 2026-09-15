// The ts half of item 459 F6 (issue #722): `fsReadPinned`, the entry point
// `stdlib/asset.rvl`'s private `load_pinned` reaches through its `= @ts ref`.
//
// WHY IT EXISTS. `asset "<path>"` is a COMPILE-time act — the compiler resolves
// the path, jails the realpath to the root compile tree and pins the file's
// sha256 into the handle `{ path, sha256 }`, and the BYTES stay on disk, which
// is the whole point of item 459. `load(handle)` is the door that turns such a
// handle back into its text while a program runs, which is what a component
// rendering its own external template needs.
//
// WHAT THIS SUITE PINS. Two properties, both of which a runtime read is the
// natural place to lose:
//
//   1. THE PIN IS ENFORCED, not dropped. A file edited after the build hashes
//      differently and the read refuses, returning NOTHING — not the bytes, not
//      their length, not a prefix. A digest that agrees on a prefix refuses too.
//      An expected digest that is not 64 lowercase hex characters is refused
//      before the open, so "no pin" is not spellable.
//   2. THE JAIL IS THE ONE THAT ALREADY EXISTS. The path decision is
//      `resolveWithin`, the same family-1 guard every witnessed mutation and
//      every inverse routes through, so a path this refuses is a path no op on
//      this surface would touch. `..` is never refused as TEXT; containment of
//      the resolved realpath is the jail, which is why a symlink whose target
//      holds exactly the pinned bytes is still refused.
//
// The cross-TIER claim — py and ts answer one corpus identically, through the
// real py guard and this entry point over the same workspace — is
// tests/test_asset_runtime_load_459.py. The single-choke-point scan that proves
// `readPinnedConfined` is a LISTED read helper is ./fs_confinement_families.test.ts.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createHash } from 'node:crypto'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { READ_HELPERS, WORKSPACE_ENV, fsReadPinned } from '../revl_fs_ts.ts'

const TEMPLATE = '<h1>{{html:title}}</h1>\n'
const SHA = createHash('sha256').update(TEMPLATE, 'utf8').digest('hex')

let base: string
let ws: string
let outside: string
let saved: string | undefined

beforeEach(() => {
  base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'revl-asset-')))
  ws = path.join(base, 'ws')
  outside = path.join(base, 'outside')
  fs.mkdirSync(path.join(ws, 'frontend'), { recursive: true })
  fs.mkdirSync(outside)
  fs.writeFileSync(path.join(ws, 'frontend', 'page.tpl'), TEMPLATE)
  fs.writeFileSync(path.join(outside, 'elsewhere.tpl'), TEMPLATE)
  fs.symlinkSync(path.join(outside, 'elsewhere.tpl'), path.join(ws, 'link_out.tpl'))
  saved = process.env[WORKSPACE_ENV]
  process.env[WORKSPACE_ENV] = ws
})

afterEach(() => {
  if (saved === undefined) delete process.env[WORKSPACE_ENV]
  else process.env[WORKSPACE_ENV] = saved
  fs.rmSync(base, { recursive: true, force: true })
})

function errCode(r: { kind: string; value: unknown }): string {
  expect(r.kind, `expected Err, got ${JSON.stringify(r)}`).toBe('Err')
  return (r.value as { code: string }).code
}

describe('the pinned read', () => {
  it('returns the text when the bytes still hash to the pin', () => {
    const r = fsReadPinned('frontend/page.tpl', SHA)
    expect(r.kind).toBe('Ok')
    expect(r.value).toBe(TEMPLATE)
  })

  it('reads through `..` that stays inside the root', () => {
    // `..` is never refused as text: containment of the RESOLVED path is the
    // jail, so a path that walks out and back in is the same handle.
    const r = fsReadPinned('frontend/../frontend/page.tpl', SHA)
    expect(r.kind).toBe('Ok')
    expect(r.value).toBe(TEMPLATE)
  })

  it('refuses a file edited after the build, and returns none of it', () => {
    const secret = '<h1>REPLACED AFTER THE BUILD</h1>\n'
    fs.writeFileSync(path.join(ws, 'frontend', 'page.tpl'), secret)
    const r = fsReadPinned('frontend/page.tpl', SHA)
    expect(errCode(r)).toBe('EDIGEST')
    const text = JSON.stringify(r.value)
    expect(text).not.toContain('REPLACED')
    expect(text).not.toContain(secret.trim())
  })

  it('refuses a digest that agrees only on a prefix', () => {
    const near = SHA.slice(0, 32) + (SHA[32] === '0' ? '1' : '0').repeat(32)
    expect(errCode(fsReadPinned('frontend/page.tpl', near))).toBe('EDIGEST')
  })

  it('refuses an expected digest that is not a sha256', () => {
    for (const bogus of ['', 'deadbeef', SHA.toUpperCase(), SHA.slice(0, 63),
      `${SHA}0`]) {
      expect(errCode(fsReadPinned('frontend/page.tpl', bogus)),
        `digest ${JSON.stringify(bogus)}`).toBe('EINVAL')
    }
  })
})

describe('the jail', () => {
  it('refuses a symlink out of the root even when its bytes match the pin', () => {
    // The load-bearing one. The target holds EXACTLY the pinned bytes, so the
    // digest check would pass; containment refuses first. A jail that ran after
    // the pin would be a read primitive for any file whose content is
    // predictable.
    expect(errCode(fsReadPinned('link_out.tpl', SHA))).toBe('EOUTSIDE')
  })

  it('refuses a `..` that leaves the root', () => {
    expect(errCode(fsReadPinned('../outside/elsewhere.tpl', SHA))).toBe('EOUTSIDE')
  })

  it('refuses an absolute path outside the root', () => {
    expect(errCode(fsReadPinned(path.join(outside, 'elsewhere.tpl'), SHA)))
      .toBe('EOUTSIDE')
  })

  it('refuses a sibling whose name merely shares the root as a prefix', () => {
    const evil = `${ws}-evil`
    fs.mkdirSync(evil)
    fs.writeFileSync(path.join(evil, 'page.tpl'), TEMPLATE)
    expect(errCode(fsReadPinned(path.join(evil, 'page.tpl'), SHA))).toBe('EOUTSIDE')
  })

  it('refuses a directory', () => {
    expect(errCode(fsReadPinned('frontend', SHA))).toBe('ENOTFILE')
  })

  it('refuses when no root is configured, rather than using the cwd', () => {
    delete process.env[WORKSPACE_ENV]
    expect(errCode(fsReadPinned('frontend/page.tpl', SHA))).toBe('EWORKSPACE')
  })

  it('answers a missing file with ENOENT and nothing about what is outside', () => {
    expect(errCode(fsReadPinned('frontend/missing.tpl', SHA))).toBe('ENOENT')
  })
})

describe('the surface', () => {
  it('lists the read helper, so the family scan covers it', () => {
    // Widening `READ_HELPERS` is a deliberate edit: a read helper is a new way
    // to LOOK at the filesystem through the jail, and `readPinnedConfined` is
    // the only one that yields file CONTENT.
    expect(READ_HELPERS).toContain('readPinnedConfined')
  })

  it('publishes no unpinned read', () => {
    // There is no `fsRead(path)` on this module and there must not be: the
    // digest is what keeps the door narrower than `resolveWithin` plus the
    // consumer's own `readFileSync`.
    const mod = globalThis as Record<string, unknown>
    expect(mod.fsRead).toBeUndefined()
    expect(fsReadPinned.length).toBe(2)
  })
})
