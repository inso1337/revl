"""In-memory compilation and the live MCP session.

Two properties are under test here. First, that a draft component never
touches the filesystem: an agent can check, admit, load, call and tear down
code that has no file. Second — the load-bearing one — that a *rejected*
candidate cannot deploy, and the composition it failed to replace keeps
answering.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

READONLY = """
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
}
"""

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

USING_MODULE = 'use "./lib.rvl" { double }\n' + """
service S { fn f(a: Int) -> Int }
component C provides s: S { provide s { fn f(a) = double(a) } }
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------------ in-memory compilation

def test_inline_source_never_touches_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = _call("revl_check", {"source": READONLY})
    assert payload["ok"] is True
    assert list(tmp_path.iterdir()) == []


def test_use_imports_resolve_from_memory():
    payload = _call("revl_check", {
        "source": USING_MODULE,
        "modules": {"./lib.rvl": "pub fn double(n: Int) -> Int { return n * 2 }"},
    })
    assert payload["ok"] is True, payload
    assert payload["loadOrder"] == ["C"]


def test_no_modules_supplied_says_so():
    # an empty `modules` is the no-imports fast path: the diagnostic names
    # the fix rather than complaining about a missing file
    payload = _call("revl_check", {"source": USING_MODULE, "modules": {}})
    assert payload["ok"] is False
    assert "`modules=`" in payload["diagnostics"][0]["message"]


def test_a_module_missing_from_the_supplied_set_is_a_normal_rejection():
    payload = _call("revl_check", {
        "source": USING_MODULE,
        "modules": {"./other.rvl": "pub fn unrelated() -> Int { return 1 }"},
    })
    assert payload["ok"] is False
    assert "cannot find imported module `./lib.rvl`" in payload["diagnostics"][0]["message"]


def test_compile_source_carries_the_ambient_manifest():
    running = compile_source(READONLY)
    admitted = compile_source(
        """service Cache { fn get(key: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component Watcher requires cache: Cache provides log: Log {
  provide log { fn note(m) = 1 }
}""",
        manifest=running,
    )
    assert [c["name"] for c in admitted["components"]] == ["Watcher"]
    assert set(admitted["manifest"]["loadOrder"]) == {"C", "Watcher"}


# --------------------------------------------------------------- live session

pytest.importorskip("cordis", reason="the session tools need the cordis-py runtime")


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


def test_load_call_and_unload_with_no_residue():
    loaded = _call("revl_load", {"source": CACHE})
    assert loaded["ok"] is True
    assert loaded["components"] == [{"name": "MemCache", "state": "ACTIVE"}]
    assert loaded["providedKeys"] == ["cache"]

    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0

    unloaded = _call("revl_unload", {})
    assert unloaded["noResidue"] is True
    assert all(unloaded["checks"].values())


def test_swap_replaces_the_running_generation():
    _call("revl_load", {"source": CACHE})
    swapped = _call("revl_swap", {"source": CACHE.replace("fn size() = 0",
                                                          "fn size() = 42")})
    assert swapped["admitted"] is True and swapped["swapped"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42


def test_a_rejected_swap_leaves_the_running_system_serving():
    _call("revl_load", {"source": CACHE})
    rejected = _call("revl_swap", {"source": CACHE.replace("fn size() = 0",
                                                           'fn size() = "nope"')})
    assert rejected["ok"] is False
    assert rejected["swapped"] is False
    assert rejected["diagnostics"][0]["code"] == "T1"
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


def test_rollback_restores_the_previous_generation():
    _call("revl_load", {"source": CACHE})
    _call("revl_swap", {"source": CACHE.replace("fn size() = 0", "fn size() = 42")})
    assert _call("revl_rollback", {})["ok"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


def test_state_reports_what_is_running():
    assert _call("revl_state", {})["loaded"] is False
    _call("revl_load", {"source": CACHE})
    state = _call("revl_state", {})
    assert state["loaded"] is True
    assert state["loadOrder"] == ["MemCache"]


def test_calling_before_loading_is_a_clean_error():
    payload = _call("revl_call", {"key": "cache", "method": "size"})
    assert payload["ok"] is False
    assert "nothing is loaded" in payload["diagnostics"][0]["message"]


# ------------------------------------------------- a chain, built in memory

CHAIN_SERVICES = """
service Db { fn get(k: Str) -> Opt[Str] }
service Cache { fn get(k: Str) -> Opt[Str] }
service Api { fn lookup(k: Str) -> Str }
"""
CHAIN_DB = """
component MemDb provides db: Db {
  let store = effect Map.new() undo store.drop()
  provide db { fn get(k) = store.get(k) }
}
"""
CHAIN_CACHE = """
component L1 requires db: Db provides cache: Cache {
  provide cache { fn get(k) = db.get(k) }
}
"""
CHAIN_CACHE_V2 = """
component L1 requires db: Db provides cache: Cache {
  provide cache { fn get(k) = db.get(k) ?? Some("default") }
}
"""
CHAIN_API = """
component Front requires cache: Cache provides api: Api {
  provide api { fn lookup(k) = "v1:" + (cache.get(k) ?? "miss") }
}
"""
CHAIN_LOG = """
service Log { fn note(m: Str) -> Int }
component Auditor requires api: Api provides log: Log {
  provide log { fn note(m) = 1 }
}
"""


def _lookup() -> object:
    return _call("revl_call", {"key": "api", "method": "lookup",
                               "args": ["k"]})["result"]


def test_a_three_link_chain_loads_in_dependency_order_and_answers():
    loaded = _call("revl_load",
                   {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE + CHAIN_API})
    assert loaded["loadOrder"] == ["MemDb", "L1", "Front"]
    assert [c["state"] for c in loaded["components"]] == ["ACTIVE"] * 3
    assert _lookup() == "v1:miss"


def test_swapping_the_middle_link_is_visible_through_the_chain():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE + CHAIN_API})
    swapped = _call("revl_swap",
                    {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2 + CHAIN_API})
    assert swapped["swapped"] is True
    # the dependent at the end of the chain sees the new middle behaviour
    assert _lookup() == "v1:default"


def test_a_running_chain_can_grow_a_new_component():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2 + CHAIN_API})
    grown = _call("revl_swap", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2
                                + CHAIN_API + CHAIN_LOG})
    assert grown["swapped"] is True
    assert grown["loadOrder"] == ["MemDb", "L1", "Front", "Auditor"]
    assert grown["providedKeys"] == ["api", "cache", "db", "log"]


def test_a_broken_middle_link_cannot_deploy_into_a_chain():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2 + CHAIN_API})
    broken = CHAIN_CACHE_V2.replace('fn get(k) = db.get(k) ?? Some("default")',
                                    "fn get(k) = 42")
    rejected = _call("revl_swap",
                     {"source": CHAIN_SERVICES + CHAIN_DB + broken + CHAIN_API})
    assert rejected["swapped"] is False
    assert "expects `Opt[Str]`, got `Int`" in rejected["diagnostics"][0]["message"]
    assert _lookup() == "v1:default"  # the whole chain keeps serving


def test_the_whole_chain_unloads_without_residue():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2
                        + CHAIN_API + CHAIN_LOG})
    unloaded = _call("revl_unload", {})
    assert unloaded["noResidue"] is True
