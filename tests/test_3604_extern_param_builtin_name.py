"""An extern parameter named like a bare-emitted Python builtin binds in the
verbatim `@py` body (#3604).

The py emitter renames user identifiers that collide with a builtin it emits
bare (`len(...)`, `max(...)` in emitted code), so a module `fn` parameter
`max` emits as `max_` at the declaration AND at every use — consistent. An
extern's `@py` body is host text copied VERBATIM, though: only the `def` line
went through the rename, so `extern pure fn bump(max: Int) = @py { return max
+ 1 }` emitted `def bump(max_): return max + 1` and the call read the Python
builtin `max`, failing with `TypeError: unsupported operand type(s)`.

The fix: an inline-body extern's parameters take the keyword-only rung
(`_mangle_kw`), not the builtin rung — nothing the emitter writes bare sits
inside that `def` (the injected secret/config locals are `_revl*` scaffolding,
and call sites pass arguments positionally), so the parameter may keep the
spelling the host body reads. A Python keyword still escapes, as before. The
`@py ref` thunk keeps the builtin rung, because its emitted thunk body is
emitter code that must not collide.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

EMIT = backend_emitter("python")


def _emitted(source: str) -> str:
    emitted = EMIT.emit(compile_source(source, "extern_3604.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def _run(source: str, fn: str, *args):
    ns: dict = {}
    exec(compile(_emitted(source), "<extern_3604>", "exec"), ns)
    return ns[fn](*args)


# ------------------------------------------------------------- run cases

def test_extern_param_named_max_binds_in_the_host_body():
    # the issue's minimal source; on the unfixed emitter the emitted
    # `def bump(max_)` leaves the body's `max` reading the builtin
    src = ("extern pure fn bump(max: Int) -> Int\n"
           "= @py {\n"
           "      return max + 1\n"
           "    }\n")
    assert "def bump(max):" in _emitted(src)
    assert _run(src, "bump", 1) == 2


def test_extern_param_named_len_binds_in_the_host_body():
    src = ("extern pure fn bump(len: Int) -> Int\n"
           "= @py {\n"
           "      return len + 1\n"
           "    }\n")
    assert "def bump(len):" in _emitted(src)
    assert _run(src, "bump", 41) == 42


def test_two_extern_params_stay_injective_on_the_new_rung():
    # `max` and `max_` are two distinct revl identifiers; both must bind their
    # own argument in the verbatim body
    src = ("extern pure fn add2(max: Int, max_: Int) -> Int\n"
           "= @py {\n"
           "      return max + max_\n"
           "    }\n")
    assert _run(src, "add2", 1, 10) == 11


def test_a_non_extern_fn_param_is_still_renamed_at_both_ends():
    # emitted code (not host text) keeps the builtin guard: the declaration
    # and every use rename together, and the fn still runs
    src = "pub fn bump(max: Int) -> Int { return max + 1 }\n"
    out = _emitted(src)
    assert "def bump(max_):" in out and "max_ + 1" in out
    assert _run(src, "bump", 1) == 2


def test_a_py_ref_thunk_keeps_the_builtin_rung():
    # the ref thunk's emitted body is emitter code, so its parameters still
    # lift off bare builtins (issue #2913's rung is unchanged there)
    assert EMIT._ident("max", "extern parameter name", temps=True) == "max_"
    # the inline-body rung: keyword-only, builtin spelling kept
    assert EMIT._ident("max", "extern parameter name", temps=False,
                       builtins=False) == "max"
    # a Python keyword still escapes on that rung, injectively
    assert EMIT._ident("class", "extern parameter name", temps=False,
                       builtins=False) == "class_"
    assert EMIT._ident("class_", "extern parameter name", temps=False,
                       builtins=False) == "class__"
