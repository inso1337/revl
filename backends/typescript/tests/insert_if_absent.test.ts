// The atomicity guard for `MapHandle.insert_if_absent` (design 397, issue
// #2731). On this tier the verb's atomicity is a property of the runtime's
// run-to-completion scheduling, not of a lock: the method is one synchronous,
// suspension-free step, so no admitted observer can separate the membership
// test from the insert. Nothing failed if that stopped being true, so these
// tests pin it:
//
//   * structural: the method is not async and is not a generator, and its body
//     carries no `await` or `yield`;
//   * behavioural: a membership test that schedules a competing caller as a
//     microtask (the yield point a suspension would expose) still yields
//     exactly one winner, because the microtask cannot run until the
//     synchronous method has returned. If an `await` appears between the check
//     and the insert, the microtask runs in that gap and both callers win.
import { describe, expect, it } from 'vitest'
import { MapHandle } from '../runtime.ts'

/** The method's source with comments removed: the method's own comment says
 * "no await", and a raw text scan would read that as a suspension point. */
function bodyWithoutComments(fn: Function): string {
  return fn.toString()
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/\/\/[^\n]*/g, ' ')
}

describe('insert_if_absent is one synchronous step (design 397)', () => {
  it('is not a coroutine or generator and carries no await or yield', () => {
    const method = MapHandle.prototype.insert_if_absent
    const kind = method.constructor.name
    expect(kind).not.toBe('AsyncFunction')
    expect(kind).not.toBe('GeneratorFunction')
    expect(kind).not.toBe('AsyncGeneratorFunction')
    const body = bodyWithoutComments(method)
    expect(body).not.toMatch(/\bawait\b/)
    expect(body).not.toMatch(/\byield\b/)
  })

  it('has exactly one winner when the check schedules a competing caller', async () => {
    const map = new MapHandle()
    const inner = (map as unknown as { data: Map<unknown, unknown> }).data
    let hook: (() => void) | undefined
    // A map object whose membership test fires a one-shot hook after answering.
    // The rest of the surface is delegated, because `insert_if_absent` only
    // reads `has` and writes `set`; `clear` lets the map drop itself below.
    const watched = {
      has(key: unknown): boolean {
        const scheduled = hook
        hook = undefined
        const present = inner.has(key)
        scheduled?.()
        return present
      },
      set(key: unknown, value: unknown): void {
        inner.set(key, value)
      },
      get(key: unknown): unknown {
        return inner.get(key)
      },
      delete(key: unknown): boolean {
        return inner.delete(key)
      },
      clear(): void {
        inner.clear()
      },
    }
    ;(map as unknown as { data: unknown }).data = watched

    const results: boolean[] = []
    hook = () => {
      queueMicrotask(() => {
        results.push(map.insert_if_absent('k', 'second'))
      })
    }
    results.push(map.insert_if_absent('k', 'first'))
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(results).toEqual([true, false])
    expect(map.get('k')).toBe('first')
    map.drop()
  })
})
