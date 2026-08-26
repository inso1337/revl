// Roadmap item 281 (compiles-implies-runs): `json_stringify` of a record with
// an `Int` field on the ts tier.
//
// `Int` maps to JS `bigint` (emit.py TYPE_MAP, for 64-bit fidelity), and the
// builtin `JSON.stringify` THROWS on any bigint ("Do not know how to serialize
// a BigInt"). So before this fix a value carrying an Int field type-checked and
// emitted clean, then died at RUNTIME on ts (the py tier serialized it fine).
// The @ts extern body (stdlib/json.rvl) now renders each bigint as a bare
// JSON number via a replacer, at full i64 precision.
//
// These EXPECTED strings are the py tier's own output for the same records,
// canonicalized to JSON's compact form. `JSON.stringify` has always emitted
// compact JSON (no space after ':'/','); the py tier's `json.dumps` default
// inserts insignificant whitespace. The two are therefore byte-equal after
// removing that whitespace, which is asserted against the py tier in
// tests/test_json_stdlib.py::test_ts_int_serialization_matches_py_tier, which
// runs the py emitter on this very fixture and compacts its output to these
// exact literals. The values below are the compact py output verbatim.
import { describe, expect, it } from 'vitest'
import {
  dump_bare_int,
  dump_large,
  dump_negative,
  dump_small,
  roundtrip,
} from './generated/fr3_json_int.ts'

describe('item 281 json_stringify serializes an Int (bigint) field, not throws', () => {
  it('a small positive Int field renders as a bare number (== py tier)', () => {
    // py: json.dumps({"input_tokens": 7, "output_tokens": 12}) compacted
    expect(dump_small()).toBe('{"input_tokens":7,"output_tokens":12}')
  })

  it('a negative Int field renders as a bare number (== py tier)', () => {
    expect(dump_negative()).toBe('{"input_tokens":-3,"output_tokens":0}')
  })

  it('Int fields BEYOND 2^53 keep full i64 precision (== py tier)', () => {
    // 9007199254740993 = 2^53 + 1 (Number() would round it to ...992);
    // 9223372036854775807 = i64 max (Number() would round it to ...5808000).
    expect(dump_large()).toBe(
      '{"input_tokens":9007199254740993,"output_tokens":9223372036854775807}',
    )
  })

  it('a bare top-level Int renders as a bare number (replacer fires at root)', () => {
    expect(dump_bare_int()).toBe('9223372036854775807')
  })

  it('does not throw the pre-fix BigInt TypeError', () => {
    expect(() => dump_small()).not.toThrow()
    expect(() => dump_large()).not.toThrow()
  })

  it('the no-bigint fast path is unchanged (parse then stringify)', () => {
    // json_parse maps JSON numbers to JS `number`, so this exercises the
    // marks.length === 0 fast path identical to the old builtin behavior.
    expect(roundtrip('{"name":"get_weather","count":2}')).toBe(
      '{"name":"get_weather","count":2}',
    )
  })
})
