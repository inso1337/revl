"""An extern with an inline `@go`/`@java` body keeps its parameter spelling (#3620).

The body is host text copied verbatim, so it names its parameters itself. The
go emitter used to rename a parameter spelled like a Go predeclared identifier
(`max` -> `max_`) while the body still read `max`, which is the Go builtin;
the java emitter renamed one spelled like a `java.lang` type (`String` ->
`String_`) while the body still named the class. The parameters now keep
their spelling on both tiers, and a spelling the tier cannot bind as a
parameter (a language keyword, or one in the emitter's `revl*`/`_revl*`
helper namespace), is refused at emit naming the extern, the parameter and
the tier. A revl `fn`'s own parameters are still renamed at declaration and
every use. The `@go ref` thunk path is unchanged.
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


def _emitted(emitter, source: str) -> str:
    emitted = emitter.emit(compile_source(source, "extern_param_3620.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


# Go binds every predeclared identifier as a parameter, so all of these keep
# their spelling (`nil`/`true` included — verified against `go build`).
@pytest.mark.parametrize("param", ["max", "len", "min", "cap", "clear",
                                   "recover", "any", "error", "String"])
def test_go_bodied_extern_param_named_like_a_go_predeclared_keeps_spelling(param):
    source = f"extern pure fn bump({param}: Int) -> Int = @go {{ return {param} + 1 }}\n"
    text = _emitted(GO, source)
    assert f"func bump({param} int64) int64 {{" in text
    assert f"return {param} + 1" in text


def test_go_issue_example_max_emits_verbatim():
    source = "extern pure fn bump(max: Int) -> Int\n= @go {\n      return max + 1\n    }\n"
    assert "func bump(max int64) int64 {" in _emitted(GO, source)


def test_go_other_parameters_of_the_same_extern_keep_their_spelling():
    source = ("extern pure fn span(min: Int, max: Int, len: Int) -> Int\n"
              "= @go { return max - min + len }\n")
    text = _emitted(GO, source)
    assert "func span(min int64, max int64, len int64) int64 {" in text


def test_go_extern_call_through_a_revl_fn_keeps_the_forward_spelling():
    source = ("extern pure fn bump(max: Int) -> Int = @go { return max + 1 }\n"
              "pub fn f(x: Int) -> Int { return bump(x) }\n")
    text = _emitted(GO, source)
    assert "func bump(max int64) int64 {" in text
    assert "bump(x)" in text


# (the rest of Go's 25 keywords are revl keywords too, so the parser rejects
# them before emit — they cannot reach an emitter as an identifier)
GO_KEYWORDS = ["case", "chan", "const", "default", "defer", "fallthrough",
               "func", "go", "goto", "import", "interface", "map", "package",
               "range", "select", "struct", "switch"]


# The literals cannot keep their spelling either: the journalled-emission
# wrapper writes a bare `false` in the same scope, which a `false` parameter
# would shadow (and Go keywords/literals never bind as a parameter anyway).
@pytest.mark.parametrize("param", ["nil", "iota"])
def test_go_literal_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @go {{ return 1 }}\n"
    with pytest.raises(GO.EmitError) as err:
        _emitted(GO, source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"'{param}'" in message
    assert "go tier" in message


@pytest.mark.parametrize("param", GO_KEYWORDS)
def test_go_keyword_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @go {{ return 1 }}\n"
    with pytest.raises(GO.EmitError) as err:
        _emitted(GO, source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"'{param}'" in message
    assert "go tier" in message


@pytest.mark.parametrize("param", ["revlAdd", "revlSecretResult", "_revl_config",
                                   "_REVL_X", "REVL_CFG"])
def test_go_helper_namespace_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @go {{ return 1 }}\n"
    with pytest.raises(GO.EmitError) as err:
        _emitted(GO, source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"'{param}'" in message
    assert "go tier" in message


def test_go_revl_fn_parameter_named_max_is_still_renamed():
    """Unchanged: a revl body is renamed at its declaration and every use."""
    source = "pub fn bump(max: Int) -> Int { return max + 1 }\n"
    assert "func bump(max_ int64) int64 {" in _emitted(GO, source)


# `java.lang` type names and emitter scaffolding locals bind fine as Java
# parameters (they shadow only inside the host body), so they keep spelling.
@pytest.mark.parametrize("param", ["String", "Math", "Object", "Integer", "Long",
                                   "System", "Thread", "ctx", "Components"])
def test_java_bodied_extern_param_named_like_a_java_lang_type_keeps_spelling(param):
    source = (f"extern pure fn bump({param}: Int) -> Int\n"
              f"= @java {{ return {param} + 1; }}\n")
    text = _emitted(JAVA, source)
    assert f"public static long bump(long {param}) {{" in text
    assert f"return {param} + 1;" in text


def test_java_issue_example_string_emits_verbatim():
    source = 'extern pure fn bump(String: Int) -> Int\n= @java { return String + 1; }\n'
    assert "public static long bump(long String) {" in _emitted(JAVA, source)


@pytest.mark.parametrize("param", ["class", "new", "int", "long", "void",
                                   "this", "super", "record", "yield", "_"])
def test_java_keyword_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @java {{ return 1; }}\n"
    with pytest.raises(JAVA.EmitError) as err:
        _emitted(JAVA, source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"'{param}'" in message
    assert "java tier" in message


@pytest.mark.parametrize("param", ["revlEq", "revlAdd", "_revl_config", "_revlEm"])
def test_java_helper_namespace_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @java {{ return 1; }}\n"
    with pytest.raises(JAVA.EmitError) as err:
        _emitted(JAVA, source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"'{param}'" in message
    assert "java tier" in message


def test_java_revl_fn_parameter_named_string_is_still_renamed():
    """Unchanged: a revl body is renamed at its declaration and every use."""
    source = "pub fn bump(String: Int) -> Int { return String + 1 }\n"
    text = _emitted(JAVA, source)
    assert "bump(long String__)" in text
    assert "public static long bump(long String)" not in text
