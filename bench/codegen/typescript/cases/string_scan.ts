// CASE: a character scan over a string.
//
// Emitter sites: backends/typescript/emit.py `_REVL_STR_HELPER` (the
// `Array.from(...)` bodies of `revlLen` / `revlCharAt` / `revlCharCodeAt` /
// `revlSlice`) and the `while` lowering in `_v3_stmt`, which re-evaluates the
// loop condition (hence `revlLen(s)`) on every iteration.
//
// Two hand variants so the two costs separate:
//   handMemo   - helper-level only: one memoised code-point array. No change
//                to the loop shape, so an emitter fix here touches ONLY the
//                helper text, not any lowering.
//   handHoist  - what a competent developer actually writes: decode once,
//                index an array, hoist the length.

import { count_digits as emittedCountDigits, checksum as emittedChecksum }
  from '../emitted/string_scan.ts'

const REVL_I64_MIN = -(2n ** 63n)
const REVL_I64_MAX = 2n ** 63n - 1n
function revlI64(v: bigint): bigint {
  if (v < REVL_I64_MIN || v > REVL_I64_MAX) throw new RangeError('revl: Int overflow')
  return v
}
function revlMod(a: bigint, b: bigint): bigint {
  const r = a % b
  return r !== 0n && (r < 0n) !== (b < 0n) ? r + b : r
}

// --- variant A: memoised helpers, loop shape untouched ---------------------
let _memoStr: string | null = null
let _memoCps: string[] = []
function cps (s: string): string[] {
  if (s !== _memoStr) { _memoCps = Array.from(s); _memoStr = s }
  return _memoCps
}
function memoLen (x: string | ArrayLike<unknown>): bigint {
  return BigInt(typeof x === "string" ? cps(x).length : x.length)
}
function memoCharAt (s: string, i: bigint): string {
  const c = cps(s)[Number(i)]
  return c === undefined ? "" : c
}
function memoCharCodeAt (s: string, i: bigint): bigint {
  const c = cps(s)[Number(i)]
  return BigInt(c === undefined ? NaN : (c.codePointAt(0) as number))
}

export function memoCountDigits(s: string): bigint {
  let n = 0n
  let i = 0n
  while ((i < memoLen(s))) {
    if (((_rc: string) => "0" <= _rc && _rc <= "9")((memoCharAt(s, i)))) {
      n = revlI64(n + 1n)
    }
    i = revlI64(i + 1n)
  }
  return n
}
export function memoChecksum(s: string): bigint {
  let acc = 0n
  let i = 0n
  while ((i < memoLen(s))) {
    acc = revlMod((revlI64(acc + memoCharCodeAt(s, i))), 65536n)
    i = revlI64(i + 1n)
  }
  return acc
}

// --- variant B: what a developer writes ------------------------------------
export function handCountDigits(s: string): bigint {
  const xs = Array.from(s)
  let n = 0n
  for (let k = 0; k < xs.length; k++) {
    const c = xs[k]
    if ("0" <= c && c <= "9") n = revlI64(n + 1n)
  }
  return n
}
export function handChecksum(s: string): bigint {
  const xs = Array.from(s)
  let acc = 0n
  for (let k = 0; k < xs.length; k++) {
    acc = revlMod((revlI64(acc + BigInt(xs[k].codePointAt(0) as number))), 65536n)
  }
  return acc
}

export const name = 'string-scan-helpers'
export const summary =
  'revlLen/revlCharAt/revlCharCodeAt each rebuild the whole code-point array, ' +
  'and the while lowering re-evaluates revlLen every iteration, so an index ' +
  'scan is quadratic in time and allocation'

export const provenance = [
  { file: 'string_scan.ts', snippet: 'return BigInt(typeof x === "string" ? Array.from(x).length : x.length)' },
  { file: 'string_scan.ts', snippet: 'const c = Array.from(s)[Number(i)]' },
  { file: 'string_scan.ts', snippet: 'while ((i < revlLen(s)))' },
]

const S = ('ab1cd2ef3gh4' as string).repeat(80)   // 960 ASCII code points

export const emitted = async () => { emittedCountDigits(S); emittedChecksum(S) }
export const handMemo = async () => { memoCountDigits(S); memoChecksum(S) }
export const hand = async () => { handCountDigits(S); handChecksum(S) }

export function check () {
  const a = [emittedCountDigits(S), emittedChecksum(S)].join(',')
  const b = [memoCountDigits(S), memoChecksum(S)].join(',')
  const c = [handCountDigits(S), handChecksum(S)].join(',')
  if (a !== b || a !== c) throw new Error(`string_scan disagreement: ${a} | ${b} | ${c}`)
  return a
}

// complexity probe: same code, four input lengths
export const sizes = [200, 400, 800, 1600]
export function probe (fn: (s: string) => bigint, n: number) {
  const s = 'ab1cd2ef3gh4'.repeat(Math.ceil(n / 12)).slice(0, n)
  return () => { fn(s) }
}
export const emittedProbeFn = emittedCountDigits
export const memoProbeFn = memoCountDigits
export const handProbeFn = handCountDigits

export const emittedHot = async () => { emittedCountDigits(S); emittedChecksum(S) }
export const handMemoHot = async () => { memoCountDigits(S); memoChecksum(S) }
export const handHot = async () => { handCountDigits(S); handChecksum(S) }
