"""The change loop says whether the change reached disk (issue #2032).

`revl_change {commit: true}` edits, admits and swaps the running composition
and answers with a full success payload — but the file on disk is unchanged.
The held source is the truth and disk an export (#1696), so that is the
designed behaviour; what made it a trap is that every field of the success
payload is session-scoped. `sessionState.dirty` is defined as "a *speculative
draft* differs from what is running" (`ambient.py`), so it is correctly
`false` here — and it is the field an agent reads as "nothing pending". An
agent that follows the documented loop, verifies against the same session
that accepted the change, and reports done leaves no artifact anywhere, and
nothing in the answer says so.

The divergence was already computed: `_export_plan` builds its target list
with `t is not None and t != _edit._read_disk(p)`. These tests hold the server
to *reporting* it, on the verbs of the change loop, in the shape the issue
asks for — `"disk": {"inSync": bool, "stale": [path]}` — and to naming
`revl_export`, the one verb that writes the held source out, where the agent
acts on it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import edit as edit_mod  # noqa: E402
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


def _change(tree, **extra) -> dict:
    return _call("revl_change", {"commit": True,
                                 "edit": {"target": str(tree["mem"]),
                                          "edits": [EDIT]}, **extra})


def _held(tree) -> str:
    """The text the session holds for the edited file."""
    return server_mod.SESSION.origin[ORIGIN_FILES_CONTENT][str(tree["mem"])]


# ------------------------------------------------- the divergence, reported

def test_a_committed_change_reports_the_file_on_disk_as_stale(tree):
    """Acceptance 1: the edit commits, and the answer says disk does not have
    it. This is the whole of #2032: the success payload used to be silent."""
    _load(tree)
    result = _change(tree)
    assert result["ok"] is True and result["committed"] is True, result
    # the trap, unchanged and now reported: the session has the edit
    assert '== "b"' in _call("revl_source", {"symbol": "MemoryStore"})["text"]
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT
    # ... and the answer says the file is not the held source
    assert result["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}


def test_the_report_is_exactly_what_an_export_would_write(tree):
    """The report is the predicate `_export_plan` already filters on, so it
    cannot drift from the verb that reconciles it."""
    _load(tree)
    result = _change(tree)
    plan = server_mod._export_plan(edit_mod.virtual_source(server_mod.SESSION), {})
    assert result["disk"]["stale"] == [path for path, _text in plan]
    assert result["disk"]["stale"] == [str(tree["mem"])]


def test_only_the_edited_file_is_stale(tree):
    """A second loaded file the change did not touch is not swept in."""
    _load(tree)
    result = _change(tree)
    assert result["disk"]["stale"] == [str(tree["mem"])]
    assert str(tree["app"]) not in result["disk"]["stale"]
    assert tree["app"].read_text(encoding="utf-8") == APP_TEXT


def test_a_refusal_still_says_where_the_disk_stands(tree):
    """The block rides on a refusal too: the answer to a refused change is
    where the caller most needs to know what is and is not durable."""
    _load(tree)
    assert _change(tree)["committed"] is True
    refused = _call("revl_change", {})            # no intent: refused
    assert refused["ok"] is False
    assert refused["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}


# ---------------------------------------------------- export reconciles it

def test_export_reconciles_and_the_answer_goes_in_sync(tree):
    """Acceptance 2: `revl_export` writes it, and the next answer says so —
    the export's own answer, so the loop closes in one call."""
    _load(tree)
    assert _change(tree)["disk"]["inSync"] is False
    exported = _call("revl_export", {})
    assert exported["ok"] is True, exported
    assert exported["written"] == [str(tree["mem"])]
    assert exported["disk"] == {"inSync": True, "stale": []}
    assert tree["mem"].read_text(encoding="utf-8") == _held(tree)
    assert '== "b"' in tree["mem"].read_text(encoding="utf-8")
    after = _call("revl_source", {"symbol": "MemoryStore"})
    assert after["disk"] == {"inSync": True, "stale": []}


def test_an_export_that_has_nothing_to_write_says_in_sync(tree):
    _load(tree)
    assert _call("revl_export", {})["written"] == []
    assert _call("revl_export", {})["disk"] == {"inSync": True, "stale": []}


# ------------------------------------------------ no false positive, no drift

def test_a_fresh_load_is_in_sync(tree):
    """Acceptance 3: loading is not a divergence, so nothing is reported."""
    _load(tree)
    read = _call("revl_source", {"symbol": "MemoryStore"})
    assert read["ok"] is True
    assert read["disk"] == {"inSync": True, "stale": []}
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT


def test_an_inline_composition_has_nothing_on_disk_to_be_stale():
    """No path is named, so there is no file to disagree with: inSync, not a
    permanent warning about a composition that was never a file."""
    assert _call("revl_load", {"source": INLINE})["ok"] is True
    edited = _call("revl_edit", {"edits": [{"anchor": "fn f() = 1",
                                            "replacement": "fn f() = 2"}]})
    assert edited["ok"] is True, edited
    assert edited["disk"] == {"inSync": True, "stale": []}


def test_the_disk_block_is_not_a_session_state_key(tree):
    """`sessionState` keeps its pinned key set (#1693): the disk answer is a
    sibling of the footer, not a new footer key."""
    _load(tree)
    result = _change(tree)
    assert result["disk"]["inSync"] is False
    assert "disk" not in result["sessionState"]
    assert set(result["sessionState"]) == {"loaded", "generation", "components",
                                           "dirty", "draft"}


# ------------------------------------- naming the verb that writes it out

def test_the_change_answer_names_the_verb_that_writes_it(tree):
    """Acceptance 4: the agent is told `revl_export` by name, on the success
    it is looking at, at the moment there is something to export — matching
    the failure path, which already explains itself with `note`."""
    _load(tree)
    result = _change(tree)
    assert "revl_export" in result["note"]
    assert "disk" in result["note"]


def test_the_export_plan_and_the_report_are_one_predicate(tree, monkeypatch):
    """The named seam #2036's unload preflight and #2037's undo will call:
    `_export_plan` filters its targets on the same `_diverges` that `disk.stale`
    is computed from, so the three fixes cannot drift apart on the next edit to
    this file. Pinned by watching both go through the one function, rather than
    by re-deriving the comparison here — a test that re-derived it would drift
    in exactly the way the seam exists to prevent."""
    _load(tree)
    result = _change(tree)
    seen: list = []
    real = server_mod._diverges

    def spy(text, path):
        seen.append(path)
        return real(text, path)

    monkeypatch.setattr(server_mod, "_diverges", spy)
    plan = server_mod._export_plan(server_mod._edit.running_source(
        server_mod.SESSION), {})
    assert [p for p, _ in plan] == result["disk"]["stale"]
    assert str(tree["mem"]) in seen


def test_diverged_paths_is_the_shared_predicate(tree):
    """`diverged_paths` is callable with a held-text mapping and a path list —
    the shape #2036's unload preflight needs before it destroys the session's
    only copy, and #2037's undo needs in the other direction."""
    _load(tree)
    _change(tree)
    held = server_mod._edit.running_source(server_mod.SESSION)
    content = held[ORIGIN_FILES_CONTENT]
    assert server_mod.diverged_paths(content, held["files"]) == [str(tree["mem"])]
    # nothing held for a path is not a divergence to report
    assert server_mod.diverged_paths({}, held["files"]) == []


def test_the_core_tier_names_the_verb_that_writes_it_out():
    """Acceptance 4's other half: `revl_change` and `revl_edit` are in the
    core tier, so a `tools/list` client is told about `revl_export` without
    having to suspect there is something to look for."""
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    described = {t["name"]: t["description"] for t in listed["result"]["tools"]}
    assert "revl_export" in described["revl_change"]
    assert "revl_export" in described["revl_edit"]
    assert "revl_export" in described["revl_source"]
