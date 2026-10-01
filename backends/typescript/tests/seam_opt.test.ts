// issue #1619: a declared `Opt[T]` reads the wire's `null` as this tier's
// `None`, which is `undefined`.
//
// The emitter's Opt checks test `=== undefined` (a `match` arm, a `?.`), and
// the wire carries `None` as JSON `null`, so a `null` passed through by
// `decodeAs` took the `Some` arm and bound `null`. The end-to-end placements
// (py -> ts, ts -> py, ts -> ts) are tests/test_ts_seam_opt_1619.py.
import { describe, expect, it } from 'vitest'

import { decodeAs } from '../bridge.ts'

const TYPES = {
  Box: { params: [], kind: 'record', fields: { name: 'Opt[Str]', n: 'Opt[Int]' } },
}

describe('decodeAs: a declared Opt', () => {
  it('reads a wire null as None (undefined)', () => {
    expect(decodeAs(null, 'Opt[Str]', TYPES)).toBeUndefined()
    expect(decodeAs(null, 'Opt[Int]', TYPES)).toBeUndefined()
    expect(decodeAs(undefined, 'Opt[Str]', TYPES)).toBeUndefined()
  })

  it('decodes a present value by the payload type', () => {
    expect(decodeAs('A', 'Opt[Str]', TYPES)).toBe('A')
    expect(decodeAs(7, 'Opt[Int]', TYPES)).toBe(7n)
  })

  it('reads a null Opt inside a record field and a list', () => {
    const box = decodeAs({ name: null, n: 3 }, 'Box', TYPES) as Record<string, unknown>
    expect(box.name).toBeUndefined()
    expect('name' in box).toBe(true)
    expect(box.n).toBe(3n)
    expect(decodeAs([1, null], 'List[Opt[Int]]', TYPES)).toEqual([1n, undefined])
  })

  it('leaves a null that is not declared Opt alone', () => {
    expect(decodeAs(null, 'Str', TYPES)).toBeNull()
  })
})
