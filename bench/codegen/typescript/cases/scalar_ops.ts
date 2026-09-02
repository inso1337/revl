// NEGATIVE CONTROL. Four shapes the emitter produces that were suspected but
// turned out to be fine once measured, kept here so a future change that makes
// them expensive is caught:
//
//   arith      `revlI64(...)` wrapped around every Int operation
//   scalar_eq  `revlEq(i, 7n)` instead of `i === 7n`
//   build      record literal + field reads
//   classify   the `((_rc) => "0" <= _rc && _rc <= "9")(c)` predicate IIFE
//
// The hand versions strip exactly the emitter artefact and keep everything
// else identical.

import { arith as eArith, scalar_eq as eEq, build as eBuild, classify as eClassify }
  from '../emitted/scalar_ops.ts'

const REVL_I64_MIN = -(2n ** 63n)
const REVL_I64_MAX = 2n ** 63n - 1n
function revlI64 (v: bigint): bigint {
  if (v < REVL_I64_MIN || v > REVL_I64_MAX) throw new RangeError('revl: Int overflow')
  return v
}

// no overflow guard at all (the strongest thing the emitter could ever do)
function hArith (n: bigint): bigint {
  let acc = 0n; let i = 0n
  while ((i < n)) { acc = acc + i; i = i + 1n }
  return acc
}
// `===` instead of the structural helper
function hEq (n: bigint): bigint {
  let hits = 0n; let i = 0n
  while ((i < n)) { if (i === 7n) { hits = revlI64(hits + 1n) } i = revlI64(i + 1n) }
  return hits
}
// same record shape, hoisted-free
function hBuild (n: bigint): bigint {
  let acc = 0n; let i = 0n
  while ((i < n)) {
    const x = i; const y = revlI64(i + 1n)
    acc = revlI64(revlI64(acc + x) + y)
    i = revlI64(i + 1n)
  }
  return acc
}
// predicate inline instead of an immediately-invoked arrow
function hClassify (c: string): bigint {
  if ("0" <= c && c <= "9") return 1n
  if (("a" <= c && c <= "z") || ("A" <= c && c <= "Z")) return 2n
  return 0n
}

export const name = 'scalar-ops-control'
export const summary =
  'NEGATIVE CONTROL: the Int overflow guard, revlEq on scalars, record ' +
  'literals and the character-class predicate IIFE are all cheap'

export const provenance = [
  { file: 'scalar_ops.ts', snippet: 'acc = revlI64(acc + i)' },
  { file: 'scalar_ops.ts', snippet: 'if (revlEq(i, 7n)) {' },
  { file: 'scalar_ops.ts', snippet: '((_rc: string) => "0" <= _rc && _rc <= "9")(c)' },
]

const N = 30000n
const CHARS = 'a1Z9 x'

export function check () {
  const a = [eArith(N), eEq(N), eBuild(N)].join(',')
  const b = [hArith(N), hEq(N), hBuild(N)].join(',')
  if (a !== b) throw new Error(`scalar_ops disagreement: ${a} | ${b}`)
  for (const c of CHARS) if (eClassify(c) !== hClassify(c)) throw new Error('classify disagreement')
  return a
}

const spin = (f: (c: string) => bigint) => { let s = 0n; for (let k = 0; k < 30000; k++) s += f(CHARS[k % CHARS.length]); return s }

export const emitted = async () => { eArith(N); eEq(N); eBuild(N); spin(eClassify) }
export const hand = async () => { hArith(N); hEq(N); hBuild(N); spin(hClassify) }
// used ONLY by `run.mjs --timing` on an idle machine
export const emittedHot = emitted
export const handHot = hand
