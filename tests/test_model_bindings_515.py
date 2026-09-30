"""Each model role's binding, recorded on the placement side (roadmap item
515, issue #1189, slice S5).

A role is bound to a member by configuration, not by the compiler: the
placement declares a host's devices, the scheduler picks each role's device,
and `model_schedule.binding_manifest` records the result with a digest over
it. The compiler IR carries none of it (the pinned property of
`tests/test_1311_model_routes_not_in_ir.py`). The operator reads the record in
`revl audit --placement` and in the conductor's boot output.

What this file pins:

* the manifest has one row per role per host, with the role's declared
  demand, the device it is bound to, and the actions that use it;
* changing one role's quantisation, its memory, or the device it binds to
  changes that row and the digest;
* a composition with no `route model` block has no manifest, and its audit
  and boot output are unchanged.

Programs are inline for the census reason `tests/test_model_portfolio_515.py`
gives.
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import model_schedule as ms  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.__main__ import main  # noqa: E402

APP = """
model role fast on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory 512 quant int8

service Answer { fn classify(text: Str) -> Str }

component First provides out: Answer {
  route model on classify { confidential -> fast | small }
  provide out { fn classify(text) = text }
}

component Second provides other: Answer {
  route model on classify { confidential -> fast | small }
  provide other { fn classify(text) = text }
}
"""

PLAIN = """
service Answer { fn classify(text: Str) -> Str }

component First provides out: Answer {
  provide out { fn classify(text) = text }
}
"""


def gpu(name="gpu0", memory=8192, quant=("q4_k_m", "q5_k_m")):
    return {"name": name, "device": "gpu", "memory_mib": memory,
            "quantisation": list(quant)}


def manifest(tmp_path, source=APP, devices=None):
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    devices = [gpu()] if devices is None else devices
    processes = {"edge": {"components": ["First", "Second"],
                          "devices": devices}}
    return ms.binding_manifest(ms.placement_schedules([str(app)], processes))


def only_row(record) -> dict:
    (host,) = record["hosts"]
    (row,) = host["bindings"]
    return row


# --------------------------------------------------------------------------
# 1. The record
# --------------------------------------------------------------------------

def test_one_row_per_role_with_its_demand_device_and_consumers(tmp_path):
    record = manifest(tmp_path)
    assert record["version"] == ms.BINDINGS_VERSION
    assert record["hosts"][0]["host"] == "edge"
    assert record["hosts"][0]["devices"] == [gpu()]
    assert only_row(record) == {
        "role": "fast",
        "residence": "on_device",
        "demand": {"device": "gpu", "memory_mib": 6144, "quant": "q4_k_m"},
        "device": "gpu0",
        "device_class": "gpu",
        "consumers": ["First.classify confidential",
                      "Second.classify confidential"],
    }


def test_the_digest_is_sha256_over_the_version_line_and_the_hosts(tmp_path):
    record = manifest(tmp_path)
    body = json.dumps(record["hosts"], sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    expected = hashlib.sha256(
        (ms.BINDINGS_VERSION + "\n" + body).encode("utf-8")).hexdigest()
    assert record["digest"] == expected
    assert manifest(tmp_path)["digest"] == expected


# --------------------------------------------------------------------------
# 2. A change to one binding changes its row and the digest
# --------------------------------------------------------------------------

def test_changing_a_roles_quant_changes_the_row_and_the_digest(tmp_path):
    before = manifest(tmp_path)
    after = manifest(tmp_path, APP.replace("quant q4_k_m", "quant q5_k_m"))
    assert only_row(before)["demand"]["quant"] == "q4_k_m"
    assert only_row(after)["demand"]["quant"] == "q5_k_m"
    assert before["digest"] != after["digest"]


def test_changing_a_roles_memory_changes_the_row_and_the_digest(tmp_path):
    before = manifest(tmp_path)
    after = manifest(tmp_path, APP.replace("memory 6144", "memory 4096"))
    assert only_row(after)["demand"]["memory_mib"] == 4096
    assert before["digest"] != after["digest"]


def test_changing_the_device_a_role_binds_to_changes_the_row_and_the_digest(tmp_path):
    before = manifest(tmp_path, devices=[gpu("gpu0")])
    after = manifest(tmp_path, devices=[gpu("gpu1")])
    assert only_row(before)["device"] == "gpu0"
    assert only_row(after)["device"] == "gpu1"
    assert before["digest"] != after["digest"]


def test_a_role_falling_back_to_another_device_class_changes_the_record(tmp_path):
    cpu = {"name": "cpu0", "device": "cpu", "memory_mib": 4096,
           "quantisation": ["int8"]}
    before = manifest(tmp_path, devices=[gpu(), cpu])
    after = manifest(tmp_path, devices=[cpu])
    assert only_row(before)["role"] == "fast"
    assert (only_row(after)["role"], only_row(after)["device_class"]) == (
        "small", "cpu")
    assert before["digest"] != after["digest"]


def test_a_change_to_a_declared_device_changes_the_digest(tmp_path):
    before = manifest(tmp_path, devices=[gpu(memory=8192)])
    after = manifest(tmp_path, devices=[gpu(memory=16384)])
    assert only_row(before) == only_row(after)
    assert before["digest"] != after["digest"]


# --------------------------------------------------------------------------
# 3. No routes: no manifest, and nothing an operator reads changes
# --------------------------------------------------------------------------

def test_a_composition_with_no_routes_has_no_manifest(tmp_path):
    assert manifest(tmp_path, PLAIN) is None
    assert ms.binding_lines(None) == []


def _audit(tmp_path, capsys, source, placement, argv_extra=()):
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    plc = tmp_path / "placement.toml"
    plc.write_text(placement, encoding="utf-8")
    rc = main(["audit", str(app), "--placement", str(plc), *argv_extra])
    return rc, capsys.readouterr().out


PLACEMENT = """
[processes.edge]
components = ["First", "Second"]

[[processes.edge.devices]]
name = "gpu0"
device = "gpu"
memory_mib = 8192
quantisation = ["q4_k_m"]
"""


def test_audit_placement_shows_each_binding_and_the_digest(tmp_path, capsys):
    rc, out = _audit(tmp_path, capsys, APP, PLACEMENT)
    assert rc == 0, out
    record = manifest(tmp_path, devices=[gpu(quant=("q4_k_m",))])
    assert ("model binding [edge]: fast on gpu0 (gpu), device gpu memory 6144 "
            "quant q4_k_m; used by First.classify confidential, "
            "Second.classify confidential") in out
    assert f"model bindings digest: {record['digest']}" in out


def test_audit_placement_refuses_a_placement_that_cannot_be_scheduled(
        tmp_path, capsys):
    rc, out = _audit(tmp_path, capsys, APP,
                     PLACEMENT.replace("memory_mib = 8192", "memory_mib = 256"))
    assert rc == 1
    assert "model bindings: error: host `edge` cannot place action" in out


def test_audit_output_for_a_plain_composition_is_unchanged(
        tmp_path, capsys, monkeypatch):
    placement = '[processes.edge]\ncomponents = ["First"]\n'
    rc, with_view = _audit(tmp_path, capsys, PLAIN, placement)
    assert rc == 0
    monkeypatch.setattr(_placement, "model_binding_view",
                        lambda files, placement: ([], None))
    rc, without_view = _audit(tmp_path, capsys, PLAIN, placement)
    assert rc == 0
    assert with_view == without_view
    assert "model binding" not in with_view


def test_the_conductor_prints_the_same_digest(tmp_path, capsys):
    """`run_placement` prints the conductor's view through `_model_schedules`,
    before anything spawns; the digest it prints is the manifest's."""
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    processes = {"edge": {"components": ["First", "Second"],
                          "devices": [gpu(quant=("q4_k_m",))]}}
    problem, handoffs = _placement._model_schedules([str(app)], processes)
    assert problem is None and set(handoffs) == {"edge"}
    out = capsys.readouterr().out
    record = manifest(tmp_path, devices=[gpu(quant=("q4_k_m",))])
    assert f"  model bindings digest: {record['digest']}" in out
    assert "  model binding [edge]: fast on gpu0 (gpu)" in out


def test_the_conductor_prints_nothing_for_a_plain_composition(tmp_path, capsys):
    app = tmp_path / "app.rvl"
    app.write_text(PLAIN, encoding="utf-8")
    problem, handoffs = _placement._model_schedules(
        [str(app)], {"edge": {"components": ["First"]}})
    assert (problem, handoffs) == (None, {})
    assert capsys.readouterr().out == ""
