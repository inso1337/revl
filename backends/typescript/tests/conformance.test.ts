// The constructs the TypeScript backend used to refuse inside a component or
// method body (docs/conformance.md "The real gaps"). Each one is exercised
// through the runtime, not just emitted, and the whole module is handed to
// tsc so a lowering that parses but does not type cannot pass.
//
// Source of the fixture: tests/fixtures/conformance.rvl
import { beforeEach, describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { Context } from 'cordis'
import { Guarded, Memory } from './generated/conformance.ts'
import { resetHost } from '../runtime.ts'

const backend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const generated = join(backend, 'tests', 'generated', 'conformance.ts')

beforeEach(() => resetHost())

async function withCalc(config?: { limit?: number }) {
  const ctx = new Context()
  const store = await ctx.plugin(Memory)
  const calc = await ctx.plugin(Guarded, config ?? {})
  return { ctx, dispose: async () => { await calc.dispose(); await store.dispose() } }
}

describe('gap 2 — calling a top-level fn from a component body', () => {
  it('lowers the `fn` call node and calls the emitted function', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.twice(21)).toBe(42)
    await dispose()
  })

  it('calls an extern through the same node shape', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.loud('ada')).toBe('ADA')
    await dispose()
  })

  it('refuses a `fn` node naming nothing the document declares', () => {
    const result = emitIr({
      ir_version: 3,
      services: { S: { methods: { f: { params: [{ name: 'x', type: 'Int' }], returns: 'Int', emission: false } } } },
      components: [
        {
          name: 'C',
          config: [],
          requires: {},
          provides: { s: 'S' },
          body: [
            {
              step: 'provide',
              name: 's',
              service: 'S',
              methods: [
                {
                  name: 'f',
                  params: ['x'],
                  body: [{ step: 'return', expr: { kind: 'fn', name: 'nope', args: [] } }],
                },
              ],
            },
          ],
        },
      ],
      functions: [],
    })
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain("call to unknown function 'nope'")
  })
})

describe('gap 5a — `if` / `fail` steps in a component body', () => {
  it('activates when the guard passes', async () => {
    const { ctx, dispose } = await withCalc({ limit: 10 })
    expect(ctx.calc).toBeDefined()
    await dispose()
  })

  it('refuses activation when the guard fails, and provides nothing', async () => {
    const ctx = new Context()
    const store = await ctx.plugin(Memory)
    const guarded = ctx.plugin(Guarded, { limit: 0 })
    await guarded.await().catch(() => undefined)

    // The body threw before reaching `ctx.provide`, so the key never appeared.
    expect(ctx.get('calc')).toBeUndefined()

    await guarded.dispose().catch(() => undefined)
    await store.dispose()
  })

  it('emits the guard as a real branch rather than unconditional code', () => {
    const source = readFileSync(generated, 'utf-8')
    expect(source).toContain('if ((config.limit < 1)) {')
    expect(source).toContain('throw new Error("limit must be positive")')
  })
})

describe('gap 5b — `??` in a component body', () => {
  it('takes the left operand when the required service returns a value', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.orZero(8)).toBe(4)
    await dispose()
  })

  it('falls back when the required service returns None', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.orZero(0)).toBe(0)
    await dispose()
  })

  it('renders the component-dialect call on the left of the coalesce', () => {
    const source = readFileSync(generated, 'utf-8')
    expect(source).toContain('return (ctx.store.lookup(k) ?? 0)')
  })
})

describe('gap 3 — `match` in a method body', () => {
  it('binds the payload of the matched arm', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.classify(7)).toBe(7)
    await dispose()
  })

  it('takes the payload-free arm', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.classify(-1)).toBe(0)
    await dispose()
  })
})

describe('gap 4 — a bare `return` for a void operation', () => {
  it('returns nothing at all', async () => {
    const { ctx, dispose } = await withCalc()
    expect(ctx.calc.touch(1)).toBeUndefined()
    await dispose()
  })

  it('emits `return` with no value', () => {
    const source = readFileSync(generated, 'utf-8')
    expect(source).toMatch(/touch\(x: number\) \{\n\s+return\n/)
  })
})

describe('the emitted module type-checks', () => {
  it('passes tsc --strict', () => {
    const result = spawnSync(
      'npx',
      [
        'tsc', '--noEmit', '--strict',
        '--target', 'ES2022', '--module', 'ESNext',
        '--moduleResolution', 'bundler', '--allowImportingTsExtensions',
        '--skipLibCheck', '--types', 'node',
        join('tests', 'generated', 'conformance.ts'),
      ],
      { cwd: backend, encoding: 'utf-8' },
    )
    expect(result.stdout + result.stderr).toBe('')
    expect(result.status).toBe(0)
  }, 60_000)
})

function emitIr(ir: unknown) {
  return spawnSync('python3', ['emit.py', '/dev/stdin'], {
    cwd: backend,
    encoding: 'utf-8',
    input: JSON.stringify(ir),
  })
}
