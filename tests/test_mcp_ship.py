"""The fused ship verb (roadmap item 50, the token economy — docs/token-economy.md).

`revl_ship` collapses the audit's four-round-trip ship loop
(check -> admit -> plan -> swap, bench/results/token-surface-audit.md finding #3)
into one early-exit call. The load-bearing claims under test:

  1. the read-only rehearsal (check -> admit -> plan) runs in ONE call and
     reports a per-stage verdict plus the round-trips it saved;
  2. it EARLY-EXITS on the first failing stage — a candidate that does not
     compile is never admitted, one that is not admissible is never planned —
     and the later stages' handlers are never invoked (no wasted work);
  3. the running manifest defaults to the composition the server holds, so the
     agent does not re-send the running IR to admit against it;
  4. `apply: true` extends the chain with the hot-swap, once every stage passes.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import ship as _ship  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


RUNNING_SRC = """
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
}
"""

# a compatible candidate: requires the running `cache`, provides a new `log`
GOOD_CANDIDATE = """
service Cache { fn get(key: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component Watcher requires cache: Cache provides log: Log {
  provide log { fn note(m) = 1 }
}"""


# ------------------------------------------------------- the wired verb (dry run)

def test_ship_is_listed_and_not_read_only():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert "revl_ship" in tools
    # capable of mutation via apply, so it advertises itself like revl_swap
    assert tools["revl_ship"]["annotations"]["readOnlyHint"] is False
    assert tools["revl_ship"]["annotations"]["destructiveHint"] is True


def test_ship_fuses_check_admit_plan_in_one_call():
    running = compile_source(RUNNING_SRC)
    payload = _call("revl_ship", {"manifest": running, "source": GOOD_CANDIDATE})
    assert payload["ok"] is True
    assert payload["shipped"] is False  # no apply: this is the rehearsal
    assert payload["stoppedAt"] is None
    assert [s["stage"] for s in payload["stages"]] == ["check", "admit", "plan"]
    assert all(s["ok"] for s in payload["stages"])
    # one call did the work of three separate ones
    assert payload["roundTrips"] == {"fused": 1, "wouldHaveBeen": 3, "saved": 2}
    # the consolidated result carries the plan delta — no extra revl_plan call
    assert "plan" in payload and payload["plan"]


def test_ship_early_exits_at_check_and_never_admits():
    running = compile_source(RUNNING_SRC)
    # a G4 violation: mutation with no undo/emit — does not compile
    payload = _call("revl_ship", {
        "manifest": running,
        "source": "component P { let pool = effect Pool.open(1) }"})
    assert payload["ok"] is False
    assert payload["shipped"] is False
    assert payload["stoppedAt"] == "check"
    assert [s["stage"] for s in payload["stages"]] == ["check"]  # nothing after
    assert payload["diagnostics"]  # the failing stage's diagnostic is merged up


def test_ship_early_exits_at_admit_on_interface_drift():
    running = compile_source(RUNNING_SRC)
    # compiles fine standalone, but redeclares Cache.get with a drifted type
    drift = """
service Cache { fn get(key: Str) -> Int }
component Other requires cache: Cache {
  let x = effect Map.new() undo x.drop()
}"""
    payload = _call("revl_ship", {"manifest": running, "source": drift})
    assert payload["ok"] is False
    assert payload["stoppedAt"] == "admit"
    # check ran and passed; admit ran and failed; plan never ran
    assert [s["stage"] for s in payload["stages"]] == ["check", "admit"]
    assert payload["stages"][0]["ok"] is True
    assert payload["stages"][1]["ok"] is False


def test_ship_early_exits_on_open_holes_before_admission():
    running = compile_source(RUNNING_SRC)
    draft = ('service Cache { fn get(key: Str) -> Str }\n'
             'component D provides c: Cache {\n'
             '  provide c { fn get(key) = hole "look it up" }\n'
             '}\n')
    payload = _call("revl_ship", {"manifest": running, "source": draft})
    assert payload["ok"] is False
    assert payload["stoppedAt"] == "check"
    assert "hole" in payload["reason"]
    # the holes (each with a fillSpec) ride along as the agent's next work
    assert payload.get("holes")


# ---------------------------------------- pure orchestration: no wasted work

def test_orchestration_does_not_run_later_stages_on_early_exit():
    """The early-exit is real: when check fails, the admit and plan handlers
    are never called. Proven with fakes so it needs no runtime."""
    calls = []

    def check(_a):
        calls.append("check")
        return {"ok": False, "diagnostics": [{"message": "nope"}]}

    def admit(_a):
        calls.append("admit")
        return {"ok": True, "admitted": True}

    def plan(_a):
        calls.append("plan")
        return {"provisions": []}

    result = _ship.ship({"source": "x"}, check=check, admit=admit, plan=plan)
    assert calls == ["check"]  # admit/plan never ran — no wasted work
    assert result["stoppedAt"] == "check"
    assert result["ok"] is False
    assert result["roundTrips"]["saved"] == 0  # only one stage ran


def test_orchestration_defaults_manifest_to_the_session():
    """Stage 2/3 default their manifest to the running composition the session
    holds — the agent never re-sends the running IR (the token win)."""
    seen = {}

    def check(_a):
        return {"ok": True, "loadOrder": ["C"], "holes": []}

    def admit(a):
        seen["admit_manifest"] = a.get("manifest")
        return {"ok": True, "admitted": True, "boundary": {}}

    def plan(a):
        seen["plan_manifest"] = a.get("manifest")
        return {"provisions": ["log"]}

    class FakeSession:
        loaded = True
        ir = {"manifest": {"loadOrder": ["C"]}}

    result = _ship.ship({"source": "x"}, check=check, admit=admit, plan=plan,
                        session=FakeSession())
    assert seen["admit_manifest"] == FakeSession.ir
    assert seen["plan_manifest"] == FakeSession.ir
    assert result["against"] == "session"
    assert result["ok"] is True and result["stoppedAt"] is None


# --------------------------------------------------------- the apply (swap) path

def test_ship_apply_swaps_the_candidate_in():
    """The full chain in one call: check -> admit -> plan -> swap. Needs the
    cordis-py runtime to actually boot a session; skipped where it is absent
    (the read-only stages above cover the fusion without it)."""
    loaded = _call("revl_load", {"source": RUNNING_SRC})
    if not loaded.get("ok"):
        pytest.skip("cordis-py runtime not installed — end-to-end swap "
                    "is exercised where the runtime is present")

    full = """
service Cache { fn get(key: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
}
component Watcher requires cache: Cache provides log: Log {
  provide log { fn note(m) = 1 }
}"""
    # manifest omitted on purpose: ship defaults it to the loaded session
    payload = _call("revl_ship", {"source": full, "apply": True})
    assert payload["ok"] is True
    assert payload["shipped"] is True
    assert payload["stoppedAt"] is None
    assert [s["stage"] for s in payload["stages"]] == \
        ["check", "admit", "plan", "swap"]
    assert payload["against"] == "session"
    assert payload["roundTrips"]["saved"] == 3
    assert "swap" in payload
    _call("revl_unload", {})
