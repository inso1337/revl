"""The three revert surfaces say whether the revert reached disk (issue #2037).

`revl_undo`, `revl_rollback` and `revl_step_back` all answer `ok: true` while
the file on disk still holds the change they just retracted. The held source is
the truth and disk an export (#1696), so the revert is correct *in the session*;
what makes it a trap is that a cold `revl_load {files}` reads **disk**, so the
most ordinary way to verify a revert — reload — resurrects the change. Nothing
in the success payload used to mention the file.

This is the mirror of #2032: there disk lagged the session (an edit never
exported); here disk **leads** it (a change the session has retracted). Both are
one missing field on a success payload, and the predicate is the one the merged
`_disk_state` already computes. These tests hold all three verbs to reporting
the divergence in that shape — `"disk": {"inSync": bool, "stale": [path]}` —
with a `note` naming `revl_export`, the one verb that writes the reverted source
out.

The clean direction is pinned as hard as the divergent one: a revert of a change
that was never exported leaves disk already holding exactly the reverted text,
so it must report `inSync: true` — the fix must not flag a revert that lost
nothing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import disclosure as _disclosure  # noqa: E402
from revl.mcp import edit as edit_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.persist import ORIGIN_FILES_CONTENT  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the revert verbs boot a composition and need the cordis-py runtime "
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

#: The three ways to revert, all of which must report the divergence.
REVERTS = ["revl_undo", "revl_rollback", "revl_step_back"]


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    server_mod.UNDO_STACK.clear()
    yield tmp_path
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.UNDO_STACK.clear()
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


def _change(tree) -> dict:
    return _call("revl_change", {"commit": True,
                                 "edit": {"target": str(tree["mem"]),
                                          "edits": [EDIT]}})


def _held(tree) -> str:
    """The text the session holds for the edited file."""
    return server_mod.SESSION.origin[ORIGIN_FILES_CONTENT][str(tree["mem"])]


def _exported_then_reverted(tree, verb) -> dict:
    """The issue's repro: change -> export -> revert.

    The session reverts; disk still carries the change. The revert answer is
    the only place an agent can learn that, so it must say so."""
    _load(tree)
    changed = _change(tree)
    assert changed["ok"] is True and changed["committed"] is True, changed
    exported = _call("revl_export", {})
    assert exported["written"] == [str(tree["mem"])], exported
    assert '== "b"' in tree["mem"].read_text(encoding="utf-8")
    return _call(verb, {})


def _reverted_without_export(tree, verb) -> dict:
    """change -> revert with **no** export in between.

    Disk never had the change, so after the revert disk already equals the
    session: there is nothing stale and the answer must not invent one."""
    _load(tree)
    changed = _change(tree)
    assert changed["ok"] is True and changed["committed"] is True, changed
    assert '== "b"' not in tree["mem"].read_text(encoding="utf-8")
    return _call(verb, {})


# ------------------------------------------- acceptance 1 & 2: report it

def test_acceptance_1_undo_names_the_retracted_change_left_on_disk(tree):
    """`revl_change` -> `revl_export` -> `revl_undo`: the response names the
    stale path and sets `inSync: false`. This is the whole of #2037 — the undo
    used to answer `ok: true` with nothing about the file."""
    result = _exported_then_reverted(tree, "revl_undo")
    assert result["ok"] is True and result["undone"] is True, result
    # the trap, unchanged and now reported: the session reverted, disk did not
    assert '== "b"' in tree["mem"].read_text(encoding="utf-8")
    assert result["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}


@pytest.mark.parametrize("verb", REVERTS)
def test_acceptance_2_every_revert_reports_the_divergence(tree, verb):
    """Acceptance 2: `revl_rollback` and `revl_step_back` are the same defect
    as `revl_undo` — all three report success while disk still holds the
    change, so all three must carry the block."""
    result = _exported_then_reverted(tree, verb)
    assert result["ok"] is True, result
    assert result["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}
    # the change really is still on disk, not merely reported as such
    assert '== "b"' in tree["mem"].read_text(encoding="utf-8")


# ------------------------------------------ acceptance 3: the clean case

@pytest.mark.parametrize("verb", REVERTS)
def test_acceptance_3_a_revert_without_an_export_is_in_sync(tree, verb):
    """Acceptance 3: `revl_change` -> revert with no export in between. Disk
    never had the change, so the revert must report `inSync: true` and leave no
    `note` — the clean case must not be flagged."""
    result = _reverted_without_export(tree, verb)
    assert result["ok"] is True, result
    assert result["disk"] == {"inSync": True, "stale": []}
    assert not result.get("note")
    assert '== "b"' not in tree["mem"].read_text(encoding="utf-8")


# ------------------------------------- acceptance 4: the round trip holds

def test_acceptance_4_the_export_after_an_undo_writes_the_reverted_text(tree):
    """Acceptance 4: `change` -> `export` -> `undo` -> `export` leaves disk
    holding the reverted text. Pinned so a fix to the reporting cannot break
    the export that makes the revert durable."""
    result = _exported_then_reverted(tree, "revl_undo")
    assert result["disk"]["inSync"] is False
    exported = _call("revl_export", {})
    assert exported["ok"] is True and exported["written"] == [str(tree["mem"])]
    assert exported["disk"] == {"inSync": True, "stale": []}
    assert tree["mem"].read_text(encoding="utf-8") == _held(tree)
    assert '== "b"' not in tree["mem"].read_text(encoding="utf-8")


# ------------------------------- naming the verb that writes the revert out

@pytest.mark.parametrize("verb", REVERTS)
def test_the_revert_answer_names_the_verb_that_writes_it(tree, verb):
    """The agent is told `revl_export` by name, on the success it is looking
    at, at the moment its revert is not durable."""
    result = _exported_then_reverted(tree, verb)
    assert "revl_export" in result["note"]
    assert "reverted" in result["note"]


@pytest.mark.parametrize("verb", REVERTS)
def test_the_clean_revert_carries_no_note(tree, verb):
    """A revert with nothing to export says nothing about exporting — and it
    still says the state is in sync, so the `note` is absent because there is
    nothing to report, not because the block is missing."""
    result = _reverted_without_export(tree, verb)
    assert result["disk"] == {"inSync": True, "stale": []}
    assert not result.get("note")


# ------------------------------------------- the report cannot drift

@pytest.mark.parametrize("verb", REVERTS)
def test_the_report_is_exactly_what_an_export_would_write(tree, verb):
    """The block is the predicate `_export_plan` already filters on, so it
    cannot drift from the verb that reconciles it: the stale set is the export
    plan's target list."""
    result = _exported_then_reverted(tree, verb)
    plan = server_mod._export_plan(edit_mod.running_source(server_mod.SESSION), {})
    assert result["disk"]["stale"] == [path for path, _text in plan]
    assert result["disk"]["stale"] == [str(tree["mem"])]


def test_the_disk_block_is_a_sibling_not_a_session_state_key(tree):
    """`sessionState` keeps its pinned key set (#1693): the disk answer rides
    beside the footer, never inside it."""
    result = _exported_then_reverted(tree, "revl_undo")
    assert result["disk"]["inSync"] is False
    assert "disk" not in result["sessionState"]
    assert set(result["sessionState"]) == {"loaded", "generation", "components",
                                           "dirty", "draft"}


def test_step_back_reports_it_on_the_inner_undo_result_too(tree):
    """`revl_step_back` runs a real undo through the same gate; the inner
    result it hands back carries the same block, so the two cannot disagree."""
    result = _exported_then_reverted(tree, "revl_step_back")
    assert result["result"]["disk"] == result["disk"]
    assert result["via"]["tool"] == "revl_undo"


@pytest.mark.parametrize("verb", REVERTS)
def test_the_revert_verb_names_the_verb_that_writes_it_out(verb, monkeypatch):
    """The other half of discoverability, matching the change loop's (#2032):
    a client that asks for the full list is told about `revl_export` on each
    revert verb without having to suspect there is something to look for. The
    three revert verbs are not core-tier, so this reads the `--all-tools`
    listing rather than the default one."""
    monkeypatch.setattr(_disclosure, "_ALL_TOOLS", True)
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    described = {t["name"]: t["description"] for t in listed["result"]["tools"]}
    assert "revl_export" in described[verb]
