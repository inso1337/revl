"""`show(n)` across every tier — the adjudication of #1850, pinned.

Issue #1850, "show() has no ts lowering (TS2339)", rests on a premise that does
not hold on main: that `show` type-checks as `Int -> Str` and only the
TypeScript tier has no lowering for it. `show` is declared nowhere in this
repository — no checker table, no stdlib file, no self-host file, no emitter —
so there is no tier that lowers it and a tier that does not, and no emitter
that could be the odd one out. The adjudication is recorded in
`docs/v2.0-roadmap.md` item 551: a user-defined `fn show(n: Int) -> Str` runs on
all six tiers, `n.to_str()` is the surface, and adding a free `show` builtin is
a language change the maintainer states is not planned.

That decision was prose, and prose is what lets the same report be filed twice.
These tests make it executable:

  - the issue's own program (`return show(n)`, no definition) is refused by the
    FRONTEND, before any tier is handed an IR, so no tier can be the odd one
    out — the shape the TS2339 claim needs;
  - `show` is absent from both builtin tables (the checker's and the one
    lowering dispatches on) and the method spelling's diagnostic names
    `to_str`, the alternative it should point at (docs/stdlib-2.0.md);
  - a program that uses `show(n)` — the repository's own convention, which
    tests/test_458_conversion_dispatch.py and
    tests/fixtures/emit_go_corpus/comp_provide_builtins.rvl already write —
    BUILDS and RUNS with the same output on every tier that can run it. The Int
    value worth pinning is 2**53 + 1: `Int` is a `bigint` on the ts tier, and a
    JS `Number` would silently render it wrong, which is the only way a "same
    output as python" claim about integer rendering can actually break;
  - and the provided-method shape (a `fn show(n) -> Str` inside a `provide`)
    emits on all six tiers, so a method named `show` is not a per-tier hazard
    either. No toolchain is needed for that one: every emitter is pure Python.

Every test here passes on the revision that closed the question. That is the
finding rather than an omission — a red test in this file means a TIER DIVERGED,
which is exactly what it exists to report. The one thing it cannot do is fail on
the base revision for the reason the issue gives: there is no missing lowering
to be missing, so no program calling `show(n)` compiles on base and not on ts.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.lower import _BUILTIN_METHODS  # noqa: E402
from revl.test import RUNNERS  # noqa: E402
from revl.typecheck import _BUILTIN_SIG  # noqa: E402


#: The issue's program, verbatim in shape: `show` is called and never defined.
ISSUE_PROGRAM = """
fn render(n: Int) -> Str { return show(n) }

test "the issue's program" { assert render(42) == "42" }
"""

#: The spelling the repository actually uses, and the one #1850 asks to be
#: proven cross-tier: a user-defined `show` that renders an Int the way `to_str`
#: does. `9007199254740993` is 2**53 + 1.
SHOW_PROGRAM = """
pub fn show(n: Int) -> Str { return n.to_str() }

test "show renders an Int the way to_str does" {
  assert show(0) == "0"
  assert show(7) == "7"
  assert show(-7) == "-7"
  assert show(42) == "42"
  assert show(1000000) == "1000000"
  assert show(9007199254740993) == "9007199254740993"
  assert show(5 - 12) == "-7"
  assert show(21 * 2) == "42"
}
"""

#: The shape a benchmark's call sites would have had: a component that both
#: provides a method named `show` and calls the module fn from another provide
#: body, with the name `show` bound at three depths at once.
SHOW_PROVIDED = """
fn show(n: Int) -> Str { return n.to_str() }

service Text { fn show(n: Int) -> Str  fn render(n: Int) -> Str }

component Renderer provides text: Text {
  provide text {
    fn show(n) { return n.to_str() }
    fn render(n) { return show(n) }
  }
}

test "a provide body may define and call a fn named show" {
  assert show(42) == "42"
}
"""

#: The tiers whose runtime already exists in the default suite.
TIERS = ("py", "ts", "go", "wasm")
#: cargo and javac make the default suite minutes rather than seconds.
SLOW_TIERS = ("rust", "java")
#: Emitter directory names — the compile floor for the provided-method shape.
EMITTER_TIERS = ("python", "typescript", "rust", "java", "wasm", "go")


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "show_cross_tier.rvl"))


def _emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"emit_1850_{tier}", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_issue_program_is_refused_by_the_frontend():
    """`show` is neither a builtin nor declared, so the refusal comes out of
    `compile_source` — before any emitter is handed an IR. That is why the
    report cannot be a per-tier lowering gap: a tier with "no lowering" would
    have to be the one that could not lower a construct the other five did, and
    here there is no construct to lower on any of them."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(ISSUE_PROGRAM, "issue_program.rvl")
    error = excinfo.value
    assert error.message == "`show` is not declared in this function"
    assert "G1" in (error.hint or "")


def test_show_is_absent_from_the_builtin_surface():
    """Both builtin tables are method-only surfaces, and neither names `show`:
    the checker's (`_BUILTIN_SIG`) and the one lowering dispatches on
    (`_BUILTIN_METHODS`). `to_str` is what renders an Int."""
    assert "show" not in _BUILTIN_SIG
    assert "show" not in _BUILTIN_METHODS
    assert "to_str" in _BUILTIN_SIG
    assert "to_str" in _BUILTIN_METHODS


def test_the_method_spelling_names_the_alternative():
    """`n.show()` is refused, and the diagnostic names the whole stdlib surface
    including `to_str`, so the issue's own suggested spelling still leads a
    reader to the construct that works."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(
            'fn render(n: Int) -> Str { return n.show() }\n'
            'test "m" { assert render(42) == "42" }\n',
            "method_show.rvl")
    message = str(excinfo.value)
    assert "no builtin method `show` on values" in message
    assert "to_str" in message
    assert "docs/stdlib-2.0.md" in message


@pytest.mark.parametrize("tier", TIERS)
def test_show_program_builds_and_runs_with_the_same_output(tier: str):
    """The cross-tier artifact #1850 asks for: one source using `show(n)`, built
    AND run per tier, with each tier independently asserting the same rendered
    strings. The assertions are the identity check — a tier rendering a value
    differently fails here rather than agreeing with itself."""
    status, message = _run(tier, SHOW_PROGRAM)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} did not run `show(n)`: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_show_program_builds_and_runs_with_the_same_output_slow(tier: str):
    status, message = _run(tier, SHOW_PROGRAM)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} did not run `show(n)`: {message}"


@pytest.mark.parametrize("tier", EMITTER_TIERS)
def test_a_method_named_show_emits_on_every_tier(tier: str):
    """The provided-method shape, which is where the report's call sites would
    have lived. No toolchain: a tier that could not express this is caught even
    where its compiler is absent."""
    _emitter(tier).emit(compile_source(SHOW_PROVIDED, "show_provided.rvl"))
