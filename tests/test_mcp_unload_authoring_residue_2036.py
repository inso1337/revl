"""`revl_unload` proves no runtime residue while destroying the only copy of
an unexported edit (issue #2036).

`revl_unload` is the one call whose contract is to prove nothing was left
behind, and its residue proof covered the runtime only — registry, provisions,
effects, listeners, host resources — with no authoring-surface check at all.
So it answered `noResidue: true` with five green checks while the session's
only copy of a committed-but-unexported edit (#2032) went nowhere: the edit
never reached disk, so nothing else held it. Every claim in the payload was
individually true; the trap was a true statement read as a stronger one.

The fix is the authoring preflight this file holds the server to: the same
`diverged_paths` predicate the change loop publishes as `disk` (#2032) is
computed in the unload path, before anything is torn down. When it is
non-empty the unload is refused fail-closed — like `revl_step_back`'s `force`
— naming the stale paths and `revl_export`, and `force: true` discards the
work anyway with the loss reported. `noResidue` is deliberately NOT overloaded:
it keeps its runtime-teardown meaning, and the `disk` block carries the
authoring answer. The refusal never points at the `undo` the unload hands
back, because that document cannot be restored (#2033).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.persist import ORIGIN_FILES_CONTENT  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime "
           "— install it with `sh backends/python/setup.sh`")

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

#: The one-line body edit the issue's reproduction makes.
EDIT = {"anchor": 'term == "a"', "replacement": 'term == "b"'}

INLINE = ("service S { fn f() -> Int }\n"
          "component C provides s: S { provide s { fn f() = 1 } }\n")

#: A holed candidate on disk: `revl_load {files: [...]}` of it opens a draft
#: held as the file's text, which is the other thing an unload destroys. Two
#: holes, so filling one leaves the draft held instead of booting it.
SCAFFOLD = ("service Greeter {\n"
            "  fn greet() -> Str\n"
            "  fn count() -> Int\n"
            "}\n"
            "\n"
            "component GreeterProvider provides greeter: Greeter {\n"
            "  provide greeter {\n"
            '    fn greet() = hole[Str] "produce greet\'s Str result"\n'
            '    fn count() = hole[Int] "produce count\'s Int result"\n'
            "  }\n"
            "}\n")

#: An edit of the second file, so the divergence can be shown to follow the
#: file that was edited rather than a fixed one.
APP_EDIT = {"anchor": "component ApiImpl requires",
            "replacement": "// touched by #2036's test\ncomponent ApiImpl requires"}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def tree(clean_session):
    """The issue's two-file composition, on disk and under the roots."""
    (clean_session / "components").mkdir()
    mem = clean_session / "components" / "memory_store.rvl"
    mem.write_text(MEM_TEXT, encoding="utf-8")
    app = clean_session / "app.rvl"
    app.write_text(APP_TEXT, encoding="utf-8")
    return {"mem": mem, "app": app}


def _load(tree) -> dict:
    loaded = _call("revl_load", {"files": [str(tree["app"]), str(tree["mem"])]})
    assert loaded["ok"] is True, loaded
    return loaded


def _change(tree, target="mem") -> dict:
    edits = [EDIT] if target == "mem" else [APP_EDIT]
    return _call("revl_change", {"commit": True,
                                 "edit": {"target": str(tree[target]),
                                          "edits": edits}})


def _held(tree) -> str:
    """The text the session holds for the edited file."""
    return server_mod.SESSION.origin[ORIGIN_FILES_CONTENT][str(tree["mem"])]


# --------------------------- acceptance 1: the unexported edit is named,
# --------------------------- and the unload is refused (the `force` route)

def test_unload_refuses_and_names_the_unexported_file(tree):
    """Acceptance 1: `revl_change {commit: true}` leaves the file unexported,
    and the unload that would destroy it is refused — naming the stale path
    and requiring `force` — instead of answering `noResidue: true`."""
    _load(tree)
    assert _change(tree)["committed"] is True
    refused = _call("revl_unload", {})
    assert refused["ok"] is False
    assert refused["refused"] is True
    assert refused["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}
    message = refused["diagnostics"][0]["message"]
    assert str(tree["mem"]) in message
    assert "force" in message
    # the composition is untouched: still loaded, still holding the edit
    assert server_mod.SESSION.loaded is True
    assert '== "b"' in _call("revl_source", {"symbol": "MemoryStore"})["text"]
    # ... and disk still does not have it — the refusal wrote nothing
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT


def test_the_refusal_offers_revl_export_not_the_unusable_undo(tree):
    """The remedy named is the call that writes the held source out, never the
    `undo` the unload would hand back — that document cannot be restored
    (#2033), so pointing at it would steer the caller into the second trap."""
    _load(tree)
    _change(tree)
    refused = _call("revl_unload", {})
    assert refused["next"] == {"tool": "revl_export", "arguments": {},
                               "ready": True}
    assert "revl_export" in refused["diagnostics"][0]["message"]
    assert "undo" not in refused


def test_noResidue_is_not_overloaded_by_the_authoring_answer(tree):
    """The ruling on #2036: `noResidue` is the runtime-teardown proof, and a
    refusal that never tore the runtime down does not fabricate one. The
    authoring loss rides in `disk`."""
    _load(tree)
    _change(tree)
    refused = _call("revl_unload", {})
    assert "noResidue" not in refused
    assert refused["disk"]["inSync"] is False


# ------------------------------------------------- acceptance 4: the axis

def test_the_axis_is_non_empty_for_any_loaded_file_that_diverges(tree):
    """Acceptance 4: whenever the held source of a loaded file differs from
    disk, the unload's authoring axis says so — whichever file it is."""
    _load(tree)
    assert _call("revl_source", {"symbol": "MemoryStore"})["disk"] == {
        "inSync": True, "stale": []}
    assert _change(tree, target="app")["disk"] == {
        "inSync": False, "stale": [str(tree["app"])]}
    refused = _call("revl_unload", {})
    assert refused["disk"] == {"inSync": False, "stale": [str(tree["app"])]}
    assert str(tree["app"]) in refused["diagnostics"][0]["message"]
    # a second loaded file the change did not touch is not swept in
    assert str(tree["mem"]) not in refused["disk"]["stale"]


def test_the_preflight_asks_the_same_question_as_the_change_loop(tree):
    """One predicate, one answer: the unload preflight and the change loop's
    `disk` block are the same computation over the same held source, so the
    verb that destroys the work cannot disagree with the verb that warns."""
    _load(tree)
    assert _change(tree)["disk"]["inSync"] is False
    assert server_mod._unload_disk_state() == server_mod._disk_state()
    assert server_mod._unload_disk_state() == {"inSync": False,
                                               "stale": [str(tree["mem"])]}


def test_the_disk_block_is_not_a_session_state_key(tree):
    """`sessionState` keeps its pinned key set (#1693): the authoring answer is
    a sibling of the footer on an unload too, not a new footer key."""
    _load(tree)
    _change(tree)
    gone = _call("revl_unload", {"force": True})
    assert "disk" not in gone["sessionState"]
    assert set(gone["sessionState"]) == {"loaded", "generation", "components",
                                         "dirty", "draft"}
    # the authoring answer is present, and is a sibling of the footer
    assert gone["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}


# ------------------------------------------------------------ the force path

def test_force_unloads_and_reports_the_loss_it_accepted(tree):
    """`force: true` proceeds — and says what it destroyed, in the field an
    agent checks, naming the verb that would have written it out."""
    _load(tree)
    _change(tree)
    forced = _call("revl_unload", {"force": True})
    assert forced["ok"] is True and forced["unloaded"] is True
    assert forced["noResidue"] is True and forced["compensationResidue"] == []
    assert forced["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}
    assert "revl_export" in forced["note"]
    assert str(tree["mem"]) in forced["note"]
    assert server_mod.SESSION.loaded is False
    # the loss the note names is real, and is exactly what was reported
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT
    assert _call("revl_load", {"files": [str(tree["mem"])]})["ok"] is True


# ------------------------------- acceptance 2 and 3: the clean case holds

def test_export_then_unload_is_clean_and_silent(tree):
    """Acceptances 2 and 3: after `revl_export` the held source is on disk, so
    the unload is clean — no refusal, empty stale set, no warning — and the
    edit survives on disk."""
    _load(tree)
    _change(tree)
    assert _call("revl_export", {})["written"] == [str(tree["mem"])]
    gone = _call("revl_unload", {})
    assert gone["ok"] is True and gone["unloaded"] is True
    assert gone["noResidue"] is True and gone["compensationResidue"] == []
    assert gone["disk"] == {"inSync": True, "stale": []}
    assert "note" not in gone
    assert '== "b"' in tree["mem"].read_text(encoding="utf-8")


def test_unload_of_an_unchanged_files_composition_is_clean(tree):
    """The clean case must not regress: loading files and unloading without an
    edit is silent, in sync, and residue-free."""
    _load(tree)
    gone = _call("revl_unload", {})
    assert gone["ok"] is True and gone["unloaded"] is True
    assert gone["disk"] == {"inSync": True, "stale": []}
    assert "note" not in gone
    assert gone["noResidue"] is True


def test_an_inline_composition_has_nothing_on_disk_to_lose():
    """An inline composition names no path, so the preflight has nothing to
    compare and the unload proceeds as before."""
    assert _call("revl_load", {"source": INLINE})["ok"] is True
    edited = _call("revl_edit", {"edits": [{"anchor": "fn f() = 1",
                                            "replacement": "fn f() = 2"}]})
    assert edited["ok"] is True and edited["disk"] == {"inSync": True,
                                                       "stale": []}
    gone = _call("revl_unload", {})
    assert gone["ok"] is True and gone["unloaded"] is True
    assert gone["disk"] == {"inSync": True, "stale": []}
    assert "note" not in gone


def test_a_cold_held_draft_is_still_discarded_when_nothing_diverges(tree):
    """The other thing `revl_unload` destroys: a draft held while nothing is
    running. A draft that has not been edited holds exactly the file's text,
    so there is nothing on disk to lose and the unload proceeds as before —
    the preflight is not a blanket refusal of the draft path."""
    (tree["mem"].parent / "greeter.rvl").write_text(SCAFFOLD,
                                                    encoding="utf-8")
    draft = _call("revl_load", {"files": [str(tree["mem"].parent /
                                               "greeter.rvl")]})
    assert draft["ok"] is True and draft["draft"] is True, draft
    gone = _call("revl_unload", {})
    assert gone["ok"] is True and gone["discardedDraft"] is True
    assert gone["disk"] == {"inSync": True, "stale": []}
    # the note is the draft-discard one, not a loss: nothing diverged
    assert "lost" not in gone["note"] and "revl_export" not in gone["note"]
    assert server_mod.SESSION.pending_draft is None


def test_a_cold_files_draft_is_refused_once_its_holes_are_filled(tree):
    """A held draft of files that HAS been edited is the same loss on the same
    axis: the filled hole exists only in the session, so the unload that would
    discard it is refused, and `force` still discards it with the loss said."""
    path = tree["mem"].parent / "greeter.rvl"
    path.write_text(SCAFFOLD, encoding="utf-8")
    draft = _call("revl_load", {"files": [str(path)]})
    line = sorted(h["line"] for h in draft["holes"])[0]
    filled = _call("revl_edit", {"edits": [{"hole": line, "expr": '"hi"'}]})
    assert filled["ok"] is True and filled["draft"] is True, filled
    assert server_mod.SESSION.loaded is False
    refused = _call("revl_unload", {})
    assert refused["ok"] is False and refused["refused"] is True
    assert refused["disk"] == {"inSync": False, "stale": [str(path)]}
    assert "revl_export" in refused["next"]["tool"]
    assert server_mod.SESSION.pending_draft is not None
    gone = _call("revl_unload", {"force": True})
    assert gone["ok"] is True and gone["discardedDraft"] is True
    assert gone["disk"]["inSync"] is False
    assert str(path) in gone["note"] and "revl_export" in gone["note"]
    assert path.read_text(encoding="utf-8") == SCAFFOLD
    assert server_mod.SESSION.pending_draft is None


# ------------------------------------------- the seam #2036 was told to use

def test_the_preflight_goes_through_diverged_paths(tree, monkeypatch):
    """The named seam: the unload preflight must call `diverged_paths` (the
    shared predicate #2032 published and #2037 will use), not re-derive the
    comparison — a re-derivation is exactly the drift the seam prevents."""
    _load(tree)
    _change(tree)
    seen: list = []
    real = server_mod.diverged_paths

    def spy(texts, paths):
        seen.append((texts, list(paths)))
        return real(texts, paths)

    monkeypatch.setattr(server_mod, "diverged_paths", spy)
    refused = _call("revl_unload", {})
    assert refused["ok"] is False
    assert seen and str(tree["mem"]) in seen[0][1]
    assert seen[0][0][str(tree["mem"])] == _held(tree)


def test_the_advertised_unload_schema_offers_force():
    """A caller refused the unload has to be able to send the escape hatch:
    `force` is in the advertised input schema, and the description names the
    authoring preflight and `revl_export`."""
    found = _call("revl_verbs", {"names": ["revl_unload"]})
    assert found["ok"] is True, found
    unload = found["tools"][0]
    assert unload["name"] == "revl_unload"
    assert "force" in unload["inputSchema"]["properties"]
    assert "revl_export" in unload["description"]
