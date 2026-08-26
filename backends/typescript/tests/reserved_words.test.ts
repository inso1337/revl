// Roadmap item 165: a valid revl identifier that collides with a JS/TS reserved
// word (`class`, `new`, `function`, `default`, …) is renamed uniformly by the
// emitter (A3 append-`_`) at the declaration site AND every use site, so the
// emitted module compiles under tsc and RUNS. This EXECUTES the emitted module
// (generated from tests/fixtures/reserved_words.ir.json by scripts/emit-
// fixtures.ts) — decl and use must agree or these calls throw / mistype.
import { describe, expect, it } from 'vitest'
import { go, make, probe, unbox } from './generated/reserved_words.ts'

describe('item 165 — reserved-word identifiers on the TS tier', () => {
  it('runs a fn with a keyword-named parameter and local', () => {
    // probe(class, new) { let function = class; return function }
    expect(probe('payload', 'other')).toBe('payload')
    expect(go('deep')).toBe('deep') // go -> probe(x, x), cross-fn call
  })

  it('constructs and reads a record with keyword-named fields', () => {
    const box = make('v')
    // item 279: a keyword-named field keeps its RAW key (`{"class": ...}`) and
    // is read by bracket access (`b["class"]`), so the write and read agree AND
    // the record has the same key a `json_parse` value would carry
    expect(unbox(box)).toBe('v')
  })
})
