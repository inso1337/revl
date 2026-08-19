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
    expect(strLen('abcd')).toBe(4n)
    expect(sliced([10n, 20n, 30n, 40n])).toEqual([20n, 30n])
    expect(concatLists([1n, 2n], [3n, 4n])).toEqual([1n, 2n, 3n, 4n])
    expect(splitOn('a,b,c', ',')).toEqual(['a', 'b', 'c'])
    expect(joinWith(['a', 'b', 'c'], '-')).toBe('a-b-c')
    expect(repeated('ab', 3n)).toBe('ababab')
    expect(indexOfSub('hello', 'l')).toBe(2n)
    expect(charAtOf('hello', 1n)).toBe('e')
    expect(codeAtOf('A', 0n)).toBe(65n)
  })

  it('push and concat are PERSISTENT (value semantics — input not mutated)', () => {
    const xs = [1n, 2n]
    expect(pushed(xs, 3n)).toEqual([1n, 2n, 3n])
    expect(xs, 'push must not mutate its argument').toEqual([1n, 2n])

    const a = [1n, 2n]
    const b = [3n, 4n]
    concatLists(a, b)
    expect(a).toEqual([1n, 2n])
    expect(b).toEqual([3n, 4n])
  })
})

describe('v3 interp — template output', () => {
  it('interleaves text and expression segments', () => {
    // a bigint interpolates without the `n` suffix — `${7n}` is "7"
    expect(greetN('ada', 7n)).toBe('hi ada#7!')
  })
})

describe('v3 Opt round-trips — optfield / optcall / Some-None match', () => {
  it('optfield short-circuits on undefined and reads through a value', () => {
    expect(optName({ id: 1n, name: 'ada' })).toBe('ada')
    expect(optName(undefined)).toBeUndefined()
  })

  it('optcall short-circuits on undefined and calls through a value', () => {
    expect(optCode('A')).toBe(65n)
    expect(optCode(undefined)).toBeUndefined()
  })

  it('Opt-match selects Some vs None (the value|undefined branch)', () => {
    expect(unwrapOr(9n, -1n)).toBe(9n)
    expect(unwrapOr(undefined, -1n)).toBe(-1n)
    // 0 is a value, not None: the branch keys on `!== undefined`, not truthiness.
    expect(unwrapOr(0n, -1n)).toBe(0n)
  })
})

describe('v3 arrow closures', () => {
  it('returns a callable closure that computes over its param', () => {
    expect(adder()(41n)).toBe(42n)
  })
})

describe('v3 adt construction + tagged match round-trip', () => {
  it('constructs Ok/Err and folds each back through a match', () => {
    expect(okOf(7n)).toEqual({ kind: 'Ok', value: 7n })
    expect(errOf('bad')).toEqual({ kind: 'Err', value: 'bad' })
    expect(resultFold(okOf(7n))).toBe(7n)
    expect(resultFold(errOf('bad'))).toBe(-3n) // -('bad'.length)
  })
})
