"""`files` means one thing in both session states (issue #2035).

`files` names the **load set**: on a cold session it is what to boot, and on a
warm one there is nothing to boot. Reading it as "load this" in the second
state made the call shape an agent learns first invalid *by its own first
success* — the identical arguments that just worked were refused, and the
refusal's own remedy (`revl_unload first`) is the path that destroys the edit
the caller was building on (#2032: the change never reached disk; #2033: the
snapshot the unload hands back cannot be restored).

These tests hold the server to the issue's preferred reading: `files` is the
load set **only when nothing is loaded**, and on a warm session it is ignored
with a note, so the call succeeds in both states. The three verbs that take it
(`revl_change`, `revl_edit`, `revl_load`) agree, and the cold path the issue
says must not regress does not.

The identical call twice is spelled with an edit that is its own fixed point
(`EDIT_IS_A_FIXED_POINT`): its anchor survives its replacement, so the second
call has exactly the same work to do as the first and the only thing that
changed between them is the session state. That isolates the argument from the
edit — with the issue's own one-line body edit the second call would fail for
an unrelated reason (the anchor is gone because the first call landed it), and
that shape is pinned separately, as "not refused for `files`".
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

#: The same call shape with an edit that is its own fixed point, so an
#: identical call is not identical by accident: the anchor occurs in its own
#: replacement, and applying it again is accepted and swaps again.
EDIT_IS_A_FIXED_POINT = {"anchor": 'term == "a"', "replacement": 'term == "a"'}

#: The note the issue asks for, verbatim.
IGNORED = ("files is ignored: a composition is already loaded; "
           "the edit is applied to the running composition")

#: What `_ride_disk` says when the held source has not reached disk (#2032).
EXPORT_NOTE = "the held source differs from disk; call revl_export to write it"

#: What the edit engine says when the anchor is gone — i.e. what the *second*
#: call of the issue's own reproducer gets back. Both the with-`files` call and
#: the control that never carried `files` must produce exactly this, which is
#: the point: the argument is not what the second call fails on.
ANCHOR_GONE = ('anchor \'term == "a"\' does not occur in the buffer, '
               "not even with its whitespace ignored")


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


def _files(tree) -> list:
    return [str(tree["mem"])]


def _change(tree, edit, **extra) -> dict:
    """The issue's shape: `files` + `edit` + `commit`, one call."""
    return _call("revl_change", {"commit": True,
                                 "edit": {"target": str(tree["mem"]),
                                          "edits": [edit]}, **extra})


def _edit(tree, edit, **extra) -> dict:
    return _call("revl_edit", {"edits": [edit], **extra})


def _running(tree) -> str:
    """The text the running composition holds for the edited component."""
    return _call("revl_source", {"symbol": "MemoryStore"})["text"]


def _message(result: dict) -> str:
    return result["diagnostics"][0]["message"]


# ------------------------------- acceptance 1: the identical call, twice

def test_the_identical_files_change_succeeds_twice(tree):
    """Acceptance 1. The call the agent learned on a cold session is the call
    that keeps working: nothing about the session state is the caller's
    business, because `files` is the load set and nothing is being loaded."""
    first = _change(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree))
    assert first["ok"] is True, first
    assert first["committed"] is True and first["swapped"] is True
    second = _change(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree))
    assert second["ok"] is True, second
    assert second["committed"] is True and second["swapped"] is True
    # the cold call is not the odd one out: the same call runs in both states
    assert first["loaded"] is True and second["loaded"] is True
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT


def test_the_identical_files_edit_succeeds_twice(tree):
    """Acceptance 2, for `revl_edit {files, edits}` — refused with the same
    message before, so it gets the same treatment."""
    first = _edit(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree))
    assert first["ok"] is True, first
    assert first["loaded"] is True and first["swapped"] is True
    second = _edit(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree))
    assert second["ok"] is True, second
    assert second["swapped"] is True
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT


def test_the_ignore_note_is_the_one_the_issue_asks_for(tree):
    """The success says what it did with the argument, in the words the issue
    names, so an agent that carried `files` forward is told rather than left
    to infer that the argument stopped applying."""
    _edit(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree))     # cold: loads
    warm = _edit(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree))
    assert warm["note"] == IGNORED


def test_the_ignore_note_keeps_the_disk_advice(tree):
    """The note does not swallow #2032's: `_ride_disk` adds its own only where
    nothing has set one, so the ignore note carries the export advice itself
    when the edit it just applied left the held source unexported."""
    _call("revl_load", {"files": _files(tree)})
    warm = _change(tree, EDIT, files=_files(tree))
    assert warm["ok"] is True, warm
    assert warm["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}
    assert warm["note"] == f"{IGNORED}; {EXPORT_NOTE}"


def test_the_real_edit_called_twice_is_never_refused_for_files(tree):
    """The issue's own reproducer, unchanged: call 1 commits the one-line body
    edit, so call 2's *edit* cannot apply — its anchor is gone. That is the
    edit failing, and the test is that the failure is not the argument
    changing meaning: no refusal about the session being loaded, and nothing
    about `files` in the diagnostic the caller gets back."""
    assert _change(tree, EDIT, files=_files(tree))["committed"] is True
    second = _change(tree, EDIT, files=_files(tree))
    assert second["ok"] is False
    message = _message(second)
    assert "already loaded" not in message
    assert "files" not in message
    assert message == ANCHOR_GONE


def test_the_real_edit_called_twice_fails_the_same_without_files(tree):
    """The control for the test above: the same pair of calls, never carrying
    `files`, fails with the identical diagnostic. The second call fails because
    the first one landed the edit, not because of the argument."""
    assert _call("revl_load", {"files": _files(tree)})["loaded"] is True
    assert _change(tree, EDIT)["committed"] is True
    control = _change(tree, EDIT)
    assert control["ok"] is False
    assert _message(control) == ANCHOR_GONE


# --------------------------- acceptance 3: the cold path does not regress

def test_a_cold_files_change_still_loads_and_edits(tree):
    """Acceptance 3. The working cold path is the one the preferred fix must
    leave alone: nothing is loaded, so `files` is the load set and the edit
    lands on what the same call booted."""
    assert not server_mod.SESSION.loaded
    result = _change(tree, EDIT, files=_files(tree))
    assert result["ok"] is True, result
    assert result["loaded"] is True and result["swapped"] is True
    assert result["committed"] is True
    assert '== "b"' in _running(tree)
    # loaded, not written: the disk is still the pre-edit text (#2032)
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT
    # a cold call ignores nothing, so it carries no ignore note
    assert IGNORED not in (result.get("note") or "")


def test_a_cold_files_edit_still_loads_and_edits(tree):
    """The same cold path for `revl_edit`, the verb #1690 added it to."""
    assert not server_mod.SESSION.loaded
    result = _edit(tree, EDIT, files=_files(tree))
    assert result["ok"] is True, result
    assert result["loaded"] is True and result["swapped"] is True
    assert '== "b"' in _running(tree)
    assert IGNORED not in (result.get("note") or "")


# ------------------------------------ the three verbs take it the same way

def test_revl_load_with_files_on_a_warm_session_ignores_it_too(tree):
    """`revl_load` agrees with the other two about the argument: a warm load
    ignores `files` and says so. Ignoring it makes the call *identical* to the
    same call with the argument left off — which is what "ignored" means, and
    is pinned below by comparing the two. It still has nothing to boot, and
    that refusal now carries the mechanical remedy and names the order that
    does not lose the held source, rather than refusing `files` as a load."""
    _call("revl_load", {"files": _files(tree)})
    warm = _call("revl_load", {"files": _files(tree)})
    assert warm["ok"] is False
    assert warm["note"] == ("files is ignored: a composition is already "
                            "loaded; nothing was booted")
    assert warm["diagnostics"][0]["fix"] == ("drop `files` — it is the load "
                                             "set, and a composition is "
                                             "already loaded")
    bare = _call("revl_load", {})
    assert bare["ok"] is False
    assert _message(warm) == _message(bare)
    assert "files" not in _message(warm)


def test_no_call_carrying_files_is_refused_for_being_loaded(tree):
    """Requirement: with the preferred fix the `files` case has no refusal
    left, on any of the three verbs. `revl_change` and `revl_edit` succeed;
    `revl_load` refuses for having nothing to boot, which is a different
    refusal and says so."""
    _call("revl_load", {"files": _files(tree)})
    results = {
        "revl_change": _change(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree)),
        "revl_edit": _edit(tree, EDIT_IS_A_FIXED_POINT, files=_files(tree)),
        "revl_load": _call("revl_load", {"files": _files(tree)}),
    }
    assert results["revl_change"]["ok"] is True, results["revl_change"]
    assert results["revl_edit"]["ok"] is True, results["revl_edit"]
    for verb, result in results.items():
        for diagnostic in result.get("diagnostics") or []:
            assert "already loaded:" not in diagnostic["message"], (verb, result)


# --------------------- acceptance 4: the refusal that remains, and its fix

def test_the_surviving_refusal_carries_the_mechanical_fix(tree):
    """Acceptance 4. One refusal in this area remains — a carried `source`,
    which is a whole composition's text and has nothing else it can mean — and
    the remedy is fully mechanical and known to the server, so it is offered:
    drop the argument. A diagnostic with a null `fix` here is the #2029 shape
    at the one point of use where the answer was never in doubt."""
    _call("revl_load", {"files": _files(tree)})
    refused = _call("revl_change", {"source": MEM_TEXT, "commit": True,
                                    "edit": {"target": str(tree["mem"]),
                                             "edits": [EDIT]}})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["fix"]
    assert "source" in refused["diagnostics"][0]["fix"]


def test_the_refusal_names_the_tool_the_caller_used(tree):
    """Defect 1: the message is returned to a `revl_change` caller and used to
    say `revl_edit patches it`, naming a tool the caller had not called. It
    names the verb that was called now, because that verb is what reached the
    refusal."""
    _call("revl_load", {"files": _files(tree)})
    change = _call("revl_change", {"source": MEM_TEXT, "commit": True,
                                   "edit": {"target": str(tree["mem"]),
                                            "edits": [EDIT]}})
    assert "revl_change patches it" in _message(change)
    assert "revl_edit patches it" not in _message(change)
    edit = _edit(tree, EDIT, source=MEM_TEXT)
    assert "revl_edit patches it" in _message(edit)


def test_the_refusal_does_not_recommend_unload_while_the_source_is_unexported(
        tree):
    """Defect 2: the remedy the refusal named was `revl_unload first`, the one
    path that destroys the held edit (#2032) and hands back a snapshot that
    cannot be restored (#2033). While a divergence is reportable the order is
    `revl_export` first, because that is what makes the unload free."""
    _call("revl_load", {"files": _files(tree)})
    assert _change(tree, EDIT)["disk"]["inSync"] is False   # held, not on disk
    refused = _call("revl_change", {"source": MEM_TEXT, "commit": True,
                                    "edit": {"target": str(tree["mem"]),
                                             "edits": [EDIT]}})
    message = _message(refused)
    assert "revl_unload first" not in message
    assert message.endswith("(or revl_export first to write the held source, "
                            "then revl_unload to load another)")
    assert refused["disk"]["stale"] == [str(tree["mem"])]


def test_the_refusal_names_unload_once_the_held_source_reached_disk(tree):
    """The other side of the same condition: with nothing left to export the
    unload is free, and is named as it always was. A condition nobody can see
    turn on is a condition nobody can check."""
    _call("revl_load", {"files": _files(tree)})
    assert _change(tree, EDIT)["committed"] is True
    assert _call("revl_export", {})["disk"] == {"inSync": True, "stale": []}
    refused = _call("revl_change", {"source": MEM_TEXT, "commit": True,
                                    "edit": {"target": str(tree["mem"]),
                                             "edits": [EDIT]}})
    assert _message(refused).endswith("(or revl_unload first to load another)")
