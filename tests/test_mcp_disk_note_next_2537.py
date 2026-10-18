"""Issue #2537: a response that tells the caller the held source is not on disk
also hands over the call that writes it.

`revl_export` is not in the listed tier, so a function-calling client cannot
emit it, and the note that says "call revl_export" names a call it cannot make
without first spending a `revl_verbs` lookup. The hatch (#2073) reaches it in
one call, and a structured `next` (#1691) is followed more reliably than prose.
So every site that sets a disk note also carries
`next = revl_verbs {name: "revl_export", args: {}}`, ready, because a disk note
exists only for a composition loaded from files and its export takes no
arguments. A `next` the payload already carries is kept. All cases run through
the real dispatch (`handle()`).

The sites (src/revl/mcp/server.py): `_ride_disk` (the change loop's verbs and
the check's ride, plus the revert verbs' own note), `_note_session_check_disk`,
`_note_held_file_differs`, and `_files_ignored_note`, which two callers carry
(`revl_edit` with `files` on a warm session, and the `revl_load` refusal).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import disclosure, remedy  # noqa: E402
from revl.mcp import edit as edit_mod  # noqa: E402
from revl.mcp import export_jail  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change loop boots a composition and needs the cordis-py "
           "runtime: install it with `sh backends/python/setup.sh`")

MEM_TEXT = ("service Store { fn find(term: Str) -> Bool }\n"
            "\n"
            "component MemoryStore provides store: Store {\n"
            "  provide store {\n"
            '    fn find(term: Str) -> Bool = term == "a"\n'
            "  }\n"
            "}\n")
APP_TEXT = ("service Api { fn lookup(term: Str) -> Bool }\n"
            "\n"
            "component ApiImpl requires store: Store provides api: Api {\n"
            "  provide api { fn lookup(term: Str) -> Bool = store.find(term) }\n"
            "}\n")
EDIT = {"anchor": 'term == "a"', "replacement": 'term == "b"'}
#: applies after EDIT has landed: its anchor survives its own replacement
FIXED_POINT = {"anchor": 'term == "b"', "replacement": 'term == "b"'}

#: The call the disk note's prose names, as the measured arm handed it over.
EXPORT_NEXT = {"tool": "revl_verbs",
               "arguments": {"name": "revl_export", "args": {}},
               "ready": True}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    remedy.forget()
    (tmp_path / "components").mkdir()
    mem = tmp_path / "components" / "memory_store.rvl"
    mem.write_text(MEM_TEXT, encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text(APP_TEXT, encoding="utf-8")
    yield {"mem": mem, "app": app}
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old
    remedy.forget()


def _committed(tree) -> dict:
    """Load the two files and commit one body edit that is not exported."""
    loaded = _call("revl_load", {"files": [str(tree["app"]), str(tree["mem"])]})
    assert loaded["ok"] is True, loaded
    changed = _call("revl_change", {"commit": True,
                                    "edit": {"target": str(tree["mem"]),
                                             "edits": [EDIT]}})
    assert changed["ok"] is True and changed["committed"] is True, changed
    return changed


def test_the_change_loop_carries_the_export_next(tree):
    changed = _committed(tree)
    assert changed["disk"]["inSync"] is False
    assert "revl_export" in changed["note"]
    assert changed["next"] == EXPORT_NEXT


def test_a_revert_that_leaves_disk_behind_carries_the_export_next(tree):
    _committed(tree)
    assert _call("revl_export", {})["ok"] is True
    undone = _call("revl_undo", {})
    assert undone["ok"] is True and undone["disk"]["inSync"] is False
    assert "reverted source" in undone["note"]
    assert undone["next"] == EXPORT_NEXT


def test_a_session_check_carries_the_export_next(tree):
    _committed(tree)
    checked = _call("revl_check", {"session": True})
    assert checked["disk"]["inSync"] is False and "revl_export" in checked["note"]
    assert checked["next"] == EXPORT_NEXT


def test_a_check_that_read_a_differing_file_carries_the_export_next(tree):
    _committed(tree)
    checked = _call("revl_check", {"files": [str(tree["mem"])]})
    assert "differs from the session's held source" in checked["note"]
    assert checked["next"] == EXPORT_NEXT


def test_an_edit_that_ignored_files_carries_the_export_next(tree):
    _committed(tree)
    edited = _call("revl_edit", {"files": [str(tree["mem"])],
                                 "target": str(tree["mem"]),
                                 "edits": [FIXED_POINT]})
    assert edited["ok"] is True, edited
    assert "files is ignored" in edited["note"] and "revl_export" in edited["note"]
    assert edited["next"] == EXPORT_NEXT


def test_the_load_refusal_that_ignored_files_carries_the_export_next(tree):
    _committed(tree)
    refused = _call("revl_load", {"files": [str(tree["mem"])]})
    assert refused["ok"] is False
    assert "files is ignored" in refused["note"]
    assert refused["next"] == EXPORT_NEXT


def test_nothing_diverged_means_no_next(tree):
    loaded = _call("revl_load", {"files": [str(tree["app"]), str(tree["mem"])]})
    assert loaded["ok"] is True
    checked = _call("revl_check", {"session": True})
    assert checked["disk"]["inSync"] is True
    assert "next" not in checked and "note" not in checked


def test_a_next_the_payload_already_carries_is_kept(tree):
    _committed(tree)
    own = remedy.call("revl_load", {"files": ["a.rvl"]})
    payload = {"ok": True, "next": own}
    assert server_mod._with_export_next(payload)["next"] is own
    assert server_mod._with_export_next({"ok": True})["next"] == EXPORT_NEXT


def test_the_handed_over_next_survives_the_hatch_and_exports(tree):
    """Sent as handed over through a real `revl_verbs` call, the `next` reaches
    the export: the copy under the export directory holds the edit, and the
    answer then says disk is in step."""
    changed = _committed(tree)
    nxt = changed["next"]
    assert nxt["tool"] == disclosure.DISCOVERY
    exported = _call(nxt["tool"], nxt["arguments"])
    assert exported["ok"] is True, exported
    copy = Path(export_jail.export_copy(str(tree["mem"]), server_mod._file_roots()))
    assert exported["written"] == [str(copy)]
    assert '== "b"' in copy.read_text(encoding="utf-8")
    assert exported["disk"] == {"inSync": True, "stale": []}
    assert "next" not in _call("revl_check", {"session": True})


def test_a_stale_edit_buffer_over_an_exported_composition_has_no_next(tree):
    """The note is judged on the working source, which includes an open edit
    buffer; the export writes the running composition. With the running source
    already on disk there is nothing to export, so a ready `next` would be a
    call that does nothing."""
    _committed(tree)
    assert _call("revl_export", {})["ok"] is True
    # an open buffer that differs from disk, over a running composition that
    # does not (the state an edit leaves while it still has holes to fill)
    buffer = edit_mod.virtual_source(server_mod.SESSION)
    texts = dict(buffer[edit_mod.ORIGIN_FILES_CONTENT])
    texts[str(tree["mem"])] = texts[str(tree["mem"])] + "\n"
    server_mod.SESSION.draft = {**buffer, edit_mod.ORIGIN_FILES_CONTENT: texts}
    try:
        seen = _call("revl_source", {"symbol": "MemoryStore"})
        assert seen["disk"]["inSync"] is False, seen
        assert "revl_export" in seen["note"]
        assert "next" not in seen, seen
    finally:
        server_mod.SESSION.draft = None
