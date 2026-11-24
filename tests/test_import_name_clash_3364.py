"""Issue #3364: a module that imports a fn or extern by name may not also
declare one of that name, and a diagnostic shows the name the author wrote.

A private `fn dice` beside `use "./kit.rvl" { dice }` used to compile without a
word: every call in the module reached the private one, the imported one was
unreachable, and a call that did not fit it was reported as `dice__m0`, the
spelling the module-privacy pass gave it. Two declarations of one name in a file
are a duplicate; an import and a declaration are the same collision and are
refused at the local declaration. `use ... as alias` names nothing locally and
is not affected.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.errors import source_spelling  # noqa: E402

KIT = "pub fn dice(x: Int, z: Int) -> Int { return x + z }\n"


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _compile(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / f"{name}.rvl").write_text(text)
    return compile_files([str(tmp_path / "main.rvl")])


def _refusal(tmp_path, **files) -> RevlError:
    with pytest.raises(RevlError) as info:
        _compile(tmp_path, **files)
    return info.value


def test_a_private_fn_named_like_an_import_is_refused(tmp_path):
    """The issue's reproduction, with the call that fits the private fn."""
    error = _refusal(tmp_path, kit=KIT, main=(
        'use "./kit.rvl" { dice }\n'
        "\n"
        "fn roll() -> Int { return dice(1) }\n"
        "fn dice(a: Int) -> Int { return a }\n"))
    assert error.message == ("`dice` is imported from ./kit.rvl on line 1 and "
                             "declared on line 4; rename one of them")
    assert error.line == 4
    assert error.filename.endswith("main.rvl")


def test_a_pub_fn_named_like_an_import_is_refused(tmp_path):
    error = _refusal(tmp_path, kit=KIT, main=(
        'use "./kit.rvl" { dice }\n'
        "\n"
        "pub fn dice(a: Int) -> Int { return a }\n"))
    assert "`dice` is imported from ./kit.rvl on line 1 and declared on line 3" \
        in error.message


def test_an_extern_named_like_an_import_is_refused(tmp_path):
    error = _refusal(
        tmp_path,
        kit="pub extern pure fn raw(x: Int) -> Int = @py { return x }\n",
        main=('use "./kit.rvl" { raw }\n'
              "\n"
              "extern pure fn raw(x: Int) -> Int = @py { return x }\n"))
    assert error.message == ("`raw` is imported from ./kit.rvl on line 1 and "
                             "declared on line 3; rename one of them")


def test_an_extern_imported_and_a_fn_declared_is_refused(tmp_path):
    """A module `fn` and an `extern fn` share one namespace."""
    error = _refusal(
        tmp_path,
        kit="pub extern pure fn raw(x: Int) -> Int = @py { return x }\n",
        main=('use "./kit.rvl" { raw }\n'
              "\n"
              "fn raw(x: Int) -> Int { return x }\n"))
    assert "`raw` is imported from ./kit.rvl on line 1 and declared on line 3" \
        in error.message


def test_the_refusal_names_the_declaration_that_comes_first(tmp_path):
    error = _refusal(
        tmp_path,
        kit=KIT + "pub fn chance(x: Int) -> Int { return x }\n",
        main=('use "./kit.rvl" { dice, chance }\n'
              "\n"
              "fn chance(a: Int) -> Int { return a }\n"
              "fn dice(a: Int) -> Int { return a }\n"))
    assert "`chance` is imported from ./kit.rvl on line 1 and declared on line 3" \
        in error.message


def test_a_used_module_is_held_to_the_same_rule(tmp_path):
    error = _refusal(
        tmp_path,
        kit=KIT,
        mid=('use "./kit.rvl" { dice }\n'
             "\n"
             "pub fn dice(a: Int) -> Int { return a }\n"),
        main=('use "./mid.rvl" { dice }\n'
              "fn use_it() -> Int { return dice(1) }\n"))
    assert error.message == ("`dice` is imported from ./kit.rvl on line 1 and "
                             "declared on line 3; rename one of them")
    assert error.filename.endswith("mid.rvl")


def test_a_module_alias_import_is_not_affected(tmp_path):
    """`use ... as alias` lists no names: the local fn and `kit.dice` coexist."""
    ir = _compile(tmp_path, kit=KIT, main=(
        'use "./kit.rvl" as kit\n'
        "\n"
        "fn dice(a: Int) -> Int { return a }\n"
        "fn roll() -> Int { return dice(1) + kit.dice(1, 2) }\n"))
    assert {fn["name"] for fn in ir["functions"]} >= {"dice", "roll"}


def test_an_import_with_no_clash_still_compiles(tmp_path):
    ir = _compile(tmp_path, kit=KIT, main=(
        'use "./kit.rvl" { dice }\n'
        "\n"
        "fn roll() -> Int { return dice(1, 2) }\n"
        "fn other(a: Int) -> Int { return a }\n"))
    assert "roll" in {fn["name"] for fn in ir["functions"]}


def test_a_type_of_the_same_name_is_not_a_fn_clash(tmp_path):
    """Only fn and extern names are refused: a type is another namespace."""
    _compile(tmp_path, kit=KIT, main=(
        'use "./kit.rvl" { dice }\n'
        "\n"
        "type Dice = { n: Int }\n"
        "fn roll() -> Int { return dice(1, 2) }\n"))


def test_a_diagnostic_shows_the_name_the_author_wrote(tmp_path):
    """Two modules each declare a private `dice`, so the privacy pass renames
    the root's apart. The arity refusal names it as written."""
    error = _refusal(
        tmp_path,
        kit=("fn dice(a: Int) -> Int { return a }\n"
             "pub fn roll() -> Int { return dice(1) }\n"),
        main=('use "./kit.rvl" { roll }\n'
              "\n"
              "fn dice(a: Int) -> Int { return a }\n"
              "fn go() -> Int { return dice(1, 2) + roll() }\n"))
    text = str(error)
    assert "__m" not in text
    assert "`dice` takes 1 argument(s), 2 given" in text
    assert "`dice` is declared `(Int)`" in text
    assert "__m" not in error.message


@pytest.mark.parametrize("raw, shown", [
    ("`dice__m0` takes 1 argument(s)", "`dice` takes 1 argument(s)"),
    ("`Ctx__m12` and `h__m1`", "`Ctx` and `h`"),
    ("`a__mx` stays", "`a__mx` stays"),
    ("`a__m1x` stays", "`a__m1x` stays"),
    ("__m0 alone stays", "__m0 alone stays"),
    ("(Item__m3) -> Item__m3", "(Item) -> Item"),
    ("no suffix", "no suffix"),
])
def test_source_spelling_strips_only_a_module_suffix(raw, shown):
    assert source_spelling(raw) == shown


def test_every_field_of_a_refusal_is_stripped():
    error = RevlError("f.rvl", 3, "`a__m0` is bad", hint="rename `a__m0`",
                      expected="a__m0", actual="b__m1")
    assert (error.message, error.hint, error.expected, error.actual) == (
        "`a` is bad", "rename `a`", "a", "b")
    assert str(error) == "f.rvl:3: `a` is bad\n  rename `a`"


def test_structured_fields_of_a_refusal_are_stripped_too():
    error = RevlError("f.rvl", 1, "m", fix="call a__m0", repair={"target": "a__m0"},
                      repairs=[{"edit": ["a__m1"]}], acquire={"extern": "e__m2"})
    assert error.fix == "call a"
    assert error.repair == {"target": "a"}
    assert error.repairs == [{"edit": ["a"]}]
    assert error.acquire == {"extern": "e"}
