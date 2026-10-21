"""An export that writes only a copy says so, and names what it left alone.

`revl_export` on a files-loaded composition writes each changed file to its
copy under `.revl/export/`, never over the loaded file (issue #2524). The
success payload used to say only "the held source was written to disk" while
`disk.inSync` was true, which reads as adopted: a client that took it as done
stopped with the loaded file still holding the old bytes. That is the shape of
the withdraw cell's premature stops (issue #2766), where the model exports and
answers correctly about breaks it computed, but the graded file never changed.

These tests hold the payload that names the copy, the loaded file left
unchanged, and the copy-out step. They do not change the write rule (#2524) or
the divergence block: `disk` still compares against the copy.
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
    reason="the session tools boot a composition and need the cordis-py "
           "runtime; install it with `sh backends/python/setup.sh`")

CLOCK = ("service Clock { fn now() -> Int }\n"
         "component FixedClock provides clock: Clock {\n"
         "  provide clock {\n"
         "    fn now() = 7\n"
         "  }\n"
         "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _reset():
    session = server_mod.SESSION
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = (tmp_path / "root").resolve()
    base.mkdir()
    monkeypatch.chdir(base)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(base),))
    _reset()
    try:
        yield base
    finally:
        _reset()
        server_mod.AUTHORING = old


def _loaded_with_one_change(root):
    """A files-loaded composition with one committed change, exported once."""
    path = (root / "clock.rvl").resolve()
    original = CLOCK
    path.write_text(original, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "FixedClock.clock.now", "source": "fn now() = 8"}})
    assert changed["ok"] is True and changed["committed"] is True, changed
    copy = (root / ".revl" / "export" / "clock.rvl").resolve()
    return path, copy


def test_a_copy_export_names_the_loaded_file_it_left_alone(root):
    path, copy = _loaded_with_one_change(root)
    original = CLOCK
    out = _call("revl_export", {})
    assert out["ok"] is True and out["written"] == [str(copy)], out
    # the loaded file is named, and the copy that holds the held text
    assert out["adopt"] == [{"file": str(path), "export": str(copy)}], out
    assert str(path) in out["note"] and str(copy) in out["note"], out
    assert "still holds the previous bytes" in out["note"], out
    assert "adopt it by copying the copy over that file" in out["note"], out
    # the write rule is unchanged, and so is the divergence block (#2524)
    assert path.read_text(encoding="utf-8") == original
    assert "fn now() = 8" in copy.read_text(encoding="utf-8")
    assert out["disk"] == {"inSync": True, "stale": []}, out


def test_a_second_export_still_names_the_file_it_left_alone(root):
    """The copy already holds the held text, so the second export writes
    nothing; the loaded file is still stale and must still be named. Deciding
    from this call's plan would answer "the disk already holds the held
    source" and drop the copy-out step (issue #2766)."""
    path, copy = _loaded_with_one_change(root)
    assert _call("revl_export", {})["written"] == [str(copy)]
    out = _call("revl_export", {})
    assert out["ok"] is True and out["written"] == [], out
    assert out["adopt"] == [{"file": str(path), "export": str(copy)}], out
    assert str(path) in out["note"], out
    assert path.read_text(encoding="utf-8") == CLOCK


def test_a_reverted_export_carries_no_adopt(root):
    """change -> export -> undo -> export: the last export rewrites the copy
    with the reverted text, which the loaded file already holds. Nothing is
    waiting for a copy-out, so the answer must not claim there is."""
    path, copy = _loaded_with_one_change(root)
    assert _call("revl_export", {})["ok"] is True
    undone = _call("revl_undo", {})
    assert undone["ok"] is True and undone["undone"] is True, undone
    out = _call("revl_export", {})
    assert out["ok"] is True and out["written"] == [str(copy)], out
    assert "adopt" not in out, out
    assert out["note"] == "the held source was written to disk", out
    assert "fn now() = 7" in copy.read_text(encoding="utf-8")


def test_an_inline_export_writes_its_path_and_carries_no_adopt(root):
    target = (root / ".revl" / "export" / "main.rvl").resolve()
    assert _call("revl_load", {"source": CLOCK})["ok"] is True
    out = _call("revl_export", {"path": str(target)})
    assert out["ok"] is True and out["written"] == [str(target)], out
    assert "adopt" not in out, out
    assert out["note"] == "the held source was written to disk", out
