// Runtime value-correctness for the Map VALUE type (docs/stdlib-2.0.md §Map)
// on the TypeScript tier: the built-in JS `Map`, copied on write.
//
// What is asserted here on real emitted output (tests/generated/v3_map.ts):
//   * persistent set — the receiver never mutates; snapshots stay distinct;
//   * lookup answers `V | undefined` (the tier's Opt) so `??` defaults;
//   * has is plain membership;
//   * ORDER-INDEPENDENT structural equality via the revlEq Map branch —
//     the same mapping built in two insertion orders compares equal, which
//     native `===` could never say for objects and revlEq's old object
//     branch could not say for Maps (Object.keys(new Map()) is []).
import { describe, expect, it } from 'vitest'
import { build, eqT, get, member, newTable, put } from './generated/v3_map.ts'

describe('v3 Map value type — value correctness', () => {
  it('starts empty and answers absent', () => {
    expect(newTable().size).toBe(0)
    expect(member(newTable(), 'a')).toBe(false)
    expect(get(newTable(), 'a')).toBe(-1n)
  })

  it('set is checked, present values round-trip', () => {
    const t = put(newTable(), 'a', 1n)
    expect(member(t, 'a')).toBe(true)
    expect(get(t, 'a')).toBe(1n)
  })

  it('set is PERSISTENT: the receiver is untouched by the write', () => {
    const t = newTable()
    const t2 = put(t, 'a', 1n)
    const t3 = put(t2, 'b', 2n)
    expect([...t.entries()]).toEqual([])
    expect([...t2.entries()]).toEqual([['a', 1n]])
    expect([...t3.entries()]).toEqual([['a', 1n], ['b', 2n]])
  })

  it('equality on maps is structural and order-independent (revlEq)', () => {
    // same mapping built in two insertion orders: revl `==` must say EQUAL
    // even though the Map objects are distinct and JS has no native
    // structural map comparison (this is exactly what the revlEq Map
    // branch exists for)
    const a = build(['a', 'bb', 'ccc'])
    const b = build(['ccc', 'bb', 'a'])
    expect(a).not.toBe(b)
    expect(eqT(a, b)).toBe(true)
    // a different mapping must NOT compare equal
    expect(eqT(a, put(b, 'd', 4n))).toBe(false)
    expect(eqT(a, build(['a', 'bb']))).toBe(false)
  })
})
