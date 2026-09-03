// CASE: building a list in a loop.
//
// Emitter site: backends/typescript/emit.py, `_ts_inplace_write` /
// `_ts_inplace_stmt` in `_v3_stmt`, on the frontend's `unique` marker
// (src/revl/ownership.py, roadmap item 445).
//
// revl's `push` has value semantics, so the builtin table's `[...xs, e]` is the
// honest default and stays the default. But in the overwhelmingly common
// `xs = xs.push(e)` the receiver is dead the instant the assignment lands, so
// the copy is never observed — and item 445 proves exactly that, in the
// frontend, per write. Where the proof holds the marker rides the IR and this
// tier emits `xs.push(e)`, which is what a competent TS developer writes. Over
// a loop of n pushes that is the difference between n(n-1)/2 element copies and
// none. Where it does not hold the marker is absent and the spread stays.
//
// Before item 445 was lowered here the emitted arm read `xs = [...xs, i]` and
// copied 1,225 / 4,950 / 19,900 / 79,800 elements at n = 50 / 100 / 200 / 400.

import { build as emittedBuild } from '../emitted/list_build.ts'

const REVL_I64_MIN = -(2n ** 63n)
const REVL_I64_MAX = 2n ** 63n - 1n
function revlI64 (v: bigint): bigint {
  if (v < REVL_I64_MIN || v > REVL_I64_MAX) throw new RangeError('revl: Int overflow')
  return v
}

export function handBuild (n: bigint): bigint[] {
  const xs: bigint[] = []
  let i = 0n
  while ((i < n)) {
    xs.push(i)
    i = revlI64(i + 1n)
  }
  return xs
}

export const name = 'list-build-spread'
export const summary =
  '`xs = xs.push(e)` on a uniquely-owned local lowers to a destructive ' +
  '`xs.push(e)`, so building an n-element list copies no elements at all ' +
  '(item 445\'s `unique` marker; the unproven case keeps the spread copy)'

export const provenance = [
  { file: 'list_build.ts', snippet: 'xs.push(i)' },
]

const N = 400n

export function check () {
  const a = emittedBuild(N).join(',')
  const b = handBuild(N).join(',')
  if (a !== b) throw new Error('list_build disagreement')
  return a.length
}

export const emitted = async () => { emittedBuild(N) }
export const hand = async () => { handBuild(N) }
// used ONLY by `run.mjs --timing` on an idle machine
export const emittedHot = emitted
export const handHot = hand

export const shape = {
  emitted: { closuresPerCall: 0, awaitsPerCall: 0 },
  hand: { closuresPerCall: 0, awaitsPerCall: 0 },
}

// EXECUTED element-copy count. Patching the array iterator forces the spread
// onto the slow path, which is exactly what makes each copied element
// observable. Counting, not timing, so the deopt does not distort the result.
export const sizes = [50, 100, 200, 400]
export function copiedElements (fn: (n: bigint) => bigint[], n: number): number {
  const proto: any = Array.prototype
  const real = proto[Symbol.iterator]
  let seen = 0
  proto[Symbol.iterator] = function * (this: any[]) {
    for (const v of { [Symbol.iterator]: real.bind(this) } as any) { seen++; yield v }
  }
  try { fn(BigInt(n)) } finally { proto[Symbol.iterator] = real }
  return seen
}
export const emittedBuildFn = emittedBuild
export const handBuildFn = handBuild
