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

  it('rejects binding names that would shadow emitter scaffolding', () => {
    const dir = mkdtempSync(join(tmpdir(), 'revl-emit-'))
    const bad = {
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
    writeFileSync(path, JSON.stringify(bad))
    const result = runEmit(path)
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('scaffolding')
  })
})
