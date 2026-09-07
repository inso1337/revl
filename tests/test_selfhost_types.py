"""The self-hosted type-SPELLING algebra (selfhost/types.rvl), compiled by
revl, emitted through the python backend, executed, and cross-checked against
the reference algebra (src/revl/typecheck.py) on every generated type string.

This is a differential oracle in the exact shape of
tests/test_selfhost_checker.py, but for the AST-free half of the type layer
(docs/design/457-selfhost-type-layer.md, slice T1): parse_type / format_type,
structural_fields / format_structural, render_type, is_wildcard / is_poison,
is_tparam_name / mark_tparams / collect_tparams / validate_explicit_tparams,
compatible, join, widen_bottom, unify / substitute, and check_type_wellformed's
message shapes. Two independent implementations of one string algebra are
forced to agree on a fixed corpus AND a random fuzz draw, so a disagreement is
always a real defect in one of them.

The comparison is over the `types=None` path (no declared-type table): the
structural-vs-NOMINAL branch of `compatible` needs that table and lands with
the checker/lower `use`-and-drop follow-up; structural-vs-structural is here.
"""

import importlib.util
import random
import sys
import types as pytypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl import typecheck as tc  # noqa: E402


# ---------------------------------------------------------------- harness

def _exec_emitted() -> dict:
    ir = compile_files([str(ROOT / "selfhost" / "types.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_types", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = pytypes.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_types.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns():
    return _exec_emitted()


# ---------------------------------------------------------------- reference shims
#
# Each mirrors one `o_*` entry point in selfhost/types.rvl, rendered in the
# same string vocabulary (lists cross the FFI as ";"-joined strings; "" is the
# reference's None; wellformed/validate refusals are spelled "err:<message>").

def _split_nl(s: str) -> list:
    return s.split(";") if s else []


def ref_parse(name: str) -> str:
    head, args = tc.parse_type(name or None)
    return ";".join([head or ""] + list(args))


def ref_format(head: str, args_nl: str) -> str:
    return tc.format_type(head or None, _split_nl(args_nl)) or ""


def ref_struct(name: str) -> str:
    sf = tc.structural_fields(name or None)
    return "!notrec" if sf is None else tc.format_structural(sf)


def ref_render(name: str) -> str:
    return tc.render_type(name or None) or ""


def ref_wildcard(name: str) -> bool:
    return tc._is_wildcard(name or None)


def ref_poison(name: str) -> bool:
    return tc.is_poison(name or None)


def ref_tparam_name(name: str, declared_nl: str) -> bool:
    return tc.is_tparam_name(name, set(_split_nl(declared_nl)))


def ref_mark(name: str, tparams_nl: str) -> str:
    return tc.mark_tparams(name or None, set(_split_nl(tparams_nl))) or ""


def ref_collect(names_nl: str, declared_nl: str, explicit_nl: str) -> str:
    got = tc.collect_tparams(_split_nl(names_nl), set(_split_nl(declared_nl)),
                             explicit=_split_nl(explicit_nl))
    return ";".join(sorted(got))


def ref_vet(names_nl: str, declared_nl: str) -> str:
    try:
        got = tc.validate_explicit_tparams(
            _split_nl(names_nl), set(_split_nl(declared_nl)), "diff.rvl", 1)
    except RevlError as e:
        return "err:" + e.message
    return "ok:" + ";".join(sorted(got))


def ref_wf(name: str, allow: bool) -> str:
    try:
        tc.check_type_wellformed("diff.rvl", 1, name or None,
                                 allow_async_param=allow)
    except RevlError as e:
        return "err:" + e.message
    return ""


def ref_compatible(e: str, a: str) -> bool:
    return tc.compatible(e or None, a or None, None)


def ref_join(a: str, b: str) -> str:
    return tc.join(a or None, b or None, None) or ""


def ref_widen(d: str, a: str) -> str:
    return tc.widen_bottom(d or None, a or None, None) or ""


def ref_unify_call(params_nl: str, actuals_nl: str, ret: str) -> str:
    params, actuals = _split_nl(params_nl), _split_nl(actuals_nl)
    subst: dict = {}
    ok = True
    for p, a in zip(params, actuals):
        # iterate every pair without short-circuit, matching o_unify_call.
        if not tc.unify(p or None, a or None, subst, None):
            ok = False
    if not ok:
        return "!conflict"
    return tc.substitute(ret or None, subst) or ""


# ---------------------------------------------------------------- corpus

# Broad, hand-picked type spellings across every shape the algebra reaches.
TYPES = [
    "", "Int", "Int32", "Float", "Str", "Bool", "Bytes", "Unit",
    "Any", "Never", "Value", "!poison",
    "Row", "Box", "T", "U", "?T", "?U", "?Elem",
    "Opt[Int]", "Opt[Str]", "Opt[?T]", "Opt[Opt[Int]]",
    "List[Int]", "List[Never]", "List[Str]", "List[?T]", "List[List[Int]]",
    "Map[Str, Int]", "Map[Str, Never]", "Map[Str, ?T]", "Map[?K, ?V]",
    "Result[Int, Str]", "Result[?T, Str]",
    "(Int) -> Str", "(Int, Str) -> Bool", "() -> Int",
    "(?T) -> ?T", "(Int) -> ((Str) -> Bool)", "(Int) -> Async[Str]",
    "Async[Str]", "Async[Int]",
    "{a: Int}", "{a: Int, b: Str}", "{b: Str, a: Int}", "{}",
    "{a: Int, h: Str}", "{h: Str, a: Int}", "{v: ?T}",
    "{p: {q: Int}}", "List[{a: Int}]", "Opt[{a: Int, b: Str}]",
]

# Structural spellings whose canonical (sorted, Any-defaulted) form is the point.
STRUCTS = [
    "{a: Int}", "{b: Str, a: Int}", "{h: Str, a: Int, z: Bool}", "{}",
    "{a: }", "{a: Int, }", "{v: List[Int]}", "{p: {q: Int}, a: Str}",
    "{a: Opt[Int], b: Map[Str, Int]}", "Int", "List[Int]", "not a record",
]

# Wellformedness: legal, and every malformed shape the reference refuses.
WF_TYPES = [
    "Int", "Opt[Int]", "List[Str]", "Map[Str, Int]", "Result[Int, Str]",
    "Opt", "List", "Map", "Result", "Map[Str]", "Result[Int]", "Opt[Int, Str]",
    "List[Int, Str]", "Map[Str, Int, Bool]",
    "(Int) -> Str", "(Int) -> Async[Str]", "Async[Str]", "Async[Int, Str]",
    "Async", "Opt[Async[Str]]", "List[Opt[Int]]", "Map[Str, List[Int]]",
    "Approval[C]", "(Str) -> Async[Async[Int]]", "((Int) -> Async[Str]) -> Int",
    "", "Row", "Box[Int]",
]

# Explicit-tparam lists: valid, duplicate, builtin shadow, declared shadow.
VET_CASES = [
    ("T", ""), ("T;U", ""), ("Elem", ""), ("T;U;V", ""),
    ("T;T", ""), ("Int", ""), ("Opt", ""), ("Value", ""),
    ("T;Int", ""), ("Row", "Row"), ("T;Row", "Row"), ("", ""),
    ("T;U;Row;V", "Row;Box"), ("Bytes", ""),
]

# collect_tparams: (names, declared, explicit).
COLLECT_CASES = [
    ("List[T]", "", ""), ("Map[K, V]", "", ""), ("(T) -> U", "", ""),
    ("List[Row]", "Row", ""), ("List[T];Opt[U]", "", ""),
    ("List[Elem]", "", "Elem"), ("List[T]", "", "Elem"),
    ("Opt[Opt[T]]", "", ""), ("Result[T, E]", "", ""),
    ("List[T];T", "T", ""), ("Map[Str, Int]", "", ""),
    ("List[E]", "", "Elem"), ("(A, B) -> C", "", ""),
]

MARK_CASES = [
    ("List[T]", "T"), ("Map[K, V]", "K;V"), ("(T) -> U", "T;U"),
    ("Opt[T]", "T"), ("List[T]", ""), ("Int", "T"),
    ("Result[T, E]", "T;E"), ("List[Row]", "T"), ("T", "T"),
    ("Map[Str, T]", "T"), ("(T, Int) -> T", "T"),
]


@pytest.mark.parametrize("name", TYPES)
def test_parse_agrees(ns, name):
    assert ns["o_parse"](name) == ref_parse(name), name


@pytest.mark.parametrize("name", TYPES)
def test_parse_format_roundtrip_agrees(ns, name):
    # format_type(head, args) of the parse must agree byte for byte.
    head, *args = ref_parse(name).split(";")
    args_nl = ";".join(args)
    assert ns["o_format"](head, args_nl) == ref_format(head, args_nl), name


@pytest.mark.parametrize("name", STRUCTS + TYPES)
def test_structural_agrees(ns, name):
    assert ns["o_struct"](name) == ref_struct(name), name


@pytest.mark.parametrize("name", TYPES)
def test_render_agrees(ns, name):
    assert ns["o_render"](name) == ref_render(name), name


@pytest.mark.parametrize("name", TYPES)
def test_wildcard_and_poison_agree(ns, name):
    assert bool(ns["o_wildcard"](name)) == ref_wildcard(name), name
    assert bool(ns["o_poison"](name)) == ref_poison(name), name


@pytest.mark.parametrize("name,tps", MARK_CASES)
def test_mark_agrees(ns, name, tps):
    assert ns["o_mark"](name, tps) == ref_mark(name, tps), (name, tps)


@pytest.mark.parametrize("names,declared,explicit", COLLECT_CASES)
def test_collect_agrees(ns, names, declared, explicit):
    assert ns["o_collect"](names, declared, explicit) == \
        ref_collect(names, declared, explicit), (names, declared, explicit)


@pytest.mark.parametrize("names,declared", VET_CASES)
def test_validate_explicit_tparams_agrees(ns, names, declared):
    assert ns["o_vet"](names, declared) == ref_vet(names, declared), \
        (names, declared)


@pytest.mark.parametrize("name", WF_TYPES)
@pytest.mark.parametrize("allow", [False, True])
def test_wellformed_message_agrees(ns, name, allow):
    assert ns["o_wf"](name, allow) == ref_wf(name, allow), (name, allow)


@pytest.mark.parametrize("e", TYPES)
def test_compatible_join_widen_agree_over_pairs(ns, e):
    # Every ordered pair (e, a) over the corpus for the value-flow relation and
    # its two derived operations.
    for a in TYPES:
        assert bool(ns["o_compatible"](e, a)) == ref_compatible(e, a), (e, a)
        assert ns["o_join"](e, a) == ref_join(e, a), (e, a)
        assert ns["o_widen"](e, a) == ref_widen(e, a), (e, a)


UNIFY_CASES = [
    ("?T", "Int", "?T"),
    ("?T;?T", "Int;Float", "?T"),
    ("?T;?T", "Int;Str", "?T"),
    ("List[?T]", "List[Int]", "?T"),
    ("Opt[?T]", "Int", "?T"),
    ("Map[?K, ?V]", "Map[Str, Int]", "Map[?K, ?V]"),
    ("?T", "Async[Str]", "?T"),
    ("?T;?U", "Int;Str", "(?T) -> ?U"),
    ("List[?T]", "List[Never]", "?T"),
    ("?T", "Any", "?T"),
    ("Result[?T, ?E]", "Result[Int, Str]", "Result[?T, ?E]"),
    ("Int", "Int", "?T"),
    ("Int", "Str", "?T"),
]


@pytest.mark.parametrize("params,actuals,ret", UNIFY_CASES)
def test_unify_call_agrees(ns, params, actuals, ret):
    assert ns["o_unify_call"](params, actuals, ret) == \
        ref_unify_call(params, actuals, ret), (params, actuals, ret)


# ---------------------------------------------------------------- fuzz

_LEAVES = ["Int", "Int32", "Float", "Str", "Bool", "Bytes", "Unit", "Any",
           "Never", "Value", "!poison", "Row", "Box", "T", "U", "E",
           "?T", "?U", "?E"]


def _gen(rng: random.Random, depth: int) -> str:
    if depth <= 0 or rng.random() < 0.4:
        return rng.choice(_LEAVES)
    kind = rng.choice(
        ["Opt", "List", "Map", "Result", "fn", "async", "struct"])
    g = lambda: _gen(rng, depth - 1)
    if kind == "Opt":
        return f"Opt[{g()}]"
    if kind == "List":
        return f"List[{g()}]"
    if kind == "Map":
        return f"Map[{g()}, {g()}]"
    if kind == "Result":
        return f"Result[{g()}, {g()}]"
    if kind == "async":
        return f"({g()}) -> Async[{g()}]"
    if kind == "fn":
        n = rng.randint(0, 2)
        params = ", ".join(g() for _ in range(n))
        return f"({params}) -> {g()}"
    keys = rng.sample(["a", "b", "c", "h", "v", "z"], rng.randint(1, 3))
    return "{" + ", ".join(f"{k}: {g()}" for k in keys) + "}"


def test_fuzz_spelling_algebra_agrees(ns):
    rng = random.Random(45718)
    for _ in range(1200):
        t = _gen(rng, rng.randint(0, 3))
        # single-string operations
        assert ns["o_parse"](t) == ref_parse(t), t
        assert ns["o_struct"](t) == ref_struct(t), t
        assert ns["o_render"](t) == ref_render(t), t
        assert bool(ns["o_wildcard"](t)) == ref_wildcard(t), t
        assert bool(ns["o_poison"](t)) == ref_poison(t), t
        assert ns["o_wf"](t, False) == ref_wf(t, False), t
        assert ns["o_wf"](t, True) == ref_wf(t, True), t
        head, *args = ref_parse(t).split(";")
        args_nl = ";".join(args)
        assert ns["o_format"](head, args_nl) == ref_format(head, args_nl), t
        # pair operations against a second random draw
        u = _gen(rng, rng.randint(0, 3))
        assert bool(ns["o_compatible"](t, u)) == ref_compatible(t, u), (t, u)
        assert ns["o_join"](t, u) == ref_join(t, u), (t, u)
        assert ns["o_widen"](t, u) == ref_widen(t, u), (t, u)
        # unify a marked param against the draw
        assert ns["o_unify_call"]("?T", t, "List[?T]") == \
            ref_unify_call("?T", t, "List[?T]"), t


def test_fuzz_marks_and_collect_agree(ns):
    rng = random.Random(90124)
    names = ["T", "U", "E", "K", "V", "Row", "Box"]
    for _ in range(400):
        t = _gen(rng, rng.randint(1, 3))
        tps = ";".join(rng.sample(names, rng.randint(0, 3)))
        assert ns["o_mark"](t, tps) == ref_mark(t, tps), (t, tps)
        declared = ";".join(rng.sample(names, rng.randint(0, 2)))
        explicit = ";".join(rng.sample(names, rng.randint(0, 2)))
        assert ns["o_collect"](t, declared, explicit) == \
            ref_collect(t, declared, explicit), (t, declared, explicit)
