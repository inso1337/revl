// Item 435(c): the code-point decode behind the `Str` helpers is memoised, so
// an index scan over a string materialises each of its code points ONCE.
//
// A `Str` is a sequence of Unicode code points and a JS string is UTF-16, so
// `revlLen` / `revlCharAt` / `revlCharCodeAt` / `revlSlice` all work against
// `Array.from(s)` — the string's code-point decomposition. Building that per
// call made an index scan quadratic: `while (i < s.length()) { s.charAt(i) }`
// calls two helpers per iteration plus one more for the final condition test,
// so it decoded the whole string 2n + 1 times to read n code points, and the
// audit measured 94.8% of the program's CPU self-samples inside the helpers
// rather than inside the user's loop.
//
// This counts the decode by EXECUTING the emitted module with `Array.from`
// intercepted, which is the only observation that distinguishes "memoised"
// from "looks memoised". Nothing here is transcribed and no count is
// hardcoded: every expected number is read off the input string.
//
// Every case builds its own strings with a distinct `tag`, so a case can never
// pass on a cache entry another case left warm, and the file's cases stay
// order-independent.
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { count_digits, checksum, common_prefix } from './generated/str_scan.ts'

const backend = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')

/** Total code points `Array.from` produced while `run` executed. */
function codePointsMaterialised(run: () => void): number {
  const real = Array.from
  let seen = 0
  Array.from = function (...args: Parameters<typeof Array.from>) {
    const out = (real as any).apply(Array, args)
    seen += out.length
    return out
  } as typeof Array.from
  try {
    run()
  } finally {
    Array.from = real
  }
  return seen
}

/** An n-code-point ASCII string whose content is unique to `tag`. */
function ascii(n: number, tag: string): string {
  return (tag + 'ab1cd2ef3gh4'.repeat(Math.ceil(n / 12) + 1)).slice(0, n)
}

describe('435(c): the Str helpers share one memoised code-point decode', () => {
  it('materialises each code point exactly once across a whole scan', () => {
    for (const n of [200, 400, 800, 1600]) {
      const s = ascii(n, `once${n}`)
      const expected = Array.from(s).length // derived from the input, not pinned
      expect(expected).toBe(n)
      // Before 435(c) this was 2n^2 + n: 5,121,600 at n = 1600.
      expect(codePointsMaterialised(() => { count_digits(s) })).toBe(expected)
    }
  })

  it('is linear, not quadratic, as the input doubles', () => {
    const sizes = [200, 400, 800, 1600]
    const counts = sizes.map((n) => {
      const s = ascii(n, `grow${n}`)
      return codePointsMaterialised(() => { count_digits(s) })
    })
    for (let i = 1; i < counts.length; i++) {
      // sizes double, so linear is x2 per step and quadratic is x4
      const growth = counts[i] / counts[i - 1]
      expect(growth, `${counts[i - 1]} -> ${counts[i]} for n=${sizes[i - 1]} -> ${sizes[i]}`)
        .toBeLessThan(2.5)
    }
  })

  it('holds for charCodeAt as well as charAt', () => {
    const s = ascii(800, 'sum')
    expect(codePointsMaterialised(() => { checksum(s) })).toBe(Array.from(s).length)
  })

  it('does not thrash on a pairwise scan over two strings', () => {
    // The reason the memo has two slots: with one, this shape misses on every
    // helper call and falls straight back to the pre-435(c) cost.
    const a = ascii(800, 'pairA')
    const b = ascii(800, 'pairB')
    expect(a).not.toBe(b)
    const expected = Array.from(a).length + Array.from(b).length
    expect(codePointsMaterialised(() => { common_prefix(a, b) })).toBe(expected)
  })

  it('still decodes by CODE POINT, so the astral answers are unchanged', () => {
    // The memo must not have quietly turned these into code-unit operations.
    expect(count_digits('😀1😀2😀3')).toBe(3n)
    // code-unit indexing would answer 3 here: the pair is two units, one point
    expect(common_prefix('😀ab', '😀ax')).toBe(2n)
    const s = '😀4😀5😀6'
    expect(codePointsMaterialised(() => { count_digits(s) }))
      .toBe(Array.from(s).length)
  })

  it('emits exactly one decode site behind the four Str helpers', () => {
    // Structural companion to the executed counts: an edit that puts an
    // `Array.from` back into a helper body is caught here too. The module also
    // carries `revlIndexOf`, which deliberately keeps its own `Array.from`
    // over a per-call substring so it cannot evict a scan's cache entry.
    const emitted = readFileSync(join(backend, 'tests', 'generated', 'str_scan.ts'), 'utf-8')
    const start = emitted.indexOf('function revlCps')
    const end = emitted.indexOf('function revlIndexOf')
    expect(start, 'revlCps is missing from the emitted Str helper').toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    const helper = emitted.slice(start, end)
    expect(helper.match(/Array\.from\(/g) ?? []).toHaveLength(1)
    for (const fn of ['revlLen', 'revlSlice', 'revlCharAt', 'revlCharCodeAt']) {
      expect(helper, `${fn} should reach the decode through revlCps`).toContain(fn)
    }
  })
})
