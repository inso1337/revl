"""A user name spelled like a python-emitter temporary keeps its value (#2913).

The python emitter binds names of its own inside emitted functions (the
bounded-arithmetic walrus `_bi`, the field-read `_fv`, ...). A user local of
the same spelling was emitted verbatim, so the two were one Python name and
the program returned a wrong value with nothing failing: `let _bi = 7` then
`return (x + 1) * _bi` answered 4 for `x = 1`.

The emitter now renames the USER identifier off every such name, the way it
already renames one that matches a bare-emitted builtin. The names live in two
tables in backends/python/emit.py, `_EMITTER_TEMPS` and `_EMITTER_TEMP_RE`;
this file reads both:

  * every fixed name and a sample of every pattern is renamed by `_mangle`,
    the rename stays injective, and none of them is renamed on the attribute
    (`_mangle_kw`) rung;
  * a program per temporary that a function body can reach runs on the python
    tier and gives the answer the program means (these fail on main);
  * every `_`-prefixed name the emitter binds itself across the emit_py corpus
    (and one `@py ref` thunk module, a shape no corpus document has) is in a
    table, derived from a user name, or internal to an emitter helper. That
    catches a new temporary only in a shape this scan emits;
  * host-spelled names keep their spelling: a ref symbol, an inline extern
    body's parameters; a `pub fn` spelled like a temporary is refused.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_files, compile_source  # noqa: E402

EMIT = backend_emitter("python")

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"

# The fixed names `_EMITTER_TEMPS` must hold, spelled out so the file still
# collects (and fails by name) on an emitter that has no table.
FIXED = ("_bi", "_fv", "_rc", "_f", "_r", "_inspect", "_body", "_run")

# One sample per pattern alternative in `_EMITTER_TEMP_RE`.
PATTERN_SAMPLES = (
    "_ov0", "_ov12", "_effect_3", "_timer_1", "_timer_2_h", "_timer_1_inflight",
    "_timer_4_cancel", "_revl_i", "_revl_i64", "_revl_div", "_REVL_I64_MAX",
    "__revl_destructure_0", "_RevlThing", "_COUNTER_CONFIG", "_HELLO_CONFIG",
)


def _emitted(source: str) -> str:
    emitted = backend_emitter("python").emit(compile_source(source, "temps_2913.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def _run(source: str, fn: str, *args):
    ns: dict = {}
    exec(compile(_emitted(source), "<temps_2913>", "exec"), ns)
    return ns[fn](*args)


# ------------------------------------------------------------------ tables

def test_every_temporary_is_in_one_table():
    assert set(EMIT._EMITTER_TEMPS) == set(FIXED)
    for name in FIXED + PATTERN_SAMPLES:
        assert EMIT._emitter_temp(name), name


@pytest.mark.parametrize("name", FIXED + PATTERN_SAMPLES)
def test_mangle_lifts_a_user_name_off_each_temporary(name):
    renamed = EMIT._mangle(name)
    assert renamed == name + "_"
    assert renamed != name
    # one rung up the ladder, so `x` and `x_` stay distinct
    assert EMIT._mangle(name + "_") == name + "__"
    # attribute / field rung is untouched (it cannot share a scope with a temp)
    assert EMIT._mangle_kw(name) == name


def test_a_name_that_is_no_temporary_is_unchanged():
    for name in ("_v", "_x", "bi", "_bix", "_ov", "_ovx", "_effect", "_timer_x",
                 "revl", "my_revl", "_body2", "_fvx"):
        assert EMIT._mangle(name) == name, name


# --------------------------------------------------------------- run cases

CASES = {
    # bounded arithmetic `_bi :=`
    "_bi": ("pub fn f(x: Int) -> Int { let _bi = 7\nlet a = x + 1\nreturn a * _bi }",
            "f", (1,), 14),
    # field read `_fv :=` on a receiver that is not a bare name
    "_fv": ("type P = { a: Int }\nfn mk(x: Int) -> P { return { a: x } }\n"
            "pub fn f(x: Int) -> Int { let _fv = 7\nreturn mk(x).a + _fv }",
            "f", (1,), 8),
    # char class `_rc :=`
    "_rc": ('pub fn f(s: Str) -> Str { let _rc = "keep"\n'
            'if (s.charAt(0).is_alpha() && _rc == "keep") { return _rc }\nreturn "no" }',
            "f", ("a",), "keep"),
    # `??` chain temps `_ov<N>`
    "_ov0": ("fn pick(x: Int) -> Int? { if (x > 0) { return Some(x) }\nreturn None }\n"
             "pub fn f(x: Int) -> Int { let _ov0 = 7\nlet _ov1 = 5\n"
             "return (pick(x) ?? 0) + _ov0 + _ov1 }",
             "f", (1,), 13),
    # a `_revl*` helper the module calls by name
    "_revl_rem": ("pub fn f(x: Int) -> Int { let _revl_rem = 7\nreturn x % 3 + _revl_rem }",
                  "f", (4,), 8),
    # the list-index temp of #2902 (passes on a tree that has no such temp yet)
    "_revl_i": ("pub fn f(xs: List[Int], i: Int) -> Int { let _revl_i = 7\n"
                "return xs[i + 0] + _revl_i }",
                "f", ([10, 20], 1), 27),
    # the true-division helper `/` on two Ints calls
    "_revl_div": ("pub fn f(x: Int) -> Float { let _revl_div = 0.5\n"
                  "return x / 2 + _revl_div }",
                  "f", (3,), 2.0),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_user_local_named_like_a_temporary_keeps_its_value(name):
    source, fn, args, expected = CASES[name]
    assert _run(source, fn, *args) == expected


@pytest.mark.parametrize("param", ["_f", "_r"])
def test_ref_extern_param_named_like_its_thunk_temporary(tmp_path, monkeypatch, param):
    """The `@py ref` thunk binds `_f` (the symbol) and `_r` (its result) next to
    the extern's own parameters; a parameter named `_f` was overwritten before
    the call. `_r` is bound after the parameter's last read, so its case passes
    either way and stays as a guard."""
    pkg = tmp_path / f"host2913{param.strip('_')}"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / f"eng{param.strip('_')}.py").write_text("def twice(v):\n    return v * 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    rvl = tmp_path / "m.rvl"
    rvl.write_text(
        f"extern pure fn twice({param}: Int) -> Int\n"
        f'    = @py ref twice from "host2913{param.strip("_")}/eng{param.strip("_")}.py"\n'
        f"pub fn f(x: Int) -> Int {{ return twice(x) }}\n")
    ir = compile_files([str(rvl)])
    ns: dict = {}
    exec(compile(backend_emitter("python").emit(ir), "<ref_2913>", "exec"), ns)
    assert ns["f"](21) == 42


def test_top_level_fn_named_like_the_inspect_import(tmp_path, monkeypatch):
    """`import inspect as _inspect` sits at module scope when a ref extern is
    present; a user fn `_inspect` replaced it, and the thunk's coroutine check
    then called the user fn."""
    pkg = tmp_path / "host2913i"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "eng.py").write_text("def twice(v):\n    return v * 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    rvl = tmp_path / "m.rvl"
    rvl.write_text(
        "extern pure fn twice(v: Int) -> Int\n"
        '    = @py ref twice from "host2913i/eng.py"\n'
        "fn _inspect(v: Int) -> Int { return v }\n"
        "pub fn f(x: Int) -> Int { return twice(_inspect(x)) }\n")
    ir = compile_files([str(rvl)])
    ns: dict = {}
    exec(compile(backend_emitter("python").emit(ir), "<ref_2913i>", "exec"), ns)
    assert ns["f"](21) == 42


# ----------------------------------------------- nothing outside the tables

# `_`-prefixed names the emitter binds that are not temporaries: dunders,
# python's own `_` wildcard, and names DERIVED from a user name (a component's
# `_<snake>_apply`, a provision class `_<Pascal>`), which cannot equal a user identifier's spelling of a
# temporary because they carry the user's own name.
_DERIVED = re.compile(r"__\w+__\Z|_\Z|_[a-z0-9_]+_apply\Z|_[A-Z][A-Za-z0-9]*\Z")


def _bound_names(text: str) -> set:
    names = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
    return {n for n in names if n.startswith("_")}


def test_every_underscore_name_the_emitter_binds_is_in_a_table():
    seen: dict = {}
    for path in sorted(CORPUS_DIR.glob("*.rvl")):
        try:
            emitted = backend_emitter("python").emit(compile_files([str(path)]))
        except Exception:  # noqa: BLE001 - refusal corpora are not this test's business
            continue
        texts = emitted.values() if isinstance(emitted, dict) else [emitted]
        for text in texts:
            if isinstance(text, str):
                for name in _bound_names(text):
                    seen.setdefault(name, path.name)
    assert seen, "the corpus emitted nothing"
    # The corpus has no user identifier spelled like a temporary except the
    # one document that exists to have them; skip the names it renamed.
    temp = getattr(EMIT, "_emitter_temp", lambda _n: False)
    loose = {n: f for n, f in seen.items()
             if not temp(n.rstrip("_")) and not _DERIVED.match(n)
             and n not in _EMITTER_INTERNAL}
    assert not loose, f"emitter-bound names in no table: {loose}"


# Names bound only inside an emitted helper whose body holds no user code (the
# extern-config reader, the async adapter, a fault-test harness): they cannot
# share a scope with a user identifier.
_EMITTER_INTERNAL = frozenset({
    "_name", "_required", "_defaults", "_cfg", "_missing", "_a", "_k", "_g",
})


def test_the_temp_names_corpus_document_runs():
    """`user_names_like_temps.rvl` is in the self-host byte-identity corpus, so the
    self-hosted emitter renames the same names the same way; here it runs."""
    ir = compile_files([str(CORPUS_DIR / "user_names_like_temps.rvl")])
    ns: dict = {}
    exec(compile(backend_emitter("python").emit(ir), "<corpus_2913>", "exec"), ns)
    assert ns["temps"](1) == 14 + 8 + 7 + 7


# ------------------------------------------- host-spelled names keep theirs

def _ref_module(tmp_path, monkeypatch, pkg: str, symbol: str, extra: str = ""):
    host = tmp_path / pkg
    host.mkdir()
    (host / "__init__.py").write_text("")
    (host / "engine.py").write_text(f"def {symbol}(x):\n    return x + '!'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    rvl = tmp_path / "m.rvl"
    rvl.write_text(
        "extern pure fn eng(x: Str) -> Str\n"
        f'    = @py ref {symbol} from "{pkg}/engine.py"\n'
        f"pub fn f(x: Str) -> Str {{ return eng(x) }}\n{extra}")
    return backend_emitter("python").emit(compile_files([str(rvl)]))


@pytest.mark.parametrize("symbol", ["_run", "_f", "_r", "_bi", "_body", "_inspect"])
def test_ref_symbol_is_imported_under_its_host_spelling(tmp_path, monkeypatch, symbol):
    """A `@py ref` symbol is a host attribute name: it is imported as spelled
    (under the thunk's `_f` alias), never lifted off a temporary."""
    text = _ref_module(tmp_path, monkeypatch, f"host2913s{symbol.strip('_')}", symbol)
    assert f" import {symbol} as _f" in text
    ns: dict = {}
    exec(compile(text, "<ref_symbol_2913>", "exec"), ns)
    assert ns["f"]("a") == "a!"


@pytest.mark.parametrize("param", ["_f", "_r", "_run", "_bi", "_body"])
def test_inline_extern_body_keeps_its_parameter_names(param):
    """An inline `@py { ... }` body is host code naming its parameters: they
    keep their spelling (only the ref thunk binds temporaries beside them)."""
    source = (f"extern pure fn g({param}: Int) -> Int = @py {{ return {param} + 1 }}\n"
              "pub fn f(x: Int) -> Int { return g(x) }\n")
    assert _run(source, "f", 1) == 2


@pytest.mark.parametrize("name", ["_run", "_bi", "_bi_", "_ov3", "_COUNTER_CONFIG"])
def test_pub_fn_spelled_like_a_temporary_is_refused_by_name(name):
    """A host calls a `pub fn` by its own spelling, so it is refused rather
    than renamed; the diagnostic names the fn and the temporary."""
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(f"pub fn {name}(x: Int) -> Int {{ return x }}\n")
    message = str(err.value)
    assert f"pub fn {name!r}" in message
    assert f"temporary {name.rstrip('_')!r}" in message


def test_private_fn_spelled_like_a_temporary_is_renamed_and_runs():
    source = ("fn _run(x: Int) -> Int { return x + 1 }\n"
              "pub fn f(x: Int) -> Int { return _run(x) }\n")
    text = _emitted(source)
    assert "def _run_(" in text
    assert _run(source, "f", 1) == 2


def test_pub_fn_spelled_like_a_builtin_is_still_renamed():
    """The builtin rung is unchanged (found, not fixed here: a host calling
    `pub fn len` by name does not find it either)."""
    assert "def len_(" in _emitted("pub fn len(x: Int) -> Int { return x }\n")


def test_fn_spelled_like_a_component_config_table_is_renamed():
    """`_<NAME>_CONFIG` is the module-scope config schema a component with a
    config emits; a top-level fn of that spelling used to be a second binding
    of the same module name."""
    source = ((CORPUS_DIR / "keyword_config_value_field.rvl").read_text(encoding="utf-8")
              + "\nfn _HELLO_CONFIG(x: Int) -> Int { return x }\n"
              "pub fn probe2913(x: Int) -> Int { return _HELLO_CONFIG(x) }\n")
    text = _emitted(source)
    module_bindings = []
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_bindings.append(node.name)
        elif isinstance(node, ast.Assign):
            module_bindings += [t.id for t in node.targets if isinstance(t, ast.Name)]
    assert module_bindings.count("_HELLO_CONFIG") == 1
    assert "_HELLO_CONFIG_" in module_bindings


def test_scan_sees_the_ref_thunk_temporaries(tmp_path, monkeypatch):
    """No emit_py corpus document has a `@py ref` (the self-hosted emitter
    does not emit one), so the corpus scan cannot see the thunk's `_f`/`_r`;
    this runs the same check over a ref-thunk module."""
    text = _ref_module(tmp_path, monkeypatch, "host2913scan", "engine_fn",
                       extra="fn helper(_f: Int, _r: Int) -> Int { return _f + _r }\n")
    bound = _bound_names(text)
    assert {"_f", "_r", "_inspect"} <= bound
    temp = getattr(EMIT, "_emitter_temp", lambda _n: False)
    loose = {n for n in bound if not temp(n.rstrip("_")) and not _DERIVED.match(n)
             and n not in _EMITTER_INTERNAL}
    assert not loose, loose
