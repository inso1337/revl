"""A module `fn` and an `extern fn` may not share a name (issue #1813).

Two plain `fn`s of one name were refused ("duplicate function `f`"), but an
`extern fn` beside a `fn` was not: both landed in the IR, and which one a call
reached was the backend's choice. The python tier emitted two `def f` and the
later one won. That is how a second `camel` in selfhost/emit_go.rvl compiled
silently while the emitted python called the first.

The refusal is in the duplicate-binding shape ("... is already declared ...",
G6), at whichever declaration comes second, and only for a program nothing
else refuses: every existing diagnostic keeps its place. The self-host gate refuses it with
the same sentence (tests/test_selfhost_lower.py REJECTED_PROGRAMS).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402

EXTERN = "extern pure fn f(x: Int) -> Int = @py { return x }\n"
FN = "fn f(x: Int) -> Int { return x + 1 }\n"


def _refusal(source: str) -> dict:
    with pytest.raises(RevlError) as caught:
        compile_source(source, "m.rvl")
    return classify(caught.value)


def test_the_issues_reproducer_is_refused_at_the_fn():
    d = _refusal(EXTERN + FN + "fn g() -> Int { return f(1) }\n")
    assert d["message"] == "`f` is already declared as an extern on line 1"
    assert (d["code"], d["category"], d["line"]) == ("G6", "binding", 2)


def test_an_extern_after_the_fn_is_refused_at_the_extern():
    d = _refusal(FN + "pub " + EXTERN)
    assert d["message"] == "`f` is already declared as a function on line 1"
    assert d["line"] == 2


def test_any_other_refusal_keeps_its_diagnostic():
    """Checked once the program is otherwise admitted, so a refusal the
    program already draws (here an undeclared `db`, G1) is the one reported,
    as it was before this rule existed."""
    source = ("service S { fn g() -> Int }\n"
              "component C provides s: S {\n"
              "  provide s { fn g() = db.get() }\n"
              "}\n" + EXTERN + FN)
    d = _refusal(source)
    assert (d["code"], d["line"]) == ("G1", 3)


def test_each_module_keeps_its_own_namespace(tmp_path):
    """The rule is per module, as the duplicate-function one is: a module's
    private names are its own (each is mangled per module in the IR), so an
    extern in one root file and a fn of the same name in another do not
    collide, and neither does a local fn shadowing an imported extern."""
    (tmp_path / "a.rvl").write_text(EXTERN, encoding="utf-8")
    (tmp_path / "b.rvl").write_text(FN, encoding="utf-8")
    ir = compile_files([str(tmp_path / "a.rvl"), str(tmp_path / "b.rvl")])
    assert len(ir["functions"]) == 1 and len(ir["externs"]) == 1
    (tmp_path / "lib.rvl").write_text("pub " + EXTERN, encoding="utf-8")
    (tmp_path / "main.rvl").write_text('use "lib.rvl" { f }\n' + FN, encoding="utf-8")
    compile_files([str(tmp_path / "main.rvl")])


def test_distinct_names_still_compile():
    ir = compile_source(EXTERN + FN.replace("fn f", "fn g") + "fn h() -> Int { return f(1) + g(1) }\n",
                        "m.rvl")
    assert {fn["name"] for fn in ir["functions"]} == {"g", "h"}
    assert [e["name"] for e in ir["externs"]] == ["f"]
