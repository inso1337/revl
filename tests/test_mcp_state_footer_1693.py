"""Issue #1693: every MCP response carries the session's ambient state.

`sessionState: {loaded, generation, components, dirty, draft}` rides on every
`tools/call` result, with the same shape on success and refusal. Pinned here:

* the shape (one schema check every case runs);
* it is present on every advertised verb's success AND refusal. Proven through
  `handle` with each verb's handler replaced by a stub that succeeds or
  refuses, because a real call of every verb would have real side effects;
* on real calls, it changes exactly when load / edit / swap / rollback /
  unload change the state, and not on a call, a refusal or a read.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import ambient  # noqa: E402
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

NOTHING = {"loaded": False, "generation": None, "components": [],
           "dirty": False, "draft": False}


def _result(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]


def _call(tool: str, arguments: dict) -> dict:
    return _result(tool, arguments)["structuredContent"]


def _footer(payload: dict) -> dict:
    footer = payload[ambient.KEY]
    _assert_footer_shape(footer)
    return footer


def _assert_footer_shape(footer: dict) -> None:
    assert set(footer) == {"loaded", "generation", "components", "dirty", "draft"}
    assert isinstance(footer["loaded"], bool)
    assert isinstance(footer["dirty"], bool)
    assert isinstance(footer["draft"], bool)
    assert isinstance(footer["components"], list)
    assert all(isinstance(name, str) for name in footer["components"])
    if footer["loaded"]:
        assert isinstance(footer["generation"], int) and footer["generation"] >= 1
    else:
        assert footer == NOTHING


@pytest.fixture(autouse=True)
def _clean_session():
    # a server that can boot a composition: without the runtime the #1692
    # gate answers a runtime verb before its handler, which the
    # runtime-gate case below covers on its own
    server_mod.set_runtime_available(True)
    yield
    server_mod.set_runtime_available(None)
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


# ------------------------------------------------- on every verb, both ways

_VERBS = sorted(server_mod._HANDLERS)


@pytest.mark.parametrize("ok", [True, False], ids=["success", "refusal"])
@pytest.mark.parametrize("verb", _VERBS)
def test_every_verb_carries_the_footer(monkeypatch, verb, ok):
    monkeypatch.setitem(server_mod._HANDLERS, verb, lambda _arguments: {"ok": ok})
    result = _result(verb, {})
    payload = result["structuredContent"]
    assert _footer(payload) == NOTHING
    assert ambient.KEY in result["content"][0]["text"]
    assert result["isError"] is (not ok)


def test_a_refusal_before_the_handler_carries_the_footer_too():
    """The path jail refuses before any handler runs; the footer still rides."""
    payload = _call("revl_check", {"files": ["/etc/hosts"]})
    assert payload["ok"] is False
    assert _footer(payload) == NOTHING


def test_the_runtime_gate_refusal_carries_the_footer():
    """A server that cannot import cordis refuses runtime verbs before any
    handler runs (#1692); the footer rides on that refusal too."""
    server_mod.set_runtime_available(False)
    try:
        payload = _call("revl_load", {"source": CACHE})
    finally:
        server_mod.set_runtime_available(None)
    assert payload["unavailable"] == "cordis-py runtime"
    assert _footer(payload) == NOTHING


def test_an_internal_fault_carries_the_footer(monkeypatch):
    def broken(_arguments):
        raise RuntimeError("boom")
    monkeypatch.setitem(server_mod._HANDLERS, "revl_state", broken)
    payload = _call("revl_state", {})
    assert payload["diagnostics"][0]["category"] == "internal"
    assert _footer(payload) == NOTHING


# ---------------------------------------- it moves exactly with the state

@needs_runtime
def test_the_footer_changes_exactly_when_the_state_does():
    seen = []

    def step(tool, arguments, *, ok=True):
        payload = _call(tool, arguments)
        assert payload["ok"] is ok, payload
        seen.append(_footer(payload))
        return seen[-1]

    assert step("revl_call", {"key": "cache", "method": "size"}, ok=False) == NOTHING

    loaded = step("revl_load", {"source": CACHE})
    assert loaded == {"loaded": True, "generation": 1, "components": ["MemCache"],
                      "dirty": False, "draft": False}
    assert step("revl_call", {"key": "cache", "method": "size"}) == loaded
    assert step("revl_state", {}) == loaded
    assert step("revl_swap", {"source": "component Broken {"}, ok=False) == loaded

    holed = step("revl_edit", {"edits": [{
        "anchor": "fn size() = 0",
        "replacement": 'fn size() = hole[Int] "count"'}]})
    assert holed == {**loaded, "dirty": True, "draft": True}

    filled = step("revl_edit", {"edits": [{"hole": 6, "expr": "7"}]})
    assert filled == {**loaded, "generation": 2}

    swapped = step("revl_swap", {"source": CACHE})
    assert swapped == {**loaded, "generation": 3}

    rolled = step("revl_rollback", {})
    assert rolled == {**loaded, "generation": 4}

    assert step("revl_unload", {}) == NOTHING
