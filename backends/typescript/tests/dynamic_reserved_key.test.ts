// Roadmap item 279: a JSON field named by a host reserved word (`function`)
// must stay reachable on a DYNAMIC (`json_parse` / `Any`) value on the TS tier,
// exactly as it already is on the py tier.
//
// This EXECUTES the emitted module (generated from
// tests/fixtures/dynamic_reserved_key.ir.json by scripts/emit-fixtures.ts): the
// dotted access `tc.function.name` and the string-index access
// `tc["function"].name` both have to read the raw `function` key that
// `JSON.parse` produced. Before the fix the dotted form emitted
// `tc.function_.name` (undefined at runtime) and the index form emitted
// `tc[Number("function")].name` (`tc[NaN]`, also undefined).
//
// The cross-tier equality (this TS result === the py tier's result) is pinned
// alongside in backends/typescript/test_dynamic_reserved_key_ts.py, which execs
// the py emission of the SAME fixture and asserts the same value.
import { describe, expect, it } from 'vitest'
import { tool_fn_name, tool_fn_name_idx } from './generated/dynamic_reserved_key.ts'

// An OpenAI-compatible tool call whose entry carries a `function` key.
const WIRE = '{"function": {"name": "get_weather", "arguments": "{}"}}'

describe('item 279 — reserved-word JSON field on a dynamic value (TS tier)', () => {
  it('reads `tc.function.name` off a json_parse value by its raw key', () => {
    // the real value, NOT undefined (the pre-fix `tc.function_.name`)
    expect(tool_fn_name(WIRE)).toBe('get_weather')
  })

  it('reads the string-index escape hatch `tc["function"].name`', () => {
    // NOT undefined (the pre-fix `tc[Number("function")].name`)
    expect(tool_fn_name_idx(WIRE)).toBe('get_weather')
  })

  it('agrees with the py tier: both return the same raw-key value', () => {
    // the constant the py cross-tier test (test_dynamic_reserved_key_ts.py)
    // asserts the py emission returns for the same WIRE — one meaning across
    // the two runtimes, which is the whole point of item 279
    const PY_RESULT = 'get_weather'
    expect(tool_fn_name(WIRE)).toBe(PY_RESULT)
    expect(tool_fn_name_idx(WIRE)).toBe(PY_RESULT)
  })
})
