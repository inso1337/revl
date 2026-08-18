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
