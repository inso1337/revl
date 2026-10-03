"""Issue #1691: a refusal carries its own next call.

A refusal with a known remedy returns `next: {tool, arguments, ready,
needs?}` beside its prose, and its message names that remedy. Pinned here:

* the shape of `next` (one schema check every case below runs), including
  the operator step the runtime gate (#1692) answers with;
* every "call revl_load first" site answers with `revl_load`, pre-filled with
  the composition this session last ran when there is one, and that `next`
  sent as-is reloads it;
* the files-loaded `revl_edit` refusal carries the patch applied to the file
  as a `revl_swap` that, sent as-is, swaps; and the files-loaded name-only
  `revl_swap` refusal carries the swap of those files.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import remedy  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cordis-py runtime not installed (sh backends/python/setup.sh)")

CACHE = (
    "service Cache { fn get(key: Str) -> Opt[Str]\n"
    "                fn size() -> Int }\n"
    "component MemCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache { fn get(key) = store.get(key)\n"
    "                  fn size() = 0 }\n"
    "}\n"
)

#: Verbs that act on the running composition, with arguments that would be
#: valid if something were loaded. Each refuses with nothing loaded.
NEEDS_LOADED = [
    ("revl_call", {"key": "cache", "method": "size", "args": []}),
    ("revl_swap", {"source": CACHE}),
    ("revl_edit", {"edits": [{"anchor": "= 0", "replacement": "= 1"}]}),
    ("revl_undo", {}),
    ("revl_unload", {}),
    ("revl_snapshot", {}),
    ("revl_live_query", {"verb": "provides"}),
]

_ADVERTISED = {tool["name"] for tool in server_mod._ADVERTISED}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _send(nxt: dict) -> dict:
    """Execute a `next` exactly as the refusal handed it over."""
    return _call(nxt["tool"], nxt["arguments"])


def _assert_next_shape(nxt) -> None:
    """The `next` schema: one call, or a non-empty list of calls in
    preference order. A call names an advertised verb, carries an arguments
    object and a `ready` boolean, and says what it `needs` when not ready."""
    calls = nxt if isinstance(nxt, list) else [nxt]
    assert calls, "an empty `next` list is not a remedy"
    for entry in calls:
        if "operator" in entry:   # a remedy no MCP call can perform
            assert entry == {"operator": entry["operator"], "ready": False}
            assert isinstance(entry["operator"], str) and entry["operator"]
            continue
        assert set(entry) <= {"tool", "arguments", "ready", "needs"}, entry
        assert entry["tool"] in _ADVERTISED, entry["tool"]
        assert isinstance(entry["arguments"], dict)
        assert isinstance(entry["ready"], bool)
        if entry["ready"]:
            assert "needs" not in entry
        else:
            assert isinstance(entry["needs"], str) and entry["needs"]


@pytest.fixture(autouse=True)
def _clean_session(tmp_path):
    old_authoring = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    remedy.forget()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    remedy.forget()
    server_mod.AUTHORING = old_authoring


# ------------------------------------------------------------ the shape

def test_the_schema_accepts_a_call_and_a_preference_list():
    one = remedy.call("revl_load", {"files": ["a.rvl"]})
    later = remedy.call("revl_load", {}, ready=False, needs="`source` or `files`")
    _assert_next_shape(one)
    _assert_next_shape([one, later])
    _assert_next_shape(remedy.operator_step("run setup, then restart"))
    assert one == {"tool": "revl_load", "arguments": {"files": ["a.rvl"]},
                   "ready": True}


# ------------------------------------------------- nothing is loaded

@pytest.fixture
def can_boot():
    """A server whose interpreter can boot a composition. Without the runtime
    the #1692 gate answers first, with its own remedy (the test below)."""
    server_mod.set_runtime_available(True)
    yield
    server_mod.set_runtime_available(None)


@pytest.mark.parametrize("tool,arguments", NEEDS_LOADED,
                         ids=[t for t, _ in NEEDS_LOADED])
def test_nothing_loaded_names_revl_load_and_what_it_needs(can_boot, tool, arguments):
    """No composition was ever loaded: `next` is `revl_load`, not ready, and
    says the caller must supply `source` or `files`."""
    payload = _call(tool, arguments)
    assert payload["ok"] is False
    nxt = payload["next"]
    _assert_next_shape(nxt)
    assert nxt["tool"] == "revl_load" and nxt["ready"] is False
    assert "`source`" in nxt["needs"] and "`files`" in nxt["needs"]
    message = payload["diagnostics"][0]["message"]
    assert message.startswith("nothing is loaded")
    assert "Next: revl_load with `source`" in message


@pytest.mark.parametrize("tool,arguments", NEEDS_LOADED,
                         ids=[t for t, _ in NEEDS_LOADED])
def test_without_the_runtime_the_remedy_is_the_operator_step(tool, arguments):
    """On a server that cannot import cordis, `revl_load` would fail too, so a
    runtime verb's refusal names what fixes it: an operator step (#1692). The
    refusal still carries `next`; offering `revl_load` would be a false remedy."""
    from revl.mcp import runtime_gate
    server_mod.set_runtime_available(False)
    try:
        payload = _call(tool, arguments)
    finally:
        server_mod.set_runtime_available(None)
    assert payload["ok"] is False
    nxt = payload["next"]
    _assert_next_shape(nxt)
    if runtime_gate.is_refused(tool, arguments):
        assert nxt["ready"] is False and "setup.sh" in nxt["operator"]
    else:
        assert nxt["tool"] == "revl_load"


@needs_runtime
@pytest.mark.parametrize("tool,arguments", NEEDS_LOADED,
                         ids=[t for t, _ in NEEDS_LOADED])
def test_after_an_unload_next_reloads_what_ran(tmp_path, tool, arguments):
    """The session ran a composition from a file and unloaded it: every
    nothing-loaded refusal offers the reload of that file, ready, and sending
    it as-is boots the composition again."""
    path = tmp_path / "cache.rvl"
    path.write_text(CACHE, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _call("revl_unload", {})["ok"] is True

    payload = _call(tool, arguments)
    assert payload["ok"] is False
    nxt = payload["next"]
    _assert_next_shape(nxt)
    assert nxt == {"tool": "revl_load", "arguments": {"files": [str(path)]},
                   "ready": True}
    assert "Next: revl_load (from cache.rvl)" in payload["diagnostics"][0]["message"]
    reloaded = _send(nxt)
    assert reloaded["ok"] is True, reloaded
    assert server_mod.SESSION.loaded


# ------------------------------------------- a files-loaded composition

@needs_runtime
def test_a_files_loaded_edit_refusal_carries_a_swap_that_succeeds(tmp_path):
    path = tmp_path / "cache.rvl"
    path.write_text(CACHE, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True

    refused = _call("revl_edit", {"edits": [{"anchor": "fn size() = 0",
                                             "replacement": "fn size() = 7"}]})
    assert refused["ok"] is False and refused["edited"] is False
    nxt = refused["next"]
    _assert_next_shape(nxt)
    assert nxt["tool"] == "revl_swap" and nxt["ready"] is True
    assert "fn size() = 7" in nxt["arguments"]["source"]
    message = refused["diagnostics"][0]["message"]
    assert "loaded from files (cache.rvl)" in message
    assert "Next: revl_swap" in message

    swapped = _send(nxt)
    assert swapped["ok"] is True and swapped["swapped"] is True, swapped
    assert _call("revl_call", {"key": "cache", "method": "size",
                               "args": []})["result"] == 7
    # the session now holds inline source, so revl_edit patches it directly
    edited = _call("revl_edit", {"edits": [{"anchor": "fn size() = 7",
                                            "replacement": "fn size() = 8"}]})
    assert edited["ok"] is True and edited["swapped"] is True, edited
    assert path.read_text(encoding="utf-8") == CACHE   # disk untouched


@needs_runtime
def test_a_files_loaded_edit_that_would_not_admit_is_not_ready(tmp_path):
    path = tmp_path / "cache.rvl"
    path.write_text(CACHE, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True

    refused = _call("revl_edit", {"edits": [{"anchor": "fn size() = 0",
                                             "replacement": "fn size() = \"x\""}]})
    nxt = refused["next"]
    _assert_next_shape(nxt)
    assert nxt["tool"] == "revl_swap" and nxt["ready"] is False
    assert "this one is refused" in nxt["needs"]


@needs_runtime
def test_a_files_loaded_name_only_swap_offers_the_files(tmp_path):
    path = tmp_path / "cache.rvl"
    path.write_text(CACHE, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True

    refused = _call("revl_swap", {})
    assert refused["ok"] is False
    nxt = refused["next"]
    _assert_next_shape(nxt)
    assert nxt == {"tool": "revl_swap", "arguments": {"files": [str(path)]},
                   "ready": True}
    assert _send(nxt)["swapped"] is True


def test_the_runtime_gate_refusal_uses_the_same_schema():
    """#1692's refusal on a server that cannot import cordis is an operator
    step: no MCP call installs a runtime."""
    from revl.mcp import runtime_gate
    nxt = runtime_gate.refusal("revl_load")["next"]
    _assert_next_shape(nxt)
    assert "setup.sh" in nxt["operator"]
