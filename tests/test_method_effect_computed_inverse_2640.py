"""Issue #2640: prove a bounded computed inverse for list append."""

import importlib.util
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "tests" / "fixtures" / "g4_method_append_inverse.rvl").read_text(
    encoding="utf-8")

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the runtime proof uses the pinned cordis-py backend; install it "
           "with `sh backends/python/setup.sh`",
)


def _runtime_source() -> str:
    source = SOURCE.replace(
        "  fn contents(id: Str) -> List[Str]\n",
        "  fn contents(id: Str) -> List[Str]\n"
        "  fn seed(id: Str) -> Unit\n")
    return source.replace(
        "    fn contents(id) = store.get(id) ?? []\n",
        "    fn contents(id) = store.get(id) ?? []\n"
        "    fn seed(id) = store.insert(id, [\"old\"])\n")


def _append_step(source: str = SOURCE) -> dict:
    ir = compile_source(source, "g4_method_append_inverse.rvl")
    [ledger] = [c for c in ir["components"] if c["name"] == "LedgerStore"]
    [provide] = [s for s in ledger["body"] if s.get("step") == "provide"]
    [append] = [m for m in provide["methods"] if m["name"] == "append"]
    return next(s for s in append["body"] if s.get("step") == "effect")


def test_same_key_list_append_has_a_proven_inverse_without_capturing_the_preimage():
    step = _append_step()

    assert step["inverse"] == "table"
    assert step.get("undo_captures", []) == []
    assert step["undo"]["kind"] == "if"
    assert step["undo"]["cond"] == {"kind": "name", "id": "known"}


def test_assignment_bound_presence_check_keeps_the_computed_inverse():
    source = SOURCE.replace(
        "let known = store.get(id) != None",
        "var known = false\n      known = store.get(id) != None")
    step = _append_step(source)

    assert step["inverse"] == "table"
    assert step["undo"]["cond"] == {"kind": "name", "id": "known"}


def test_presence_check_through_a_defaulted_read_is_refused():
    source = SOURCE.replace(
        "let known = store.get(id) != None",
        "let known = (store.get(id) ?? []) != None")
    with pytest.raises(RevlError) as excinfo:
        _append_step(source)

    assert excinfo.value.code == "G4"
    assert "computed append inverse" in str(excinfo.value)


def test_a_same_key_effect_nested_under_if_is_refused_before_g4():
    source = SOURCE.replace(
        "      effect store.insert(id, (store.get(id) ?? []).push(msg))",
        "      if (msg != \"\") {\n"
        "        effect store.insert(id, [\"between\"]) undo store.remove(id)\n"
        "      }\n"
        "      effect store.insert(id, (store.get(id) ?? []).push(msg))")

    with pytest.raises(RevlError) as excinfo:
        _append_step(source)

    assert "teardown-registering step" in str(excinfo.value)
    assert "Group 3" in str(excinfo.value)


def test_unproven_drop_last_helper_is_refused_with_a_named_g4_reason():
    source = SOURCE.replace(
        "return xs.slice(0, xs.length() - 1)",
        "return xs.slice(1, xs.length())")

    with pytest.raises(RevlError) as excinfo:
        _append_step(source)

    assert getattr(excinfo.value, "code", None) == "G4"
    assert "computed append inverse" in str(excinfo.value)


def _reassigned_key_source() -> str:
    source = SOURCE.replace(
        "      let known = store.get(id) != None",
        "      var key = id\n"
        "      let known = store.get(key) != None\n"
        "      key = \"other\"")
    source = source.replace("store.insert(id, (store.get(id) ?? []).push(msg))",
                            "store.insert(key, (store.get(key) ?? []).push(msg))")
    source = source.replace("store.insert(id, drop_last(store.get(id) ?? []))",
                            "store.insert(key, drop_last(store.get(key) ?? []))")
    return source.replace("store.remove(id)", "store.remove(key)")


@pytest.mark.parametrize("source", [
    SOURCE.replace("store.get(id) != None", 'store.get("other") != None'),
    SOURCE.replace("(store.get(id) ?? []).push(msg)",
                   '(store.get("other") ?? []).push(msg)'),
    SOURCE.replace("drop_last(store.get(id) ?? [])",
                   'drop_last(store.get("other") ?? [])'),
    SOURCE.replace("let known = store.get(id) != None",
                   "var known = store.get(id) != None\n      known = false"),
    _reassigned_key_source(),
    SOURCE.replace(
        "      effect store.insert(id, (store.get(id) ?? []).push(msg))",
        "      effect store.insert(id, [\"between\"])\n"
        "      undo store.remove(id)\n"
        "      effect store.insert(id, (store.get(id) ?? []).push(msg))"),
])
def test_mismatched_or_stale_computed_inverse_is_refused_by_g4(source: str):
    with pytest.raises(RevlError) as excinfo:
        _append_step(source)

    assert excinfo.value.code == "G4"
    assert "computed append inverse" in str(excinfo.value)


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    frame = driver.runtime._frame_for_ctx(fiber.ctx)
    assert frame is not None
    return frame


@needs_cordis
def test_runtime_append_reverses_present_and_absent_keys_without_residue():
    from revl.mcp.session import Session

    session = Session()
    session.load(compile_source(_runtime_source(), "g4_method_append_inverse.rvl"))
    session.call("ledger", "seed", ["present"])
    assert session.call("ledger", "contents", ["present"])["result"] == ["old"]
    assert session.call("ledger", "contents", ["absent"])["result"] == []
    session.call("ledger", "append", ["present", "new"])
    session.call("ledger", "append", ["absent", "new"])

    frame = _sole_frame(session)
    report = session.unload()

    assert report["noResidue"] is True
    assert report["compensationResidue"] == []
    assert frame._journal == {}


@needs_cordis
def test_method_inverse_closure_does_not_capture_the_preexisting_list():
    from revl.mcp.session import Session

    session = Session()
    session.load(compile_source(_runtime_source(), "g4_method_append_inverse.rvl"))
    session.call("ledger", "seed", ["present"])
    old = session.call("ledger", "contents", ["present"])["result"]
    session.call("ledger", "append", ["present", "new"])

    frame = _sole_frame(session)
    [entry] = [e for e in frame._adopted
               if e._meta.get("label", "").startswith("LedgerStore.ledger.append")]
    closure = entry._runner.execute.__closure__ or ()
    prior = next(row[2] for row in frame._journal.values()
                 if row[1] == "present")
    assert prior == old
    assert prior == ["old"]
    assert all(cell.cell_contents is not prior for cell in closure)

    report = session.unload()
    assert report["noResidue"] is True
    assert frame._journal == {}
