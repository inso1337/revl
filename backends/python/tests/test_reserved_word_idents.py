"""Roadmap item 165: a valid revl identifier that collides with a *Python*
reserved word (`from`, `class`, `lambda`, `global`, …) no longer crashes at
emit — it is deterministically renamed (A3 append-`_`, the scheme
`src/revl/lower.py::_safe_name` already uses for revl keywords) at the
declaration site AND every use site, so the emitted module compiles and RUNS.

`from` is a perfectly legal revl identifier — it is not a revl keyword — so
`fn f(from: Str) -> Str { return from }` type-checks and lowers, then used to
die in this backend with `parameter name 'from' is not a usable Python
identifier`. The collision is target-specific and surfaced late; the fix mangles
it uniformly in `_ident`/`_mangle`.

These tests EXEC the emitted module (not merely assert emit does not raise) and
call the emitted functions, proving the decl-site and use-site renames agree.
"""

from __future__ import annotations

import sys
import types

import pytest
from revl import compile_source

import emit


def _emit(src: str):
    return emit.emit(compile_source(src))


def load_module(source: str, name: str = "revl_reserved_word_mod") -> types.ModuleType:
    """Exec emitted source as a real, registered module. Registration in
    ``sys.modules`` is what lets an emitted ``@dataclass`` resolve its own
    module — the conftest helper skips it (its scenarios declare no types)."""
    module = types.ModuleType(name)
    module.__dict__["__name__"] = name
    sys.modules[name] = module
    try:
        exec(compile(source, f"{name}.py", "exec"), module.__dict__)
    finally:
        pass
    return module


# a spread of Python reserved words that are NOT revl keywords, so each is a
# legal revl identifier the frontend accepts and hands to this backend verbatim
PY_KEYWORDS = ["from", "class", "lambda", "global", "def", "import", "pass"]


@pytest.mark.parametrize("kw", PY_KEYWORDS)
def test_keyword_param_and_local_runs(kw):
    """A fn whose parameter and a local are both named after a Python keyword
    compiles, emits, execs, and returns the right value — the param's decl and
    the local's decl and their uses all rename consistently."""
    # param `kw`, local `kw2` (a second keyword), return the local
    other = next(k for k in PY_KEYWORDS if k != kw)
    src = f"""
fn echo({kw}: Str) -> Str {{
  let {other} = {kw}
  return {other}
}}
"""
    module = load_module(_emit(src))
    assert hasattr(module, "echo")
    assert module.echo("payload") == "payload"


def test_keyword_field_record_runs():
    """A record type with a keyword-named field, constructed and read back in a
    fn body, round-trips — record values are dicts keyed by the source name, so
    the read returns the stored value."""
    src = """
type Box = { class: Str, from: Str }

fn unbox(b: Box) -> Str {
  return b.class
}

fn make(a: Str) -> Box {
  return { class: a, from: a }
}
"""
    module = load_module(_emit(src))
    box = module.make("hi")
    assert module.unbox(box) == "hi"


def test_repro_from_param():
    """The exact bug repro from the roadmap item."""
    module = load_module(_emit("fn f(from: Str) -> Str { return from }"))
    assert module.f("x") == "x"


def test_non_keyword_idents_unchanged():
    """Regression guard for the byte-identical promise: a non-keyword identifier
    is emitted verbatim — `_mangle` is the identity off the keyword set."""
    src = "fn g(value: Str) -> Str { let out = value\n  return out }"
    source = _emit(src)
    assert "def g(value):" in source
    assert "out = value" in source
    assert "value_" not in source and "out_" not in source


# --------------------------------------------------------------------------
# Injectivity: the rename must never merge two distinct revl identifiers.
#
# The item-165 rename was "append `_` while the name is a keyword". That is a
# pure function of the name, which is what the decl/use agreement needs, but it
# is NOT injective: `lambda` maps to `lambda_` and the equally legal revl
# identifier `lambda_` maps to itself, so both reach `lambda_`. On this tier
# that is SILENT — the second binding captures the first and the function
# returns the wrong value. These tests pin the injective rule.
# --------------------------------------------------------------------------

import keyword  # noqa: E402

ALL_PY_KEYWORDS = sorted(set(keyword.kwlist) | set(keyword.softkwlist))


def test_mangle_is_injective_over_the_keyword_ladder():
    """For every Python keyword `kw`, the whole `kw`/`kw_`/`kw__` ladder maps to
    distinct Python identifiers, and none of the images is itself a keyword."""
    seen: dict[str, str] = {}
    for kw in ALL_PY_KEYWORDS:
        for name in (kw, kw + "_", kw + "__", kw + "___"):
            out = emit._mangle(name)
            assert not keyword.iskeyword(out) and not keyword.issoftkeyword(out), (
                f"{name!r} mangled to the keyword {out!r}"
            )
            assert out not in seen, (
                f"{name!r} and {seen[out]!r} both mangle to {out!r}"
            )
            seen[out] = name


def test_mangle_is_injective_over_a_wide_identifier_sample():
    """The identity half of the map may not collide with the shifted half."""
    sample = [
        kw + tail
        for kw in ALL_PY_KEYWORDS
        for tail in ("", "_", "__")
    ] + ["value", "value_", "out", "out_", "_v", "_v_", "_", "__", "___",
         "lambda_x", "classic", "fromage"]
    images = [emit._mangle(n) for n in sample]
    assert len(set(images)) == len(set(sample))


def test_keyword_local_does_not_capture_its_underscore_twin():
    """The reported exploit: two distinct revl locals, `lambda` and `lambda_`.

    Before the fix the emitted body was two assignments to one name and `probe`
    returned 'SEKRIT-CANARY-416'. It must return the value revl bound to
    `lambda`."""
    src = """
fn probe() -> Str {
  let lambda = "PUBLIC-VALUE"
  let lambda_ = "SEKRIT-CANARY-416"
  return lambda
}
"""
    source = _emit(src)
    module = load_module(source, "revl_injective_local")
    assert module.probe() == "PUBLIC-VALUE"
    assert "lambda_ = 'PUBLIC-VALUE'" in source
    assert "lambda__ = 'SEKRIT-CANARY-416'" in source


def test_top_level_fn_pair_stays_two_definitions():
    """Two top-level fns whose names differ only by a trailing `_` stay two
    `def`s — the module's exported API keeps both, and neither overwrites the
    other."""
    src = """
pub fn lambda() -> Str { return "PUBLIC-VALUE" }
pub fn lambda_() -> Str { return "SEKRIT-CANARY-416" }
fn pick(flag: Bool) -> Str { if (flag) { return lambda() } return lambda_() }
"""
    source = _emit(src)
    assert source.count("def lambda_(") == 1
    assert source.count("def lambda__(") == 1
    module = load_module(source, "revl_injective_fn")
    assert module.pick(True) == "PUBLIC-VALUE"
    assert module.pick(False) == "SEKRIT-CANARY-416"


def test_record_field_pair_stays_two_dataclass_fields():
    """A record with `from` and `from_` keeps TWO fields on the emitted
    dataclass. Before the fix both annotations were `from_`, the second
    overwrote the first, and `dataclasses.fields()` reported one field for a
    two-field revl record while the record VALUE (a dict) still carried both
    raw keys."""
    import dataclasses

    src = """
type Transfer = { from: Str, from_: Str }

fn mk(a: Str, b: Str) -> Transfer {
  return { from: a, from_: b }
}
"""
    source = _emit(src)
    module = load_module(source, "revl_injective_record")
    names = [f.name for f in dataclasses.fields(module.Transfer)]
    assert names == ["from_", "from__"]
    # the runtime value keeps the RAW revl keys, unchanged by the rename
    assert module.mk("a", "b") == {"from": "a", "from_": "b"}


def test_ordinary_keyword_rename_is_unchanged():
    """False-positive guard: a keyword rename with no twin in the program is
    exactly what it always was — one `_`, not two."""
    src = "fn f(from: Str) -> Str { let class = from\n  return class }"
    source = _emit(src)
    assert "def f(from_):" in source
    assert "class_ = from_" in source
    assert "return class_" in source
    assert "from__" not in source and "class__" not in source


def test_non_keyword_underscore_names_are_untouched():
    """False-positive guard: `value_`/`out_` have no keyword root, so they are
    emitted byte-for-byte."""
    src = "fn g(value_: Str) -> Str { let out_ = value_\n  return out_ }"
    source = _emit(src)
    assert "def g(value_):" in source
    assert "out_ = value_" in source
    assert "value__" not in source and "out__" not in source


def test_underscore_wildcard_does_not_capture_its_twin():
    """A second live instance of the same defect, in the most idiomatic corner
    of the space: `_` is a Python SOFT keyword, so it escapes to `__`, and the
    equally legal revl binding `__` used to be left alone — both landed on `__`
    and `probe()` returned 'SEKRIT-CANARY-416'.

    This is why the ladder shift has to reach ALL-underscore names too: because
    `_` escapes, `__` must escape as well or the two collide. The nine emitted
    lines that move across the whole `.rvl` corpus are exactly this, a `let _ =`
    discard going from `__ =` to `___ =`."""
    src = """
pub fn probe() -> Str {
  let _ = "PUBLIC-VALUE"
  let __ = "SEKRIT-CANARY-416"
  return _
}
"""
    source = _emit(src)
    module = load_module(source, "revl_injective_underscore")
    assert module.probe() == "PUBLIC-VALUE"
    assert "__ = 'PUBLIC-VALUE'" in source
    assert "___ = 'SEKRIT-CANARY-416'" in source
