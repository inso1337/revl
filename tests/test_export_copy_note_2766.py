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

import hashlib
import importlib.util
import json
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

OTHER = ("service Other { fn tick() -> Int }\n"
         "component FixedOther provides other: Other {\n"
         "  provide other {\n"
         "    fn tick() = 9\n"
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


def _loaded_with_two_changes(root):
    """A files-loaded composition of two files, both with a committed change,
    loaded in this order: `clock.rvl`, then `sub/other.rvl`. Each changed file
    is exported to a copy under the root's export directory, so the answer
    carries two pairs and the note names both (issue #2766)."""
    first = (root / "clock.rvl").resolve()
    (root / "sub").mkdir()
    second = (root / "sub" / "other.rvl").resolve()
    first.write_text(CLOCK, encoding="utf-8")
    second.write_text(OTHER, encoding="utf-8")
    loaded = _call("revl_load", {"files": [str(first), str(second)]})
    assert loaded["ok"] is True, loaded
    for component, source in (("FixedClock.clock.now", "fn now() = 8"),
                              ("FixedOther.other.tick", "fn tick() = 10")):
        changed = _call("revl_change", {"commit": True, "replace": {
            "component": component, "source": source}})
        assert changed["ok"] is True and changed["committed"] is True, changed
    first_copy = (root / ".revl" / "export" / "clock.rvl").resolve()
    second_copy = (root / ".revl" / "export" / "sub" / "other.rvl").resolve()
    return first, second, first_copy, second_copy


def test_a_two_file_copy_export_names_both_pairs_in_load_order(root):
    """The multi-file note branch: two loaded files left stale, not one, so
    the note names both pairs in the order they were loaded and the adopt list
    carries both entries. The wording is the branch's own, not the single-file
    note's."""
    first, second, first_copy, second_copy = _loaded_with_two_changes(root)
    out = _call("revl_export", {})
    assert out["ok"] is True, out
    assert out["written"] == [str(first_copy), str(second_copy)], out
    assert out["adopt"] == [{"file": str(first), "export": str(first_copy)},
                            {"file": str(second),
                             "export": str(second_copy)}], out
    assert out["note"] == (
        "the held source is at export copies; these loaded files still hold "
        f"the previous bytes: {first} (copy {first_copy}); "
        f"{second} (copy {second_copy}). An export never writes over a loaded "
        "file outside the export directory: adopt it by copying each copy "
        "over its file"), out
    # the write rule still holds for both: the loaded files keep the old bytes
    assert first.read_text(encoding="utf-8") == CLOCK
    assert second.read_text(encoding="utf-8") == OTHER
    assert "fn now() = 8" in first_copy.read_text(encoding="utf-8")
    assert "fn tick() = 10" in second_copy.read_text(encoding="utf-8")
    assert out["disk"] == {"inSync": True, "stale": []}, out


def test_an_export_area_file_is_its_own_copy_and_is_not_adopted(root):
    """A loaded file inside the export directory is where its own export
    writes, so the copy path equals the loaded path and there is no separate
    copy to adopt. The export writes it in place, adopts nothing for it, and
    the note names only the operator file left unchanged."""
    export = root / ".revl" / "export"
    export.mkdir(parents=True)
    jailed = (export / "jailed.rvl").resolve()
    jailed.write_text(OTHER, encoding="utf-8")
    # an export replaces bytes in the export directory only when its manifest
    # records them, so prime the record an earlier export would have written
    (export / "manifest.json").write_text(json.dumps({"files": {
        ".revl/export/jailed.rvl": {
            "sha256": hashlib.sha256(OTHER.encode("utf-8")).hexdigest()}}}),
        encoding="utf-8")
    operator = (root / "clock.rvl").resolve()
    operator.write_text(CLOCK, encoding="utf-8")
    loaded = _call("revl_load", {"files": [str(jailed), str(operator)]})
    assert loaded["ok"] is True, loaded
    for component, source in (("FixedOther.other.tick", "fn tick() = 10"),
                              ("FixedClock.clock.now", "fn now() = 8")):
        changed = _call("revl_change", {"commit": True, "replace": {
            "component": component, "source": source}})
        assert changed["ok"] is True and changed["committed"] is True, changed
    operator_copy = (root / ".revl" / "export" / "clock.rvl").resolve()
    out = _call("revl_export", {})
    assert out["ok"] is True, out
    # the jailed file is written where it sits, the operator file to its copy
    assert out["written"] == [str(jailed), str(operator_copy)], out
    assert out["adopt"] == [{"file": str(operator),
                             "export": str(operator_copy)}], out
    assert out["note"] == (
        f"the held source is at the export copy {operator_copy}; the loaded "
        f"file {operator} still holds the previous bytes. An export never "
        "writes over a loaded file outside the export directory: adopt it by "
        "copying the copy over that file"), out
    assert "fn tick() = 10" in jailed.read_text(encoding="utf-8")
    assert operator.read_text(encoding="utf-8") == CLOCK
    assert "fn now() = 8" in operator_copy.read_text(encoding="utf-8")


def test_a_loaded_file_without_held_text_is_never_an_adoptable_copy(root):
    """A load records `files_content` only for the files it could read, so a
    path with no held text has nothing to copy out and the guard keeps it out
    of the adopt list. The control shows the very same file is adoptable once
    it has held text that differs from disk, so the guard is what excludes
    it."""
    path = (root / "clock.rvl").resolve()
    path.write_text(CLOCK, encoding="utf-8")
    copy = (root / ".revl" / "export" / "clock.rvl").resolve()
    held_text = "fn now() = 8\n"
    assert server_mod._copied_not_adopted(
        {"files": [str(path)], "files_content": {str(path): held_text}}) == [
            {"file": str(path), "export": str(copy)}]
    assert server_mod._copied_not_adopted(
        {"files": [str(path)], "files_content": {}}) == []
