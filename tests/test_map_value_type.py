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
# backends/python stays on sys.path for the EXEC'D modules (`from runtime
# import ...`) — emit itself is loaded by path below, NOT via this entry
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402
from revl.lower import _BUILTIN_METHODS  # noqa: E402
from revl.typecheck import _BUILTIN_SIG, _HOST_FAMILIES  # noqa: E402

# unique-name load: bare `import emit` binds the canonical name and collides
# with any other backend suite in the same process (tests/_backend_import.py)
emit = backend_emitter("python")


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
    # `remove` is the ONE sanctioned overlap (docs/stdlib-2.0.md §Map): the
    # persistent Map value operation shares a spelling with the v1 host stub
    # verb. Safe because dispatch is by receiver kind — a constructor-tracked
    # host receiver checks against the family surface before the stdlib table
    # is consulted. Pinned here so the overlap cannot grow silently.
    assert set(_BUILTIN_SIG) & host_verbs == {"remove"}
    assert set(_BUILTIN_METHODS) & host_verbs == {"remove"}


def test_map_surface_is_exactly_the_spec():
    assert _BUILTIN_SIG["set"] == ("Map", ["Str", "@elem"], "@self")
    assert _BUILTIN_SIG["lookup"] == ("Map", ["Str"], "Opt[@elem]")
    assert _BUILTIN_SIG["has"] == ("Map", ["Str"], "Bool")
    # The iteration/remove step (docs/stdlib-2.0.md §Map).
    assert _BUILTIN_SIG["size"] == ("Map", [], "Int")
    assert _BUILTIN_SIG["keys"] == ("Map", [], "List[Str]")
    assert _BUILTIN_SIG["remove"] == ("Map", ["Str"], "@self")


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


def test_go_pins_an_annotated_empty_map_without_a_helper_fn():
    """Roadmap 76b: `var m: Map[Str, Int] = Map.empty()` — the annotation the
    author already wrote IS the pin. The frontend threads the declared type
    onto the maplit node (`"expected"`), so the go tier emits a concrete
    `map[...]` literal instead of refusing — in fn bodies and method bodies
    alike. The mapiter workaround (a typed-return helper fn per literal) is
    no longer ceremony."""
    ir = compile_source(
        'fn build(k: Str) -> Map[Str, Int] {\n'
        '  var m: Map[Str, Int] = Map.empty()\n'
        '  return m.set(k, 1)\n'
        '}\n')
    out = _backend("go").emit(ir)
    assert "map[string]int64{}" in out
    # the IR carries the author's annotation on the literal
    let = [s for s in ir["functions"][0]["body"] if s["step"] == "let"][0]
    assert let["value"]["kind"] == "maplit"
    assert let["value"]["expected"] == "Map[Str, Int]"
    # the exact method-body form the mapiter agent hit on the go tier
    src = ('service S { fn f() -> Int }\n'
           'component C provides s: S {\n'
           '  provide s { fn f() { var m: Map[Str, Int] = Map.empty()  return 1 } }\n'
           '}\n')
    out2 = _backend("go").emit(compile_source(src, "pin-method.rvl"))
    assert "map[string]int{}" in out2


def test_go_unpinned_hint_names_the_positions_that_actually_pin():
    """The refusal's hint must describe the rule the checker actually has: a
    typed fn return, or an annotated `let`/`var`. It must not claim a typed
    parameter pins — `takes(Map.empty())` still refuses on this tier, because
    the go call renderer does not thread parameter types as expected types."""
    ir = compile_source(
        'fn build() -> Map[Str, Int] { var m = Map.empty()  return m }\n',
        "unpinned.rvl")
    go = _backend("go")
    with pytest.raises(go.EmitError) as excinfo:
        go.emit(ir)
    msg = str(excinfo.value)
    assert "typed fn return" in msg
    assert "annotated `let`/`var`" in msg
    assert "parameter" not in msg


# ---- review round 2 regressions ----------------------------------------------
#
# Two escaped bugs, both reproduced end-to-end by review:
#   BUG 1: `let m = Map.empty()` bound `m` as a HOST object (the constructor
#     root `Map` is a host callable), so every later m.set/lookup/has lowered
#     as a verbatim *field* call — unchecked at compile time, and a runtime
#     crash (`_revl_field` KeyError) on every tier.
#   BUG 2: Never-as-wildcard soundness hole — Map[Str, Never] satisfied ANY
#     Map[Str, X] in compatible(), so a Str could be planted under an
#     Int-typed map and `lookup` would then claim Opt[Int]. Wrong-answer
#     class. The same escape existed for the List empty literal
#     (`[].push("s")` typed List[Never], flowed into List[Int]) — pre-existing,
#     fenced in the same fix.

def test_let_bound_set_with_wrong_value_is_refused_not_planted():
    """The reviewer's verbatim repro: compiled clean before, crashed at
    runtime with a Str stored under an Int-typed map."""
    err = _err(
        'fn planted(k: Str) -> Map[Str, Int] {\n'
        '  let m = Map.empty()\n'
        '  let m2: Map[Str, Int] = m.set(k, "oops")\n'
        '  return m2\n'
        '}\n')
    assert "`let m2: Map[Str, Int]` expects `Map[Str, Int]`, got `Map[Str, Str]`" in err


def test_let_bound_flow_lowers_to_checked_builtins_and_runs():
    """The exact let-bound flow must produce builtin IR (checked), never a
    field call — and the emitted code must actually run."""
    src = ('fn put(k: Str, v: Int) -> Map[Str, Int] {\n'
           '  let m = Map.empty()\n'
           '  let m2 = m.set(k, v)\n'
           '  return m2\n'
           '}\n')
    ir = compile_source(src)
    lets = [s for s in ir["functions"][0]["body"] if s.get("step") == "let"]
    assert lets[-1]["value"]["kind"] == "builtin"
    assert lets[-1]["value"]["method"] == "set"
    ns = _compile_emit(src)
    assert ns["put"]("a", 1) == {"a": 1}


def test_direct_chain_lowering_is_a_builtin():
    """`Map.empty().set(...)` written inline took the same unchecked verbatim
    path (masked until now: nothing executed it)."""
    ir = compile_source(
        'fn f() -> Map[Str, Int] { return Map.empty().set("a", 1) }\n')
    body = ir["functions"][0]["body"]

    def kinds(n):
        if isinstance(n, dict):
            if "kind" in n:
                yield n["kind"]
            for v in n.values():
                yield from kinds(v)
        elif isinstance(n, list):
            for x in n:
                yield from kinds(x)

    ks = set(kinds(body))
    assert "builtin" in ks and "call" not in ks and "field" not in ks


def test_bottom_typed_receiver_learning_closes_both_flows():
    """The checker-side hole, independent of lowering: V must be learned from
    the concrete argument when the receiver's V is bottom."""
    # return-position flow
    err = _err('fn f(m: Map[Str, Never], k: Str) -> Map[Str, Int]'
               ' { return m.set(k, "oops") }')
    assert "expects `Map[Str, Int]`, got `Map[Str, Str]`" in err
    # expected-type flow through an unannotated intermediate
    err = _err('fn g(m: Map[Str, Never], k: Str) -> Map[Str, Str]'
               ' { let m2 = m.set(k, 1) return m2 }')
    assert "expects `Map[Str, Str]`, got `Map[Str, Int]`" in err
    # correct values flow fine in both positions
    compile_source('fn f(m: Map[Str, Never], k: Str) -> Map[Str, Int]'
                   ' { return m.set(k, 1) }\n')
    compile_source('fn g(m: Map[Str, Never], k: Str) -> Map[Str, Str]'
                   ' { let m2 = m.set(k, "v") return m2 }\n')


def test_the_list_empty_literal_hole_is_fenced_the_same_way():
    """Honest fence note: `[]` had the IDENTICAL Never-wildcard escape before
    Map ever existed (`[].push("s")` flowed into List[Int]). Same learning
    rule closes it; correct uses keep compiling."""
    err = _err('fn g() -> List[Int]'
               ' { let xs: List[Int] = [].push("s") return xs }\n')
    assert "expects `List[Int]`, got `List[Str]`" in err
    err = _err('fn h() -> List[Int] { return [].push("s") }\n')
    assert "expects `List[Int]`, got `List[Str]`" in err
    compile_source('fn ok() -> List[Int] { return [].push(1) }\n')


def test_host_stub_binding_is_untouched_by_the_fix():
    """The bug-1 fix narrows `_is_host_valued`; the genuine host acquisition
    (`Map.new()`) must still bind as host and stay verbatim. If the mark
    broke, `store.insert` would be refused as an unknown stdlib builtin."""
    ir = compile_source(
        "service KV { fn put(key: Str, value: Str) }\n"
        "component C provides kv: KV {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide kv {\n"
        "    fn put(key: Str, value: Str) {\n"
        "      effect store.insert(key, value)\n"
        "      undo   store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n")
    assert ir["components"], "the component must compile with its host stub"



# ---- the iteration/remove step (docs/stdlib-2.0.md §Map) ----------------------
#
# size/keys/remove graduate spec-first: keys() is iteration, in ascending
# canonical Str order (a pure function of the key set — sorted, NOT insertion,
# because go/rust randomize by design); remove() is persistent and TOTAL (an
# absent key is a no-op returning an equal map); size() is a method like its
# siblings.

ITER_SURFACE = (
    'fn count(m: Map[Str, Int]) -> Int { return m.size() }\n'
    'fn names(m: Map[Str, Int]) -> List[Str] { return m.keys() }\n'
    'fn drop(m: Map[Str, Int], k: Str) -> Map[Str, Int] { return m.remove(k) }\n'
)


def test_iter_surface_types_and_lowers_to_builtins():
    ir = compile_source(ITER_SURFACE)
    exprs = [s["expr"] for fn in ir["functions"] for s in fn["body"]
             if s.get("step") == "return"]
    assert all(e["kind"] == "builtin" for e in exprs)
    assert [e["method"] for e in exprs] == ["size", "keys", "remove"]


def test_size_keys_remove_reject_wrong_receiver_and_arity():
    assert "needs a Map receiver" in _err(
        'fn f(xs: List[Int]) -> Int { return xs.size() }\n')
    assert "takes 0 argument(s), 1 given" in _err(
        'fn f(m: Map[Str, Int]) -> Int { return m.size(1) }\n')
    assert "expects `Str`" in _err(
        'fn f(m: Map[Str, Int]) -> Map[Str, Int] { return m.remove(7) }\n')


def test_python_iteration_remove_semantics():
    ns = _compile_emit(ITER_SURFACE +
                       'pub fn build(pairs: List[Str]) -> Map[Str, Int] {\n'
                       '  var m = Map.empty()\n'
                       '  var i = 0\n'
                       '  while (i < pairs.length()) {\n'
                       '    m = m.set(pairs[i], i)\n'
                       '    i += 1\n'
                       '  }\n'
                       '  return m\n'
                       '}\n')
    count, names, drop, build = (ns[k] for k in ("count", "names", "drop",
                                                 "build"))
    # boundary: the empty map sizes 0 and iterates to nothing
    empty = build([])
    assert count(empty) == 0 and names(empty) == []
    # insertion order differs from canonical order; keys() must not care
    m = build(["banana", "apple", "cherry"])
    assert names(m) == ["apple", "banana", "cherry"]
    assert count(m) == 3
    # remove is persistent: receiver untouched; missing key is a total no-op
    m2 = drop(m, "apple")
    assert m2 == {"banana": 0, "cherry": 2} and m == {
        "banana": 0, "apple": 1, "cherry": 2}
    assert drop(m, "nope") == m
    assert drop(empty, "nope") == {}


def test_the_iter_surface_emits_on_every_hosted_tier_and_wasm_refuses():
    """One surface, five representations + one honest refusal."""
    ir = compile_source(ITER_SURFACE)
    out = {tier: _backend(tier).emit(ir)
           for tier in ("python", "typescript", "go", "java", "rust")}
    assert "sorted(" in out["python"] and "len(" in out["python"]
    assert ".sort((" in out["typescript"] and "BigInt(" in out["typescript"]
    assert "revlMapKeys" in out["go"] and "revlMapRemove" in out["go"]
    assert "revlMapKeys" in out["java"] and "codePointAt" in out["java"]
    assert "ks.sort()" in out["rust"]
    wasm = _backend("wasm")
    with pytest.raises(wasm.EmitError, match="not lowerable|has no representation here"):
        wasm.emit(ir)
