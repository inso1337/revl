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
    assert set(names) == {"json_parse", "json_stringify"}
    assert names["json_parse"]["returns"] == "Any"
    assert names["json_stringify"]["returns"] == "Str"
    # the module ships @py and @ts bodies; the other tiers are documented
    # refusals (docs/stdlib-json.md)
    assert set(names["json_parse"]["bodies"]) == {"py", "ts"}
    assert consumer_ir["ir_version"] == 3


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert "pub extern pure fn json_parse" in text
    assert "pub extern pure fn json_stringify" in text


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


def test_ts_tier_emits_builtin_json(consumer_ir):
    out = _emit_with("typescript", consumer_ir)
    assert "JSON.parse(s)" in out
    assert "JSON.stringify(v)" in out


def test_rust_tier_refuses_with_the_honest_message(consumer_ir):
    # an Any extern return type-erases to cordis::Value on rust, so a parsed
    # record cannot be read back there (docs/stdlib-json.md); the module
    # ships no @rs body until the emitter types Any-typed bindings by their
    # declared type
    with pytest.raises(Exception, match="no @rs body"):
        _emit_with("rust", consumer_ir)


def test_java_tier_refuses_with_the_honest_message(consumer_ir):
    with pytest.raises(Exception, match="no @java body"):
        _emit_with("java", consumer_ir)


def test_go_tier_refuses_with_the_honest_message(consumer_ir):
    with pytest.raises(Exception, match="no @go body"):
        _emit_with("go", consumer_ir)


def test_wasm_tier_refuses_extern_without_body(consumer_ir):
    with pytest.raises(Exception, match="not a lowerable function"):
        _emit_with("wasm", consumer_ir)

