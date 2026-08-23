"""`revl plan -o` → `revl apply` — the plan as an executable artifact.

Two halves, mirrored on `test_mcp_session.py`. The pure half (serialization,
drift detection, step-verification predicates, operation derivation) runs
everywhere. The live half actually drives a cordis-py composition through the
apply engine — a clean apply, a drift refusal, and the load-bearing one: a
mid-plan failure that rolls the applied prefix back by derived LIFO inverses
and leaves no residue. Those are gated per-test on the runtime, not per-module,
so the pure tests still get counted where cordis is absent.
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.apply import (ApplyError, build_artifact, drift, fingerprint,  # noqa: E402
                        validate_artifact, verify_step)
from revl.plan import plan  # noqa: E402

# --------------------------------------------------------------- fixtures text

SERVICES = """
service Db { fn get(k: Str) -> Opt[Str] }
service Cache { fn get(k: Str) -> Opt[Str] }
service Api { fn lookup(k: Str) -> Str }
"""
DB = """
component MemDb provides db: Db {
  let store = effect Map.new() undo store.drop()
  provide db { fn get(k) = store.get(k) }
}
"""
CACHE = """
component L1 requires db: Db provides cache: Cache {
  provide cache { fn get(k) = db.get(k) }
}
"""
FRONT_V1 = """
component Front requires cache: Cache provides api: Api {
  provide api { fn lookup(k) = "v1:" + (cache.get(k) ?? "miss") }
}
"""
FRONT_V2 = """
component Front requires cache: Cache provides api: Api {
  provide api { fn lookup(k) = "v2:" + (cache.get(k) ?? "gone") }
}
"""
AUDITOR = """
service Log { fn note(m: Str) -> Int }
component Auditor requires api: Api provides log: Log {
  provide log { fn note(m) = 1 }
}
"""

RUNNING_SRC = SERVICES + DB + CACHE + FRONT_V1


def _running_ir() -> dict:
    return compile_source(RUNNING_SRC)


def _add_auditor_artifact() -> dict:
    running = _running_ir()
    result = plan(source=AUDITOR, manifest=running, include_ir=True)
    return build_artifact(result, running)


def _replace_front_artifact() -> dict:
    running = _running_ir()
    result = plan(source=SERVICES + FRONT_V2, manifest=running, include_ir=True)
    return build_artifact(result, running)


# =============================================================== pure: artifact

def test_serialize_an_admitted_plan():
    artifact = _add_auditor_artifact()
    assert artifact["revlPlan"] == 1
    assert validate_artifact(artifact) is artifact
    # basis is the running composition; resulting includes the new component
    assert artifact["basis"]["components"] == ["Front", "L1", "MemDb"]
    assert "Auditor" in artifact["resulting"]["components"]
    # one operation: load the added component
    assert [(o["op"], o["name"]) for o in artifact["operations"]] == [("load", "Auditor")]
    assert artifact["operations"][0]["predict"] == {"state": "ACTIVE", "keys": ["log"]}
    # the resulting bodies are carried, so apply can load them
    assert {c["name"] for c in artifact["resultingIR"]["components"]} == {
        "MemDb", "L1", "Front", "Auditor"}


def test_a_rejected_plan_cannot_be_serialized():
    running = _running_ir()
    # a candidate that fails the gate (type error in the body)
    broken = 'service Log { fn note(m: Str) -> Int }\n' \
             'component Auditor requires api: Api provides log: Log ' \
             '{ provide log { fn note(m) = "nope" } }'
    result = plan(source=broken, manifest=running, include_ir=True)
    assert result["admissible"] is False
    with pytest.raises(ApplyError):
        build_artifact(result, running)


def test_replacement_is_a_teardown_then_a_load():
    artifact = _replace_front_artifact()
    assert [(o["op"], o["name"]) for o in artifact["operations"]] == [
        ("dispose", "Front"), ("load", "Front")]
    # a replaced component is not a true withdrawal — nothing to check gone
    assert artifact["operations"][0]["predict"]["withdrawnKeys"] == []
    assert artifact["operations"][1]["predict"] == {"state": "ACTIVE", "keys": ["api"]}


def test_validate_rejects_a_foreign_file():
    with pytest.raises(ApplyError):
        validate_artifact({"not": "a plan"})
    with pytest.raises(ApplyError):
        validate_artifact({"revlPlan": 999})


# =============================================================== pure: drift

def test_no_drift_when_identical():
    running = _running_ir()
    assert drift(fingerprint(running), fingerprint(running)) is None


def test_drift_names_what_moved():
    running = _running_ir()
    other = compile_source(SERVICES + DB)  # Front and L1 are gone
    diff = drift(fingerprint(running), fingerprint(other))
    assert diff is not None
    assert diff["componentsVanished"] == ["Front", "L1"]
    assert "api <- Front" in diff["provisionsVanished"]


# =============================================================== pure: verify

def test_step_verification_predicates():
    load = {"op": "load", "name": "X", "predict": {"state": "ACTIVE", "keys": ["k"]}}
    assert verify_step(load, {"state": "ACTIVE", "providedKeys": ["k"]}) is None
    assert verify_step(load, {"state": "PENDING", "providedKeys": []}) is not None
    assert verify_step(load, {"state": "ACTIVE", "providedKeys": []}) is not None
    dispose = {"op": "dispose", "name": "Y",
               "predict": {"absent": True, "withdrawnKeys": ["k"]}}
    assert verify_step(dispose, {"absent": True, "providedKeys": []}) is None
    assert verify_step(dispose, {"absent": False, "state": "ACTIVE",
                                 "providedKeys": ["k"]}) is not None
    assert verify_step(dispose, {"absent": True, "providedKeys": ["k"]}) is not None


# =============================================================== live: the engine

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the apply engine drives a live cordis-py composition — install it "
           "with `sh backends/python/setup.sh` and run under its venv",
)


def _fresh():
    from revl.mcp.session import Session

    return Session()


@needs_runtime
def test_a_clean_plan_applies_against_a_live_composition():
    artifact = _add_auditor_artifact()
    session = _fresh()
    try:
        session.load(_running_ir())
        report = session.apply(artifact)
        assert report["applied"] is True
        assert "Auditor" in report["resulting"]
        assert "log" in session._provided_keys()
        # the new component actually answers
        assert session.call("log", "note", ["hi"])["result"] == 1
        assert session.call("api", "lookup", ["k"])["result"] == "v1:miss"
    finally:
        residue = session.unload()
        assert residue["noResidue"] is True


@needs_runtime
def test_apply_refuses_a_drifted_composition():
    from revl.mcp.session import SessionError

    artifact = _add_auditor_artifact()
    session = _fresh()
    try:
        # boot a DIFFERENT composition than the plan's basis
        session.load(compile_source(SERVICES + DB))
        with pytest.raises(SessionError) as caught:
            session.apply(artifact)
        assert "DRIFTED" in str(caught.value)
        # the refusal touched nothing — MemDb is still serving
        assert session.call("db", "get", ["k"])["result"] is None
    finally:
        session.unload()


@needs_runtime
def test_a_mid_plan_failure_rolls_the_prefix_back_with_no_residue():
    # a two-step plan (dispose old Front, load new Front); tamper the SECOND
    # step's prediction so verification fails AFTER the first step applied.
    artifact = _replace_front_artifact()
    artifact = copy.deepcopy(artifact)
    assert artifact["operations"][1]["op"] == "load"
    artifact["operations"][1]["predict"]["state"] = "PENDING"  # it will be ACTIVE

    session = _fresh()
    try:
        session.load(_running_ir())
        report = session.apply(artifact)
        assert report["applied"] is False
        assert report["failedAt"] == "Front"
        # the applied prefix was undone LIFO by derived inverses
        assert [u["undo"] for u in report["rolledBack"]] == ["dispose", "restore"]
        assert report["noResidue"] is True
        assert report["registry"]["afterRollback"] == report["registry"]["baseline"]
        # and the ORIGINAL composition is intact and still serving v1
        assert session.call("api", "lookup", ["k"])["result"] == "v1:miss"
    finally:
        residue = session.unload()
        assert residue["noResidue"] is True


@needs_runtime
def test_step_verification_catches_a_reality_prediction_mismatch():
    artifact = copy.deepcopy(_add_auditor_artifact())
    # claim the added component will come up PENDING; it will actually be ACTIVE
    artifact["operations"][0]["predict"]["state"] = "PENDING"

    session = _fresh()
    try:
        session.load(_running_ir())
        report = session.apply(artifact)
        assert report["applied"] is False
        assert "predicted to leave it PENDING" in report["reason"]
        assert report["noResidue"] is True
        # nothing added survived the rollback
        assert "log" not in session._provided_keys()
    finally:
        session.unload()


@needs_runtime
def test_a_step_that_cannot_execute_also_rolls_back():
    # a genuine execution failure (not a tampered prediction): the second step
    # names a component the resulting IR does not carry, so loading it raises.
    artifact = _replace_front_artifact()
    artifact = copy.deepcopy(artifact)
    artifact["operations"][1]["name"] = "DoesNotExist"

    session = _fresh()
    try:
        session.load(_running_ir())
        report = session.apply(artifact)
        assert report["applied"] is False
        assert report["noResidue"] is True
        # Front (v1) was torn down then restored by the rollback
        assert session.call("api", "lookup", ["k"])["result"] == "v1:miss"
    finally:
        session.unload()
