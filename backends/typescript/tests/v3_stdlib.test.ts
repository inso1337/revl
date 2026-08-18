// Runtime value-correctness for the v3 expression kinds the consolidated
// shape-dispatched renderer (emit.py, commit b6df3f6) supports but that no
// other suite EXECUTES. v3_types_functions.test.ts runs bin/var/if/lit/index/
// record/field/list/un and variant-`match`; conformance.test.ts runs `??` and
// method-body `match`. What was tsc-/golden-gated only, and is asserted here
// on real emitted output, is:
//   * every stdlib `builtin` method (length/push/slice/concat/split/join/
//     repeat/indexOf/charAt/charCodeAt) — including the push/concat
//     value-semantics claim (persistent: the input is never mutated);
//   * `interp` string-template output;
//   * `optfield` / `optcall` (Opt round-trips via optional chaining);
//   * Opt-`match` (the Some/None `value | undefined` branch, a DISTINCT code
//     path from the tagged `.kind` switch that was the only match tested);
//   * `arrow` closures;
//   * `adt` tagged construction (Result) round-tripped back through a match.
//
// Fixture: tests/fixtures/v3_stdlib.ir.json -> tests/generated/v3_stdlib.ts.
import { describe, expect, it } from 'vitest'
import {
  adder,
  charAtOf,
  codeAtOf,
  concatLists,
  errOf,
  greetN,
  indexOfSub,
  joinWith,
  okOf,
  optCode,
  optName,
  pushed,
  repeated,
  resultFold,
  sliced,
  splitOn,
  strLen,
  unwrapOr,
} from './generated/v3_stdlib.ts'

describe('v3 stdlib builtins — value correctness', () => {
  it('computes each stdlib method', () => {
    expect(strLen('abcd')).toBe(4)
    expect(sliced([10, 20, 30, 40])).toEqual([20, 30])
    expect(concatLists([1, 2], [3, 4])).toEqual([1, 2, 3, 4])
    expect(splitOn('a,b,c', ',')).toEqual(['a', 'b', 'c'])
    expect(joinWith(['a', 'b', 'c'], '-')).toBe('a-b-c')
    expect(repeated('ab', 3)).toBe('ababab')
    expect(indexOfSub('hello', 'l')).toBe(2)
    expect(charAtOf('hello', 1)).toBe('e')
    expect(codeAtOf('A', 0)).toBe(65)
  })

  it('push and concat are PERSISTENT (value semantics — input not mutated)', () => {
    const xs = [1, 2]
    expect(pushed(xs, 3)).toEqual([1, 2, 3])
    expect(xs, 'push must not mutate its argument').toEqual([1, 2])

    const a = [1, 2]
    const b = [3, 4]
    concatLists(a, b)
    expect(a).toEqual([1, 2])
    expect(b).toEqual([3, 4])
  })
})

describe('v3 interp — template output', () => {
  it('interleaves text and expression segments', () => {
    expect(greetN('ada', 7)).toBe('hi ada#7!')
  })
})

describe('v3 Opt round-trips — optfield / optcall / Some-None match', () => {
  it('optfield short-circuits on undefined and reads through a value', () => {
    expect(optName({ id: 1, name: 'ada' })).toBe('ada')
    expect(optName(undefined)).toBeUndefined()
  })

  it('optcall short-circuits on undefined and calls through a value', () => {
    expect(optCode('A')).toBe(65)
    expect(optCode(undefined)).toBeUndefined()
  })

  it('Opt-match selects Some vs None (the value|undefined branch)', () => {
    expect(unwrapOr(9, -1)).toBe(9)
    expect(unwrapOr(undefined, -1)).toBe(-1)
    // 0 is a value, not None: the branch keys on `!== undefined`, not truthiness.
    expect(unwrapOr(0, -1)).toBe(0)
  })
})

describe('v3 arrow closures', () => {
  it('returns a callable closure that computes over its param', () => {
    expect(adder()(41)).toBe(42)
  })
})

describe('v3 adt construction + tagged match round-trip', () => {
  it('constructs Ok/Err and folds each back through a match', () => {
    expect(okOf(7)).toEqual({ kind: 'Ok', value: 7 })
    expect(errOf('bad')).toEqual({ kind: 'Err', value: 'bad' })
    expect(resultFold(okOf(7))).toBe(7)
    expect(resultFold(errOf('bad'))).toBe(-3) // -('bad'.length)
  })
})
