"""One code, several failure modes: the remedy is chosen by mode, not by code.

`G1` (declared access) is refused for four different mistakes, and the per-code
row in `diagnostics.FIXES` is the rewrite for only one of them — a read of a key
the component does not require. Selecting it by code alone handed the other
three the advice to add a key to `requires`, so an undeclared LOCAL variable
(issue #2029's measured case, the residual of #1652) was told to "add the key to
the component's `requires` clause, or drop the access" — advice that contradicts
the `hint` sitting on the same record.

The raise site knows which mode it is refusing, so the mode is the lookup key
(`diagnostics.FIXES_BY_CATEGORY`, keyed `(code, category)`); the per-code row
stays as the fallback for a code with one mode. These tests pin all four modes,
including the one the per-code row was written for, so the split cannot be
"fixed" by swapping the blunt remedy for another blunt remedy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import diagnostics  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import FIXES, GUARANTEES, classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# read through `getattr` so this module still imports on a tree that predates
# the mode table: the mode tests below must fail on the old behaviour (a wrong
# remedy in the record), not on an import error.
FIXES_BY_CATEGORY = getattr(diagnostics, "FIXES_BY_CATEGORY", {})
GUARANTEES_BY_CATEGORY = getattr(diagnostics, "GUARANTEES_BY_CATEGORY", {})

SERVICE = "service Kv { fn get(k: Str) -> Opt[Str] }\n"

# each G1 mode: the program, the category its raise site names, phrases its
# remedy must carry, and phrases it must NOT carry (the wrong mode's advice).
MODES: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    # a local read before its declaration (lower.py, the `ExprVar` and
    # `AssignStmt` paths)
    "local": (
        "fn f() -> Int {\n  return missing\n}\n",
        "binding",
        ("`let`", "`var`"),
        ("requires",),
    ),
    # a read of a key the component does not require (`intercept`, and the
    # plain-body requirement lookups) — the mode FIXES["G1"] was written for
    "requirement": (
        SERVICE + "component Watcher requires kv: Kv {\n"
                  "  intercept db with { quota: 5 }\n"
                  "  let probe = effect kv.get(\"boot\") undo probe.drop()\n"
                  "}\n",
        "requirement",
        ("`requires`",),
        ("`let`",),
    ),
    # `Delegate[X]` naming something that is not a declared service
    "delegation": (
        "service S { fn f(d: Delegate[Nope]) -> Int }\n",
        "delegation",
        ("`service`",),
        ("requires",),
    ),
    # a requirement key that shadows a builtin type or a host root
    "wiring": (
        SERVICE + "component C requires List: Kv {\n"
                  "  provide x { fn f() -> Int { return 1 } }\n"
                  "}\n",
        "wiring",
        ("rename",),
        ("requires",),
    ),
}


def _record(src: str) -> dict:
    with pytest.raises(RevlError) as info:
        compile_source(src, "app.rvl")
    return classify(info.value)


@pytest.mark.parametrize("mode", sorted(MODES))
def test_each_g1_mode_is_named_by_its_own_category(mode):
    src, category, _, _ = MODES[mode]
    record = _record(src)
    assert record["code"] == "G1"
    assert record["category"] == category


@pytest.mark.parametrize("mode", sorted(MODES))
def test_each_g1_mode_gets_the_remedy_for_that_mode(mode):
    src, _, wanted, unwanted = MODES[mode]
    fix = _record(src)["fix"]
    for phrase in wanted:
        assert phrase in fix, f"{mode}: {fix!r} lacks {phrase!r}"
    for phrase in unwanted:
        assert phrase not in fix, f"{mode}: {fix!r} carries {phrase!r}"


def test_the_undeclared_local_is_not_told_to_add_a_require():
    """The measured defect. An undeclared local variable is not a requirement
    and not a component, so the per-code remedy is the rewrite for a different
    mistake — and its guarantee ("a component reads only what it requires")
    misdescribes it too."""
    record = _record(MODES["local"][0])
    assert record["fix"] != FIXES["G1"]
    assert "requires" not in record["fix"]
    assert "`let`" in record["fix"]
    assert record["guarantee"] != GUARANTEES["G1"]


def test_the_undeclared_requirement_still_gets_the_requires_remedy():
    """The mode the per-code row was written for. It keeps that row, so the
    split is not a blunt swap from one wrong remedy to another."""
    record = _record(MODES["requirement"][0])
    assert record["fix"] == FIXES["G1"]
    assert record["guarantee"] == GUARANTEES["G1"]


def test_no_two_g1_modes_share_a_remedy():
    """The whole point: four modes, four rewrites. A table that maps two modes
    to the same string has re-merged them."""
    fixes = {mode: _record(src)["fix"] for mode, (src, *_) in MODES.items()}
    assert len(set(fixes.values())) == len(MODES), fixes


def test_every_mode_row_names_a_code_revl_explains():
    """A row keyed by a code no one can look up is a typo that silently never
    fires; and a remedy must be a non-empty string."""
    for table in (FIXES_BY_CATEGORY, GUARANTEES_BY_CATEGORY):
        for (code, category), text in table.items():
            assert code in GUARANTEES, (code, category)
            assert category and isinstance(category, str)
            assert text


def test_a_rejection_with_its_own_fix_still_outranks_the_mode_row():
    """The precedence the mode table slots into: a rewrite specific to one
    rejection beats both the mode row and the per-code row (#1652's mechanism)."""
    err = RevlError("app.rvl", 1, "`missing` is not declared in this function",
                    hint="declare it with `let`/`var` (G1)",
                    code="G1", category="binding",
                    fix="declare it with `let`/`var` in the effect block")
    record = classify(err)
    assert record["fix"] == "declare it with `let`/`var` in the effect block"
    assert record["fix"] not in FIXES_BY_CATEGORY.values()


def test_a_code_with_one_mode_keeps_its_per_code_row():
    """The mode table is an override, not a replacement: a rejection of a code
    with no mode row is unchanged."""
    err = RevlError("app.rvl", 1, "provision conflict: key `kv` is provided by both",
                    code="G2", category="linking")
    assert classify(err)["fix"] == FIXES["G2"]
