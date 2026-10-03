"""A files-loaded session holds each file's text from the load on (issue #1842).

The held source is the truth and disk is only an export (#1696). Until this,
a loaded file's buffer was read from disk until its first edit, so a name-only
swap of an unedited file picked up a change made on disk after the load, and a
file deleted after the load left the session unable to edit or swap it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh`")

SERVICE = "service Tool { fn describe() -> Str }\n"
LIB = 'pub fn label() -> Str { return "v1" }\n'
MAIN = ('use "lib.rvl" { label }\n'
        "component T provides tool: Tool {\n"
        "  provide tool { fn describe() = label() }\n"
        "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _describe() -> str:
    return _call("revl_call", {"key": "tool", "method": "describe"})["result"]


@pytest.fixture
def loaded(tmp_path):
    paths = []
    for name, text in (("svc.rvl", SERVICE), ("lib.rvl", LIB), ("main.rvl", MAIN)):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    assert _call("revl_load", {"files": [str(p) for p in paths]})["ok"] is True
    try:
        yield paths
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


def test_the_load_records_every_files_text(loaded):
    held = _call("revl_snapshot", {})["snapshot"]["sources"]["files_content"]
    assert held == {str(p): text for p, text in zip(loaded, (SERVICE, LIB, MAIN))}


def test_a_name_only_swap_keeps_the_held_text_after_a_disk_change(loaded):
    _svc, lib, _main = loaded
    lib.write_text('pub fn label() -> Str { return "from disk" }\n', encoding="utf-8")
    swapped = _call("revl_swap", {})
    assert swapped["ok"] is True and swapped["swapped"] is True, swapped
    assert _describe() == "v1"


def test_edit_and_swap_still_work_after_the_files_are_deleted(loaded):
    _svc, lib, main = loaded
    lib.unlink()
    main.unlink()
    edited = _call("revl_edit", {"target": str(lib), "edits": [
        {"anchor": '"v1"', "replacement": '"v2"'}]})
    assert edited["ok"] is True and edited["swapped"] is True, edited
    assert _describe() == "v2"
    swapped = _call("revl_swap", {})
    assert swapped["ok"] is True and swapped["swapped"] is True, swapped
    assert _describe() == "v2"
    assert not lib.exists() and not main.exists()   # nothing written back


def test_a_files_swap_records_the_text_it_swapped_in(loaded):
    _svc, lib, _main = loaded
    lib.write_text('pub fn label() -> Str { return "v3" }\n', encoding="utf-8")
    swapped = _call("revl_swap", {"files": [str(p) for p in loaded]})
    assert swapped["swapped"] is True, swapped
    lib.write_text('pub fn label() -> Str { return "later" }\n', encoding="utf-8")
    assert _call("revl_swap", {})["swapped"] is True
    assert _describe() == "v3"
