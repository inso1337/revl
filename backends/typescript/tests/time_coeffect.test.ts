// Time as a coeffect — the clock + timer runtime (roadmap item 57).
//
// The rules these assert are the same ones backends/python/runtime.py pins in
// tests/test_time_coeffect.py; the two tiers must agree tick-for-tick. A timer
// is a revertible schedule: arming registers work with the clock coeffect, its
// inverse is cancellation, and the clock advances only when the harness drives
// it — so firings are deterministic timeline steps, never wall-clock races.
import { beforeEach, describe, expect, it } from 'vitest'
import {
  Clock,
  hostLog,
  liveResources,
  resetHost,
  scheduleAfter,
  scheduleEvery,
} from '../runtime.ts'

beforeEach(() => resetHost())

describe('Clock — the clock coeffect', () => {
  it('does not advance on its own; a firing lands on an exact tick', () => {
    const seen: number[] = []
    scheduleEvery(10, () => seen.push(Clock.now()))
    expect(Clock.now()).toBe(0)
    expect(seen).toEqual([]) // nothing fires unbidden
    const fired = Clock.advance(35) // inject 35ms of time
    expect(fired).toBe(3)
    expect(seen).toEqual([10, 20, 30]) // deterministic steps
    expect(Clock.firings()[2]).toEqual([1, 30]) // 'fires on the 3rd tick', at 30ms
  })

  it('interleaves multiple timers in true time order, ties by arm order', () => {
    const order: string[] = []
    scheduleEvery(10, () => order.push('a')) // a: 10,20,30
    scheduleEvery(15, () => order.push('b')) // b: 15,30
    Clock.advance(30)
    expect(order).toEqual(['a', 'b', 'a', 'a', 'b']) // 10a 15b 20a 30a 30b
  })
})

describe('timer — a revertible schedule', () => {
  it('unload cancels an `every` timer: no residue, no orphaned firing', () => {
    const seen: number[] = []
    const handle = scheduleEvery(10, () => seen.push(Clock.now()))
    Clock.advance(25) // fires at 10, 20
    expect(seen).toEqual([10, 20])
    expect(Clock.pending()).toBe(1) // live schedule = residue
    expect(liveResources.has(handle.label)).toBe(true)
    // teardown runs the derived inverse (the emitted `() => handle.cancel()`)
    expect(handle.cancel()).toBe(true)
    expect(Clock.pending()).toBe(0) // no residue
    expect(liveResources.size).toBe(0)
    Clock.advance(1000) // lots more time passes
    expect(seen).toEqual([10, 20]) // but no orphaned firing
  })

  it('traces schedule/cancel so an uncancelled timer is caught as residue', () => {
    const h = scheduleEvery(10, () => {})
    expect(hostLog).toContain(`${h.label}.schedule every 10ms`)
    h.cancel()
    expect(hostLog).toContain(`${h.label}.cancel`)
  })

  it('`after` fires once and is spent — no residue, teardown cancel is a no-op', () => {
    const seen: string[] = []
    const handle = scheduleAfter(50, () => seen.push('boom'))
    expect(Clock.advance(49)).toBe(0)
    expect(seen).toEqual([])
    expect(Clock.advance(11)).toBe(1) // fires at 50
    expect(seen).toEqual(['boom'])
    expect(Clock.pending()).toBe(0) // spent — no residue
    expect(liveResources.size).toBe(0)
    expect(handle.cancel()).toBe(false) // teardown no-op
    expect(Clock.advance(1000)).toBe(0) // never fires again
  })

  it('rejects a non-positive interval', () => {
    expect(() => scheduleEvery(0, () => {})).toThrow()
    expect(() => scheduleAfter(-1, () => {})).toThrow()
  })
})
