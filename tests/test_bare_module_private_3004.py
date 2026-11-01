"""Issue #3004: a non-`pub` top-level declaration of another module is refused
on the bare-name path exactly as on the named-import path.

`use "m.rvl" { raw }` was refused with "`raw` is module-private in `m.rvl` and
cannot be imported (G1)", but a consumer that imported only `E` from `m.rvl`
could still call `raw(1)` by bare name: fn bodies resolved externs against
every extern of the merged program, and `test` / `prop test` blocks and
components against every fn and extern of it. A body now resolves its own
module's declarations plus the names its `use` lists import, and a bare name
that is another module's private declaration earns the G1 module-private
diagnostic.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

MODULE = (
    "pub type E = { code: Str, message: Str }\n"
    "\n"
    "extern pure fn raw(n: Int) -> Str\n"
    "  = @py { return str(n) }\n"
    "\n"
    "pub extern pure fn shown(n: Int) -> Str\n"
    "  = @py { return str(n) }\n"
    "\n"
    "fn helper(n: Int) -> Int { return n + 1 }\n"
    "\n"
    "pub fn wrap(n: Int) -> Str { return raw(helper(n)) }\n"
    "\n"
    'test "the module reaches its own privates" {\n'
    '  assert raw(helper(1)) == "2"\n'
    "}\n"
)

PRIVATE = "`raw` is module-private in `m.rvl` and cannot be imported (G1)"


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    # the diagnostic names the module relative to the working directory, as
    # the named-import path does
    monkeypatch.chdir(tmp_path)


def _compile(tmp_path, consumer: str):
    (tmp_path / "m.rvl").write_text(MODULE)
    (tmp_path / "main.rvl").write_text(consumer)
    return compile_files([str(tmp_path / "main.rvl")])


def _refusal(tmp_path, consumer: str) -> str:
    with pytest.raises(RevlError) as info:
        _compile(tmp_path, consumer)
    return str(info.value)


def test_bare_call_in_a_test_block_is_module_private(tmp_path):
    """The issue's reproduction: only `E` is imported, `raw` is called bare."""
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        "\n"
        'test "raw is reachable without pub" {\n'
        '  assert raw(1) == "1"\n'
        "}\n"
    ))
    assert PRIVATE in message, message


def test_bare_call_in_a_fn_body_is_module_private(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'pub fn f() -> Str { return raw(1) }\n'
    ))
    assert PRIVATE in message, message


def test_bare_call_in_a_component_is_module_private(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        "service S {\n"
        "  fn get() -> Str\n"
        "}\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn get() = raw(1)\n"
        "  }\n"
        "}\n"
    ))
    assert PRIVATE in message, message


def test_bare_private_fn_in_a_test_block_is_module_private(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'test "t" {\n'
        "  assert helper(1) == 2\n"
        "}\n"
    ))
    assert "`helper` is module-private in `m.rvl` and cannot be imported (G1)" \
        in message, message


def test_named_import_refusal_is_unchanged(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { raw }\n'
        'pub fn f() -> Str { return raw(1) }\n'
    ))
    assert PRIVATE in message, message


def test_unimported_pub_extern_is_not_in_scope(tmp_path):
    """A `pub` extern the consumer did not name is a missing import, not a
    private one: the ordinary undeclared-name refusal, as for a `pub fn`."""
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'pub fn f() -> Str { return shown(1) }\n'
    ))
    assert "`shown` is not declared in this function" in message, message


def test_imported_pub_symbols_stay_callable_by_bare_name(tmp_path):
    """Control: a `pub` extern and a `pub` fn named in the `use` list are
    callable bare from a fn, a test and a component, and the imported module's
    own test still reaches that module's private `raw` and `helper`."""
    ir = _compile(tmp_path, (
        'use "m.rvl" { E, shown, wrap }\n'
        'pub fn f() -> Str { return shown(1).concat(wrap(1)) }\n'
        'test "t" {\n'
        '  assert shown(1) == "1"\n'
        '  assert wrap(1) == "2"\n'
        "}\n"
        "service S {\n"
        "  fn get() -> Str\n"
        "}\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn get() = wrap(1)\n"
        "  }\n"
        "}\n"
    ))
    tests = {t["name"] for t in ir["tests"]}
    assert {"t", "the module reaches its own privates"} <= tests, tests
