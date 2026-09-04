// The self-rebind (unique-ownership) lowering, roadmap item 435 (d) on item
// 445's frontend `unique` marker.
//
// `xs = xs.push(e)` renders `[...xs, e]` — a whole-array copy per write, so the
// loop the developer wrote as O(n) is emitted O(n^2). Where the frontend proves
// the binding uniquely owns its object at that write, the emitter now writes
// `xs.push(e)` instead. That is a MUTATION standing in for a copy, so a wrong
// proof does not crash: it silently changes a value. These tests execute the
// emitted module and assert the values the copying semantics require.
//
// The spelling assertions live in ../test_unique_writes_ts.py (toolchain-free).
// What is here is the part only execution can answer.
//
// Fixture: tests/fixtures/unique_writes.rvl -> tests/generated/unique_writes.ts
import { describe, expect, it } from 'vitest'
import {
  appended,
  built,
  chunked,
  counted,
  dropped,
  moved,
  snapshots,
} from './generated/unique_writes.ts'

describe('unique-ownership writes — the four in-place shapes', () => {
  it('builds the same list a copying push would', () => {
    expect(built(5n)).toEqual([0n, 1n, 2n, 3n, 4n])
    expect(built(0n)).toEqual([])
  })

  it('accumulates map entries', () => {
    expect([...counted(['a', 'b', 'a']).entries()]).toEqual([['a', 1n], ['b', 1n]])
  })

  it('removes a key, and a missing key is not an error (Map.remove is total)', () => {
    expect([...dropped(['a', 'b'], 'a').keys()]).toEqual(['b'])
    expect([...dropped(['a', 'b'], 'zz').keys()]).toEqual(['a', 'b'])
  })

  it('applies a record update SIMULTANEOUSLY, so a swap is a swap', () => {
    // `{ p | x = p.y, y = p.x }`: field by field in place would read the
    // already-written x back and leave { x: 2, y: 2 }
    expect(moved(1n)).toEqual({ x: 2n, y: 1n })
    expect(moved(2n)).toEqual({ x: 1n, y: 2n })
    expect(moved(3n)).toEqual({ x: 2n, y: 1n })
  })
})

describe('unique-ownership writes — where the copy is observable it survives', () => {
  it('keeps every snapshot distinct when the receiver was handed to another list', () => {
    // `out.push(xs)` retains `xs`, so the write after it must build a new array.
    // In place, all four entries would be the same (finished) array.
    expect(snapshots(4n)).toEqual([[0n], [0n, 1n], [0n, 1n, 2n]])
  })

  it('keeps flow-sensitively reborn chunks distinct (item 445 (a))', () => {
    // `res` escapes into `out` at the end of each pass and is re-declared from a
    // fresh literal at the start of the next, so its pushes ARE in place while
    // the chunks stay separate objects
    const out = chunked(3n)
    expect(out).toEqual([[0n, 1n], [2n, 3n], [4n, 5n]])
    expect(out[0]).not.toBe(out[1])
  })

  it('never writes through a binding born off the caller\'s object', () => {
    const caller = [1n, 2n]
    expect(appended(caller, 3n)).toEqual([1n, 2n, 3n])
    expect(caller).toEqual([1n, 2n])
  })
})
