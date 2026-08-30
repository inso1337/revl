"""The stdlib JSON module (FR-3, docs/stdlib-json.md, stdlib/json.rvl).

The harness's real tool calls carry structured args; before this module the
mock wire protocol was flattened to `TOOL_CALL name arg1 arg2`. The module
is two `pub extern pure fn`s whose implementations are per-tier `@backend`
bodies: `json_parse(s: Str) -> Any` and `json_stringify(v: Any) -> Str`,
imported with a named `use`.

Checked here:
  * the module imports through `use` and its externs reach the IR;
  * the py tier executes exactly: round-trips of every value type the
    harness needs, the typed-record position (`let tc: ToolCall =
    json_parse(s)`), and the `Any` flow;
  * tiers with a body emit it; tiers without refuse with the honest
    "no @X body" message (the documented path, same shape as the
    conformance corpus's bodyless externs).
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "json.rvl"

#: a harness-shaped consumer: parse a tool-call record, then stringify it back
CONSUMER = """\
use "stdlib/json.rvl" { json_parse, json_stringify }

type ToolCall = { name: Str, args: List[Str] }

fn tool_name(s: Str) -> Str {
  let tc: ToolCall = json_parse(s)
  return tc.name
}

fn arg_count(s: Str) -> Int {
  let tc: ToolCall = json_parse(s)
  return tc.args.length()
}

fn roundtrip(s: Str) -> Str { return json_stringify(json_parse(s)) }
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface)
    d = tmp_path_factory.mktemp("json_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "json.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                           encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_externs_reach_the_ir(consumer_ir):
    names = {e["name"]: e for e in consumer_ir["externs"]}
    assert set(names) == {"json_parse", "json_stringify", "json_try_parse"}
    assert names["json_parse"]["returns"] == "Any"
    assert names["json_stringify"]["returns"] == "Str"
    # item 362: the total parse returns the Result channel instead of raising
    assert names["json_try_parse"]["returns"] == "Result[Any, Str]"
    # the module ships @py, @ts, @rs and @go bodies (item 140); java and wasm
    # remain documented refusals (docs/stdlib-json.md)
    assert set(names["json_parse"]["bodies"]) == {"py", "ts", "rs", "go"}
    assert set(names["json_stringify"]["bodies"]) == {"py", "ts", "rs", "go"}
    assert set(names["json_try_parse"]["bodies"]) == {"py", "ts", "rs", "go"}
    assert consumer_ir["ir_version"] == 3


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert "pub extern pure fn json_parse" in text
    assert "pub extern pure fn json_stringify" in text
    assert "pub extern pure fn json_try_parse" in text


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_json", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "json.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_py_tier_recovers_tool_call_fields(consumer_ir):
    ns = _exec_python(consumer_ir)
    wire = '{"name": "get_weather", "args": ["london"]}'
    assert ns["tool_name"](wire) == "get_weather"
    assert ns["arg_count"](wire) == 1
    assert ns["arg_count"]('{"name": "f", "args": ["a", "b", "c"]}') == 3


def test_py_tier_roundtrips_every_value_type(consumer_ir):
    ns = _exec_python(consumer_ir)
    # Str / Int / Bool / Float / List / null (the Opt None case) / nested
    for doc in ('"hi"', "42", "-7", "true", "false", "2.5", "[1, 2, 3]",
                "null", '{"a": [1, 2.5, true, null, "x"]}'):
        assert ns["roundtrip"](doc) == doc, f"roundtrip failed for {doc}"


def test_py_tier_stringify_then_parse_is_identity(consumer_ir):
    ns = _exec_python(consumer_ir)
    # dumps(loads(s)) is the exact canonical form for these documents
    for doc in ('{"b": 2, "a": [true, null]}', "0", "-0", "1e2"):
        assert ns["roundtrip"](doc) == ns["roundtrip"](ns["roundtrip"](doc))


# ---------------------------------------------- item 281: ts Int == py tier

# The ts-tier runtime proof runs under vitest
# (backends/typescript/tests/fr3_json_int.test.ts) with these SAME expected
# strings. Here the PY tier is executed on the SAME committed fixture, and its
# output — compacted to JSON's canonical form — must equal them. That closes
# the cross-tier claim: the ts bigint replacer produces exactly the py tier's
# JSON (a bare number, at full i64 precision), the only residual difference
# being the insignificant whitespace `json.dumps` inserts by default.
_INT_FIXTURE = ROOT / "backends" / "typescript" / "tests" / "fixtures" / \
    "fr3_json_int.ir.json"

#: the compact JSON the ts tier emits, keyed by fixture fn — mirrored verbatim
#: by fr3_json_int.test.ts
_TS_EXPECTED = {
    "dump_small": '{"input_tokens":7,"output_tokens":12}',
    "dump_negative": '{"input_tokens":-3,"output_tokens":0}',
    "dump_large":
        '{"input_tokens":9007199254740993,"output_tokens":9223372036854775807}',
    "dump_bare_int": "9223372036854775807",
}


def _compact(doc: str) -> str:
    return json.dumps(json.loads(doc), separators=(",", ":"))


def test_ts_int_serialization_matches_py_tier():
    """The py tier serializes each Int-field record; compacted, it equals the
    exact string the ts tier produces (asserted in vitest). Proves ts == py
    for the Int/bigint case the builtin `JSON.stringify` used to throw on."""
    ir = json.loads(_INT_FIXTURE.read_text(encoding="utf-8"))
    ns = _exec_python(ir)
    for fn, ts_out in _TS_EXPECTED.items():
        py_out = ns[fn]()
        # the py tier does not throw and produces the same JSON value...
        assert json.loads(py_out) == json.loads(ts_out), fn
        # ...and byte-equal to the ts tier once whitespace is normalized
        assert _compact(py_out) == ts_out, fn

    # the large-int digits survive EXACTLY on the py reference (a `Number()`
    # cast in the ts replacer would have rounded these — the bug we avoided)
    big = ns["dump_large"]()
    assert "9007199254740993" in big  # 2^53 + 1, not ...992
    assert "9223372036854775807" in big  # i64 max, not ...5808000


# ------------------------------------------- item 311: ts PARSE == py tier

#: documents whose stringify∘parse round-trip the vitest twin
#: (fr3_json_int.test.ts) asserts on the ts tier with these SAME strings. The
#: large-int case is the whole point of item 311: the builtin `JSON.parse`
#: decodes a JSON integer to a lossy f64, so `9007199254740993` came back as
#: ...992 on ts while py's `json.loads` kept it exact. The bigint-aware ts
#: json_parse now decodes it to a JS `bigint`, so the digits survive both tiers.
_PARSE_ROUNDTRIP = [
    "9007199254740993",  # 2^53 + 1 — lossy through the old builtin parse
    "9223372036854775807",  # i64 max
    "-9223372036854775808",  # i64 min
    '{"input_tokens":9007199254740993,"output_tokens":9223372036854775807}',
    "-7",  # negative
    "2.5",  # a float stays a float (revl Float / JS number)
    '{"a":[1,2.5,true,null,"x"]}',  # nested: int + float + bool + null + str
    '{"name":"get_weather","count":2}',  # the ordinary tool-call shape
]


def test_ts_parse_roundtrip_matches_py_tier(consumer_ir):
    """The py tier's stringify∘parse of each document, compacted, equals the
    exact string the ts tier produces (asserted in vitest, fr3_json_int.test.ts).
    Closes the cross-tier claim for the PARSE direction: the bigint-aware ts
    json_parse recovers a large integer at full i64 precision, matching py."""
    ns = _exec_python(consumer_ir)
    for doc in _PARSE_ROUNDTRIP:
        py_out = ns["roundtrip"](doc)  # json_stringify(json_parse(doc)) on py
        assert _compact(py_out) == _compact(doc), doc

    # the large-integer digits are preserved on the py reference — the very
    # value item 311 makes the ts tier preserve too (a lossy parse would have
    # rounded 2^53 + 1 to ...992)
    big = ns["roundtrip"]("9007199254740993")
    assert "9007199254740993" in big


# ---------------------------------------------------------------- tier gates

def _emit_with(backend: str, ir: dict):
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_json_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.emit(ir))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))


def test_ts_tier_emits_bigint_aware_json(consumer_ir):
    out = _emit_with("typescript", consumer_ir)
    # item 311: json_parse is a number-preserving recursive-descent parse (NOT
    # the builtin `JSON.parse`, which decodes every number to a lossy f64) that
    # decodes a JSON integer literal to a JS `bigint` (revl `Int`) and a float
    # to a JS `number` (revl `Float`), matching the py tier past 2^53.
    assert "function json_parse(s: string): any {" in out
    assert "return float ? Number(tok) : BigInt(tok);" in out
    assert "JSON.parse(s)" not in out  # the lossy builtin is gone from parse
    # item 281: json_stringify wraps JSON.stringify in a bigint replacer (an
    # `Int` field is JS `bigint`, which the builtin throws on) rather than
    # returning the builtin result bare.
    assert "JSON.stringify(v, (_k, x) =>" in out
    assert '"@@revlBigInt:"' in out


def test_rust_tier_emits_json_bodies(consumer_ir):
    # item 140: `Any` erases to `cordis::Value`, and the runtime already
    # carries `serde_json` — so json_parse boxes a parsed `serde_json::Value`
    # into a `cordis::Value` and json_stringify recovers and serialises it.
    # (The executable round-trip proof runs under cargo in
    # backends/rust/test_emit_rust.py.)
    out = _emit_with("rust", consumer_ir)
    assert "fn json_parse(s: String) -> Value" in out
    assert "serde_json::from_str::<serde_json::Value>(&s)" in out
    assert "fn json_stringify(v: Value) -> String" in out
    assert "v.downcast::<serde_json::Value>()" in out


def test_java_tier_refuses_with_the_honest_message(consumer_ir):
    with pytest.raises(Exception, match="no @java body"):
        _emit_with("java", consumer_ir)


def test_go_tier_emits_json_bodies_and_hoists_encoding_json(consumer_ir):
    # item 140: revl `Any` erases to Go `any`, and the `//revl:import
    # encoding/json` directive in the @go body is hoisted to the module's
    # import block (a verbatim extern body cannot spell its own `import`).
    # (The executable round-trip proof runs under `go test` in
    # backends/go/scenarios/emitted/jsonwire/.)
    out = _emit_with("go", consumer_ir)
    assert "func json_parse(s string) any" in out
    assert "func json_stringify(v any) string" in out
    assert "json.Unmarshal([]byte(s), &v)" in out
    assert "json.Marshal(v)" in out
    # the directive is hoisted, not left dangling in the body
    assert '"encoding/json"' in out
    assert "//revl:import" not in out


def test_wasm_tier_refuses_extern_without_body(consumer_ir):
    with pytest.raises(Exception, match="not a lowerable function"):
        _emit_with("wasm", consumer_ir)

