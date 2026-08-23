"""The Map VALUE type (docs/stdlib-2.0.md §Map): `Map.empty()` / `set` /
`lookup` / `has` — persistent, Str-keyed, structurally equal.

Guarantees under test:
  - `Map.empty()` types as `Map[Str, Never]` and flows into any
    `Map[Str, V]` (the bottom-type trick the untyped `[]` plays);
  - `set` is checked (key Str, value V) and returns the receiver's type;
  - `lookup` returns `Opt[V]`; `has` returns Bool;
  - the surface coexists with the HOST stub object: `let m = Map.new()`
    keeps its unchecked verb set in the same document, and the two method
    namespaces are disjoint BY CONSTRUCTION (pinned here as an invariant
    over both tables);
  - persistent value semantics on the emitted python tier: `set` copies,
    the receiver never mutates, and `==` on maps is order-independent.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import emit  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402
from revl.lower import _BUILTIN_METHODS  # noqa: E402
from revl.typecheck import _BUILTIN_SIG, _HOST_FAMILIES  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    return str(excinfo.value)


# ---- the namespace invariant -------------------------------------------------
#
# docs/stdlib-2.0.md promises the stdlib method table and the host-object
# family surfaces are collision-free by construction. That is now a *checked*
# claim: extending either table with a name from the other side fails here.

def test_value_and_host_method_namespaces_are_disjoint():
    host_verbs = set()
    for methods in _HOST_FAMILIES.values():
        host_verbs |= set(methods)
    assert host_verbs == {"open", "close", "query", "execute", "new",
                          "drop", "insert", "remove", "get", "run"}
    assert set(_BUILTIN_SIG).isdisjoint(host_verbs)
    assert set(_BUILTIN_METHODS).isdisjoint(host_verbs)


def test_map_surface_is_exactly_the_spec():
    assert _BUILTIN_SIG["set"] == ("Map", ["Str", "@elem"], "@self")
    assert _BUILTIN_SIG["lookup"] == ("Map", ["Str"], "Opt[@elem]")
    assert _BUILTIN_SIG["has"] == ("Map", ["Str"], "Bool")


# ---- typing ------------------------------------------------------------------

def test_empty_map_flows_into_any_typed_map():
    ir = compile_source(
        "fn newTable() -> Map[Str, Int] { return Map.empty() }\n"
        "fn alias() -> Map[Str, Str] { return Map.empty() }\n")
    assert len(ir["functions"]) == 2


def test_set_lookup_has_lower_to_ir_v3_with_maplit():
    ir = compile_source(
        'fn put(m: Map[Str, Int], k: Str) -> Map[Str, Int]'
        ' { return m.set(k, 1) }\n'
        'fn get(m: Map[Str, Int], k: Str) -> Int'
        ' { return m.lookup(k) ?? 0 }\n'
        'fn member(m: Map[Str, Int], k: Str) -> Bool { return m.has(k) }\n')
    body = ir["functions"][0]["body"]
    assert body[0]["expr"]["kind"] == "builtin"
    assert body[0]["expr"]["method"] == "set"


def test_lookup_returns_opt_and_feeds_the_defaulting_operator():
    ir = compile_source(
        'fn get(m: Map[Str, Int], k: Str) -> Int { return m.lookup(k) ?? 0 }\n')
    assert ir["functions"][0]["returns"] == "Int"


def test_set_rejects_a_non_str_key():
    err = _err("fn f(m: Map[Str, Int]) -> Map[Str, Int]"
               " { return m.set(1, 2) }")
    assert "builtin `set` argument expects `Str`, got `Int`" in err


def test_set_rejects_a_value_outside_V():
    err = _err('fn f(m: Map[Str, Int]) -> Map[Str, Int]'
               ' { return m.set("k", "v") }')
    assert "builtin `set` argument expects `Int`, got `Str`" in err


def test_unknown_method_on_a_map_value_is_refused():
    err = _err('fn f(m: Map[Str, Int]) -> Int'
               ' { return m.fetch("k") ?? 0 }')
    assert "no builtin method `fetch`" in err


def test_map_builtins_refuse_non_map_receivers():
    err = _err('fn f(s: Str) -> Bool { return s.has("x") }')
    assert "builtin `has` needs a Map receiver, got `Str`" in err


def test_set_arity_and_empty_args_are_refused():
    err = _err('fn f(m: Map[Str, Int]) -> Map[Str, Int]'
               ' { return m.set("k") }')
    assert "builtin `set` takes 2 argument(s), 1 given" in err
    err = _err("fn f() -> Map[Str, Int] { return Map.empty(1) }")
    assert "`Map.empty()` takes no arguments, 1 given" in err


def test_host_stub_object_coexists_with_the_value_type():
    """One document using BOTH Maps: the host stub keeps its verbs; the
    value type keeps its surface; neither leaks into the other."""
    ir = compile_source(
        "component C provides kv: KV {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide kv {\n"
        "    fn put(key: Str, value: Str) {\n"
        "      effect store.insert(key, value)\n"
        "      undo   store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n"
        "service KV { fn put(key: Str, value: Str) }\n"
        'fn fresh() -> Map[Str, Str] { return Map.empty().set("a", "b") }\n')
    assert ir["ir_version"] >= 3


# ---- emitted-python semantics ------------------------------------------------

def _compile_emit(source):
    ns = {}
    exec(compile(emit.emit(compile_source(source)), "emitted.py", "exec"), ns)
    return ns


def test_python_set_is_persistent_and_lookup_has_agree():
    ns = _compile_emit(
        'fn put(m: Map[Str, Int], k: Str, v: Int) -> Map[Str, Int]'
        ' { return m.set(k, v) }\n'
        'fn get(m: Map[Str, Int], k: Str) -> Int'
        ' { return m.lookup(k) ?? 0 - 1 }\n'
        'fn has(m: Map[Str, Int], k: Str) -> Bool { return m.has(k) }\n'
        'pub fn emptyT() -> Map[Str, Int] { return Map.empty() }\n')
    put, get, has, empty = ns["put"], ns["get"], ns["has"], ns["emptyT"]
    t = empty()
    t2 = put(t, "a", 1)
    # persistence: the receiver is untouched by set
    assert t == {} and t2 == {"a": 1}
    assert get(t2, "a") == 1
    assert get(t, "a") == -1          # absent -> the ?? fallback
    assert has(t2, "a") is True
    assert has(t, "a") is False
    # rebinding builds a table without aliasing earlier snapshots
    t3 = put(put(t2, "b", 2), "c", 3)
    assert t3 == {"a": 1, "b": 2, "c": 3} and t2 == {"a": 1}


def test_python_equality_on_maps_is_order_independent():
    ns = _compile_emit(
        'fn build(pairs: List[Str]) -> Map[Str, Int] {\n'
        '  var m = Map.empty()\n'
        '  var i = 0\n'
        '  while (i < pairs.length()) {\n'
        '    m = m.set(pairs[i], pairs[i].length())\n'
        '    i += 1\n'
        '  }\n'
        '  return m\n'
        '}\n')
    build = ns["build"]
    a = build(["a", "bb", "ccc"])
    b = build(["ccc", "bb", "a"])
    assert a == b            # same mapping, different insertion order


# ---- every hosted tier takes the surface; wasm refuses ------------------------

def _backend(tier: str):
    import importlib.util
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"map_{tier}_emit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAP_SURFACE = (
    'fn put(m: Map[Str, Int], k: Str) -> Map[Str, Int] { return m.set(k, 1) }\n'
    'fn get(m: Map[Str, Int], k: Str) -> Int { return m.lookup(k) ?? 0 }\n'
    'fn member(m: Map[Str, Int], k: Str) -> Bool { return m.has(k) }\n'
    'fn fresh() -> Map[Str, Int] { return Map.empty() }\n'
)


def test_the_surface_emits_on_every_hosted_tier():
    """One surface, five representations — the portability floor. The revl
    names never leak into any backend; each tier spells the operations with
    its own per-tier representation (docs/stdlib-2.0.md §Map)."""
    ir = compile_source(MAP_SURFACE)
    out = {tier: _backend(tier).emit(ir)
           for tier in ("python", "typescript", "go", "java", "rust")}
    for tier, code in out.items():
        assert "Map.empty" not in code and ".lookup(" not in code, tier
    # per-tier representations, spot-checked against the spec table
    assert "{**" in out["python"] and ".get(" in out["python"]      # dict, copy-on-write
    assert "new Map(" in out["typescript"]                          # built-in JS Map
    assert "revlMapSet" in out["go"] and "map[string]" in out["go"] # map[string]V + helpers
    assert "revlMapSet" in out["java"] and "HashMap<>" in out["java"]
    assert "HashMap::new()" in out["rust"] and ".cloned()" in out["rust"]


def test_go_refuses_an_unpinned_empty_map():
    """The documented Go limit (docs/stdlib-2.0.md §Map): Go infers composite
    literals positionally, never from later use, so `var m = Map.empty()`
    with no annotating flow refuses at emit time instead of emitting Go that
    does not compile."""
    ir = compile_source(
        'fn build(k: Str) -> Map[Str, Int] {\n'
        '  var m = Map.empty()\n'
        '  return m.set(k, 1)\n'
        '}\n')
    go = _backend("go")
    with pytest.raises(go.EmitError,
                       match="needs an expected Map type on this tier"):
        go.emit(ir)

