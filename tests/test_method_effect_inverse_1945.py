"""A provide-method effect's `undo` is checked against its inverse (issue #1945).

#1859 held an activation-body host ACQUISITION's `undo` to its family's
release. The other bracket, an effect inside a provide-method body, was never
checked: `effect store.insert(k, v) undo store.remove("not-the-key")` compiled,
the inserted key outlived the call, and `assert no_residue` passed, because the
enclosing bracket's `store.drop()` released the map and masked the lost
inverse. No other mechanism reaches the position: `verified effect` is refused
in a method, a fault test cannot fault a method step, and #1859's check keys on
the acquisition verbs.

The rule now (decisions D1 to D3 on the issue):

- a host Map write's `undo` is its table inverse on the same handle with the
  same key expression: `insert(k, v)` and `insert_if_absent(k, v)` by
  `remove(k)`, `remove(k)` by `insert(k, e)`. Anything else is refused, G4,
  naming the inverse;
- every method-body effect whose `undo` is written records how far that check
  reached: `inverse: table | declared | asserted`.

The extern-acquire half of the same bracket was closed after D1 to D3, by
#1885's slice 3 (landed on main as f8b49436e, and inside the merge this branch
carries): an `extern acquire` that DECLARES its `undo` has exactly one legal
site spelling, so `effect lock_row(k) undo forget(k)` is refused with G4 rather
than reported `asserted`. An extern that declares no inverse keeps the
`asserted` classification.

Each refusal is mirrored in selfhost/lower.rvl; the census holds the gate to
examples/rejections/g4_method_write_not_inverse.rvl.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]

_SERVICE = """service Kv {
  fn get(k: Str) -> Str
  fn set(k: Str, v: Str) -> Str
}
fn noop() -> Int { return 0 }
fn key_of(k: Str) -> Str { return k.concat("!") }
"""


def _store(effect: str, undo: str, prelude: str = "") -> str:
    return (prelude + _SERVICE
            + "component Store provides kv: Kv {\n"
            "  let store = effect Map.new() undo store.drop()\n"
            "  let other = effect Map.new() undo other.drop()\n"
            "  provide kv {\n"
            "    fn get(k) = store.get(k)\n"
            "    fn set(k, v) {\n"
            f"      effect {effect}\n"
            f"      undo   {undo}\n"
            "      return v\n"
            "    }\n"
            "  }\n"
            "}\n")


def _set_step(source: str) -> dict:
    ir = compile_source(source, "t.rvl")
    [store] = [c for c in ir["components"] if c["name"] == "Store"]
    [provide] = [s for s in store["body"] if s.get("step") == "provide"]
    [set_] = [m for m in provide["methods"] if m["name"] == "set"]
    return set_["body"][0]


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return excinfo.value


def _needs(effect: str, inverse: str) -> str:
    return (f"the `undo` of `effect {effect}(...)` must be its inverse on the "
            f"same handle and key: write `undo {inverse}`")


INSERT = "store.insert(k, v)"
NEEDS_REMOVE = _needs("store.insert", "store.remove(k)")

#: The issue's table for `effect store.insert(k, v)`: only the inverse compiles.
PAIRINGS = [
    ("the inverse", "store.remove(k)", None),
    ("a no-op fn", "noop()", NEEDS_REMOVE),
    ("a read", "store.get(k)", NEEDS_REMOVE),
    ("the same effect again", 'store.insert(k, "x")', NEEDS_REMOVE),
    ("another handle", "other.remove(k)", NEEDS_REMOVE),
    ("a literal", "1", NEEDS_REMOVE),
    ("the wrong key", 'store.remove("not-the-key")', NEEDS_REMOVE),
    ("a destructive drop", "store.drop()", NEEDS_REMOVE),
]


@pytest.mark.parametrize("undo, refused",
                         [(u, r) for _n, u, r in PAIRINGS],
                         ids=[n for n, _u, _r in PAIRINGS])
def test_only_the_inverse_of_a_method_write_compiles(undo, refused):
    source = _store(INSERT, undo)
    if refused is None:
        assert _set_step(source)["inverse"] == "table"
        return
    error = _refusal(source)
    assert error.code == "G4"
    assert refused in str(error)


def test_a_wrong_literal_key_is_spelled_in_the_refusal():
    error = _refusal(_store('store.insert("a", v)', 'store.remove("b")'))
    assert _needs("store.insert", 'store.remove("a")') in str(error)


def test_a_key_through_a_helper_fn_is_the_same_key():
    """Indirection is covered: a key computed by a helper fn is the same key
    when it is the same expression, and a different one when it is not."""
    step = _set_step(_store("store.insert(key_of(k), v)",
                            "store.remove(key_of(k))"))
    assert step["inverse"] == "table"
    error = _refusal(_store("store.insert(key_of(k), v)", "store.remove(k)"))
    assert _needs("store.insert", "store.remove(<the same key>)") in str(error)


def test_a_remove_is_undone_by_an_insert_of_the_same_key_and_is_asserted():
    """`remove(k)` destroys the value, so `insert(k, e)` is checked for its
    handle and key while `e` stays the author's word."""
    assert _set_step(_store("store.remove(k)", "store.insert(k, v)"))["inverse"] \
        == "asserted"
    error = _refusal(_store("store.remove(k)", 'store.insert("z", v)'))
    assert _needs("store.remove", "store.insert(k, <the value it held>)") \
        in str(error)


def test_a_service_reversal_is_asserted():
    source = ("service Ledger {\n  fn charge(k: Str) -> Int\n"
              "  fn refund(k: Str) -> Int\n}\n" + _SERVICE
              + "component Store requires ledger: Ledger provides kv: Kv {\n"
              "  provide kv {\n"
              "    fn get(k) = k\n"
              "    fn set(k, v) {\n"
              "      effect ledger.charge(k)\n"
              "      undo   ledger.refund(k)\n"
              "      return v\n"
              "    }\n"
              "  }\n"
              "}\n")
    assert _set_step(source)["inverse"] == "asserted"


_EXTERNS = (
    "type Lock = { id: Int }\n"
    "extern pure fn unlock_rows() -> Unit = @py { return }\n"
    "extern pure fn forget(k: Str) -> Unit = @py { return }\n"
    "extern acquire fn lock_row(k: Str) -> Lock undo unlock_rows()"
    " = @py { return {'id': 1} }\n")


def _extern_store(undo: str) -> str:
    return (_EXTERNS + _SERVICE
            + "component Store provides kv: Kv {\n"
            "  provide kv {\n"
            "    fn get(k) = k\n"
            "    fn set(k, v) {\n"
            "      effect lock_row(k)\n"
            f"      undo   {undo}\n"
            "      return v\n"
            "    }\n"
            "  }\n"
            "}\n")


def _extern_no_inverse(effect: str, undo: str) -> str:
    return (_EXTERNS + _SERVICE
            + "component Store provides kv: Kv {\n"
            "  provide kv {\n"
            "    fn get(k) = k\n"
            "    fn set(k, v) {\n"
            f"      effect {effect}\n"
            f"      undo   {undo}\n"
            "      return v\n"
            "    }\n"
            "  }\n"
            "}\n")


def test_an_extern_undone_by_its_declared_inverse_is_declared():
    assert _set_step(_extern_store("unlock_rows()"))["inverse"] == "declared"


def test_an_extern_undone_by_anything_else_is_refused_by_its_declaration():
    """`lock_row` declares its inverse, so its site `undo` has one legal
    spelling and `asserted` is not reachable for it: the declaration asserts
    `unlock_rows` reverts, not that any call may stand in for it (issue #1859
    slice 3, which landed after D1 to D3 — see the table in
    docs/verified-effect.md)."""
    error = _refusal(_extern_store("forget(k)"))
    assert error.code == "G4"
    assert error.message == (
        "the `undo` of `effect lock_row(...)` must be the inverse `lock_row` "
        "declares: write `undo unlock_rows(...)` as the declaration calls it")


def test_an_extern_that_declares_no_inverse_is_asserted():
    """An extern that is not an `extern acquire` declares no inverse, so there
    is nothing to hold its site `undo` to and the reversal stays the author's
    word."""
    assert _set_step(_extern_no_inverse("forget(k)", "unlock_rows()"))["inverse"] \
        == "asserted"


def test_a_typed_hole_in_the_undo_is_an_obligation_with_no_provenance():
    step = _set_step(_store(INSERT, "hole[Int]"))
    assert "inverse" not in step


def test_an_unbracketed_write_in_a_provide_method_is_legal():
    """D6: the caller brackets the crossing. `examples/verified_effect.rvl`'s
    provider writes `fn seed(k) = data.insert(k, 1)`."""
    source = (_SERVICE
              + "component Store provides kv: Kv {\n"
              "  let store = effect Map.new() undo store.drop()\n"
              "  provide kv {\n"
              "    fn get(k) = store.get(k)\n"
              "    fn set(k, v) = store.insert(k, v)\n"
              "  }\n"
              "}\n")
    compile_source(source, "t.rvl")


def test_activation_body_ir_carries_no_inverse_key():
    """D3: provide-method steps only, so activation-body IR is byte-identical."""
    ir = compile_source(_store(INSERT, "store.remove(k)"), "t.rvl")
    [store] = [c for c in ir["components"] if c["name"] == "Store"]
    for step in store["body"]:
        if step.get("step") in ("effect", "let-effect"):
            assert "inverse" not in step


# ---------------------------------------------------------- `revl test`

_LIFECYCLE = """
lifecycle test "a method-body write is reversed" {
  load Store
  call kv.set("alpha", "one")
  unload Store
  assert no_residue
}
"""

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="`revl test` runs the lifecycle test on cordis-py; install it with "
           "`sh backends/python/setup.sh`")


def _revl_test(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    path = tmp_path / "store.rvl"
    path.write_text(source, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", str(path)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


def test_the_issues_wrong_key_program_no_longer_compiles(tmp_path):
    """Its `revl test` used to print `PASS a method-body write is reversed`."""
    result = _revl_test(tmp_path, _store(INSERT, 'store.remove("not-the-key")')
                        + _LIFECYCLE)
    assert result.returncode != 0
    assert "PASS" not in result.stdout
    assert NEEDS_REMOVE in result.stdout + result.stderr


@needs_cordis
def test_the_inverse_still_passes_its_lifecycle_test(tmp_path):
    """No over-rejection: the correct pairing compiles and its write is
    reversed at unload."""
    result = _revl_test(tmp_path, _store(INSERT, "store.remove(k)") + _LIFECYCLE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS a method-body write is reversed" in result.stdout


# ------------------------------------------------- the teardown report (D4)

_LEDGER = ("service Ledger {\n  fn charge(k: Str) -> Int\n"
           "  fn refund(k: Str) -> Int\n}\n"
           "component Bank provides ledger: Ledger {\n"
           "  provide ledger {\n"
           "    fn charge(k) = 1\n"
           "    fn refund(k) = 1\n"
           "  }\n"
           "}\n")


@needs_cordis
def test_an_asserted_reversal_is_listed_as_trust_the_author_not_failed():
    """D4: `noResidue` judges what the runtime observes; a reversal revl
    cannot prove is listed beside it."""
    from revl.mcp.session import Session  # noqa: PLC0415
    source = (_LEDGER + _SERVICE
              + "component Store requires ledger: Ledger provides kv: Kv {\n"
              "  provide kv {\n"
              "    fn get(k) = k\n"
              "    fn set(k, v) {\n"
              "      effect ledger.charge(k)\n"
              "      undo   ledger.refund(k)\n"
              "      return v\n"
              "    }\n"
              "  }\n"
              "}\n")
    session = Session()
    session.load(compile_source(source, "t.rvl"))
    session.call("kv", "set", ["alpha", "one"])
    report = session.unload()
    assert report["noResidue"] is True
    assert report["trustTheAuthor"] == [
        {"component": "Store", "method": "kv.set", "inverse": "asserted"}]


@needs_cordis
def test_a_table_inverse_adds_nothing_to_the_report():
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(compile_source(_store(INSERT, "store.remove(k)"), "t.rvl"))
    session.call("kv", "set", ["alpha", "one"])
    report = session.unload()
    assert report["noResidue"] is True
    assert "trustTheAuthor" not in report
