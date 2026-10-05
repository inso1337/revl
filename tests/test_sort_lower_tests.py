"""`selfhost/lower.rvl`'s in-file test blocks are sorted by name (issue #1768).

Every self-host pull request that added tests used to append them at the end
of the file, so any two of them conflicted on its tail, and again on the tail
of the generated `crates/revl-gate/src/selfhost.rs`. `tools/sort_lower_tests.py`
keeps the blocks in name order, so a new test goes where its name falls and two
such pull requests merge. These tests hold that the committed file is sorted,
that sorting is a pure reorder, that the layout merges where the old one did
not, and that the conflict resolver keeps both sides and refuses anything it
cannot decide.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402
from _merge_tree import git_has_merge_tree, merge  # noqa: E402

TOOL = load_by_path("sort_lower_tests_tool", ROOT / "tools" / "sort_lower_tests.py")
MARKER = TOOL.MARKER


def _file(*names: str, helper: bool = False) -> str:
    """A small lower.rvl-shaped file: a body, the marker, test blocks in the
    order given (a block's body holds a `}` inside a string, which only a
    lexer reads correctly)."""
    head = 'fn f() -> Str {\n  return "x"\n}\n\n' + MARKER + "\n"
    items = []
    if helper:
        items.append('// a helper the tests call\nfn h() -> Str {\n  return "}"\n}')
    for name in names:
        items.append(f'// about {name}\ntest "{name}" {{\n  assert f() != "}}{name}"\n}}')
    return head + "\n" + "\n\n".join(items) + "\n"


def test_the_committed_lower_rvl_is_sorted():
    assert TOOL.problems() == [], (
        "selfhost/lower.rvl's test blocks are not sorted by name; run "
        "`python3 tools/sort_lower_tests.py --write`")


def test_sorting_the_real_file_is_a_pure_reorder():
    """Same lines, same blank lines, same head; only the order of the items
    after the marker can differ. The sorted form is a fixed point."""
    text = TOOL.LOWER.read_text(encoding="utf-8")
    fresh = TOOL.sort_text(text)
    assert TOOL.sort_text(fresh) == fresh
    assert text[:text.index(MARKER)] == fresh[:fresh.index(MARKER)]
    assert sorted(l for l in text.splitlines() if l.strip()) == \
        sorted(l for l in fresh.splitlines() if l.strip())


def test_sort_orders_by_name_and_keeps_helpers_and_comments():
    out = TOOL.sort_text(_file("b test", "a test", helper=False))
    assert out.index('test "a test"') < out.index('test "b test"')
    assert out.index("// about a test") < out.index('test "a test"')
    helper = TOOL.sort_text(_file("b test", "a test", helper=True))
    assert helper.index("fn h()") < helper.index('test "a test"')
    assert TOOL.sort_text(helper) == helper


@pytest.mark.skipif(not git_has_merge_tree(), reason="git merge-tree --write-tree needs git 2.38")
def test_two_pull_requests_adding_tests_merge_in_the_sorted_layout():
    """The exit test. Two pull requests each add a test whose name falls in a
    different place. Sorted, they insert at different places and merge.
    Appended at the end, as before, the same two conflict."""
    base_names = ["a1", "c1", "e1", "g1"]
    base = {"selfhost/lower.rvl": _file(*base_names)}
    left = {"selfhost/lower.rvl": TOOL.sort_text(_file(*base_names, "b2"))}
    right = {"selfhost/lower.rvl": TOOL.sort_text(_file(*base_names, "f2"))}
    clean, conflicted = merge(base, left, right)
    assert clean, conflicted
    appended_left = {"selfhost/lower.rvl": _file(*base_names, "b2")}
    appended_right = {"selfhost/lower.rvl": _file(*base_names, "f2")}
    clean, conflicted = merge(base, appended_left, appended_right)
    assert not clean and conflicted == ["selfhost/lower.rvl"]


def _conflicted(ours: list[str], theirs: list[str], base: list[str]) -> str:
    text = _file(*base)
    head, _ = TOOL._split(text)
    def block(names):
        return "\n\n".join(f'test "{n}" {{\n  assert f() != "{n}"\n}}' for n in names)
    return (head + "\n" + block(base) + "\n\n<<<<<<< ours\n" + block(ours)
            + "\n=======\n" + block(theirs) + "\n>>>>>>> theirs\n")


def test_write_resolves_a_same_gap_conflict_by_keeping_both_sides():
    out = TOOL.sort_text(_conflicted(["b-left"], ["b-right"], ["a", "c"]))
    assert "<<<<<<<" not in out and ">>>>>>>" not in out
    order = [out.index(f'test "{n}"') for n in ("a", "b-left", "b-right", "c")]
    assert order == sorted(order)


def test_write_refuses_what_it_cannot_decide():
    with pytest.raises(TOOL.SortError, match="twice"):
        TOOL.sort_text(_conflicted(["same"], ["same"], ["a"]))
    outside = "<<<<<<< ours\nfn g() -> Str {\n  return \"a\"\n}\n=======\n" \
              "fn g() -> Str {\n  return \"b\"\n}\n>>>>>>> theirs\n" + _file("a")
    with pytest.raises(TOOL.SortError, match="outside the tests section"):
        TOOL.sort_text(outside)


def test_check_fails_on_an_unsorted_file_and_names_the_fix(tmp_path):
    path = tmp_path / "lower.rvl"
    path.write_text(_file("b", "a"), encoding="utf-8")
    found = TOOL.problems(path)
    assert found and "sort_lower_tests.py --write" in found[0]
    assert TOOL.main(["--write", "--path", str(path)]) == 0
    assert TOOL.problems(path) == []


def test_ci_runs_the_check():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python3 tools/sort_lower_tests.py --check" in ci
