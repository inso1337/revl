// Time as a coeffect — the clock + timer runtime (roadmap item 57).
//
// The rules these assert are the same ones backends/python/runtime.py pins in
// tests/test_time_coeffect.py; the two tiers must agree tick-for-tick. A timer
// is a revertible schedule: arming registers work with the clock coeffect, its
// inverse is cancellation, and the clock advances only when the harness drives
// it — so firings are deterministic timeline steps, never wall-clock races.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  Clock,
  hostLog,
  liveResources,
  resetHost,
  scheduleAfter,
  scheduleEvery,
  TimerHandle,
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

// A long-running host (issue #3377): the same rules tests/test_time_coeffect.py
// pins for the python clock.
describe('Clock, a long-running host', () => {
  it('nextDue names the ms to the earliest live timer, null with none', () => {
    expect(Clock.nextDue()).toBeNull() // nothing armed
    const every = scheduleEvery(30, () => {}) // due at 30, 60, ...
    scheduleAfter(50, () => {}) // due at 50
    expect(Clock.nextDue()).toBe(30)
    Clock.advance(10)
    expect(Clock.nextDue()).toBe(20)
    Clock.advance(25) // now 35: every -> 60
    expect(Clock.nextDue()).toBe(15) // the after, at 50
    every.cancel()
    expect(Clock.nextDue()).toBe(15)
    Clock.advance(15) // the after fires
    expect(Clock.nextDue()).toBeNull() // spent and cancelled
  })

  it('releases spent and cancelled handles: the clock holds only live ones', () => {
    const cancelled = Array.from({ length: 20 }, () => scheduleEvery(10, () => {}))
    const spent = Array.from({ length: 20 }, () => scheduleAfter(5, () => {}))
    scheduleEvery(7, () => {})
    for (const h of cancelled) h.cancel()
    expect(Clock.advance(5)).toBe(20) // every `after` fires
    expect(Clock._held()).toBe(1)
    expect(Clock.pending()).toBe(1)
    for (const h of [...cancelled, ...spent]) expect(h.cancel()).toBe(false)
  })

  it('keeps only the most recent FIRINGS_CAP firings', () => {
    const cap = Clock.FIRINGS_CAP
    scheduleEvery(1, () => {})
    expect(Clock.advance(cap * 3)).toBe(cap * 3)
    const log = Clock.firings()
    expect(log.length).toBe(cap)
    expect(log[0]).toEqual([1, cap * 2 + 1])
    expect(log[log.length - 1]).toEqual([1, cap * 3])
  })

  it('advance visits as many handles after 500 swaps as after none', () => {
    // A swap cancels the old generation's timer and arms the new one. The
    // count is of handle state reads, not a wall-clock time.
    const visitsAfter = (swaps: number): number => {
      resetHost()
      let h = scheduleEvery(16, () => {})
      for (let i = 0; i < swaps; i++) {
        Clock.advance(16)
        h.cancel()
        h = scheduleEvery(16, () => {})
      }
      const spy = vi.spyOn(TimerHandle.prototype, 'state')
      try {
        expect(Clock.advance(16)).toBe(1)
        expect(Clock.pending()).toBe(1)
        return spy.mock.calls.length
      } finally {
        spy.mockRestore()
      }
    }
    expect(visitsAfter(500)).toBe(visitsAfter(0))
  })
})
