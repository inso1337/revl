"""Issue #3614: `Session.module` — a public, read-only accessor for the emitted
module of the composition currently loaded.

Semantics pinned here: None before `load`, after a failed load, and after
`unload`; after a live `swap` it is the module of the composition now running
(and a refused swap leaves the running one's in place); assignment raises
`AttributeError` (read-only handle).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)

CACHE = """
pub fn twice(n: Int) -> Int { return n * 2 }
service Cache { fn size() -> Int }
component MemCache provides cache: Cache {
  provide cache { fn size() = 0 }
}
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


def test_module_is_none_before_any_load():
    assert Session().module is None


def test_module_is_read_only():
    session = Session()
    with pytest.raises(AttributeError):
        session.module = object()


def test_module_is_none_after_a_failed_load(monkeypatch):
    """The real `_boot` assigns `_module` before activation runs; a load whose
    activation then refuses must leave the accessor at its pre-load value
    (None on a fresh session) — the load checkpoint puts it back."""
    session = Session()

    def fake_boot(ir, config, record, origin):
        session._module = object()   # what `_boot` does before `_load` runs
        raise SessionError("activation refused")

    monkeypatch.setattr(session, "_boot", fake_boot)
    with pytest.raises(SessionError):
        session.load(compile_source(CACHE))
    assert session.module is None


@needs_runtime
def test_module_is_the_loaded_composition_and_callable():
    from revl.mcp import server as server_mod

    _call("revl_load", {"source": CACHE})
    module = server_mod.SESSION.module
    assert module is not None
    assert module.twice(21) == 42


@needs_runtime
def test_module_follows_a_live_swap():
    from revl.mcp import server as server_mod

    _call("revl_load", {"source": CACHE})
    before = server_mod.SESSION.module
    swapped = _call("revl_swap", {"source": CACHE.replace("n * 2", "n * 3")})
    assert swapped["ok"] is True and swapped["swapped"] is True
    assert server_mod.SESSION.module is not before
    assert server_mod.SESSION.module.twice(21) == 63


@needs_runtime
def test_module_stays_on_the_running_composition_after_a_refused_swap():
    from revl.mcp import server as server_mod

    _call("revl_load", {"source": CACHE})
    before = server_mod.SESSION.module
    rejected = _call("revl_swap", {"source": CACHE.replace(
        "fn size() = 0", 'fn size() = "nope"')})
    assert rejected["ok"] is False and rejected["swapped"] is False
    assert server_mod.SESSION.module is before
    assert server_mod.SESSION.module.twice(21) == 42


@needs_runtime
def test_module_is_none_after_unload():
    from revl.mcp import server as server_mod

    _call("revl_load", {"source": CACHE})
    assert server_mod.SESSION.module is not None
    _call("revl_unload", {})
    assert server_mod.SESSION.module is None
