// CASE: building a list in a loop.
//
// Emitter site: backends/typescript/emit.py, the builtin-method table:
//     if method == "push":
//         return f"[...{target}, {args[0]}]"
//
// revl's `push` has value semantics, so a copy is the honest default. But the
// shape the emitter actually produces for the overwhelmingly common
// `xs = xs.push(e)` is a whole-array spread whose source binding is dead the
// instant the assignment lands, so the copy is never observed. A competent TS
// developer writes `xs.push(e)`. Over a loop of n pushes that is the difference
// between n(n-1)/2 element copies and n.

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
  '`xs = xs.push(e)` lowers to a whole-array spread copy per iteration, so ' +
  'building an n-element list copies n(n-1)/2 elements instead of n'

export const provenance = [
  { file: 'list_build.ts', snippet: 'xs = [...xs, i]' },
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
