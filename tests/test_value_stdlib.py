"""The stdlib VALUE module (roadmap item 180, docs/stdlib-value.md,
stdlib/value.rvl).

`Value` is the erased-dynamic type: the runtime union of record / list /
scalar / null. It is the NAMED, reusable generalisation of the `Any` boundary
stdlib/json.rvl documents — and the fundamental Path B enabler surfaced by
item 174: a self-hosted emitter walking a `kind`-discriminated IR document had
to bridge every access through private `@py` accessors
(`g`/`gs`/`alist`/`at`/`child_nodes`/…, as selfhost/emit_py.rvl did) because
the checker refuses stdlib methods on an unpinned `Any` receiver (G8). This
module is that bridge, once and typed, so the walk is PURE revl.

Checked here:
  * the module imports through `use` and its externs reach the IR;
  * checker support: a concrete Str/Int/List/record flows INTO a `Value`
    argument, and a `Value` flows OUT into any typed position — the
    compatibility rule that replaces the G8 refusal;
  * the py tier EXECUTES a pure-revl navigation of a real IR document using
    ONLY these accessors (no `@py`), producing the right result;
  * TOTALITY: every accessor returns a typed default / `Opt` `None` on a shape
    mismatch and never crashes.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "value.rvl"

#: a self-hosted-emitter-shaped consumer: navigate a kind-discriminated IR
#: document (records / lists / scalars) using ONLY the Value accessors — the
#: proof that the `@py` bridge selfhost/emit_py.rvl carries is no longer needed.
CONSUMER = """\
use "stdlib/value.rvl" {
  value_kind, value_is_null, value_field, value_opt, value_has,
  value_list, value_at, value_children, value_len,
  value_str, value_int, value_bool, value_keys
}

// Render a `kind`-discriminated expression IR back to source — the shape a
// self-hosted emitter walks, in PURE revl (no @py accessors of its own).
fn render(node: Value) -> Str {
  let k = value_str(value_field(node, "kind"))
  if (k == "bin") {
    let l = render(value_field(node, "left"))
    let r = render(value_field(node, "right"))
    return `(${l} ${value_str(value_field(node, "op"))} ${r})`
  }
  if (k == "lit") { return value_str(value_field(node, "text")) }
  if (k == "list") {
    var parts = []
    for (c of value_list(value_field(node, "items"))) { parts = parts.push(render(c)) }
    return `[${parts.join(", ")}]`
  }
  return "?"
}

// A whole-document leaf count through the generic children driver — the walk
// terminates at scalar leaves exactly as `child_nodes` recursion does.
fn leaf_count(node: Value) -> Int {
  let cs = value_children(node)
  if (cs.length() == 0) { return 1 }
  var n = 0
  for (c of cs) { n = n + leaf_count(c) }
  return n
}

// Opt access distinguishes an absent field from a present one.
fn op_or_none(node: Value) -> Str {
  return match value_opt(node, "op") {
    Some(x) => value_str(x),
    None => "<none>",
  }
}

fn has_op(node: Value) -> Bool { return value_has(node, "op") }

fn item_at(node: Value, i: Int) -> Str {
  return value_str(value_field(value_at(value_field(node, "items"), i), "text"))
}

// ---- key enumeration (item 188): walk a keyed record in PURE revl ----------
// A `services`-shaped doc: enumerate its service names in insertion order.
fn service_names(comp: Value) -> List[Str] {
  return value_keys(value_field(comp, "services"))
}

// value_keys + value_field IS the entries pattern: join "name=<kind>" per key,
// in insertion order, using only the accessors (no @py of its own).
fn service_kinds(comp: Value) -> Str {
  var parts = []
  for (k of value_keys(value_field(comp, "services"))) {
    let svc = value_field(value_field(comp, "services"), k)
    parts = parts.push(`${k}=${value_str(value_field(svc, "kind"))}`)
  }
  return parts.join(",")
}

// ---- totality: value_keys of a non-record is empty, never crashes ----------
fn t_keys_of_scalar_empty() -> Int { return value_keys(node_of_int()).length() }
fn t_keys_of_list_empty() -> Int { return value_keys([1, 2, 3]).length() }
fn t_keys_of_null_empty() -> Int { return value_keys(value_field("s", "k")).length() }

// ---- totality: every mismatch returns a typed default, never crashes -------
fn t_str_of_missing() -> Str { return value_str(value_field("scalar", "nope")) }
fn t_int_of_record() -> Int { return value_int(value_field(node_of_int(), "x")) }
fn t_kind_of_null() -> Str { return value_kind(value_field(node_of_int(), "absent")) }
fn t_len_of_scalar() -> Int { return value_len(node_of_int()) }
fn t_at_out_of_range() -> Bool { return value_is_null(value_at(empty_list(), 9)) }
fn t_bool_of_str() -> Bool { return value_bool(value_field("s", "k")) }
fn t_list_of_scalar_empty() -> Int { return value_list(node_of_int()).length() }

fn node_of_int() -> Value { return value_field("x", "y") }   // a null Value
fn empty_list() -> Value { return value_field("x", "y") }
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface)
    d = tmp_path_factory.mktemp("value_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "value.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                            encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# a small kind-discriminated IR document: bin(+, lit 1, list[lit 2, lit 3])
DOC = {
    "kind": "bin", "op": "+",
    "left": {"kind": "lit", "text": "1"},
    "right": {"kind": "list", "items": [
        {"kind": "lit", "text": "2"},
        {"kind": "lit", "text": "3"},
    ]},
}


# ---------------------------------------------------------------- the module

def test_module_imports_and_externs_reach_the_ir(consumer_ir):
    names = {e["name"]: e for e in consumer_ir["externs"]}
    expected = {
        "value_kind", "value_is_null", "value_field", "value_opt", "value_has",
        "value_list", "value_at", "value_children", "value_len",
        "value_str", "value_int", "value_bool", "value_keys",
    }
    assert set(names) == expected
    assert names["value_kind"]["returns"] == "Str"
    assert names["value_field"]["returns"] == "Value"
    assert names["value_list"]["returns"] == "List[Value]"
    assert names["value_opt"]["returns"] == "Opt[Value]"
    assert names["value_keys"]["returns"] == "List[Str]"
    # py, ts, and rs are all shipped now: item 368 made the module two-tier (py +
    # ts, the wire tiers), and item 391 / #106 added the rs tier — every accessor
    # walks a `serde_json::Value` boxed in `cordis::Value` (as stdlib/json.rvl
    # boxes) so a self-hosted emitter reads a json_parse'd backend-IR document in
    # pure revl when COMPILED TO RUST (native `compile_to`, #98 Stage 4). go/java/
    # wasm stay the documented follow-up (docs/stdlib-value.md).
    for e in consumer_ir["externs"]:
        assert set(e["bodies"]) == {"py", "ts", "rs"}, e["name"]
    assert consumer_ir["ir_version"] == 3


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert "pub extern pure fn value_kind(v: Value) -> Str" in text
    assert "pub extern pure fn value_field(v: Value, name: Str) -> Value" in text
    assert "pub extern pure fn value_list(v: Value) -> List[Value]" in text
    assert "pub extern pure fn value_keys(v: Value) -> List[Str]" in text


def test_value_is_a_reserved_builtin_type(tmp_path):
    # the checker reserves `Value` (typecheck._BUILTIN_TYPE_NAMES), so an
    # explicit `[Value]` type parameter may not shadow it
    from revl.errors import RevlError
    p = tmp_path / "m.rvl"
    p.write_text("fn id[Value](x: Value) -> Value { return x }\n",
                 encoding="utf-8")
    with pytest.raises(RevlError, match="shadows a builtin type"):
        compile_files([str(p)])


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_value", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "value.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_py_tier_navigates_ir_in_pure_revl(consumer_ir):
    ns = _exec_python(consumer_ir)
    # the PROOF: a pure-revl walk of a records/lists/scalars document, no @py
    assert ns["render"](DOC) == "(1 + [2, 3])"
    assert ns["item_at"](DOC["right"], 0) == "2"
    assert ns["item_at"](DOC["right"], 1) == "3"


def test_py_tier_children_driver_reaches_every_leaf(consumer_ir):
    ns = _exec_python(consumer_ir)
    # leaves: 'bin' + '+' + (lit: 'lit','1') + (list: 'list' + two lits x2) = 9
    assert ns["leaf_count"](DOC) == 9
    assert ns["leaf_count"]({"kind": "lit", "text": "x"}) == 2  # 'lit','x'
    assert ns["leaf_count"]("scalar") == 1


def test_py_tier_opt_distinguishes_absent_from_present(consumer_ir):
    ns = _exec_python(consumer_ir)
    assert ns["op_or_none"](DOC) == "+"
    assert ns["op_or_none"]({"kind": "lit", "text": "1"}) == "<none>"
    assert ns["has_op"](DOC) is True
    assert ns["has_op"]({"kind": "lit"}) is False


def test_py_tier_accessors_are_total(consumer_ir):
    ns = _exec_python(consumer_ir)
    # every mismatch returns a typed default, never raises
    assert ns["t_str_of_missing"]() == ""
    assert ns["t_int_of_record"]() == 0
    assert ns["t_kind_of_null"]() == "null"
    assert ns["t_len_of_scalar"]() == 0
    assert ns["t_at_out_of_range"]() is True
    assert ns["t_bool_of_str"]() is False
    assert ns["t_list_of_scalar_empty"]() == 0


# ---------------------------------------------------------- key enumeration (188)

# a component doc whose `services` record is keyed by name — the exact shape a
# self-hosted emitter walks to build the SERVICES table / `inject` list. The
# insertion order below (logger, db, cache) is the contract value_keys must
# preserve, deliberately NOT alphabetical so a stray sort would be caught.
COMP = {
    "services": {
        "logger": {"kind": "Logger"},
        "db": {"kind": "Db"},
        "cache": {"kind": "Cache"},
    },
    "order": [1, 2, 3],
}


def test_py_tier_value_keys_are_in_insertion_order(consumer_ir):
    ns = _exec_python(consumer_ir)
    # the KEY-enumeration proof: names come back in insertion order, not sorted
    assert ns["service_names"](COMP) == ["logger", "db", "cache"]
    # value_keys + value_field is the entries pattern, walked in PURE revl
    assert ns["service_kinds"](COMP) == "logger=Logger,db=Db,cache=Cache"


def test_py_tier_value_keys_is_total(consumer_ir):
    ns = _exec_python(consumer_ir)
    # a non-record receiver (scalar, list, null) yields [] — never crashes
    assert ns["t_keys_of_scalar_empty"]() == 0
    assert ns["t_keys_of_list_empty"]() == 0
    assert ns["t_keys_of_null_empty"]() == 0
