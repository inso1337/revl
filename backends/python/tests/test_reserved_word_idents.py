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

import keyword
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
    mangled = kw + "_" if keyword.iskeyword(kw) else kw
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
