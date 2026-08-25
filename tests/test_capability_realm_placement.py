"""Capability/realm-aware host placement (roadmap item 119).

Item 56/149/151 gave placement a *backend* (which tier) and a *network seam*
(which machine); this item adds which HOST by capability and realm. A host
(process) may declare the capabilities it offers (`[processes.<p>] capabilities
= [...]`) and the realm it is pinned to (`realm = "..."`); a component is
refused before anything spawns if it is placed on a host that lacks a
capability it needs (declared in the placement's `[capabilities]` table) or
that is pinned to a realm the component does not belong to (its `isolate`
realms, already in the IR). Both are read off the placement toml + IR — no
`.rvl` grammar change.

Two layers of coverage:

* the pure validators (`capability_realm_diagnostic`, `colocation_advice`),
  driven on a compiled IR directly — no processes;
* the whole conductor via `run_placement(..., once=True)`, with the child
  runner stubbed (the `_StubProc` pattern from tests/test_network_placement.py)
  so a valid placement comes up and a violating one is refused with rc != 0 and
  a naming diagnostic, none of it needing cordis or a real subprocess.

The additivity guard proves a placement that declares neither capabilities nor
a realm produces byte-identical specs to today.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement as _placement  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

APP = ROOT / "examples" / "placement" / "caprealm_app.rvl"


@pytest.fixture(scope="module")
def ir() -> dict:
    return compile_files([str(APP)])


# ---------------------------------------------------------------------------
# 1. the pure validators, on a compiled IR (no processes)
# ---------------------------------------------------------------------------


def test_valid_capability_and_realm_placement_has_no_diagnostic(ir):
    processes = {
        "vault": {"components": ["Vault"], "capabilities": ["seal"]},
        "tenant_a": {"components": ["TenantAStore", "TenantAApp"], "realm": "tenant_a"},
        "tenant_b": {"components": ["TenantBStore", "TenantBApp"], "realm": "tenant_b"},
    }
    required = {"Vault": {"seal"}}
    assert _placement.capability_realm_diagnostic(processes, ir, required) is None


def test_component_on_a_capability_lacking_host_is_refused(ir):
    # the vault host offers "db" but not the "seal" permit Vault needs
    processes = {"vault": {"components": ["Vault"], "capabilities": ["db"]}}
    required = {"Vault": {"seal"}}
    problem = _placement.capability_realm_diagnostic(processes, ir, required)
    assert problem is not None
    assert "Vault" in problem and "'seal'" in problem and "vault" in problem


def test_capability_host_offering_a_superset_is_fine(ir):
    processes = {"vault": {"components": ["Vault"], "capabilities": ["seal", "db", "net"]}}
    assert _placement.capability_realm_diagnostic(processes, ir, {"Vault": {"seal"}}) is None


def test_realm_isolation_violation_is_refused(ir):
    # TenantBApp belongs to realm tenant_b but is placed on a tenant_a-pinned host
    processes = {
        "tenant_a": {"components": ["TenantAStore", "TenantAApp", "TenantBApp"],
                     "realm": "tenant_a"},
    }
    problem = _placement.capability_realm_diagnostic(processes, ir, {})
    assert problem is not None
    assert "TenantBApp" in problem and "tenant_b" in problem and "tenant_a" in problem


def test_unpinned_host_constrains_no_realm(ir):
    # no `realm` on the host -> a realm-isolated component rides freely (additive)
    processes = {"mix": {"components": ["TenantAApp", "TenantBApp"]}}
    assert _placement.capability_realm_diagnostic(processes, ir, {}) is None


def test_shared_component_rides_any_pinned_host(ir):
    # Vault isolates nothing -> belongs to no named realm -> a pinned host takes it
    processes = {"tenant_a": {"components": ["Vault", "TenantAStore", "TenantAApp"],
                              "realm": "tenant_a", "capabilities": ["seal"]}}
    assert _placement.capability_realm_diagnostic(processes, ir, {"Vault": {"seal"}}) is None


def test_colocation_advice_flags_a_split_realm(ir):
    # TenantAStore and TenantAApp (both realm tenant_a) split across two hosts
    placed = {"TenantAStore": "hostX", "TenantAApp": "hostY",
              "TenantBStore": "hostZ", "TenantBApp": "hostZ", "Vault": "hostX"}
    processes = {"hostX": {}, "hostY": {}, "hostZ": {}}
    advice = _placement.colocation_advice(processes, placed, ir)
    assert any("tenant_a" in line for line in advice)
    # tenant_b is not split, so it is not flagged
    assert not any("tenant_b" in line for line in advice)


def test_colocation_advice_silent_when_realms_are_colocated(ir):
    placed = {"TenantAStore": "a", "TenantAApp": "a",
              "TenantBStore": "b", "TenantBApp": "b", "Vault": "a"}
    assert _placement.colocation_advice({"a": {}, "b": {}}, placed, ir) == []


# ---------------------------------------------------------------------------
# 2. the whole conductor, with the child runner stubbed
# ---------------------------------------------------------------------------


class _StubProc:
    """Minimal Popen stand-in (mirrors tests/test_network_placement.py)."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
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

    def write(self, _t):
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


def _run(tmp_path, monkeypatch, placement_text, app=APP):
    procs: dict = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        procs[spec["name"]] = _StubProc(spec["name"], spec)
        return procs[spec["name"]]

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    plc = tmp_path / "p.toml"
    plc.write_text(placement_text, encoding="utf-8")
    rc = _placement.run_placement([str(app)], str(plc), once=True)
    return rc, procs


_VALID = """
[capabilities]
Vault = ["seal"]

[processes.vault]
components = ["Vault"]
capabilities = ["seal"]

[processes.tenant_a]
components = ["TenantAStore", "TenantAApp"]
realm = "tenant_a"

[processes.tenant_b]
components = ["TenantBStore", "TenantBApp"]
realm = "tenant_b"
"""


def test_conductor_accepts_a_valid_capability_realm_placement(tmp_path, monkeypatch):
    rc, procs = _run(tmp_path, monkeypatch, _VALID)
    assert rc == 0, rc
    assert set(procs) == {"vault", "tenant_a", "tenant_b"}


_CAP_LACKING = """
[capabilities]
Vault = ["seal"]

[processes.vault]
components = ["Vault"]
capabilities = ["db"]

[processes.tenant_a]
components = ["TenantAStore", "TenantAApp"]
realm = "tenant_a"

[processes.tenant_b]
components = ["TenantBStore", "TenantBApp"]
realm = "tenant_b"
"""


def test_conductor_refuses_a_component_on_a_capability_lacking_host(tmp_path, monkeypatch, capsys):
    rc, procs = _run(tmp_path, monkeypatch, _CAP_LACKING)
    assert rc != 0
    err = capsys.readouterr().err
    assert "Vault" in err and "seal" in err and "vault" in err
    assert procs == {}  # refused before anything spawned


_REALM_VIOLATION = """
[processes.tenant_a]
components = ["TenantAStore", "TenantAApp", "TenantBApp"]
realm = "tenant_a"

[processes.tenant_b]
components = ["TenantBStore"]
realm = "tenant_b"

[processes.vault]
components = ["Vault"]
"""


def test_conductor_refuses_a_realm_isolation_violation(tmp_path, monkeypatch, capsys):
    rc, procs = _run(tmp_path, monkeypatch, _REALM_VIOLATION)
    assert rc != 0
    err = capsys.readouterr().err
    assert "TenantBApp" in err and "tenant_b" in err and "tenant_a" in err
    assert procs == {}


_UNKNOWN = """
[capabilities]
NoSuchComponent = ["seal"]

[processes.vault]
components = ["Vault"]
capabilities = ["seal"]

[processes.tenant_a]
components = ["TenantAStore", "TenantAApp"]
realm = "tenant_a"

[processes.tenant_b]
components = ["TenantBStore", "TenantBApp"]
realm = "tenant_b"
"""


def test_conductor_refuses_capabilities_for_an_unknown_component(tmp_path, monkeypatch, capsys):
    rc, _ = _run(tmp_path, monkeypatch, _UNKNOWN)
    assert rc != 0
    err = capsys.readouterr().err
    assert "NoSuchComponent" in err and "unknown" in err


_COLOCATE_REPORT = """
report_colocation = true

[processes.a_store]
components = ["TenantAStore"]
realm = "tenant_a"

[processes.a_app]
components = ["TenantAApp"]
realm = "tenant_a"

[processes.tenant_b]
components = ["TenantBStore", "TenantBApp"]
realm = "tenant_b"

[processes.vault]
components = ["Vault"]
capabilities = ["seal"]

[capabilities]
Vault = ["seal"]
"""


def test_conductor_prints_colocation_advice_when_requested(tmp_path, monkeypatch, capsys):
    # tenant_a split across a_store/a_app -> a same-realm seam the advisory names
    rc, _ = _run(tmp_path, monkeypatch, _COLOCATE_REPORT)
    assert rc == 0, rc
    out = capsys.readouterr().out
    assert "co-location" in out and "tenant_a" in out


# ---------------------------------------------------------------------------
# 3. additivity: a placement using neither feature is byte-identical
# ---------------------------------------------------------------------------

_PLAIN = """
[processes.a]
components = ["TenantAStore", "TenantAApp", "Vault"]

[processes.b]
components = ["TenantBStore", "TenantBApp"]
"""


def test_placement_without_capabilities_or_realms_is_unaffected(tmp_path, monkeypatch, capsys):
    rc, procs = _run(tmp_path, monkeypatch, _PLAIN)
    assert rc == 0, rc
    # no capability/realm output leaks into a plain run
    out = capsys.readouterr().out
    assert "co-location" not in out
    # the specs carry no capability/realm residue (the feature added no keys)
    for spec in (p.spec for p in procs.values()):
        assert "capabilities" not in spec and "realm" not in spec
