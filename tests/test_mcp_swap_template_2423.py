"""Issue #2423: `revl_swap` over MCP on a composition that spawns a template.

A `spawn` target is a template: it is one of the running IR's components but
not one of the linked ROOT manifest's, so `query.withdrawal` answers `unknown
component` and carries no `breaks`. The authoring loop's blast radius indexed
that key unconditionally, so a swap that touched the template raised
`KeyError: 'breaks'` and the caller got an internal error instead of the swap
result.

The fix branches on the withdrawal answer. A name the manifest lists as a
template gets an explicit template entry: an empty cascade and zero breaks,
because its provisions come up per instance, with a precision and note saying
the static composition excludes it. Any other unknown component refuses
instead of reporting zeros, so an inconsistent radius cannot hide a real
withdrawal set.

The composition and the swap are the legitimate template path of item 334,
driven through the MCP verb the issue names.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import authoring_loop  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

CLEAN = ("service S { fn f() -> Int }\n"
         "component C provides s: S { provide s { fn f() = 1 } }\n")

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
    # radius as an explicit template answer, not a fabricated exact one, and
    # the successor really is live.
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    entry = swapped["blastRadius"]["components"]["Worker"]
    assert entry["template"] is True
    assert entry["precision"] == "template"
    assert "excluded from the static composition" in entry["precisionNote"]
    assert "per instance" in entry["precisionNote"]
    assert entry["breaks"] == 0
    assert entry["cascade"] == []
    assert swapped["blastRadius"]["breaks"] == 0
    # The response note must not claim an exact answer for a component the
    # static graph does not hold.
    assert "exact, from the running composition" not in swapped["blastRadius"]["note"]
    assert "spawn template" in swapped["blastRadius"]["note"]
    called = _call("revl_call", {"key": "admin", "method": "ver"})
    assert called["result"] == 2, called


def test_a_non_template_unknown_component_refuses_instead_of_zeroing():
    """Only a named template may take the empty shape. Any other component the
    withdrawal query does not know is an inconsistent radius: it must name the
    refusal, never return a zero that would read as an empty cascade."""
    before = compile_source(CLEAN, "clean.rvl")
    after = compile_source(CLEAN, "clean.rvl")
    # A component of the running IR that the linked manifest does not hold and
    # does not name as a template.
    before["components"].append({"name": "Ghost", "provides": {}})
    after["components"].append({"name": "Ghost", "provides": {"ghost": "S"}})

    with pytest.raises(authoring_loop.BlastRadiusRefused) as raised:
        authoring_loop.blast_radius(before, after)

    assert raised.value.component == "Ghost"
    assert raised.value.answer["ok"] is False
    assert "unknown component" in str(raised.value)
