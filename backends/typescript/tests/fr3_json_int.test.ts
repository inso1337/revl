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

  it('an ordinary tool-call shape round-trips unchanged', () => {
    // `count` decodes to a bigint (item 311's bigint-aware parse), which
    // json_stringify renders back as the bare number 2 — the string is
    // byte-identical to the input, matching the py tier.
    expect(roundtrip('{"name":"get_weather","count":2}')).toBe(
      '{"name":"get_weather","count":2}',
    )
  })
})

// Roadmap item 311 (found by item 281): the PARSE direction. The builtin
// `JSON.parse` decodes every JSON number to a JS `number` (f64), so an integer
// past 2^53 came back rounded on the ts tier while the py tier (`json.loads` ->
// Python int) kept it exact — a silent cross-tier value divergence. The @ts
// `json_parse` (stdlib/json.rvl) is now a number-preserving recursive-descent
// parse: a JSON integer literal decodes to a JS `bigint` (revl `Int`, full i64
// precision) and a float to a JS `number` (revl `Float`), matching py.
//
// `roundtrip` is json_stringify(json_parse(s)); a lossy parse would surface as
// changed digits after the round-trip. These EXPECTED strings are the py tier's
// own compacted output for the same documents, cross-checked against the py
// tier in tests/test_json_stdlib.py::test_ts_parse_roundtrip_matches_py_tier.
describe('item 311 json_parse decodes a large integer without losing precision', () => {
  it('a bare integer past 2^53 survives stringify∘parse exactly (== py)', () => {
    // 2^53 + 1: the builtin JSON.parse would round this to ...992.
    expect(roundtrip('9007199254740993')).toBe('9007199254740993')
  })

  it('i64 max and i64 min survive exactly (== py)', () => {
    expect(roundtrip('9223372036854775807')).toBe('9223372036854775807')
    expect(roundtrip('-9223372036854775808')).toBe('-9223372036854775808')
  })

  it('large Int fields inside an object keep full i64 precision (== py)', () => {
    expect(
      roundtrip(
        '{"input_tokens":9007199254740993,"output_tokens":9223372036854775807}',
      ),
    ).toBe('{"input_tokens":9007199254740993,"output_tokens":9223372036854775807}')
  })

  it('negative integers, floats, and nested mixes round-trip (== py)', () => {
    expect(roundtrip('-7')).toBe('-7')
    // a float literal stays a JS number (revl Float), not a bigint
    expect(roundtrip('2.5')).toBe('2.5')
    expect(roundtrip('{"a":[1,2.5,true,null,"x"]}')).toBe(
      '{"a":[1,2.5,true,null,"x"]}',
    )
  })
})
