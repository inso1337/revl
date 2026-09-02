// CASE: a `match` evaluated inside an async-coloured function.
//
// Emitted side is imported straight out of `emitted/match_async.ts`, so it is
// the compiler's real output, not a transcription. Hand side keeps the exact
// same leaf functions (`decode`, `fetch_one`, `revlI64` semantics) and changes
// only the SHAPE of the match, so the delta is attributable to the lowering.
//
// Emitter site: backends/typescript/emit.py `_v3_match_expr` (the `a = "async
// " if ctx.in_async else ""` block and the per-arm `(await (async (bind) =>
// ...)(tmp.value))` lines).

import { drive as emittedDrive, decode, fetch_one } from '../emitted/match_async.ts'

// verbatim copy of the helper the emitted module declares, so both sides pay
// the identical overflow-check cost
const REVL_I64_MIN = -(2n ** 63n)
const REVL_I64_MAX = 2n ** 63n - 1n
function revlI64(v: bigint): bigint {
  if (v < REVL_I64_MIN || v > REVL_I64_MAX) throw new RangeError('revl: Int overflow')
  return v
}

// What a competent TS developer writes for the same revl program: the
// scrutinee into a local, a bare `switch`, arm bodies inline. No IIFE, no
// per-arm arrow, no promise around an already-computed string.
export async function handDrive(prompt: string, n: bigint): Promise<string> {
  if ((n <= 0n)) {
    return "done"
  }
  const $m = decode((await fetch_one(prompt)))
  switch ($m.kind) {
    case "Final":
      return $m.value
    case "NeedTool":
      return (await handDrive($m.value, revlI64(n - 1n)))
    default:
      throw new TypeError("non-exhaustive match")
  }
}

export const name = 'match-in-async-fn'
export const summary =
  'a match in an async body lowers to an awaited async IIFE plus one awaited ' +
  'async arrow per bound arm, even when no arm suspends'

// fragments that must still be present in the emitted file, else the hand
// comparison is measuring a shape the compiler no longer produces
export const provenance = [
  { file: 'match_async.ts', snippet: 'return (await (async ($revl_match_1) => {' },
  { file: 'match_async.ts', snippet: 'return (await (async (answer) => (answer))($revl_match_1.value))' },
]

const DEPTH = 12n
// each `drive` call below its base case evaluates exactly one match
export const opsPerRun = 12
export const emitted = () => emittedDrive('x', DEPTH)
export const hand = () => handDrive('x', DEPTH)

// heavier variants, used ONLY by `run.mjs --timing` on an idle machine
export const emittedHot = async () => { for (let i = 0; i < 200; i++) await emittedDrive('x', DEPTH) }
export const handHot = async () => { for (let i = 0; i < 200; i++) await handDrive('x', DEPTH) }

// shape metric, read off emitted/match_async.ts (not executed)
export const shape = {
  emitted: { closuresPerCall: 2, awaitsPerCall: 3 },  // IIFE + arm arrow; extern/recursive await
  hand: { closuresPerCall: 0, awaitsPerCall: 2 },
}
