// Pins the "vanishing coverage" hazard (docs/v2.0-roadmap.md §1e follow-ups).
//
// The `test` blocks a .rvl file declares are lowered by emit.py into a real
// vitest module (tests/generated/v3_tests.test.ts). Two things can make those
// tests disappear *without turning CI red*:
//
//   1. the generated module is not checked in, so a cold checkout has nothing
//      to collect (this is what made a cold clone collect two fewer tests
//      than a warm one), and
//   2. the emitter stops lowering `test` blocks, in which case the global
//      setup happily overwrites the checked-in module with a smaller one and
//      the suite still reports green — just with less in it.
//
// Both are silent by construction: a test that is never collected cannot
// fail. These assertions are the gate.
import { describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const backend = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const generated = join(backend, 'tests', 'generated', 'v3_tests.test.ts')
const fixture = join(backend, 'tests', 'fixtures', 'v3_tests.ir.json')

describe('generated test-block coverage', () => {
  it('is checked in, so a cold checkout collects the same tests as a warm one', () => {
    // Scrub inherited GIT_* vars and use a repo-relative pathspec: a git hook
    // exports GIT_DIR (and in a linked worktree it points at
    // .git/worktrees/<name>), which makes an absolute pathspec resolve
    // against the wrong top-level and this gate misfire.
    const env = { ...process.env }
    for (const key of Object.keys(env)) if (key.startsWith('GIT_')) delete env[key]
    const relative = 'tests/generated/v3_tests.test.ts'
    const inRepo = spawnSync('git', ['ls-files', '--error-unmatch', '--', relative], {
      cwd: backend,
      encoding: 'utf-8',
      env,
    })
    if (inRepo.error) return // no git available (source tarball) — nothing to assert
    expect(
      inRepo.status,
      `backends/typescript/${relative} is not tracked by git: a cold clone ` +
        `would collect fewer tests than this working copy, and nothing would ` +
        `go red.\n${inRepo.stderr}`,
    ).toBe(0)
  })

  it('lowers every `test` block the fixture declares', () => {
    const ir = JSON.parse(readFileSync(fixture, 'utf-8'))
    const declared: Array<{ name: string }> = ir.tests ?? []
    expect(declared.length, 'fixture declares no test blocks — the gate would be vacuous')
      .toBeGreaterThan(0)

    const source = readFileSync(generated, 'utf-8')
    const emitted = source.match(/^it\(/gm) ?? []
    expect(
      emitted.length,
      `emit.py lowered ${emitted.length} of ${declared.length} declared ` +
        `\`test\` blocks into ${generated}`,
    ).toBe(declared.length)

    // and each one by name, so a rename cannot quietly drop a case
    for (const t of declared) {
      expect(source, `declared test "${t.name}" is missing from the emitted module`)
        .toContain(JSON.stringify(t.name))
    }
  })

  it('emits a filename vitest actually collects', () => {
    // `include: ['tests/**/*.test.ts']` — an output named v3_tests.ts rather
    // than v3_tests.test.ts would be emitted, committed, and never run.
    expect(generated.endsWith('.test.ts')).toBe(true)
  })
})

// The TS twin of go's test_checked_in_generated_is_current (findings-map.md):
// a checked-in generated module can drift from its fixture + emitter —
// someone edits the emitter or a fixture, regenerates some outputs but not
// others, and CI stays green while the committed module no longer describes
// what this tree emits. Comparing against the WORKING TREE is useless here:
// vitest's globalSetup has already re-written every generated module from
// the fixtures by the time tests run (this file's own header documents that
// hazard). So the pin compares each fresh emit against the module AS
// COMMITTED (git HEAD) — a stale golden can only go green again once the
// regenerated outputs are actually committed.
describe('checked-in generated modules are current', () => {
  const script = join(backend, 'scripts', 'emit-fixtures.ts')
  const pairs = (readFileSync(script, 'utf-8').match(
    /emitFixture\('([^']+)',\s*'([^']+)'\)/g) ?? [])
    .map((call) => {
      const m = call.match(/emitFixture\('([^']+)',\s*'([^']+)'\)/)!
      return { fixture: m[1], output: m[2] }
    })

  // Scrub inherited GIT_* vars and resolve paths repo-relative: a git hook
  // exports GIT_DIR (and in a linked worktree it points at
  // .git/worktrees/<name>), which misdirects git calls made from here.
  function gitShow(repoPath: string): string | null {
    const env = { ...process.env }
    for (const key of Object.keys(env)) if (key.startsWith('GIT_')) delete env[key]
    const root = spawnSync('git', ['rev-parse', '--show-toplevel'], {
      cwd: backend, encoding: 'utf-8', env,
    })
    if (root.status !== 0) return null // no git available — nothing to assert
    const shown = spawnSync('git', ['show', `HEAD:${repoPath}`], {
      cwd: root.stdout.trim(), encoding: 'utf-8', env,
      maxBuffer: 16 * 1024 * 1024,
    })
    return shown.status === 0 ? shown.stdout : null
  }

  it('found fixture/output pairs to pin (a vacuous gate protects nothing)', () => {
    expect(pairs.length).toBeGreaterThan(0)
  })

  for (const { fixture, output } of pairs) {
    it(`is committed current with its fixture: ${output}`, () => {
      // Same recipe scripts/emit-fixtures.ts uses at setup time.
      const fresh = spawnSync(
        'python3',
        ['emit.py', '--runtime', '../../runtime.ts', join('tests', 'fixtures', fixture)],
        { cwd: backend, encoding: 'utf-8', maxBuffer: 16 * 1024 * 1024 },
      )
      if (fresh.error) return // no python3 available (source tarball) — nothing to assert
      expect(fresh.status, `emit.py failed for ${fixture}:\n${fresh.stderr}`).toBe(0)
      const committed = gitShow(`backends/typescript/tests/generated/${output}`)
      if (committed === null) {
        throw new Error(
          `tests/generated/${output} is not committed — a cold clone would ` +
            `run whatever its own setup emits instead of this tree's proof`)
      }
      expect(
        fresh.stdout,
        `tests/generated/${output} does not match a fresh emit of ` +
          `tests/fixtures/${fixture} + emit.py — regenerate ` +
          `(backends/typescript/scripts/emit-fixtures.ts) and commit`,
      ).toBe(committed)
    })
  }
})
