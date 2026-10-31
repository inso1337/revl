"""The operator update surface: check, plan, apply, roll back (docs/updates.md).

The live tests boot a real cordis session; they are marked per test so the
runtime-free assertions (the verbs exist, a disabled server refuses by name,
the widening gate, the tag fallback refusal) still run everywhere.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _update_feed_fixture import serve_feed, tar_gz  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp import update_tools  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

CACHE_V2 = CACHE.replace("fn size() = 0", "fn size() = 43")

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the update apply verbs need the cordis-py runtime")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _release_feed(version: str = "9.9.9", source: str = CACHE_V2):
    artifact = tar_gz({"cache.rvl": source.encode()},
                      top=f"app-{version}")
    return serve_feed(version=version, artifact_bytes=artifact)


@pytest.fixture(autouse=True)
def _isolated():
    update_tools.reset()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    update_tools.reset()


def _configure(feed, tmp_path: Path) -> None:
    update_tools.configure(enabled=True, repo="owner/app", project="app",
                           api_base=feed.base_url, home=str(tmp_path))


# ------------------------------------------------------------ runtime free

def test_the_four_verbs_are_registered_and_documented_as_a_topic():
    names = {tool["name"] for tool in server_mod.TOOLS}
    assert {"revl_update_check", "revl_update_apply", "revl_update_status",
            "revl_update_rollback"} <= names
    topic = server_mod._disclosure.TOPICS["update"][1]
    assert set(topic) == {"revl_update_check", "revl_update_apply",
                          "revl_update_status", "revl_update_rollback"}


def test_a_disabled_server_refuses_the_mutating_update_verbs_by_name():
    for verb in ("revl_update_check", "revl_update_apply",
                 "revl_update_rollback"):
        payload = _call(verb, {})
        assert payload["ok"] is False
        assert payload["refusal"]["code"] == "UPDATE_DISABLED"


def test_status_answers_even_when_disabled_and_names_the_config():
    payload = _call("revl_update_status", {})
    assert payload["ok"] is True
    assert payload["enabled"] is False
    assert payload["config"]["repo"] == "inso1337/revl"
    assert payload["staged"] == []
    assert payload["identity"]["version"]


def test_rollback_without_an_applied_release_is_refused():
    update_tools.configure(enabled=True)
    payload = _call("revl_update_rollback", {})
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_NOTHING_APPLIED"


def test_check_compares_the_feed_against_the_running_identity(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        payload = _call("revl_update_check", {})
    finally:
        feed.close()
    assert payload["ok"] is True
    assert payload["comparison"]["updateAvailable"] is True
    assert payload["comparison"]["bump"] == "major"
    assert payload["release"]["version"] == "9.9.9"
    assert payload["applyAvailable"] is False  # nothing loaded yet
    assert payload["applyReason"] == "nothing is loaded"


def test_apply_without_a_plan_hash_needs_a_running_composition(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        payload = _call("revl_update_apply", {})
    finally:
        feed.close()
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_NOTHING_LOADED"


@needs_runtime
def test_a_tag_fallback_is_refused_for_apply(tmp_path):
    feed = serve_feed(tag_only=True, version="9.9.9")
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        payload = _call("revl_update_apply", {})
    finally:
        feed.close()
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_TAG_FALLBACK"


# -------------------------------------------------------------- live apply

@needs_runtime
def test_plan_apply_and_rollback_through_the_gate(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        assert _call("revl_load", {"source": CACHE})["ok"] is True
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
        check = _call("revl_update_check", {})
        assert check["applyAvailable"] is True
        planned = _call("revl_update_apply", {})
        assert planned["ok"] is True and planned["planned"] is True
        assert planned["applied"] is False
        assert planned["generation"] == 1
        assert planned["widening"]["widened"] is False
        plan_hash = planned["planHash"]
        applied = _call("revl_update_apply", {"planHash": plan_hash})
        assert applied["ok"] is True and applied["applied"] is True
        assert applied["generationBefore"] == 1
        assert applied["generation"] == 2
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 43
        rolled = _call("revl_update_rollback", {})
        assert rolled["ok"] is True and rolled["rolledBack"] is True
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
    finally:
        feed.close()


@needs_runtime
def test_a_stale_plan_hash_is_refused(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", {})
        plan_hash = planned["planHash"]
        # the running composition moves after the plan: the plan is stale.
        assert _call("revl_swap", {"source": CACHE_V2})["swapped"] is True
        stale = _call("revl_update_apply", {"planHash": plan_hash})
        assert stale["ok"] is False
        assert stale["refusal"]["code"] == "UPDATE_PLAN_STALE"
    finally:
        feed.close()


@needs_runtime
def test_widening_requires_acknowledgement(monkeypatch, tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        monkeypatch.setattr(update_tools, "_widening",
                            lambda _ir, _files: {
                                "widened": True,
                                "added": ["emit:MemCache:send"],
                                "reachWeakened": []})
        planned = _call("revl_update_apply", {})
        plan_hash = planned["planHash"]
        refused = _call("revl_update_apply", {"planHash": plan_hash})
        assert refused["ok"] is False
        assert refused["approvalRequired"] is True
        assert refused["refusal"]["code"] == "UPDATE_WIDENS"
        accepted = _call("revl_update_apply",
                         {"planHash": plan_hash, "acknowledgeWidening": True})
        assert accepted["ok"] is True and accepted["applied"] is True
        assert accepted["widening"]["widened"] is True
    finally:
        feed.close()


@needs_runtime
def test_a_refused_admission_leaves_the_running_composition_serving(tmp_path):
    broken = CACHE.replace("fn size() = 0", 'fn size() = "nope"')
    feed = _release_feed(source=broken)
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", {})
        assert planned["ok"] is False
        assert planned["stoppedAt"] == "admit"
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
        assert _call("revl_state", {})["generation"] == 1
    finally:
        feed.close()
