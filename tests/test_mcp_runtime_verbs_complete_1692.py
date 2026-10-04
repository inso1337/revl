"""`RUNTIME_VERBS` and `LIMITED_VERBS` are measured, not trusted (issue #1692).

`src/revl/mcp/runtime_gate.py` keeps the two lists by hand, and a hand-kept
list goes stale silently: a new live verb would be served dead on a cordis-less
interpreter with no refusal and no announcement. So this file re-runs the
measurement the lists came from and asserts they still match it.

One subprocess, with `cordis` made unimportable (a meta-path finder refuses it,
so the result does not depend on whether this interpreter has the runtime) and
the #1692 gate held OPEN, so each handler answers for itself. For every verb
the server advertises, on a fresh session: try a recording `revl_load` (it
fails without the runtime, as it would for an agent), then call the verb once
with arguments that would make it act on a live composition. A verb is

* RUNTIME when that call fails for lack of the runtime, and its runtime-free
  form (if `PLAN` gives one) fails the same way;
* LIMITED when it still answers but says its runtime half did not run, or when
  only its runtime-free form works;
* otherwise static.

Every advertised verb must have a `PLAN` entry, so a new verb fails here until
someone decides how to exercise it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import runtime_gate as gate  # noqa: E402
from revl.mcp import server  # noqa: E402

SOURCE = ("service S { fn f() -> Int }\n"
          "component C provides s: S {\n  provide s {\n    fn f() = 1\n  }\n}\n")
SOURCE2 = SOURCE.replace("fn f() = 1", "fn f() = 2")
SNAPSHOT = {"sources": {"source": SOURCE}, "manifest": {},
            "meta": {"components": ["C"]}}
# an inline recording, in `Recorder.as_dict()` shape, for the history verbs
TIMELINE = {"components": [{"component": "C", "steps": []}]}

# verb -> (arguments that act on a live composition, runtime-free form or None)
PLAN: dict[str, tuple[dict, dict | None]] = {
    "revl_check": ({"source": SOURCE}, None),
    "revl_admit": ({"manifest": {}, "source": SOURCE}, None),
    "revl_plan": ({"source": SOURCE}, None),
    "revl_ship": ({"source": SOURCE, "manifest": {}, "apply": True},
                  {"source": SOURCE, "manifest": {}}),
    "revl_deploy": ({"placement": {}}, None),
    "revl_audit": ({"source": SOURCE}, None),
    "revl_tools": ({"source": SOURCE}, None),
    "revl_load": ({"source": SOURCE, "record": True}, None),
    "revl_call": ({"key": "s", "method": "f", "args": []}, None),
    "revl_swap": ({"source": SOURCE2}, None),
    "revl_edit": ({"edits": [{"op": "replace", "find": "= 1", "with": "= 2"}]}, None),
    "revl_change": ({"edit": {"edits": [{"anchor": "= 1", "replacement": "= 2"}]},
                     "commit": True}, None),
    "revl_export": ({"path": "exported.rvl"}, None),
    "revl_source": ({"symbol": "C", "source": SOURCE}, None),
    "revl_knowledge": ({"op": "query"}, None),
    "revl_gauntlet": ({"source": SOURCE2}, None),
    "revl_quarantine": ({"source": SOURCE2}, None),
    "revl_repair": ({"component": "C"}, None),
    "revl_rollback": ({}, None),
    "revl_undo": ({}, None),
    "revl_unload": ({}, None),
    "revl_commit": ({}, None),
    "revl_commit_confirm": ({"hash": "x"}, None),
    "revl_abort": ({}, None),
    "revl_estop": ({"reason": "measure"}, None),
    "revl_estop_report": ({}, None),
    "revl_fork": ({"at": 1, "component": "C"}, None),
    "revl_fork_confirm": ({"hash": "x"}, None),
    "revl_approve": ({"capability": "net"}, None),
    "revl_revoke": ({"capability": "net"}, None),
    "revl_escalate": ({"hash": "x"}, None),
    "revl_override": ({"hash": "x", "reason": "r"}, None),
    "revl_quorum": ({"hash": "x"}, None),
    "revl_distillation_offers": ({}, None),
    "revl_apply_distillation": ({"offerId": "x"}, None),
    "revl_revoke_distillation": ({"rule": "x"}, None),
    "revl_state": ({}, None),
    "revl_lease": ({"component": "C"}, None),
    "revl_snapshot": ({}, None),
    "revl_restore": ({"snapshot": SNAPSHOT}, None),
    "revl_timeline": ({"component": "C"}, None),
    "revl_inspect_step": ({"at": 1, "component": "C"}, None),
    "revl_step_back": ({"to": 1, "component": "C"}, None),
    "revl_replay_bisect": ({"assert": "true", "component": "C"}, None),
    "revl_replay_forward": ({"from": 1, "component": "C"}, None),
    "revl_grammar": ({}, None),
    "revl_idiom": ({"name": "provide-method"}, None),
    "revl_scaffold": ({"service": "S"}, None),
    "revl_fmt": ({"source": SOURCE}, None),
    "revl_explain": ({"code": "A1"}, None),
    "revl_verbs": ({"topic": "session"}, None),
    "revl_resolve": ({"need": "service S { fn f() -> Int }"}, None),
    "revl_canary": ({"realm": "r", "candidate": SOURCE}, None),
    "revl_query_emitters": ({"target": "S", "source": SOURCE}, None),
    "revl_query_withdraw": ({"component": "C", "source": SOURCE}, None),
    "revl_query_dependents": ({"target": "S", "source": SOURCE}, None),
    "revl_query_reach": ({"component": "C", "source": SOURCE}, None),
    "revl_query_drift": ({"service": "S", "source": SOURCE}, None),
    "revl_live_query": ({"verb": "depends-on", "target": "S"}, None),
    "revl_history_emitted_between": ({"from": 0, "to": 1},
                                     {"from": 0, "to": 1, "timeline": TIMELINE}),
    "revl_history_lifetime": ({"component": "C"},
                              {"component": "C", "timeline": TIMELINE}),
}

# What a handler says when the runtime is missing: the import itself, or the
# live composition (or its recording) that the import would have produced.
RUNTIME_SIGNS = (
    "cordis-py runtime is not installed", "nothing is loaded",
    "needs a live composition", "not loaded with recording on",
    "no recorded timeline", "no recorded run", "no previous generation",
    "no earlier generation",
)

_HARNESS = r'''
import importlib.abc, json, sys

class _NoCordis(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "cordis" or name.startswith("cordis."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, _NoCordis())
from revl.mcp import server
from revl.mcp.session import Session

server.set_runtime_available(True)  # the gate OPEN: measure the handlers
server.set_authoring_trust(host_code=False, granted=None, providers=None,
                           roots=None)
plan, source = json.loads(sys.stdin.read())

def call(name, args):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
    return response["result"]["structuredContent"]

out = {}
for verb, (args, alt) in plan.items():
    server.SESSION = Session()
    call("revl_load", {"source": source, "record": True})
    first = call(verb, args)
    second = None
    if alt is not None:
        server.SESSION = Session()
        second = call(verb, alt)
    out[verb] = [first, second]
print(json.dumps(out))
'''


def _runtime_failure(payload: dict) -> bool:
    text = json.dumps(payload)
    return payload.get("ok") is not True and any(s in text for s in RUNTIME_SIGNS)


def _degraded(payload: dict) -> bool:
    return payload.get("ok") is True and "cordis-py runtime is not installed" in \
        json.dumps(payload)


def _measure() -> tuple[set, set]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-P", "-c", _HARNESS],
        input=json.dumps([PLAN, SOURCE]), capture_output=True, text=True,
        env=env, timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    runtime, limited = set(), set()
    for verb, (first, second) in results.items():
        if _degraded(first):
            limited.add(verb)
        elif _runtime_failure(first):
            if second is None or _runtime_failure(second):
                runtime.add(verb)
            else:
                limited.add(verb)
    return runtime, limited


def test_every_advertised_verb_has_a_plan_entry():
    advertised = {tool["name"] for tool in server.TOOLS}
    assert advertised == set(PLAN), (
        f"no PLAN entry: {sorted(advertised - set(PLAN))}; "
        f"stale: {sorted(set(PLAN) - advertised)}")


def test_the_runtime_and_limited_lists_match_the_measurement():
    runtime, limited = _measure()
    assert runtime == gate.RUNTIME_VERBS, (
        f"measured but not in RUNTIME_VERBS: {sorted(runtime - gate.RUNTIME_VERBS)}; "
        f"in RUNTIME_VERBS but answering without the runtime: "
        f"{sorted(gate.RUNTIME_VERBS - runtime)}")
    assert limited == set(gate.LIMITED_VERBS), (
        f"measured limited: {sorted(limited)}; "
        f"LIMITED_VERBS: {sorted(gate.LIMITED_VERBS)}")


def test_the_classifier_is_not_vacuous():
    """A runtime failure, a plain argument error and a degraded answer read as
    three different things."""
    assert _runtime_failure({"ok": False, "diagnostics": [
        {"message": "nothing is loaded — call revl_load first"}]})
    assert not _runtime_failure({"ok": False, "diagnostics": [
        {"message": "`to` is required"}]})
    assert _degraded({"ok": True, "substrate": {
        "reason": "the cordis-py runtime is not installed ('cordis' missing)"}})
    assert not _degraded({"ok": True})
