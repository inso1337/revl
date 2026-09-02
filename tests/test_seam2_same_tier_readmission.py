"""Item 337 Seam 2 asks the PLAN-TIME seam question, not the swap question.

The boot seam (`_process_runner._boot_wiring_decision`) called
`placement.swap_admission`, which asks "may this component be MOVED to that
tier". Moving is a real hazard: a cutover re-points a LIVE consumer's
in-address-space call site across a new process seam, so `swap_admission`
refuses an address-space-bound (sync `fn`/`emission`) provided service.

At BOOT nothing moves. The provider is already on its tier, the seam already
exists, and the consumer's call sites were compiled against that seam from the
start. The conductor's own plan-time gate (`cross_tier_boundary_check`) says so
explicitly: it refuses a resource crossing tier-agnostically, but the sync
"address-space-bound" half is a REPORT, and only across tiers — "a same-tier
value-typed seam is still byte-identical to today".

So the conductor planned and spawned a py<->py `user_cache` placement whose
`Database` service is sync, and the py CONSUMER then refused to wire it:

    [consumer] proxy | db | REFUSED (admission): cannot swap 'PgDatabase' to
                            the py tier: service `Database` (key 'db') is
                            address-space-bound (...)
    [consumer] probe | cache.put('alice', '42')| ERROR ValueError:
                            'cache' has no method 'put'

and the run still exited 0. Two defects, both pinned here:

1. the boot seam now asks `placement.seam_readmission` — recompile against the
   running manifest, then run the CONDUCTOR'S OWN `cross_tier_boundary_check`
   over the seam topology. Same gate, same verdict, independently re-derived by
   the receiver: still a real re-admission, just the correct one.
2. a refused boot proxy is a BOOT FAILURE (`_process_runner.BootRefused`): the
   process names the refused seams, never prints `UP`, and exits non-zero, so
   the conductor's `--once` gate reports it instead of a green half-dead run.

This shipped because `ci/placement_smoke.sh` only ran placements where py is
the PROVIDER, and only a py CONSUMER runs Seam 2. The end-to-end tests at the
bottom close that gap; `py-py:examples/placement/user_cache.toml` closes it in
the smoke script.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _process_runner as _pr  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.placement import (  # noqa: E402
    cross_tier_boundary_check,
    seam_readmission,
    swap_admission,
)

# REVL_PY, like ci/placement_smoke.sh, so a checkout without a venv at the
# repo root (a worktree, say) can still run these against a real runtime.
CORDIS_PY = Path(os.environ.get(
    "REVL_PY", str(ROOT / "backends" / "python" / ".venv" / "bin" / "python")))
needs_cordis = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="needs the cordis-py runtime (backends/python/.venv/bin/python)")

# The repro's own source. `Database` is address-space-bound — `query` is a sync
# `fn` and `execute` a sync `emission` — and UserCache consumes it across the
# seam, which is exactly the shape the boot seam wrongly refused.
_APP = str(ROOT / "examples" / "user_cache.rvl")

# The same seam with an OWNED resource handle on the wire. A resource cannot
# cross a process seam by copy in either direction, tier-agnostically, so the
# CONDUCTOR refuses this one at plan time — and so must the boot re-admission.
_HANDLE_SEAM_RVL = """
type Sock = { fd: Int }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Sock undo close_sock(0) = @py { return {"fd": 1} }

service Database {
  fn query(sql: Str) -> Sock
  emission fn execute(sql: Str) -> Int
}
service Cache { fn get(key: Str) -> Sock }

component PgDatabase provides db: Database {
  provide db {
    fn query(sql) = open_sock()
    fn execute(sql) = 1
  }
}

component UserCache requires db: Database provides cache: Cache {
  provide cache { fn get(key) = db.query(key) }
}
"""


def _write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def _seam_dir(tmp_path, spec=None, name="consumer"):
    """A realistic placement directory and the seam anchor a process booted out
    of it holds (item 337, `_process_runner._seam_anchor`) — the same helper
    tests/test_swap.py uses. The conductor makes one 0700 `mkdtemp` per
    placement, writes each process's spec into it, and binds every seam socket
    there, so a socket inside it is an address this receiver sanctions."""
    directory = tmp_path / "revl_placement"
    directory.mkdir(exist_ok=True)
    spec_file = directory / f"{name}.spec.json"
    spec_file.write_text(json.dumps(spec or {}), encoding="utf-8")
    return directory, _pr._seam_anchor(spec or {}, str(spec_file))


def _seam_maps():
    """The real two-process topology of the repro: `provider` hosts PgDatabase
    and serves `db`, `consumer` hosts UserCache and proxies it."""
    requires = {"consumer": {"db": "Database"}}
    provides = {"provider": {"db": "Database"}, "consumer": {"cache": "Cache"}}
    owner = {"db": "provider", "cache": "consumer"}
    return requires, provides, owner


# ---------------------------------------------------------------------------
# 1. the predicate: what the conductor sanctioned is what the boot seam asks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_tier", ["py", "rust"])
def test_the_conductor_permits_this_seam_at_plan_time(provider_tier):
    """The premise the boot seam must match. `cross_tier_boundary_check` is the
    gate `run_placement` runs before it spawns anything: an address-space-bound
    service across a process seam is PERMITTED, silently when the two ends share
    a tier and with a report line when they do not. It is never a refusal."""
    ir = compile_files([_APP])
    requires, provides, owner = _seam_maps()
    backends = {"provider": provider_tier, "consumer": "py"}

    problem, report = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert problem is None                      # the conductor spawns this
    assert bool(report) is (provider_tier != "py")   # reported only cross-tier


@pytest.mark.parametrize("provider_tier", ["py", "rust"])
def test_boot_seam_readmits_the_seam_the_conductor_sanctioned(provider_tier):
    """The fix, at the level of the predicate: the consumer independently
    re-admits the provider AS IT IS on the tier it is already on, and reaches
    the conductor's verdict — while `swap_admission`, which asks whether the
    provider may be MOVED there, refuses. The seam judged is real (the
    re-admission recompiles against the running manifest); only the question
    changed."""
    running = compile_files([_APP])

    candidate, error = seam_readmission([_APP], running, "PgDatabase", provider_tier)
    assert error is None, error
    assert candidate is not None

    # the swap question, on the same inputs, says no — that divergence IS the
    # bug, and it is correct at a cutover: a move re-points a live call site.
    _moved, swap_error = swap_admission([_APP], running, "PgDatabase", provider_tier)
    assert swap_error is not None
    assert "address-space-bound" in swap_error


def test_boot_wiring_wires_the_same_tier_sync_seam(tmp_path):
    """The defect, end of the decision chain: `_apply_boot_wiring` now invokes
    `wire` for the py<->py seam the conductor planned. Before the fix the
    callback was never called and the consumer booted with an unwired `db`.

    The seam is presented the way the conductor really presents it: `component`
    bound to `db` by the running manifest, and the socket bound inside this
    process's own placement directory, so the receiver-derived binding and
    address checks (item 337, `_selector_binding` / `_sanction_address`) pass
    and the verdict comes from `seam_readmission`. Those two checks and this one
    are independent, and an honest seam must clear all three."""
    running = compile_files([_APP])
    plc, anchor = _seam_dir(tmp_path)
    sock = str(plc / "provider.sock")
    info = {"socket": sock, "methods": ["query", "execute"],
            "service": "Database", "component": "PgDatabase", "backend": "py"}
    wired = []

    async def wire():
        wired.append(info["socket"])

    ok, reason = _pr._boot_wiring_decision([_APP], running, "db", info, anchor)
    assert ok is True and reason is None

    assert asyncio.run(_pr._apply_boot_wiring("db", info, [_APP], running, wire,
                                              anchor=anchor)) is True
    assert wired == [sock]


def test_re_admission_is_still_a_real_gate_not_a_same_tier_bypass(tmp_path):
    """The fix must not degrade into "same tier, wire it". A resource crossing
    is refused by the conductor tier-agnostically, so the re-admission refuses
    it at a py<->py seam too — and a component the running manifest does not
    contain at all is refused outright."""
    src = _write(tmp_path, "handle_seam.rvl", _HANDLE_SEAM_RVL)
    running = compile_files([src])

    candidate, error = seam_readmission([src], running, "PgDatabase", "py")
    assert candidate is None
    assert error is not None and "Sock" in error and "resource" in error

    _c, error = seam_readmission([src], running, "NotAComponent", "py")
    assert error is not None and "not a component of the running composition" in error


def test_the_full_boot_decision_still_refuses_a_resource_crossing_at_a_py_py_seam(tmp_path):
    """The same gate through the WHOLE merged decision, not just the predicate.
    Both receiver-derived checks are made to PASS — `PgDatabase` really provides
    `db` in this manifest, and the socket really is in this process's own
    placement directory — and the provider tier is `py`, the consumer's own. So
    nothing but `seam_readmission` can produce the refusal, and it still does:
    the same-tier relaxation is about the sync/relocation clause only, never
    about resources. An unknown component is refused through the same path."""
    src = _write(tmp_path, "handle_seam.rvl", _HANDLE_SEAM_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    info = {"socket": str(plc / "provider.sock"), "methods": ["query", "execute"],
            "service": "Database", "component": "PgDatabase", "backend": "py"}

    ok, reason = _pr._boot_wiring_decision([src], running, "db", info, anchor)
    assert ok is False
    assert reason and "Sock" in reason and "resource" in reason

    wired = []

    async def wire():
        wired.append(info["socket"])

    assert asyncio.run(_pr._apply_boot_wiring("db", info, [src], running, wire,
                                              anchor=anchor)) is False
    assert wired == []


# ---------------------------------------------------------------------------
# 2. a refused boot proxy is a boot failure, not a log line
# ---------------------------------------------------------------------------


def _consumer_spec(name="consumer", proxy_extra=None):
    """A hand-written `_process_runner` spec for the user_cache consumer slice,
    the same shape `placement.py` writes."""
    proxy = {"socket": "/nonexistent/provider.sock",
             "methods": ["query", "execute"], "service": "Database"}
    proxy.update(proxy_extra or {})
    return {"name": name,
            "files": [str(ROOT / "examples" / "user_cache.rvl")],
            "components": ["UserCache"], "provides": ["cache"],
            "proxies": {"db": proxy}, "config": {}, "probe": []}


@needs_cordis
def test_a_refused_boot_proxy_exits_non_zero_and_never_reports_up(tmp_path):
    """A boot seam this process refuses leaves it with an unwired dependency:
    every call into its own components would fail at runtime. There is no
    healthy prior state to fall back on the way a refused REPOINT has one, so
    the process fails the boot — it names the refused key, does not print `UP`,
    and exits non-zero. (This entry carries no component/backend, the
    fail-closed case, so the refusal itself is not in question here.)"""
    spec_file = tmp_path / "consumer.spec.json"
    spec_file.write_text(json.dumps(_consumer_spec()), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl._process_runner", str(spec_file)],
        capture_output=True, text=True, env=env, timeout=180)

    assert result.returncode != 0, result.stdout
    assert "REFUSED (admission)" in result.stdout
    assert "BOOT REFUSED" in result.stdout and "db" in result.stdout
    assert "[consumer] UP" not in result.stdout


# The two refusals the ADDRESS/BINDING half of item 337 adds. Neither existed
# when the fatality above was written, and the fatality did not exist when they
# were: that a boot refused for one of THESE reasons is equally fatal is a
# property only the two together have, so it is pinned here. The socket in each
# is inside this process's own placement directory (`tmp_path`, where its spec
# is written) except where the point is that it is not.
_ADDRESS_AND_BINDING_REFUSALS = [
    # an honest selector with the address swapped underneath it
    pytest.param({"component": "PgDatabase", "backend": "py",
                  "socket": "/tmp/attacker-controlled.sock"},
                 "placement directory", id="unsanctioned-address"),
    # an admissible but unrelated component offered as a pass token for `db`
    pytest.param({"component": "UserCache", "backend": "py"},
                 "does not provide key 'db'", id="unrelated-selector"),
]


@needs_cordis
@pytest.mark.parametrize("extra,expected", _ADDRESS_AND_BINDING_REFUSALS)
def test_an_address_or_binding_refusal_is_equally_fatal_to_the_boot(tmp_path, extra,
                                                                    expected):
    """A seam refused because the receiver would not sanction its ADDRESS, or
    because the selector is not bound to the key, leaves exactly the same
    unwired dependency as any other refusal — so it must fail the boot the same
    way, not degrade into a log line under a green exit."""
    proxy = {"socket": str(tmp_path / "provider.sock")}
    proxy.update(extra)
    spec_file = tmp_path / "consumer.spec.json"
    spec_file.write_text(json.dumps(_consumer_spec(proxy_extra=proxy)),
                         encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl._process_runner", str(spec_file)],
        capture_output=True, text=True, env=env, timeout=180)

    assert result.returncode != 0, result.stdout
    assert expected in result.stdout, result.stdout
    assert "BOOT REFUSED" in result.stdout and "db" in result.stdout
    assert "[consumer] UP" not in result.stdout


_SEAM3_RVL = """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
service ApiSvc { async fn hit() -> Opt[Str] }

component Api requires cache: Cache provides api: ApiSvc {
  provide api { async fn hit() = cache.get("k") }
}
"""


@needs_cordis
def test_a_genuine_seam3_entry_is_not_gated_and_never_causes_a_boot_refusal(tmp_path):
    """The other half of making a refusal fatal: it must not become fatal for
    entries this seam does not judge. `cache` is provided by NO component of
    this composition, which is how the receiver knows it is a genuine
    cross-composition handoff (item 151, Seam 3) rather than a same-composition
    seam wearing a `remote` flag. It is wired ungated, so it can never land in
    the refused list, and no `BOOT REFUSED` is raised on its account.

    The peer is not running here, so the process still dies — at the CONNECT,
    which is the honest failure and a different one. What is being pinned is
    that Seam 2 neither judged nor refused it."""
    src = _write(tmp_path, "seam3.rvl", _SEAM3_RVL)
    spec = {"name": "consumer", "files": [src], "components": ["Api"],
            "provides": ["api"], "config": {}, "probe": [],
            "proxies": {"cache": {"socket": str(tmp_path / "peer.sock"),
                                  "methods": ["get", "put"], "service": "Cache",
                                  "remote": True}}}
    spec_file = tmp_path / "consumer.spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl._process_runner", str(spec_file)],
        capture_output=True, text=True, env=env, timeout=180)
    trace = result.stdout + result.stderr

    assert "Seam 3" in result.stdout, trace          # recognized, not gated
    assert "REFUSED (admission)" not in trace, trace
    assert "BOOT REFUSED" not in trace, trace
    # it got as far as actually trying to reach the peer -- it WAS wired
    assert "_connect" in trace or "OSError" in trace, trace


# ---------------------------------------------------------------------------
# 3. end-to-end: a py CONSUMER over a real seam (the coverage gap)
# ---------------------------------------------------------------------------
#
# `ci/placement_smoke.sh`'s default seams all place py as the PROVIDER, and only
# a py CONSUMER runs Seam 2 — which is why a py-consumer seam could break with
# a green CI. These boot the real conductor with real child processes.


def _placement_run(toml_path, once=True):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    argv = [str(CORDIS_PY), "-m", "revl", "run",
            str(ROOT / "examples" / "user_cache.rvl"), "--placement",
            str(toml_path)] + (["--once"] if once else [])
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, cwd=str(ROOT), timeout=300)


@needs_cordis
def test_py_consumer_crosses_a_real_seam_end_to_end():
    """The repro, as a test. `examples/placement/user_cache.toml` is the
    canonical py<->py split: PgDatabase serves `db`, UserCache proxies it, and
    `cache.put`'s `emit db.execute(...)` has to cross the process boundary.

    Before the fix this printed `REFUSED (admission)`, both probes failed with
    `'cache' has no method ...`, and the run still exited 0."""
    result = _placement_run(ROOT / "examples" / "placement" / "user_cache.toml")
    trace = result.stdout + result.stderr

    assert result.returncode == 0, trace
    assert "REFUSED" not in trace, trace
    assert "BOOT REFUSED" not in trace, trace
    # the seam was actually wired and actually crossed, both directions
    assert "[consumer] proxy | db" in trace, trace
    assert "pool#1.execute" in trace, trace       # the emission reached the provider
    assert "cache.get('alice')| => '42'" in trace, trace
    assert "probe" in trace and "ERROR" not in trace, trace
    assert trace.count("residue no residue") == 2, trace
