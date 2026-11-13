"""Issue #3023: a `use` list is the whole cross-module bare-name surface.

docs/syntax-2.0.md section 1 says `use "m.rvl" { a, b }` imports the
declarations it names. The lowerer enforced that for a non-`pub` declaration
(issue #3004) but not for a `pub` one: a module could call another module's
`pub fn` or `pub extern` by bare name without naming it in its `use` list, as
long as that module was part of the program. Externs leaked everywhere; `test`,
`prop test` and component bodies saw every declaration of the merged program.

Now an un-imported `pub` fn or extern referenced by bare name is refused with
"not declared" in fn bodies, tests, prop tests and components, exactly as an
un-imported `pub fn` already was in a plain fn body. A `pub` name named in the
`use` list stays callable, and the root modules of a composition keep their one
shared namespace.
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
    "pub extern pure fn shown(n: Int) -> Str\n"
    "  = @py { return str(n) }\n"
    "\n"
    "pub fn wrap(n: Int) -> Str { return shown(n) }\n"
)

SERVICE = (
    "service S {\n"
    "  fn get() -> Str\n"
    "}\n"
)


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _compile(tmp_path, consumer: str, module: str = MODULE):
    (tmp_path / "m.rvl").write_text(module)
    (tmp_path / "main.rvl").write_text(consumer)
    return compile_files([str(tmp_path / "main.rvl")])


def _refusal(tmp_path, consumer: str, module: str = MODULE) -> str:
    with pytest.raises(RevlError) as info:
        _compile(tmp_path, consumer, module)
    return str(info.value)


def _not_declared(name: str):
    return f"`{name}` is not declared in this function"


# --------------------------------------------------------------- fn bodies

def test_unimported_pub_extern_in_a_fn_body_is_not_declared(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'pub fn f() -> Str { return shown(1) }\n'
    ))
    assert _not_declared("shown") in message, message


def test_unimported_pub_fn_in_a_fn_body_is_not_declared(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'pub fn f() -> Str { return wrap(1) }\n'
    ))
    assert _not_declared("wrap") in message, message


# --------------------------------------------------------- test statements

def test_unimported_pub_fn_in_a_test_is_not_declared(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'test "t" {\n'
        '  assert wrap(1) == "1"\n'
        "}\n"
    ))
    assert _not_declared("wrap") in message, message


def test_unimported_pub_extern_in_a_test_is_not_declared(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'test "t" {\n'
        '  assert shown(1) == "1"\n'
        "}\n"
    ))
    assert _not_declared("shown") in message, message


def test_unimported_pub_fn_in_a_prop_test_is_not_declared(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        'prop test "p" (n: Int) {\n'
        "  assert wrap(n) == wrap(n)\n"
        "}\n"
    ))
    assert _not_declared("wrap") in message, message


# ------------------------------------------------------------- components

def test_unimported_pub_fn_in_a_component_is_not_declared(tmp_path):
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        + SERVICE +
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn get() = wrap(1)\n"
        "  }\n"
        "}\n"
    ))
    assert _not_declared("wrap") in message, message


def test_unimported_pub_fn_as_a_component_value_is_not_declared(tmp_path):
    """A first-class read of the fn (not only its call) draws the same refusal,
    not the component's requirement wording."""
    message = _refusal(tmp_path, (
        'use "m.rvl" { E }\n'
        + SERVICE +
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn get() = wrap\n"
        "  }\n"
        "}\n"
    ))
    assert _not_declared("wrap") in message, message


# ---------------------------------------------------------------- controls

def test_imported_pub_names_stay_callable_everywhere(tmp_path):
    """An imported `pub` extern (fn body) and an imported `pub` fn (fn body,
    test, prop test and component) keep resolving, and the imported module's
    own body still reaches its declarations."""
    ir = _compile(tmp_path, (
        'use "m.rvl" { E, shown, wrap }\n'
        'pub fn f() -> Str { return shown(1).concat(wrap(1)) }\n'
        'test "t" {\n'
        '  assert shown(1) == "1"\n'
        '  assert wrap(1) == "1"\n'
        "}\n"
        'prop test "p" (n: Int) {\n'
        "  assert wrap(n) == wrap(n)\n"
        "}\n"
        + SERVICE +
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn get() = wrap(1)\n"
        "  }\n"
        "}\n"
    ))
    tests = {t["name"] for t in ir["tests"]}
    assert "t" in tests, tests
    assert "p" in {p["name"] for p in ir["prop_tests"]}, ir["prop_tests"]


def test_an_alias_import_keeps_the_whole_interface(tmp_path):
    """`use "m.rvl" as m` imports every public declaration, called as `m.fn`."""
    ir = _compile(tmp_path, (
        'use "m.rvl" as m\n'
        'pub fn f() -> Str { return m.wrap(1) }\n'
    ))
    assert "f" in {fn["name"] for fn in ir["functions"]}


def test_root_modules_keep_one_shared_namespace(tmp_path):
    """Co-root files are one composition: a root fn body reaches a root extern
    declared by another root, and a root test reaches another root's fn,
    without a `use` (a row file names the externs of the files its composition
    `use`s)."""
    (tmp_path / "a.rvl").write_text(
        "pub extern pure fn seed() -> Int\n"
        "  = @py { return 7 }\n"
        "\n"
        "pub fn base() -> Int { return seed() }\n")
    (tmp_path / "b.rvl").write_text(
        "pub fn derived() -> Int { return seed() }\n"
        'test "t" {\n'
        "  assert base() == 7\n"
        "}\n"
        "service S { fn get() -> Int }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn get() = base()\n"
        "  }\n"
        "}\n")
    ir = compile_files([str(tmp_path / "a.rvl"), str(tmp_path / "b.rvl")])
    assert {"base", "derived"} <= {fn["name"] for fn in ir["functions"]}
    assert "t" in {test["name"] for test in ir["tests"]}
    assert "C" in {comp["name"] for comp in ir["components"]}


def test_a_transitive_module_is_not_imported_by_bare_name(tmp_path):
    """`main` imports only `m`'s declarations; `m` imports `util`'s `helper`.
    The closure carries `util` into the program, but `main` never imported it,
    so `helper` is not in its namespace."""
    (tmp_path / "util.rvl").write_text(
        "pub fn helper(n: Int) -> Int { return n + 1 }\n")
    (tmp_path / "m.rvl").write_text(
        'use "util.rvl" { helper }\n'
        "pub type E = { code: Str }\n"
        "pub fn via(n: Int) -> Int { return helper(n) }\n")
    (tmp_path / "main.rvl").write_text(
        'use "m.rvl" { E, via }\n'
        'pub fn f() -> Int { return helper(1) }\n')
    with pytest.raises(RevlError) as info:
        compile_files([str(tmp_path / "main.rvl")])
    message = str(info.value)
    assert _not_declared("helper") in message, message


def test_a_pub_name_of_another_module_is_not_module_private(tmp_path):
    """A name some included module declares `pub` is a missing import, never
    another module's private declaration, so the refusal is "not declared"."""
    (tmp_path / "m.rvl").write_text(
        "pub type E = { code: Str }\n"
        "pub fn open(n: Int) -> Int { return n }\n")
    (tmp_path / "p.rvl").write_text(
        "pub type P = { id: Int }\n"
        "fn open(n: Int) -> Int { return n }\n")
    (tmp_path / "main.rvl").write_text(
        'use "m.rvl" { E }\n'
        'use "p.rvl" { P }\n'
        'pub fn f() -> Int { return open(1) }\n')
    with pytest.raises(RevlError) as info:
        compile_files([str(tmp_path / "main.rvl")])
    message = str(info.value)
    assert _not_declared("open") in message, message
    assert "module-private" not in message, message
