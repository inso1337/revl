"""A bodied extern's parameters keep their source spelling on the go and java
tiers, and a name that cannot be kept is refused at emit (#3620).

The go and java emitters renamed a bodied extern's parameter for an
emitter-only reason (a predeclared builtin `max`/`len` in Go, a prelude class
`String`/`Math` in Java), but the `@go`/`@java` body is host text copied
verbatim, so the body kept reading the builtin/class instead of the argument —
the #3604 python case in go and java clothes. ts and rust already keep the
spelling.

The fix (mirrored in `selfhost/emit_go.rvl` so the byte-agreement oracle
stays exact): a bodied extern's parameter is emitted verbatim; a name that
CANNOT be kept — a target-language keyword, or an emitter scaffolding/helper
name the wrapper spells bare — is refused at emit naming the extern, the
parameter and the tier.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

GO = backend_emitter("go")
JAVA = backend_emitter("java")


def _emitted(backend, source: str) -> str:
    emitted = backend.emit(compile_source(source, "extern_3620.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


GO_BUMP = ("extern pure fn bump(max: Int) -> Int\n"
           "= @go { return max + 1 }\n")

JAVA_BUMP = ("extern pure fn bump(String: Int) -> Int\n"
             "= @java { return String + 1; }\n")


def test_go_extern_param_named_max_keeps_its_spelling():
    out = _emitted(GO, GO_BUMP)
    assert "func bump(max int64) int64 {" in out
    assert "\treturn max + 1" in out
    assert "max_" not in out


def test_go_extern_param_named_len_keeps_its_spelling():
    src = ("extern pure fn bump(len: Int) -> Int\n"
           "= @go { return len + 1 }\n")
    out = _emitted(GO, src)
    assert "func bump(len int64) int64 {" in out
    assert "len_" not in out


def test_go_two_extern_params_stay_distinct():
    src = ("extern pure fn add2(max: Int, max_: Int) -> Int\n"
           "= @go { return max + max_ }\n")
    out = _emitted(GO, src)
    assert "func add2(max int64, max_ int64) int64 {" in out


def test_go_extern_param_that_is_a_keyword_is_refused_at_emit():
    src = ("extern pure fn bump(func: Int) -> Int\n"
           "= @go { return func + 1 }\n")
    with pytest.raises(GO.EmitError, match=r"extern `bump` parameter 'func'.*go tier"):
        _emitted(GO, src)


def test_go_extern_param_in_the_helper_namespace_is_refused_at_emit():
    src = ("extern pure fn bump(revlEq: Int) -> Int\n"
           "= @go { return revlEq + 1 }\n")
    with pytest.raises(GO.EmitError, match=r"extern `bump` parameter 'revlEq'.*go tier"):
        _emitted(GO, src)


def test_java_extern_param_named_string_keeps_its_spelling():
    out = _emitted(JAVA, JAVA_BUMP)
    assert "public static long bump(long String) {" in out
    assert "return String + 1;" in out
    assert "String_" not in out


@pytest.mark.parametrize("name", ["Math", "Object", "Integer"])
def test_java_extern_param_named_like_a_prelude_class_keeps_its_spelling(name):
    src = (f"extern pure fn bump({name}: Int) -> Int\n"
           f"= @java {{ return {name} + 1; }}\n")
    out = _emitted(JAVA, src)
    assert f"public static long bump(long {name}) {{" in out
    assert f"{name}_" not in out


def test_java_extern_param_that_is_a_keyword_is_refused_at_emit():
    src = ("extern pure fn bump(class: Int) -> Int\n"
           "= @java { return class + 1; }\n")
    with pytest.raises(JAVA.EmitError, match=r"extern `bump` parameter 'class'.*java tier"):
        _emitted(JAVA, src)


def test_java_extern_param_in_the_helper_namespace_is_refused_at_emit():
    src = ("extern pure fn bump(revlEq: Int) -> Int\n"
           "= @java { return revlEq + 1; }\n")
    with pytest.raises(JAVA.EmitError, match=r"extern `bump` parameter 'revlEq'.*java tier"):
        _emitted(JAVA, src)


def test_a_non_extern_fn_param_is_still_renamed_at_both_ends():
    # emitted code (not host text) keeps the guard on both tiers
    out = _emitted(GO, "pub fn bump(max: Int) -> Int { return max + 1 }\n")
    assert "func bump(max_ int64) int64 {" in out and "revlAdd(max_, 1)" in out
    out = _emitted(JAVA, "pub fn bump(String: Int) -> Int { return String + 1 }\n")
    assert "String_" in out
