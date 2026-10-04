"""Realm-scoped and hole-bearing compositions reach the edit loop (issue #1851).

Re-measured on main after #1741, under the default untrusted-author profile:

* a hole-bearing composition, loaded from `files` or `source`, opens a draft,
  and `revl_edit {hole, expr}` fills it and boots it. No keyhole is left;
* editing a method of a realm-isolated component the operator wrote was
  refused G9, "this profile forbids naming a realm", although the edit did not
  touch the `isolate` clause. Trust follows the text declaration by
  declaration (#1715), so the changed component was the agent's, isolate and
  all. That was the keyhole: the clause is still the operator's, so it is no
  longer read as the agent naming a realm. Moving a component to another
  realm, or adding one with a realm, is still refused;
* `revl_load {}` with no draft to boot answered with the compiler's internal
  "ValueError: provide `source` or `files`". It is now a named refusal.

Writing a realm in inline `source` stays refused G9: that is the rule. The
agent-writable form is a realm placeholder the operator binds (#1773, #1805).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.errors import RevlError  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import compile_under_authoring, handle  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh`")

TENANTS = (ROOT / "examples" / "tenants.rvl").read_text(encoding="utf-8")
GET_A = "fn get(k) = store.get(k)\n    fn set(k, v) {\n      effect store.insert(k, v)"
ISOLATE_A = 'isolate kv in realm("tenant_a")\n  let store'
HOLED = ("service Kv { fn get(k: Str) -> Opt[Str] }\n"
         "component Store provides kv: Kv {\n"
         '  provide kv { fn get(k) = hole "look it up" }\n'
         "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "SESSION", Session())
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(host_code=False, roots=(str(tmp_path),))
    assert server_mod.AUTHORING.profile() is not None
    (tmp_path / "tenants.rvl").write_text(TENANTS, encoding="utf-8")
    try:
        yield tmp_path
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.AUTHORING = old


def _overlay(root, text):
    path = str(root / "tenants.rvl")
    return compile_under_authoring(None, [path], modules={path: text})


# ------------------------------------------------ the compile decision


def test_a_method_edit_keeps_the_operators_isolate_clause(root):
    edited = TENANTS.replace(GET_A, GET_A.replace("store.get(k)", "None"), 1)
    ir = _overlay(root, edited)
    isolate = {c["name"]: c.get("isolate") for c in ir["manifest"]["components"]}
    assert isolate["TenantAStore"] == {"kv": "tenant_a"}


@pytest.mark.parametrize("change", [
    (ISOLATE_A, ISOLATE_A.replace("tenant_a", "tenant_b")),     # another realm
    (ISOLATE_A, ISOLATE_A.replace("tenant_a", "tenant_c")),     # a new realm
    ("component TenantBApp",                                    # a new component
     'component Spy requires kv: Kv {\n  isolate kv in realm("tenant_a")\n'
     '  effect kv.set("x", "y") undo kv.set("x", "")\n}\n\ncomponent TenantBApp'),
])
def test_an_agent_placement_is_still_refused_g9(root, change):
    old, new = change
    with pytest.raises(RevlError) as caught:
        _overlay(root, TENANTS.replace(old, new, 1))
    assert caught.value.code == "G9"
    assert "forbids naming a realm" in caught.value.message


def test_inline_source_naming_a_realm_is_still_refused_g9(root):
    with pytest.raises(RevlError) as caught:
        compile_under_authoring(TENANTS, None)
    assert caught.value.code == "G9"


# ------------------------------------------------ through the edit loop


@needs_runtime
def test_revl_edit_of_a_method_in_a_realm_isolated_component_swaps(root):
    assert _call("revl_load", {"files": [str(root / "tenants.rvl")]})["ok"] is True
    edited = _call("revl_edit", {"edits": [
        {"anchor": GET_A, "replacement": GET_A.replace("store.get(k)", "None")}]})
    assert edited["ok"] is True and edited["swapped"] is True, edited


@needs_runtime
@pytest.mark.parametrize("load", ["files", "source"])
def test_a_hole_in_a_loaded_draft_is_filled_through_revl_edit(root, load):
    path = root / "holed.rvl"
    path.write_text(HOLED, encoding="utf-8")
    loaded = _call("revl_load", {"files": [str(path)]} if load == "files"
                   else {"source": HOLED})
    assert loaded["ok"] is True and loaded["draft"] is True, loaded
    filled = _call("revl_edit", {"edits": [{"hole": 3, "expr": "None"}]})
    assert filled["ok"] is True and filled["loaded"] is True, filled
    assert server_mod.SESSION.loaded


@needs_runtime
def test_revl_load_with_nothing_to_boot_is_a_named_refusal(root):
    out = _call("revl_load", {})
    assert out["ok"] is False
    message = out["diagnostics"][0]["message"]
    assert "ValueError" not in message
    assert "needs `source`" in message and out["diagnostics"][0]["category"] == "session"
    assert _call("revl_load", {"files": [str(root / "tenants.rvl")]})["ok"] is True
    again = _call("revl_load", {})
    assert again["ok"] is False
    assert "already running" in again["diagnostics"][0]["message"]
