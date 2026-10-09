"""The model schedule reaches the child that runs the model (roadmap item 515,
issue #1189, slice S4 part 2).

`tests/test_model_schedule_515.py` pins the plan-time decision. This file pins
what makes that decision enforced rather than printed:

* the conductor hands each host's schedule to its child in the spec it
  already writes (`spec["modelSchedule"]`), and to nothing else;
* the py runner re-derives the schedule from the composition's files and the
  host's declared devices, and refuses to boot on a missing, unexpected or
  tampered one;
* code in the child reads the scheduled device through
  `revl.model_placement`, and asking for a role that is not scheduled there,
  or claiming a device other than the scheduled one, is refused by name;
* a composition with no `route model` block spawns exactly as before.

Groups 1 and 2 run in-process. Group 3 boots real py children through
`run_placement`, which needs the pinned cordis-py runtime; without it those
tests SKIP, and a skip there is not a pass (CI's frontend-cordis job runs
them).

Programs are inline for the census reason `tests/test_model_portfolio_515.py`
gives.
"""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import model_placement as mp  # noqa: E402
from revl import model_schedule as ms  # noqa: E402
from revl import placement as _placement  # noqa: E402

# Two probes read the device a role is scheduled on, and two claim one. The
# role and device are literals inside the program rather than probe
# arguments, because the runner redacts a probe's own arguments from its
# output (issue #814) and these tests read the names back.
APP = """
model role fast on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory 512 quant int8

service Answer {
  fn classify(text: Str) -> Str
  fn fast() -> Str
  fn small() -> Str
  fn claim_scheduled() -> Str
  fn claim_other() -> Str
}

extern pure fn model_device(role: Str) -> Str
  = @py { from revl import model_placement; return model_placement.device_for(role) }

extern pure fn claim_device(role: Str, device: Str) -> Str
  = @py { from revl import model_placement; return model_placement.claim(role, device) }

component Classifier provides out: Answer {
  route model on classify { confidential -> fast | small }
  provide out {
    fn classify(text) = text
    fn fast() = model_device("fast")
    fn small() = model_device("small")
    fn claim_scheduled() = claim_device("fast", "gpu0")
    fn claim_other() = claim_device("fast", "gpu1")
  }
}
"""

PLAIN = """
service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(text) = text }
}
"""

PROBES = ('probe = ["out.fast()", "out.small()", "out.claim_scheduled()", '
          '"out.claim_other()"]\n')

GPU_HOST = """
[processes.edge]
components = ["Classifier"]
""" + PROBES + """
[[processes.edge.devices]]
name = "gpu0"
device = "gpu"
memory_mib = 8192
quantisation = ["q4_k_m"]
"""

CPU_HOST = """
[processes.pi]
components = ["Classifier"]
""" + PROBES + """
[[processes.pi.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 1024
quantisation = ["int8"]
"""


def _write(tmp_path, source: str, placement: str):
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    plc = tmp_path / "placement.toml"
    plc.write_text(placement, encoding="utf-8")
    return str(app), str(plc)


@pytest.fixture
def clean_registry():
    """Every test that installs a schedule leaves the process as it found it."""
    before = mp.installed()
    mp.uninstall()
    try:
        yield
    finally:
        mp.uninstall()
        if before is not None:
            mp.install(before["host"], before["resident"])
    assert mp.installed() == before


# --------------------------------------------------------------------------
# 1. The registry code in the child reads
# --------------------------------------------------------------------------

def test_a_scheduled_role_answers_its_device(clean_registry):
    mp.install("edge", {"fast": "gpu0"})
    assert mp.device_for("fast") == "gpu0"
    assert mp.claim("fast", "gpu0") == "gpu0"


def test_claiming_another_device_is_refused_by_name(clean_registry):
    mp.install("edge", {"fast": "gpu0"})
    with pytest.raises(mp.ModelPlacementRefused) as excinfo:
        mp.claim("fast", "gpu1")
    assert excinfo.value.role == "fast"
    assert ("model role `fast` was claimed on device `gpu1`, but the schedule "
            "for host `edge` places it on `gpu0`") in str(excinfo.value)


def test_a_role_not_scheduled_here_is_refused(clean_registry):
    mp.install("edge", {"fast": "gpu0"})
    with pytest.raises(mp.ModelPlacementRefused) as excinfo:
        mp.device_for("small")
    assert ("model role `small` is not scheduled on host `edge` (scheduled "
            "here: fast on gpu0)") in str(excinfo.value)


def test_a_process_with_no_schedule_answers_nothing(clean_registry):
    with pytest.raises(mp.ModelPlacementRefused) as excinfo:
        mp.device_for("fast")
    assert "was handed no model schedule" in str(excinfo.value)


def test_a_second_different_schedule_is_refused(clean_registry):
    mp.install("edge", {"fast": "gpu0"})
    mp.install("edge", {"fast": "gpu0"})
    with pytest.raises(mp.ModelPlacementRefused):
        mp.install("edge", {"fast": "gpu1"})
    assert mp.device_for("fast") == "gpu0"


def test_the_installed_schedule_cannot_be_edited_through_the_view(clean_registry):
    mp.install("edge", {"fast": "gpu0"})
    view = mp.installed()
    view["resident"]["fast"] = "gpu1"
    assert mp.device_for("fast") == "gpu0"


# --------------------------------------------------------------------------
# 2. The handoff and its verification, in-process
# --------------------------------------------------------------------------

def _entry(tmp_path, source=APP, placement=GPU_HOST, host="edge"):
    app, _plc = _write(tmp_path, source, placement)
    devices = [{"name": "gpu0", "device": "gpu", "memory_mib": 8192,
                "quantisation": ["q4_k_m"]}]
    decided = ms.placement_schedules(
        [app], {host: {"components": ["Classifier"], "devices": devices}})
    return app, ms.handoff(decided[0])


def test_the_handoff_carries_the_devices_and_the_decision(tmp_path):
    _app, entry = _entry(tmp_path)
    assert entry["host"] == "edge"
    assert entry["devices"] == [{"name": "gpu0", "device": "gpu",
                                 "memory_mib": 8192, "quantisation": ["q4_k_m"]}]
    assert entry["schedule"]["resident"] == {"fast": "gpu0"}
    assert json.loads(json.dumps(entry)) == entry


def test_an_untouched_handoff_verifies(tmp_path):
    app, entry = _entry(tmp_path)
    assert ms.verify_handoff([app], "edge", ["Classifier"], entry) == {"fast": "gpu0"}


def test_a_tampered_device_is_refused(tmp_path):
    app, entry = _entry(tmp_path)
    entry["schedule"]["resident"]["fast"] = "gpu1"
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "does not match the one derived" in str(excinfo.value)
    assert "'fast': 'gpu1'" in str(excinfo.value)


def test_a_tampered_placement_is_refused(tmp_path):
    app, entry = _entry(tmp_path)
    entry["schedule"]["placements"][0]["rank"] = 1
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "the placements differ" in str(excinfo.value)


def test_a_missing_handoff_is_refused_when_the_host_routes_a_model(tmp_path):
    app, _entry_ = _entry(tmp_path)
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], None)
    assert "its spec carries no model schedule" in str(excinfo.value)


def test_a_handoff_for_another_host_is_refused(tmp_path):
    app, entry = _entry(tmp_path)
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "pi", ["Classifier"], entry)
    assert "was handed the model schedule of host `edge`" in str(excinfo.value)


def test_a_handoff_to_a_host_that_routes_nothing_is_refused(tmp_path):
    app, entry = _entry(tmp_path)
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", [], entry)
    assert "routes no model action" in str(excinfo.value)


def test_a_malformed_handoff_is_refused(tmp_path):
    app, entry = _entry(tmp_path)
    del entry["devices"]
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "is malformed" in str(excinfo.value)


def test_a_plain_host_needs_and_gets_nothing(tmp_path):
    app, _plc = _write(tmp_path, PLAIN, "")
    assert ms.verify_handoff([app], "edge", ["Classifier"], None) is None


# --------------------------------------------------------------------------
# 2b. The residency an opted-in arm ranked on (item 2118)
# --------------------------------------------------------------------------

# `APP` with the clause on the one arm that has a fallback, and a host that
# declares both devices so `small` can really be placed when residency moves
# it first. Everything else is byte-identical to the program above.
OPTED_IN_APP = APP.replace(
    "route model on classify { confidential -> fast | small }",
    "route model on classify { confidential -> fast | small prefer resident }")

BOTH_DEVICES = """
[processes.edge]
components = ["Classifier"]
""" + PROBES + """
[[processes.edge.devices]]
name = "gpu0"
device = "gpu"
memory_mib = 8192
quantisation = ["q4_k_m"]

[[processes.edge.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 16384
quantisation = ["int8"]
"""

DEVICES = [{"name": "gpu0", "device": "gpu", "memory_mib": 8192,
            "quantisation": ["q4_k_m"]},
           {"name": "cpu0", "device": "cpu", "memory_mib": 16384,
            "quantisation": ["int8"]}]


def _decide(tmp_path, source, residency=None):
    """Write `source` for a host that declares both devices, decide its
    schedule with `residency` as what the host already holds, and hand back
    the file and the decision."""
    app, _plc = _write(tmp_path, source, BOTH_DEVICES)
    decided = ms.placement_schedules(
        [app], {"edge": {"components": ["Classifier"], "devices": DEVICES}},
        residency={"edge": residency or {}})
    return app, decided[0]


def test_an_arm_that_did_not_opt_in_carries_no_residency(tmp_path):
    """An input no arm read is not part of the decision, so it is not carried:
    the entry is byte for byte the one this program got before the clause
    existed, whatever the host reports."""
    app, decided = _decide(tmp_path, APP, {"small": "cpu0"})
    assert decided.resident == {"fast": "gpu0"}
    entry = ms.handoff(decided)
    assert "residency" not in entry
    assert ms.verify_handoff([app], "edge", ["Classifier"], entry) == \
        {"fast": "gpu0"}


def test_an_opted_in_handoff_carries_its_residency_and_verifies(tmp_path):
    app, decided = _decide(tmp_path, OPTED_IN_APP, {"small": "cpu0"})
    assert decided.resident == {"small": "cpu0"}
    entry = ms.handoff(decided)
    assert entry["residency"] == {"small": "cpu0"}
    # The child re-derives the SAME decision from the input the conductor used
    # rather than believing the entry: same placement, same written rank.
    assert ms.verify_handoff([app], "edge", ["Classifier"], entry) == \
        {"small": "cpu0"}
    assert entry["schedule"]["placements"][0]["rank"] == 1


def test_a_residency_no_arm_reads_is_refused(tmp_path):
    """The child is handed an input it must consume. A residency for a program
    whose arms all left the clause out is refused rather than carried, for the
    reason a schedule for a host that routes nothing is: nothing would read
    it."""
    app, decided = _decide(tmp_path, APP)
    entry = ms.handoff(decided)
    entry["residency"] = {"small": "cpu0"}
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "no arm placed here wrote `prefer resident`" in str(excinfo.value)


def test_a_malformed_residency_is_refused(tmp_path):
    app, decided = _decide(tmp_path, OPTED_IN_APP, {"small": "cpu0"})
    entry = ms.handoff(decided)
    entry["residency"] = {"small": 1}
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "must map role names to the device" in str(excinfo.value)


@pytest.mark.parametrize("value", [{}, [], "", 0, False])
def test_an_empty_residency_key_is_refused(tmp_path, value):
    """`handoff` writes the key only for a non-empty residency, so a key that
    is present and empty (or empty of another type) is not one it wrote, and
    is refused rather than read as "holds nothing"."""
    app, decided = _decide(tmp_path, OPTED_IN_APP)
    entry = ms.handoff(decided)
    assert "residency" not in entry
    entry["residency"] = value
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "must map role names to the device" in str(excinfo.value)


@pytest.mark.parametrize("value", [{"small": ""}, {"": "cpu0"}])
def test_an_empty_role_or_device_is_refused(tmp_path, value):
    app, decided = _decide(tmp_path, OPTED_IN_APP, {"small": "cpu0"})
    entry = ms.handoff(decided)
    entry["residency"] = value
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "must map role names to the device" in str(excinfo.value)


def test_a_tampered_residency_is_refused(tmp_path):
    """The residency is an INPUT of the decision, so a different input is a
    different decision - and the child derives the decision rather than
    believing it, which is what catches this."""
    app, decided = _decide(tmp_path, OPTED_IN_APP, {"small": "cpu0"})
    entry = ms.handoff(decided)
    entry["residency"] = {"fast": "gpu0"}
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff([app], "edge", ["Classifier"], entry)
    assert "does not match the one derived" in str(excinfo.value)


# --------------------------------------------------------------------------
# 3. End to end through run_placement, with real py children
# --------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    not _placement._cordis_py_installed(),
    reason="cordis-py runtime not installed (sh backends/python/setup.sh); "
           "these boot real py children")


def _capture_specs(monkeypatch, edit=None):
    """Record every spec the conductor writes, optionally editing it on disk
    before the real child reads it."""
    written: dict = {}
    real_popen = _placement.subprocess.Popen

    def popen(cmd, **kwargs):
        path = Path(str(cmd[-1]))
        if path.name.endswith(".spec.json"):
            spec = json.loads(path.read_text(encoding="utf-8"))
            written[spec["name"]] = path.read_bytes()
            if edit is not None:
                edit(spec)
                path.write_text(json.dumps(spec), encoding="utf-8")
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(_placement.subprocess, "Popen", popen)
    return written


_PROBE = re.compile(r"\] probe \| (.+?)\s*\| (.*)$")


def _probe_lines(out: str) -> dict:
    """`{probe expression: result}` from the children's console lines."""
    lines = {}
    for line in out.splitlines():
        match = _PROBE.search(line)
        if match:
            lines[match.group(1)] = match.group(2).strip()
    return lines


@needs_cordis
def test_the_child_receives_the_scheduled_device(tmp_path, monkeypatch, capfd):
    written = _capture_specs(monkeypatch)
    app, plc = _write(tmp_path, APP, GPU_HOST)
    rc = _placement.run_placement([app], plc, once=True)
    out = capfd.readouterr().out
    assert rc == 0, out
    spec = json.loads(written["edge"])
    assert spec["modelSchedule"]["schedule"]["resident"] == {"fast": "gpu0"}
    probes = _probe_lines(out)
    assert probes["out.fast()"] == "=> 'gpu0'"
    assert probes["out.claim_scheduled()"] == "=> 'gpu0'"
    assert "[edge] UP" in out


@needs_cordis
def test_the_child_refuses_a_role_or_device_it_was_not_scheduled(
        tmp_path, monkeypatch, capfd):
    _capture_specs(monkeypatch)
    app, plc = _write(tmp_path, APP, GPU_HOST)
    assert _placement.run_placement([app], plc, once=True) == 0
    probes = _probe_lines(capfd.readouterr().out)
    assert probes["out.small()"].startswith(
        "ERROR ModelPlacementRefused: model role `small` is not scheduled on "
        "host `edge` (scheduled here: fast on gpu0)")
    assert probes["out.claim_other()"].startswith(
        "ERROR ModelPlacementRefused: model role `fast` was claimed on device "
        "`gpu1`, but the schedule for host `edge` places it on `gpu0`")


@needs_cordis
def test_a_different_device_profile_reaches_the_child_as_a_different_answer(
        tmp_path, monkeypatch, capfd):
    _capture_specs(monkeypatch)
    app, plc = _write(tmp_path, APP, CPU_HOST)
    rc = _placement.run_placement([app], plc, once=True)
    out = capfd.readouterr().out
    assert rc == 0, out
    probes = _probe_lines(out)
    assert probes["out.small()"] == "=> 'cpu0'"
    assert probes["out.fast()"].startswith(
        "ERROR ModelPlacementRefused: model role `fast` is not scheduled on "
        "host `pi` (scheduled here: small on cpu0)")


@needs_cordis
def test_a_tampered_schedule_is_refused_at_boot(tmp_path, monkeypatch, capfd):
    def tamper(spec):
        spec["modelSchedule"]["schedule"]["resident"]["fast"] = "gpu1"

    _capture_specs(monkeypatch, edit=tamper)
    app, plc = _write(tmp_path, APP, GPU_HOST)
    rc = _placement.run_placement([app], plc, once=True)
    out = capfd.readouterr().out
    assert rc != 0
    assert "[edge] BOOT REFUSED: host `edge`: the model schedule in its spec" in out
    assert "[edge] UP" not in out
    assert "probe" not in out


@needs_cordis
def test_a_missing_schedule_is_refused_at_boot(tmp_path, monkeypatch, capfd):
    def drop(spec):
        del spec["modelSchedule"]

    _capture_specs(monkeypatch, edit=drop)
    app, plc = _write(tmp_path, APP, GPU_HOST)
    rc = _placement.run_placement([app], plc, once=True)
    out = capfd.readouterr().out
    assert rc != 0
    assert ("[edge] BOOT REFUSED: host `edge` routes model action(s) but its "
            "spec carries no model schedule") in out
    assert "[edge] UP" not in out


def test_a_scheduled_host_on_a_tier_that_does_not_read_it_is_refused(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(_placement.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or None)
    app, plc = _write(tmp_path, APP,
                      GPU_HOST.replace("[processes.edge]\n",
                                       '[processes.edge]\nbackend = "node"\n'))
    rc = _placement.run_placement([app], plc, once=True)
    err = capsys.readouterr().err
    assert rc != 0
    assert spawned == []
    assert "placed on the node tier, whose process runner does not read" in err


@needs_cordis
def test_a_composition_without_route_model_spawns_exactly_as_before(
        tmp_path, monkeypatch, capfd):
    """The spec a plain composition's child gets is byte-identical to the one
    written with the schedule step removed, carries no schedule key, and the
    child's probe output is unchanged."""
    placement = '[processes.edge]\ncomponents = ["Classifier"]\n' \
                'probe = ["out.classify(\'hi\')"]\n'

    def run_once(disable: bool):
        if disable:
            monkeypatch.setattr(_placement, "_model_schedules",
                                lambda files, processes, residency=None:
                                (None, {}))
        written = _capture_specs(monkeypatch)
        work = tmp_path / ("off" if disable else "on")
        work.mkdir()
        app, plc = _write(work, PLAIN, placement)
        rc = _placement.run_placement([app], plc, once=True)
        out = capfd.readouterr().out
        assert rc == 0, out
        spec = written["edge"].decode("utf-8").replace(str(work), "<work>")
        return spec, _probe_lines(out)

    with_step, probes_on = run_once(disable=False)
    without_step, probes_off = run_once(disable=True)
    assert "modelSchedule" not in json.loads(with_step)
    assert _strip_placement_dir(with_step) == _strip_placement_dir(without_step)
    assert probes_on == probes_off == {"out.classify('hi')": "=> 'hi'"}


def _strip_placement_dir(spec_text: str) -> str:
    """The conductor's per-boot mkdtemp directory is the only part of a spec
    that differs between two boots of one placement."""
    spec = json.loads(spec_text)
    return json.dumps(_scrub(spec, _placement_dirs(spec)), sort_keys=True)


def _placement_dirs(value, found=None) -> set:
    found = set() if found is None else found
    if isinstance(value, dict):
        for item in value.values():
            _placement_dirs(item, found)
    elif isinstance(value, list):
        for item in value:
            _placement_dirs(item, found)
    elif isinstance(value, str) and "revl_placement_" in value:
        found.add(value.split("revl_placement_")[0] + "revl_placement_"
                  + value.split("revl_placement_")[1].split("/")[0])
    return found


def _scrub(value, dirs):
    if isinstance(value, dict):
        return {k: _scrub(v, dirs) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, dirs) for v in value]
    if isinstance(value, str):
        for d in dirs:
            value = value.replace(d, "<placement>")
    return value


# --------------------------------------------------------------------------
# 4. A swap successor is scheduled for itself, or the swap refuses
# --------------------------------------------------------------------------

def test_a_swap_successor_gets_a_schedule_it_will_verify(tmp_path):
    app, entry = _entry(tmp_path)
    succ, refusal = _placement._successor_model_schedule(
        [app], {"modelSchedule": entry}, "Classifier__t1", "Classifier", "py")
    assert refusal is None
    assert succ["host"] == "Classifier__t1"
    assert succ["devices"] == entry["devices"]
    assert ms.verify_handoff([app], "Classifier__t1", ["Classifier"], succ) == {
        "fast": "gpu0"}


def test_a_swap_onto_a_tier_that_does_not_read_the_schedule_refuses(tmp_path):
    app, entry = _entry(tmp_path)
    succ, refusal = _placement._successor_model_schedule(
        [app], {"modelSchedule": entry}, "Classifier__t1", "Classifier", "node")
    assert succ is None
    assert "the node tier's runner does not read a model schedule" in refusal


def test_a_swap_candidate_that_no_longer_fits_refuses(tmp_path):
    app, entry = _entry(tmp_path)
    entry["devices"][0]["memory_mib"] = 1024
    succ, refusal = _placement._successor_model_schedule(
        [app], {"modelSchedule": entry}, "Classifier__t1", "Classifier", "py")
    assert succ is None
    assert "cannot place action `classify` (Classifier)" in refusal


def test_a_plain_swap_successor_gets_no_schedule(tmp_path):
    app, _plc = _write(tmp_path, PLAIN, "")
    assert _placement._successor_model_schedule(
        [app], {}, "Classifier__t1", "Classifier", "py") == (None, None)
