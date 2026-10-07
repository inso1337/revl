// Emitter tests: golden regeneration + contract acceptance.
import { describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const backend = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const repoIr = resolve(backend, '..', '..', 'examples', 'user_cache.ir.json')
const fixtureIr = join(backend, 'tests', 'fixtures', 'user_cache.ir.json')

function runEmit(irPath: string, extraArgs: string[] = []) {
  return spawnSync('python3', ['emit.py', ...extraArgs, irPath], {
    cwd: backend,
    encoding: 'utf-8',
  })
}

describe('emitter (docs/backend-ir.md §Acceptance item 1)', () => {
  it('accepts the reference IR verbatim and matches the checked-in golden file', () => {
    const irPath = existsSync(repoIr) ? repoIr : fixtureIr
    const result = runEmit(irPath)
    expect(result.status, result.stderr).toBe(0)
    const golden = readFileSync(join(backend, 'golden', 'user_cache.ts'), 'utf-8')
    expect(result.stdout).toBe(golden)
  })

  it('keeps the vendored fixture byte-identical to the repo reference IR', () => {
    if (!existsSync(repoIr)) return // backend used standalone; fixture is the reference
    expect(readFileSync(fixtureIr, 'utf-8')).toBe(readFileSync(repoIr, 'utf-8'))
  })

  it('is deterministic', () => {
    const a = runEmit(fixtureIr)
    const b = runEmit(fixtureIr)
    expect(a.stdout).toBe(b.stdout)
  })

  it('rejects references to undeclared requirements (G1 analogue)', () => {
    const dir = mkdtempSync(join(tmpdir(), 'revl-emit-'))
    const bad = {
      ir_version: 1,
      services: { Database: { methods: { query: { params: [{ name: 'sql', type: 'Str' }], returns: null, emission: false } } } },
      components: [
        {
          name: 'Bad',
          config: [],
          requires: {},
          provides: {},
          body: [
            {
              step: 'emit',
              expr: {
                kind: 'call',
                target: { kind: 'req', name: 'db' },
                method: 'query',
                args: [{ kind: 'lit', value: 'x' }],
              },
            },
          ],
        },
      ],
    }
    const path = join(dir, 'bad.json')
    writeFileSync(path, JSON.stringify(bad))
    const result = runEmit(path)
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('undeclared requirement')
  })

  it('rejects unsupported ir_version', () => {
    const dir = mkdtempSync(join(tmpdir(), 'revl-emit-'))
    const path = join(dir, 'v0.json')
    writeFileSync(path, JSON.stringify({ ir_version: 0, services: {}, components: [] }))
    const result = runEmit(path)
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('ir_version')
  })

  it('emits IR v3 test blocks as runnable vitest its', () => {
    const vitest = join(backend, 'node_modules', '.bin', 'vitest')
    const result = spawnSync(vitest, ['run', 'tests/generated/v3_tests.test.ts'], {
      cwd: backend,
      encoding: 'utf-8',
      env: { ...process.env, CI: '1' },
    })
    expect(result.status, result.stderr).toBe(0)
    // NB: which stream carries the per-file line is reporter-dependent;
    // the pass-count is the stable signal. Derive the expected count from the
    // generated file rather than hard-coding it: a literal count silently
    // becomes wrong the moment anyone adds a `test` block to the fixture,
    // which is exactly how this broke. What the assertion is really for is
    // that the run was not VACUOUS, so it must still pin a number.
    const emitted = readFileSync(join(backend, 'tests/generated/v3_tests.test.ts'), 'utf-8')
    const expected = (emitted.match(/^\s*it\(/gm) || []).length
    expect(expected).toBeGreaterThan(0)
    expect(result.stdout + result.stderr).toContain(`${expected} passed`)
  })

  // A user-chosen name that collides with emitter scaffolding is RENAMED, not
  // refused (erratum A3, docs/contract-errata.md; issue #2132). The name is
  // internal to the emitted component body, so escaping it costs nothing and
  // cannot change what the program publishes. This cell used to assert the
  // refusal; it now asserts the rename AND that the declaration and its use
  // agree without a symbol table, which is the property the rename has to have.
  it('renames binding names that would shadow emitter scaffolding', () => {
    const dir = mkdtempSync(join(tmpdir(), 'revl-emit-'))
    const shadowing = {
      ir_version: 1,
      services: {},
      components: [
        {
          name: 'Shadow',
          config: [],
          requires: {},
          provides: {},
          body: [
            {
              step: 'let-effect',
              bind: 'ctx',
              acquire: { kind: 'host', fn: 'Map.new', args: [] },
              undo: {
                kind: 'call',
                target: { kind: 'name', id: 'ctx' },
                method: 'drop',
                args: [],
              },
            },
          ],
        },
      ],
    }
    const path = join(dir, 'shadow.json')
    writeFileSync(path, JSON.stringify(shadowing))
    const result = runEmit(path)
    expect(result.status, result.stderr).toBe(0)
    // the binding is escaped by exactly one `_` ...
    expect(result.stdout).toContain('const ctx_ = host.Map.new()')
    // ... the use rides the same ladder, so it reaches the renamed binding ...
    expect(result.stdout).toContain('() => ctx_.drop()')
    expect(result.stdout).not.toContain('const ctx = ')
    // ... and the frame journal key keeps the AUTHOR's spelling: it is the
    // emitter's own bookkeeping, not a published wire key.
    expect(result.stdout).toContain('key: "ctx"')
  })

  // The reservations that are NOT renamable stay loud: a service name is the
  // portability contract's own name, so a collision there is refused rather
  // than silently escaped.
  it('rejects service names that would shadow emitter scaffolding', () => {
    const dir = mkdtempSync(join(tmpdir(), 'revl-emit-'))
    const path = join(dir, 'service.json')
    writeFileSync(path, JSON.stringify({
      ir_version: 1,
      services: { ctx: { methods: {} } },
      components: [],
    }))
    const result = runEmit(path)
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('scaffolding')
  })

  // A3 (issue #2132) renames a scaffolding name ONLY where the spelling is
  // internal to the emitted module. Every other role still refuses, and a
  // later reader who widens the renamable set has to change these cells: each
  // one is an end-to-end program, not a unit call, so it pins the refusal the
  // emitter actually produces. `config field` is here for its own reason --
  // its key is written by two helpers, so relaxing one would emit an interface
  // key the `load … with {…}` literal never supplies.
  it.each([
    ['component', { ir_version: 1, services: {}, components: [{ name: 'ctx', config: [], requires: {}, provides: {}, body: [] }] }],
    ['type name', { ir_version: 3, types: { host: { kind: 'record', fields: { a: 'Str' } } } }],
    ['config field', { ir_version: 1, services: {}, components: [{ name: 'C', config: [{ name: 'host', type: 'Str', default: null }], requires: {}, provides: {}, body: [] }] }],
  ])('still refuses a scaffolding name at the %s position', (role, ir) => {
    const dir = mkdtempSync(join(tmpdir(), 'revl-emit-'))
    const path = join(dir, 'refused.json')
    writeFileSync(path, JSON.stringify(ir))
    const result = runEmit(path)
    expect(result.status, `${role} was emitted: ${result.stdout}`).not.toBe(0)
    expect(result.stderr).toContain('scaffolding')
  })
})
