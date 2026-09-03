// The `vitest` module, as an emitted revl module is allowed to use it — and
// nothing more (issue #295).
//
// `emit.py` reaches for exactly two names out of vitest, at exactly two call
// sites:
//
//     import { expect, it } from 'vitest'          (emit.py, `import` line)
//     it(<name>, () => { ... })                    (sync and async forms)
//     expect(<value>).toBeTruthy()                 (a bare `assert <expr>`)
//     expect(revlEq(l, r), <message>).toBe(true|false)   (an `==` / `!=` assert)
//
// `scripts/node-tier-runner.mjs` resolves the specifier `vitest` to THIS file
// so the generated test module can be executed by plain `node` — the runtime
// `revl run --backend ts` and `revl run --placement` actually ship on. The
// point is not to reimplement vitest; it is that the module under test sees a
// PLAIN NODE module scope (no `require`, no `__dirname`, no vite resolution,
// node's own strip-only TypeScript) instead of vitest's.
//
// The surface is deliberately the emitted surface and no larger. A matcher
// this file does not implement throws by name rather than silently passing:
// if `emit.py` starts emitting `expect(x).toBe(y)`, the ts tier goes red here
// with "matcher `toBe` is not implemented" and someone extends this file —
// which is the opposite of the failure this shim exists to close.

function show(value) {
  if (typeof value === 'bigint') return `${value}n`
  if (typeof value === 'string') return JSON.stringify(value)
  try {
    return String(value)
  } catch {
    return '<unprintable>'
  }
}

/** Collected `it()` cases. The runner creates the sink before it imports the
 *  module under test, so this only ever reads it. */
function sink() {
  const s = globalThis.__REVL_NODE_TIER__
  if (!s) {
    throw new Error(
      'revl node-tier shim: no test sink — this module is only importable ' +
      'through backends/typescript/scripts/node-tier-runner.mjs',
    )
  }
  return s
}

export function it(name, fn) {
  sink().cases.push({ name: String(name), fn })
}

export const test = it

export function expect(value, message) {
  const matchers = {
    toBeTruthy() {
      if (!value) {
        throw new Error(
          message != null ? String(message) : `expected ${show(value)} to be truthy`,
        )
      }
    },
    // vitest's `toBe` is Object.is. The emitter only ever compares the boolean
    // `revlEq(...)` produced, so Object.is is exactly right here; revl's own
    // structural equality is `revlEq`, which the emitted module carries.
    toBe(expected) {
      if (!Object.is(value, expected)) {
        const detail = `expected ${show(value)} to be ${show(expected)}`
        throw new Error(message != null ? `${message}\n  ${detail}` : detail)
      }
    },
  }
  return new Proxy(matchers, {
    get(target, prop) {
      if (prop in target) return target[prop]
      if (typeof prop === 'symbol') return undefined
      return () => {
        throw new Error(
          `revl node-tier shim: matcher \`${String(prop)}\` is not implemented. ` +
          'backends/typescript/emit.py emits a matcher this shim does not know; ' +
          'add it to backends/typescript/scripts/vitest-shim.mjs (issue #295).',
        )
      }
    },
  })
}
