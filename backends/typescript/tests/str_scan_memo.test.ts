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
//
// Since #2313 a string with no surrogate unit is its own code-point view and
// materialises NO array, so the exact-count cases use strings that carry an
// astral scalar: those still need the decode, and must get it exactly once.
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  agree_three, char_at, checksum, code_at, common_prefix, count_digits, count_marks, len_of, slice_of,
} from './generated/str_scan.ts'

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

/** An n-code-point string with an astral scalar every few points, unique to `tag`. */
function astral(n: number, tag: string): string {
  return Array.from(tag + 'ab1\u{1F600}cd2<ef3>'.repeat(Math.ceil(n / 11) + 1)).slice(0, n).join('')
}

describe('435(c): the Str helpers share one memoised code-point decode', () => {
  it('materialises each code point exactly once across a whole scan', () => {
    for (const n of [200, 400, 800, 1600]) {
      const s = astral(n, `once${n}`)
      const expected = Array.from(s).length // derived from the input, not pinned
      expect(expected).toBe(n)
      // Before 435(c) this was 2n^2 + n: 5,121,600 at n = 1600.
      expect(codePointsMaterialised(() => { count_digits(s) })).toBe(expected)
    }
  })

  it('is linear, not quadratic, as the input doubles', () => {
    const sizes = [200, 400, 800, 1600]
    const counts = sizes.map((n) => {
      const s = astral(n, `grow${n}`)
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
    const s = astral(800, 'sum')
    expect(codePointsMaterialised(() => { checksum(s) })).toBe(Array.from(s).length)
  })

  it('does not thrash on a pairwise scan over two strings', () => {
    // The reason the memo has more than one slot: with one, this shape misses
    // on every helper call and falls straight back to the pre-435(c) cost.
    const a = astral(800, 'pairA')
    const b = astral(800, 'pairB')
    expect(a).not.toBe(b)
    const expected = Array.from(a).length + Array.from(b).length
    expect(codePointsMaterialised(() => { common_prefix(a, b) })).toBe(expected)
  })

  it('still decodes by CODE POINT, so the astral answers are unchanged', () => {
    // The memo must not have quietly turned these into code-unit operations.
    expect(count_digits('😀1😀2😀3')).toBe(3n)
    // code-unit indexing would answer 3 here: the pair is two units, one point
    expect(common_prefix('😀ab', '😀ax')).toBe(2n)
    // Long enough (64 UTF-16 units or more) to go through the memo: a shorter
    // string is decoded again per call since #2313, at most 63 units each time.
    const s = '😀4😀5😀6'.repeat(12)
    let digits = 0n
    expect(codePointsMaterialised(() => { digits = count_digits(s) }))
      .toBe(Array.from(s).length)
    expect(digits).toBe(36n)
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

describe('#2321: the lookup does not compare near-equal long strings by content', () => {
  /** Index of the first code point at which `a` and `b` differ. */
  function firstDifference(a: string, b: string): number {
    const x = Array.from(a)
    const y = Array.from(b)
    let i = 0
    while (i < x.length && x[i] === y[i]) i++
    return i
  }

  it('checks a per-slot fingerprint before the content compare', () => {
    // Structural: `===` on two distinct same-length strings runs from the start
    // until they differ, so reaching it for a slot that holds the OTHER
    // near-equal string costs O(common prefix) per lookup. The slot test must
    // reject on the fingerprint first and compare contents only on a match.
    const emitted = readFileSync(join(backend, 'tests', 'generated', 'str_scan.ts'), 'utf-8')
    const start = emitted.indexOf('function revlCps(')
    const end = emitted.indexOf('function revlLen')
    expect(start, 'revlCps is missing from the emitted Str helper').toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    const lookup = emitted.slice(start, end)
    const fingerprint = lookup.indexOf('revlCpsF[k] !== f')
    const content = lookup.indexOf('revlCpsK[k] !== s')
    expect(fingerprint, 'slot test has no fingerprint check').toBeGreaterThan(-1)
    expect(content, 'slot test has no content check').toBeGreaterThan(fingerprint)
    // and the fingerprint must be a bounded read, not a walk of the string
    const fp = emitted.slice(emitted.indexOf('function revlCpsFp('), start)
    expect(fp).not.toMatch(/\b(for|while)\b/)
  })

  it('never aliases two strings that share length and every sampled unit', () => {
    // The fingerprint samples the first, middle and last code unit and the
    // length. These pairs agree on all four and differ one code point in: a
    // fingerprint that decided hits would hand `b` the view of `a`.
    const base = astral(400, 'alias')
    const units = base.length
    const mid = (units - 1) >> 1
    for (const at of [3, 40, 101]) {
      expect(at).not.toBe(0)
      expect(at).not.toBe(mid)
      expect(at).not.toBe(units - 1)
      const a = base.slice(0, at) + 'x' + base.slice(at + 1)
      const b = base.slice(0, at) + 'y' + base.slice(at + 1)
      expect(a.length).toBe(b.length)
      expect(a.length).toBe(units)
      const k = firstDifference(a, b)
      // alternate reads, as the pairwise scan does: each answer is its own
      for (let round = 0; round < 3; round++) {
        expect(char_at(a, BigInt(k))).toBe('x')
        expect(char_at(b, BigInt(k))).toBe('y')
        expect(len_of(a)).toBe(BigInt(Array.from(a).length))
      }
      expect(common_prefix(a, b)).toBe(BigInt(k))
      expect(common_prefix(b, a)).toBe(BigInt(k))
    }
  })

  it('decodes a colliding pair once each', () => {
    const base = astral(400, 'collide')
    const a = base.slice(0, 7) + 'x' + base.slice(8)
    const b = base.slice(0, 7) + 'y' + base.slice(8)
    const expected = Array.from(a).length + Array.from(b).length
    expect(codePointsMaterialised(() => { common_prefix(a, b) })).toBe(expected)
  })

  it('evicts the least recently used string, as before', () => {
    const ss = Array.from({ length: 9 }, (_, i) => astral(300, `lru${i}`))
    const decoded = (s: string) => codePointsMaterialised(() => { len_of(s) })
    const n = Array.from(ss[0]).length
    for (let i = 0; i < 8; i++) expect(decoded(ss[i]), `fill ${i}`).toBe(n)
    // all eight resident: nothing decodes
    for (let i = 0; i < 8; i++) expect(decoded(ss[i]), `hit ${i}`).toBe(0)
    // the ninth evicts the oldest touched, ss[0]
    expect(decoded(ss[8])).toBe(n)
    for (let i = 1; i < 9; i++) expect(decoded(ss[i]), `still resident ${i}`).toBe(0)
    expect(decoded(ss[0]), 'evicted').toBe(n)
  })
})

describe('#2313: a scan stays linear whatever else the loop body reads', () => {
  it('a BMP-only string is its own view and materialises no array', () => {
    const s = ascii(1600, 'flat')
    expect(codePointsMaterialised(() => { count_digits(s); checksum(s) })).toBe(0)
  })

  it('two one-character literals in the body do not evict the scanned string', () => {
    // Before #2313 the two literals filled both memo slots on every pass, so
    // `text` was decoded again each time: n^2-order code points, not n.
    for (const n of [400, 1600]) {
      const text = astral(n, `marks${n}`)
      expect(codePointsMaterialised(() => { count_marks(text) })).toBe(Array.from(text).length)
    }
  })

  it('three long strings read round-robin are each decoded once', () => {
    // Skipping the cache for short strings alone would not keep this linear:
    // all three are long, and two slots hold only two of them.
    const a = astral(1600, 'rrA')
    const b = astral(1600, 'rrB')
    const c = astral(1600, 'rrC')
    const expected = Array.from(a).length + Array.from(b).length + Array.from(c).length
    expect(codePointsMaterialised(() => { agree_three(a, b, c) })).toBe(expected)
  })

  // Deterministic growth check on the issue's reproducer shape, with ASCII
  // text as in the issue. An ASCII string materialises no array at all, so
  // `codePointsMaterialised` above is 0 for it whether the scan is linear or
  // not; what a scan that decodes per index does re-read is the string's
  // UTF-16 units, once per helper call instead of once per scan. Counting the
  // units read therefore measures the same property a wall clock did, with no
  // clock: one scan of a 4x larger input reads 4x the units when the decode is
  // memoised and 16x when it is not. These cases used to be timed, and the
  // clock ratio kept flaking on loaded CI runners (8.3x against an 8x bound,
  // #3096) because a shared box only ever adds time to either side of a ratio;
  // a count has no such noise. These are not duplicates of the count cases
  // above: those pin the decode to one per string, these bound the work of a
  // whole scan whatever shape the loop takes.
  function unitReads(run: () => void): number {
    const realFrom = Array.from
    const realCharCodeAt = String.prototype.charCodeAt
    let seen = 0
    Array.from = function (...args: Parameters<typeof Array.from>) {
      const out = (realFrom as any).apply(Array, args)
      seen += out.length
      return out
    } as typeof Array.from
    String.prototype.charCodeAt = function (this: string, index: number) {
      seen += 1
      return realCharCodeAt.call(this, index)
    } as typeof String.prototype.charCodeAt
    try {
      run()
    } finally {
      Array.from = realFrom
      String.prototype.charCodeAt = realCharCodeAt
    }
    return seen
  }

  function readGrowth(run: (n: number) => () => void, n: number): number {
    return unitReads(run(4 * n)) / unitReads(run(n))
  }

  it('count_marks reads a linear number of string units for a 4x larger input', () => {
    const ratio = readGrowth((n) => {
      const text = '<p>hello</p> '.repeat(Math.ceil(n / 13)).slice(0, n)
      return () => { count_marks(text) }
    }, 2048)
    // linear is 4x, a decode per index is 16x, so the midpoint has no noise
    expect(ratio, `4x input read ${ratio.toFixed(2)}x the units`).toBeLessThan(8)
  })

  it('a round-robin scan of three long strings reads a linear number of string units for a 4x larger input', () => {
    const ratio = readGrowth((n) => {
      const a = ascii(n, `tA${n}`), b = ascii(n, `tB${n}`), c = ascii(n, `tC${n}`)
      return () => { agree_three(a, b, c) }
    }, 2048)
    expect(ratio, `4x input read ${ratio.toFixed(2)}x the units`).toBeLessThan(8)
  })

  it('answers in code points on astral strings and lone surrogates, cached or not', () => {
    const seeds = [
      'plain ascii',
      'café 中文',
      'a\u{1F600}b\u{10000}c',
      'lone high \uD800 here',
      'lone low \uDC00 here',
      'reversed \uDC00\uD800 pair',
      '\uD83D',
      '',
    ]
    // each seed short (decided per call) and long (through the cache)
    const inputs = seeds.flatMap((seed) => [seed, seed.repeat(Math.ceil(80 / Math.max(seed.length, 1)))])
    for (const s of inputs) {
      const cps = Array.from(s)
      const n = cps.length
      expect(len_of(s), JSON.stringify(s)).toBe(BigInt(n))
      for (let i = 0; i < n; i++) {
        expect(char_at(s, BigInt(i))).toBe(cps[i])
        expect(code_at(s, BigInt(i))).toBe(BigInt(cps[i].codePointAt(0) as number))
      }
      for (const i of [-1, n, n + 1]) {
        expect(() => char_at(s, BigInt(i)), `charAt(${i})`).toThrow(RangeError)
        expect(() => code_at(s, BigInt(i)), `charCodeAt(${i})`).toThrow(RangeError)
      }
      for (const [a, b] of [[0, n], [1, 3], [2, n - 1], [-3, n], [0, -2], [3, 1], [n, n + 5]]) {
        expect(slice_of(s, BigInt(a), BigInt(b)), `slice(${a}, ${b}) of ${JSON.stringify(s)}`)
          .toBe(cps.slice(a, b).join(''))
      }
    }
  })
})
