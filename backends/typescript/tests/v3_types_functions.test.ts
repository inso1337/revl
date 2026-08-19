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
    expect(makeRow(1n, 'ada')).toEqual({ id: 1n, name: 'ada' })
    expect(add(1n, 2n)).toBe(3n)
    expect(classify(-1n)).toBe('neg')
    expect(classify(0n)).toBe('zero')
    expect(classify(5n)).toBe('pos')
    expect(first([7n, 8n])).toBe(7n)
    expect(list()).toEqual([1n, 2n, 3n])
    expect(choose(true)).toBe(1n)
    expect(choose(false)).toBe(2n)
    expect(neg(3n)).toBe(-3n)
    expect(negate(true)).toBe(false)
  })

  it('emits variants as discriminated unions and lowers match expressions', () => {
    expect(Ok(makeRow(1n, 'ada')).kind).toBe('Ok')
    expect(describeOutcome(Ok(makeRow(1n, 'ada')))).toBe('ada')
    expect(describeOutcome(NotFound())).toBe('not found')
    expect(describeOutcome(Invalid('bad'))).toBe('bad')
    expect(label(NotFound())).toBe('other')
    expect(label(Ok(makeRow(2n, 'bob')))).toBe('bob')
  })

  it('emits @ts extern bodies verbatim', () => {
    expect(greet('ada')).toBe('hello ada')
  })
})
