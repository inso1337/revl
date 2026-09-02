// CASE (headline, now a REGRESSION GUARD): async colour applied to a match
// whose arms never suspend.
//
// `classify` is async-coloured because it calls an async extern once, at the
// top. The match that follows is entirely synchronous - every arm body is a
// bound variable or a literal. The emitter used to render it as an awaited
// `async` IIFE plus, for each BOUND arm, an awaited `async` arrow whose body
// is the identity on the bound value:
//
//     return (await (async (a) => (a))($revl_match_1.value))
//
// Each of those was a promise around an already-computed value and a microtask
// turn the program never asked for. The count is exact and does not move with
// machine load, which is the whole point of measuring it this way.
//
// Emitter site: backends/typescript/emit.py `_v3_match_expr`. It used to read
//   a = "async " if ctx.in_async else ""
// where `ctx.in_async` is a property of the ENCLOSING FUNCTION, not of whether
// any arm suspends, so a match with no awaiting arm was coloured exactly like
// one that awaits in every arm. Item 435(a) made the colour follow the
// rendered arm body instead, which is the `de-coloured` variant below; the
// emitted arm should now measure equal to it and to the hand-written switch.

import { classify as emittedClassify, decode, fetch_one } from '../emitted/match_sync_arms.ts'

// What a competent TS developer writes for the same revl program.
export async function handClassify (p: string): Promise<string> {
  const resp = (await fetch_one(p))
  const $m = decode(resp)
  switch ($m.kind) {
    case "Final":
      return $m.value
    case "NeedTool":
      return $m.value
    case "Retry":
      return "retry"
    default:
      throw new TypeError("non-exhaustive match")
  }
}

export const name = 'match-sync-arms-in-async-fn'
export const summary =
  'a match whose arms never suspend must lower to a PLAIN IIFE and plain arm ' +
  'arrows: no async, no await (item 435(a))'

export const provenance = [
  { file: 'match_sync_arms.ts', snippet: 'return (($revl_match_1) => {' },
  { file: 'match_sync_arms.ts', snippet: 'return ((a) => (a))($revl_match_1.value)' },
  { file: 'match_sync_arms.ts', snippet: 'return ((t) => (t))($revl_match_1.value)' },
]

// one logical operation = one `classify` call that takes a BOUND arm
// MINIMAL FIX variant: the emitted shape unchanged except that `async` is
// dropped from any arrow whose rendered body contains no `await`, and the
// matching `await` is dropped at its call site. The IIFE and the arm arrows
// stay; only the colour goes. This is the smallest possible emitter patch, so
// its number is the win attributable to de-colouring alone, separate from the
// larger win of lowering the match to a plain `switch`.
export async function decolouredClassify (p: string): Promise<string> {
  const resp = (await fetch_one(p))
  return (($revl_match_1) => {
    switch ($revl_match_1.kind) {
      case "Final":
        return ((a) => (a))($revl_match_1.value)
      case "NeedTool":
        return ((t) => (t))($revl_match_1.value)
      case "Retry":
        return ("retry")
      default:
        throw new TypeError("non-exhaustive match")
    }
  })(decode(resp))
}

export const opsPerRun = 1
export const emitted = () => emittedClassify('x')
export const handMemo = () => decolouredClassify('x')   // middle variant slot
export const handMemoLabel = 'de-coloured'
export const hand = () => handClassify('x')

export async function check () {
  const a = await emittedClassify('x')
  const b = await handClassify('x')
  const c = await decolouredClassify('x')
  if (a !== b || a !== c) throw new Error(`match_sync_arms disagreement: ${a} ${b} ${c}`)
  return a
}

// shape metric: function expressions the emitted body evaluates per call
export const shape = {
  emitted: { closuresPerCall: 3, awaitsPerCall: 1 },   // IIFE + arm arrows; only the extern await
  hand: { closuresPerCall: 0, awaitsPerCall: 1 },      // only the extern await
}
