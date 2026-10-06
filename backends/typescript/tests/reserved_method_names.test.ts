// issue #1512: a provided method named after a TypeScript keyword is provided
// and called on the ts tier.
//
// The emitter renamed the method at its definition (`delete` -> `delete_`) and
// then looked the renamed spelling up in the service, so it refused 25 keywords
// ("method 'delete_' is not declared by service 'S'"); names on the append-`_`
// ladder (`delete_` -> `delete__`) the same way. The fix is one function,
// `_method_ident`, at the interface, the definition and every call site: the
// contract name, verbatim, because a keyword is a legal property name.
//
// The fixture (tests/fixtures/_gen_reserved_method_names.py) puts every
// TypeScript keyword the revl frontend admits, plus four ladder names, on ONE
// service: `P` provides each as `x + 1`, and `C` calls each through a required
// service as `s.<name>(x) * 10`. Each is called here by its contract name and
// through the consumer. `scripts/typecheck-generated.mjs` typechecks the module.
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { C, P } from './generated/reserved_method_names.ts'

const require = createRequire(import.meta.url)
const fixture = JSON.parse(
  readFileSync(new URL('./fixtures/reserved_method_names.ir.json', import.meta.url), 'utf-8'),
)
const NAMES: string[] = Object.keys(fixture.services.S.methods)

// TypeScript keywords that are revl keywords too: the frontend refuses them as
// identifiers, so no emitter ever sees one. Pinned in the generator as well.
const FRONTEND_KEYWORDS = new Set([
  'break', 'continue', 'else', 'false', 'for', 'if', 'in', 'null', 'return',
  'true', 'try', 'var', 'while', 'with', 'let', 'as', 'assert', 'async',
  'await', 'type', 'of',
])

function typescriptKeywords(): string[] {
  const ts = require('typescript')
  const out: string[] = []
  for (let kind = ts.SyntaxKind.FirstKeyword; kind <= ts.SyntaxKind.LastKeyword; kind++) {
    out.push(ts.tokenToString(kind))
  }
  return out
}

async function boot(): Promise<Context> {
  const ctx = new Context()
  const provider = ctx.plugin(P)
  await provider.await()
  const consumer = ctx.plugin(C)
  await consumer.await()
  expect(provider.state).toBe(FiberState.ACTIVE)
  expect(consumer.state).toBe(FiberState.ACTIVE)
  return ctx
}

describe('the fixture covers every TypeScript keyword', () => {
  it('each keyword is a method here or a revl keyword the frontend refuses', () => {
    const missing = typescriptKeywords().filter(
      (kw) => !NAMES.includes(kw) && !FRONTEND_KEYWORDS.has(kw),
    )
    expect(missing).toEqual([])
    // a sample of the 25 the emitter used to refuse
    for (const kw of ['case', 'class', 'default', 'delete', 'new', 'this', 'yield']) {
      expect(NAMES).toContain(kw)
    }
  })
})

describe('a keyword-named method is provided and called', () => {
  for (const [index, name] of NAMES.entries()) {
    it(`${name}: by its contract name, and through a required service`, async () => {
      const ctx = await boot()
      expect((ctx as any).s[name](41n)).toBe(42n)
      expect((ctx as any).ops[`run${index}`](41n)).toBe(420n)
    })
  }
})
