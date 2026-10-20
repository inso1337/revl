"""Issue #2423: `revl_swap` over MCP on a composition that spawns a template.

A `spawn` target is a template: it is one of the running IR's components but
not one of the linked ROOT manifest's, so `query.withdrawal` answers `unknown
component` and carries no `breaks`. The authoring loop's blast radius indexed
that key unconditionally, so a swap that touched the template raised
`KeyError: 'breaks'` and the caller got an internal error instead of the swap
result. A template's provisions come up per instance, so nothing at ROOT loses
one: the entry carries an empty cascade and zero breaks.

The composition and the swap are the legitimate template path of item 334,
driven through the MCP verb the issue names.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")


def _instance_source(version: int) -> str:
    """`Worker` is a template (a `spawn` target) providing `store`; `store`
    lives per instance and never at ROOT."""
    return f"""
service Store {{ fn version() -> Int }}
service Admin {{ fn ver() -> Int }}

component Worker provides store: Store {{
  provide store {{ fn version() = {version} }}
}}

component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{ fn ver() = w.store.version() }}
}}
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session():
    """The MCP surface shares one module-level session and undo stack."""
    from revl.mcp import server as server_mod

    server_mod.UNDO_STACK.clear()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.UNDO_STACK.clear()


@needs_runtime
def test_revl_swap_on_a_spawned_template_composition_answers():
    """Load the template composition, then swap the generation that changes
    the template's body. That swap's preflight looks `Worker` up in the linked
    ROOT graph and finds no entry, which is the shape that used to raise."""
    loaded = _call("revl_load", {"source": _instance_source(1)})
    assert loaded["ok"] is True, loaded
    assert loaded["providedKeys"] == ["admin"]  # `store` is per instance

    swapped = _call("revl_swap", {"source": _instance_source(2)})

    # A swap result, not an internal error: the template is in the blast
    # radius with an empty cascade, and the successor really is live.
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    entry = swapped["blastRadius"]["components"]["Worker"]
    assert entry["breaks"] == 0
    assert entry["cascade"] == []
    assert swapped["blastRadius"]["breaks"] == 0
    called = _call("revl_call", {"key": "admin", "method": "ver"})
    assert called["result"] == 2, called
