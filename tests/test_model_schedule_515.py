"""The model scheduler (roadmap item 515, issue #1189, slice S4).

`docs/model-scheduling.md` is the page and `docs/design/539-model-portfolio.md`
section 10 the design. Slice 1 made a model role's device DEMAND checkable;
this is the decision that reads a host's declared devices (the SUPPLY) and
picks, for every routed action, one of the candidates the program named, or
refuses.

The central assertion is the first group: ONE admitted program, scheduled
against different declared device profiles, lands on different roles, and on
a host where nothing fits it is refused. The later groups pin the rules that
make the decision more than a lookup: a role is loaded once per host and
shared, memory is accounted across placements, the search backtracks rather
than giving up on its first greedy choice, a council places every member, and
a malformed host profile is a refusal. The last group drives the placement
conductor end to end.

Programs are inline for the census reason `tests/test_model_portfolio_515.py`
gives: `examples/` and `tests/fixtures/` are corpus roots.
"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import model_schedule as ms  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.parser import Parser  # noqa: E402

# The flagship program of slice 1, unchanged: one member at two quantisation
# points, written as two profiled roles and one ordered candidate set.
PORTFOLIO = """
model role fast on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory 512 quant int8
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> fast | small,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
"""


def gpu(name="gpu0", memory=8192, quant=("q4_k_m",)):
    return {"name": name, "device": "gpu", "memory_mib": memory,
            "quantisation": list(quant)}


def cpu(name="cpu0", memory=16384, quant=("int8",)):
    return {"name": name, "device": "cpu", "memory_mib": memory,
            "quantisation": list(quant)}


def plan(source, devices, host="edge"):
    """Admit `source`, then schedule every routed action onto `devices`."""
    compile_source(source, "schedule.rvl")
    roles, table = ms.routes_of(Parser(source, "schedule.rvl").parse())
    return ms.schedule(host, ms.parse_devices(host, devices), roles,
                       ms.steps_for(table))


def refusal(source, devices, host="edge") -> str:
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        plan(source, devices, host)
    return str(excinfo.value)


def chosen(schedule) -> dict:
    return {(p.component, p.action, p.origin): (p.role, p.device)
            for p in schedule.placements}


# --------------------------------------------------------------------------
# 1. One program, three device profiles, three decisions
# --------------------------------------------------------------------------

def test_a_gpu_host_runs_the_confidential_arm_on_the_head_candidate():
    decided = plan(PORTFOLIO, [gpu(), cpu()])
    assert chosen(decided)[("Classifier", "classify", "confidential")] == (
        "fast", "gpu0")
    assert decided.resident == {"fast": "gpu0"}


def test_a_cpu_only_host_falls_back_to_the_next_named_candidate():
    decided = plan(PORTFOLIO, [cpu(memory=1024)], host="pi")
    assert chosen(decided)[("Classifier", "classify", "confidential")] == (
        "small", "cpu0")
    placement = decided.placements[0]
    assert placement.rank == 1
    assert "fallback 1 of 1" in decided.lines()[0]


def test_the_decision_changes_with_the_declared_device_profile():
    """The same admitted program, three hosts: the device profile is an input
    to the decision, not a label on it."""
    arm = ("Classifier", "classify", "confidential")
    on_gpu = chosen(plan(PORTFOLIO, [gpu(), cpu()]))[arm]
    on_cpu = chosen(plan(PORTFOLIO, [cpu(memory=1024)], host="pi"))[arm]
    assert on_gpu == ("fast", "gpu0")
    assert on_cpu == ("small", "cpu0")
    message = refusal(PORTFOLIO, [cpu(memory=256)], host="tiny")
    assert "host `tiny` cannot place action `classify` (Classifier)" in message


def test_the_refusal_names_every_candidate_and_why_it_misses():
    message = refusal(PORTFOLIO, [cpu(memory=256)], host="tiny")
    assert "origin `confidential`" in message
    assert "`fast` (device gpu memory 6144 quant q4_k_m): no gpu device" in message
    assert ("`small` (device cpu memory 512 quant int8): cpu0 has 256 of 256 "
            "MiB free, needs 512") in message
    assert "[[processes.tiny.devices]]" in message


def test_a_host_that_declares_no_devices_offers_none():
    message = refusal(PORTFOLIO, None, host="bare")
    assert "host `bare` declares no devices" in message


def test_a_quantisation_the_device_does_not_load_is_a_miss():
    message = refusal(PORTFOLIO, [gpu(quant=("fp16",)), cpu(quant=("fp16",))])
    assert "gpu0 does not load quant q4_k_m" in message
    assert "cpu0 does not load quant int8" in message


def test_an_npu_does_not_stand_in_for_a_gpu():
    npu = {"name": "npu0", "device": "npu", "memory_mib": 65536,
           "quantisation": ["q4_k_m", "int8"]}
    assert "no gpu device" in refusal(PORTFOLIO, [npu])


def test_an_off_device_arm_reserves_nothing_on_the_host():
    decided = plan(PORTFOLIO, [gpu()])
    catch_all = chosen(decided)[("Classifier", "classify", "*")]
    assert catch_all == ("cloud", None)
    assert "cloud" not in decided.resident


def test_an_unprofiled_single_role_reserves_nothing():
    source = PORTFOLIO.replace("confidential -> fast | small",
                               "confidential -> local").replace(
        "model role cloud off_device",
        "model role cloud off_device\nmodel role local on_device")
    decided = plan(source, None, host="bare")
    assert chosen(decided)[("Classifier", "classify", "confidential")] == (
        "local", None)
    assert decided.placements[0].how == ms.UNPROFILED


# --------------------------------------------------------------------------
# 2. One provision per role, and memory accounted across placements
# --------------------------------------------------------------------------

TWO_CONSUMERS = """
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


def test_two_consumers_of_one_role_share_one_load():
    """8192 MiB holds one 6144 MiB load, not two: both consumers get `fast`
    only because the second reuses the first's provision."""
    decided = plan(TWO_CONSUMERS, [gpu(memory=8192)])
    assert [(p.component, p.role, p.device, p.how) for p in decided.placements] == [
        ("First", "fast", "gpu0", ms.RESERVED),
        ("Second", "fast", "gpu0", ms.SHARED),
    ]
    assert decided.loads() == 1


def test_two_distinct_roles_contend_for_memory_and_the_second_falls_back():
    """The control for sharing: the same two consumers on two DIFFERENT gpu
    roles need 12288 MiB, so the second falls back to its cpu candidate."""
    source = TWO_CONSUMERS.replace(
        "model role small",
        "model role fast2 on_device device gpu memory 6144 quant q4_k_m\n"
        "model role small").replace(
        "component Second provides other: Answer {\n"
        "  route model on classify { confidential -> fast | small }",
        "component Second provides other: Answer {\n"
        "  route model on classify { confidential -> fast2 | small }")
    decided = plan(source, [gpu(memory=8192), cpu()])
    assert [(p.component, p.role, p.device) for p in decided.placements] == [
        ("First", "fast", "gpu0"), ("Second", "small", "cpu0")]
    assert decided.loads() == 2


def test_a_contention_refusal_names_what_holds_the_memory():
    source = TWO_CONSUMERS.replace(
        "model role small",
        "model role fast2 on_device device gpu memory 6144 quant q4_k_m\n"
        "model role small").replace(
        "component Second provides other: Answer {\n"
        "  route model on classify { confidential -> fast | small }",
        "component Second provides other: Answer {\n"
        "  route model on classify { confidential -> fast2 }")
    message = refusal(source, [gpu(memory=8192)])
    assert "cannot place action `classify` (Second)" in message
    assert "gpu0 has 2048 of 8192 MiB free, held by fast, needs 6144" in message


def test_the_search_backtracks_instead_of_keeping_its_first_choice():
    """First prefers `big` (6144 on gpu). Second can only run `mid` (4096 on
    gpu). Greedy first-fit gives First `big` and then cannot place Second on
    the remaining 2048; the search moves First to its fallback instead."""
    source = """
model role big on_device device gpu memory 6144 quant q4_k_m
model role mid on_device device gpu memory 4096 quant q4_k_m
model role small on_device device cpu memory 512 quant int8

service Answer { fn classify(text: Str) -> Str }

component First provides out: Answer {
  route model on classify { confidential -> big | small }
  provide out { fn classify(text) = text }
}

component Second provides other: Answer {
  route model on classify { confidential -> mid }
  provide other { fn classify(text) = text }
}
"""
    decided = plan(source, [gpu(memory=8192), cpu()])
    assert [(p.component, p.role, p.device) for p in decided.placements] == [
        ("First", "small", "cpu0"), ("Second", "mid", "gpu0")]


def test_devices_are_tried_in_declared_order():
    decided = plan(PORTFOLIO, [gpu("gpu1"), gpu("gpu0")])
    assert decided.resident == {"fast": "gpu1"}


def test_a_search_that_does_not_settle_is_refused(monkeypatch):
    monkeypatch.setattr(ms, "SEARCH_BUDGET", 0)
    message = refusal(PORTFOLIO, [gpu()])
    assert "did not settle within 0 placement attempts" in message


# --------------------------------------------------------------------------
# 3. A council places every member
# --------------------------------------------------------------------------

COUNCIL = """
model role edge on_device device gpu memory 4096 quant q4_k_m
model role aux on_device device cpu memory 512 quant int8

service Answer { fn classify(text: Str) -> Str }

model council Release {
  proposer  -> edge,
  verifier  -> aux,
  aggregate unanimous
}

component Classifier provides out: Answer {
  route model on classify { confidential -> Release }
  provide out { fn classify(text) = text }
}
"""


def test_a_council_arm_places_every_member():
    decided = plan(COUNCIL, [gpu(), cpu()])
    assert [(p.role, p.device, p.council) for p in decided.placements] == [
        ("edge", "gpu0", "Release"), ("aux", "cpu0", "Release")]


def test_a_council_with_one_member_that_fits_nowhere_is_refused():
    message = refusal(COUNCIL, [gpu()])
    assert "(member of council `Release`)" in message
    assert "`aux` (device cpu memory 512 quant int8): no cpu device" in message


# --------------------------------------------------------------------------
# 4. The host profile is closed configuration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("devices, expected", [
    ({"name": "gpu0"}, "must be a list of tables"),
    (["gpu0"], "must be a table"),
    ([dict(gpu(), vram=1)], "unknown key(s) vram"),
    ([{"name": "gpu0", "device": "gpu", "memory_mib": 8192}],
     "missing quantisation"),
    ([dict(gpu(), device="gpu0")], "unknown device class 'gpu0'"),
    ([dict(gpu(), memory_mib=0)], "must be a positive integer"),
    ([dict(gpu(), memory_mib=True)], "must be a positive integer"),
    ([dict(gpu(), memory_mib="8192")], "must be a positive integer"),
    ([dict(gpu(), quantisation=[])], "non-empty list of tags"),
    ([dict(gpu(), quantisation="q4_k_m")], "non-empty list of tags"),
    ([dict(gpu(), name="")], "`name` must be a non-empty string"),
    ([gpu(), gpu()], "declares device `gpu0` twice"),
])
def test_a_malformed_device_table_is_refused(devices, expected):
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.parse_devices("edge", devices)
    assert expected in str(excinfo.value)


def test_a_valid_device_table_parses_to_the_declared_values():
    (device,) = ms.parse_devices("edge", [gpu(quant=("q4_k_m", "int8"))])
    assert device == ms.Device("gpu0", "gpu", 8192, ("q4_k_m", "int8"))


# --------------------------------------------------------------------------
# 5. The placement conductor, end to end, with the child runner stubbed
# --------------------------------------------------------------------------

class _StubProc:
    """Minimal Popen stand-in (the pattern of tests/test_network_placement.py)."""

    def __init__(self, name):
        self.name = name
        self._lines = [f"[{name}] UP"]
        self._down = False
        self.stdin = self
        self.returncode = 0

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self._lines:
                return self._lines.pop(0)
            if self._down:
                raise StopIteration
            time.sleep(0.005)

    def write(self, _text):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return 0 if self._down else None

    def wait(self, timeout=None):
        self._teardown()
        return 0

    def terminate(self):
        self._teardown()

    def kill(self):
        self._teardown()

    def _teardown(self):
        if not self._down:
            self._lines.append(f"[{self.name}] DOWN")
            self._down = True


def _conduct(tmp_path, monkeypatch, capsys, sources: dict, placement: str):
    procs: dict = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        procs[spec["name"]] = _StubProc(spec["name"])
        return procs[spec["name"]]

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    files = []
    for name, text in sources.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        files.append(str(path))
    plc = tmp_path / "placement.toml"
    plc.write_text(placement, encoding="utf-8")
    rc = _placement.run_placement(files, str(plc), once=True)
    out = capsys.readouterr()
    return rc, procs, out.out, out.err


GPU_HOST = """
[processes.edge]
components = ["Classifier"]

[[processes.edge.devices]]
name = "gpu0"
device = "gpu"
memory_mib = 8192
quantisation = ["q4_k_m"]
"""

CPU_HOST = """
[processes.pi]
components = ["Classifier"]

[[processes.pi.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 1024
quantisation = ["int8"]
"""


def test_the_conductor_prints_the_gpu_decision_and_runs(tmp_path, monkeypatch, capsys):
    rc, procs, out, err = _conduct(tmp_path, monkeypatch, capsys,
                                   {"app.rvl": PORTFOLIO}, GPU_HOST)
    assert rc == 0, err
    assert ("model schedule [edge]: Classifier.classify confidential -> fast "
            "on gpu0") in out
    assert set(procs) == {"edge"}


def test_the_conductor_prints_the_cpu_decision_for_the_same_program(
        tmp_path, monkeypatch, capsys):
    rc, _procs, out, err = _conduct(tmp_path, monkeypatch, capsys,
                                    {"app.rvl": PORTFOLIO}, CPU_HOST)
    assert rc == 0, err
    assert ("model schedule [pi]: Classifier.classify confidential -> small "
            "on cpu0, fallback 1 of 1") in out


def test_the_conductor_refuses_before_anything_spawns(tmp_path, monkeypatch, capsys):
    rc, procs, _out, err = _conduct(
        tmp_path, monkeypatch, capsys, {"app.rvl": PORTFOLIO},
        CPU_HOST.replace("memory_mib = 1024", "memory_mib = 256"))
    assert rc != 0
    assert procs == {}
    assert "host `pi` cannot place action `classify` (Classifier)" in err


def test_the_conductor_refuses_a_profiled_program_on_a_host_without_devices(
        tmp_path, monkeypatch, capsys):
    rc, procs, _out, err = _conduct(
        tmp_path, monkeypatch, capsys, {"app.rvl": PORTFOLIO},
        '[processes.edge]\ncomponents = ["Classifier"]\n')
    assert rc != 0
    assert procs == {}
    assert "host `edge` declares no devices" in err


def test_a_tiers_form_placement_has_no_devices_to_offer(tmp_path, monkeypatch, capsys):
    """The `[tiers]` form synthesizes its processes and has nowhere to declare
    devices, so a profiled program placed with it is refused, not run
    unscheduled."""
    rc, procs, _out, err = _conduct(tmp_path, monkeypatch, capsys,
                                    {"app.rvl": PORTFOLIO}, 'default_tier = "py"\n')
    assert rc != 0
    assert procs == {}
    assert "host `tier_py` declares no devices" in err


def test_the_conductor_refuses_a_malformed_device_table(tmp_path, monkeypatch, capsys):
    rc, procs, _out, err = _conduct(
        tmp_path, monkeypatch, capsys, {"app.rvl": PORTFOLIO},
        GPU_HOST.replace('device = "gpu"', 'device = "tpu"'))
    assert rc != 0
    assert procs == {}
    assert "unknown device class 'tpu'" in err


PLAIN = """
service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(text) = text }
}
"""


def test_a_composition_without_route_model_schedules_nothing(
        tmp_path, monkeypatch, capsys):
    rc, procs, out, err = _conduct(
        tmp_path, monkeypatch, capsys, {"app.rvl": PLAIN},
        '[processes.edge]\ncomponents = ["Classifier"]\n')
    assert rc == 0, err
    assert set(procs) == {"edge"}
    assert "model schedule" not in out


ROLES_FILE = """
model role fast on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory 512 quant int8

service Answer { fn classify(text: Str) -> Str }

component First provides out: Answer {
  route model on classify { confidential -> fast | small }
  provide out { fn classify(text) = text }
}
"""

SECOND_FILE = """
service Other { fn label(text: Str) -> Str }

component Second provides other: Other {
  route model on label { confidential -> fast | small }
  provide other { fn label(text) = text }
}
"""


def test_two_files_on_one_host_share_one_load(tmp_path, monkeypatch, capsys):
    """A role declared in one root file and routed to from another: the
    conductor reads the whole composition, and the two consumers share one
    load on the host they share."""
    placement = GPU_HOST.replace('components = ["Classifier"]',
                                 'components = ["First", "Second"]')
    rc, _procs, out, err = _conduct(
        tmp_path, monkeypatch, capsys,
        {"roles.rvl": ROLES_FILE, "second.rvl": SECOND_FILE}, placement)
    assert rc == 0, err
    assert "First.classify confidential -> fast on gpu0" in out
    assert ("Second.label confidential -> fast on gpu0, shared with an "
            "earlier placement") in out


def test_two_hosts_each_load_their_own_copy(tmp_path, monkeypatch, capsys):
    """One provision per role PER HOST: the same role on two hosts is two
    loads, and each host is scheduled against its own devices."""
    placement = GPU_HOST.replace('components = ["Classifier"]',
                                 'components = ["First"]') + """
[processes.pi]
components = ["Second"]

[[processes.pi.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 1024
quantisation = ["int8"]
"""
    rc, _procs, out, err = _conduct(
        tmp_path, monkeypatch, capsys,
        {"roles.rvl": ROLES_FILE, "second.rvl": SECOND_FILE}, placement)
    assert rc == 0, err
    assert "model schedule [edge]: First.classify confidential -> fast on gpu0" in out
    assert ("model schedule [pi]: Second.label confidential -> small on cpu0, "
            "fallback 1 of 1") in out
