// Regression: the ts `json_parse` (@ts body in stdlib/json.rvl) must not let a
// JSON `"__proto__"` key reach the object's prototype. The object branch used
// to assign `o[k] = value()` on a plain `{}`, so a key of `"__proto__"` invoked
// the Object.prototype setter and polluted every object in the process (a
// sibling of the #319 dunder-key issue). It now stores `"__proto__"` as an OWN
// DATA property (`Object.defineProperty`), matching the py tier where
// `json.loads` keeps it an ordinary dict key — retrievable as data, inert as a
// prototype.
//
// `json_parse` is imported from the emitted fr3 fixture (the SAME @ts body the
// stdlib ships), so this exercises the real emitter output.
import { describe, expect, it } from 'vitest'
import { json_parse } from './generated/fr3_json_int.ts'

describe('json_parse does not allow __proto__ prototype pollution', () => {
  it('a "__proto__" payload does not pollute Object.prototype', () => {
    const before = ({} as Record<string, unknown>).polluted
    expect(before).toBeUndefined()
    json_parse('{"__proto__": {"polluted": true}}')
    // every plain object in the process is unaffected
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })

  it('the parsed object keeps a normal prototype and "__proto__" as own data', () => {
    const o = json_parse('{"__proto__": {"polluted": true}, "keep": 1}')
    // not re-parented: still a normal object, no injected prototype
    expect(Object.getPrototypeOf(o)).toBe(Object.prototype)
    expect((o as Record<string, unknown>).polluted).toBeUndefined()
    // "__proto__" survives as an OWN DATA property (py-tier parity)
    expect(Object.prototype.hasOwnProperty.call(o, '__proto__')).toBe(true)
    expect((o as any)['__proto__']).toEqual({ polluted: true })
    // the sibling key is unharmed
    expect((o as Record<string, unknown>).keep).toBe(1n)
  })

  it('a "constructor" key stays own data and does not reach the prototype', () => {
    const o = json_parse('{"constructor": {"x": 1}}')
    expect(Object.getPrototypeOf(o)).toBe(Object.prototype)
    expect((o as any).constructor).toEqual({ x: 1n })
    // Object.prototype.constructor is still the Object constructor
    expect(({} as any).constructor).toBe(Object)
  })
})
