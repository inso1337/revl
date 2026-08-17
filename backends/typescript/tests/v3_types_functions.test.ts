// IR v3 types + pure functions on the TypeScript backend.
import { describe, expect, it } from 'vitest'
import {
  Invalid,
  NotFound,
  Ok,
  add,
  choose,
  classify,
  describe as describeOutcome,
  first,
  greet,
  label,
  list,
  makeRow,
  neg,
  negate,
} from './generated/v3_types_functions.ts'

describe('IR v3 types & functions', () => {
  it('emits records as TS interfaces and exposes pure functions', () => {
    expect(makeRow(1, 'ada')).toEqual({ id: 1, name: 'ada' })
    expect(add(1, 2)).toBe(3)
    expect(classify(-1)).toBe('neg')
    expect(classify(0)).toBe('zero')
    expect(classify(5)).toBe('pos')
    expect(first([7, 8])).toBe(7)
    expect(list()).toEqual([1, 2, 3])
    expect(choose(true)).toBe(1)
    expect(choose(false)).toBe(2)
    expect(neg(3)).toBe(-3)
    expect(negate(true)).toBe(false)
  })

  it('emits variants as discriminated unions and lowers match expressions', () => {
    expect(Ok(makeRow(1, 'ada')).kind).toBe('Ok')
    expect(describeOutcome(Ok(makeRow(1, 'ada')))).toBe('ada')
    expect(describeOutcome(NotFound())).toBe('not found')
    expect(describeOutcome(Invalid('bad'))).toBe('bad')
    expect(label(NotFound())).toBe('other')
    expect(label(Ok(makeRow(2, 'bob')))).toBe('bob')
  })

  it('emits @ts extern bodies verbatim', () => {
    expect(greet('ada')).toBe('hello ada')
  })
})
