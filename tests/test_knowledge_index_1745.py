"""A derived index over existing comments, re-checked at load, riding on
responses (issue #1745; knowledge slice 2 of the design study).

The study's exits:

* the `expected error:` blocks the compiler reproduces are classified derived
  and served as a diagnostic reference, and the ones that drifted are reported
  `refuted`, with the compile as the evidence (158 and 5 on the tree the study
  measured, and on this one);
* editing `NotesHttp` stales exactly its entries, and the response names them;
* a comment-only edit (a `revl fmt` reformat) stales nothing.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import knowledge  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.diagnostics import GUARANTEES, report  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.formatter import format_source  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

NOTES = ROOT / "examples" / "app" / "notes.rvl"
CODES = set(GUARANTEES)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading a composition needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _compile_report(path: Path):
    try:
        compile_files([str(path)])
        return None
    except RevlError as error:
        return report(error)
    except Exception as error:  # noqa: BLE001 — the study's verifier does the same
        return {"message": f"{type(error).__name__}: {error}"}


# ------------------------------------------------ the study's verifier, as the oracle


def _study_blocks(text: str) -> list[list[str]]:
    """`verify_expected.py`'s block reader, verbatim in behaviour."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        if lines[i].strip() == "// expected error:":
            exp, j = [], i + 1
            while j < len(lines) and lines[j].startswith("//") and lines[j].strip() != "//":
                exp.append(lines[j][2:].strip())
                j += 1
            if exp:
                out.append(exp)
            i = j
        else:
            i += 1
    return out


def _study_reproduces(expected: list[str], rep) -> bool:
    if rep is None:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    haystack = norm(json.dumps(rep, ensure_ascii=False)).replace('\\"', '"')
    return all(norm(re.sub(r"^\S+\.rvl:\d+:\s*", "", line)).replace('\\"', '"')
               in haystack for line in expected if norm(line))


def _expected_error_files() -> list[Path]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.rvl"],
                            capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / rel for rel in listed if not rel.startswith("bench/results")
            and "// expected error:" in (ROOT / rel).read_text(encoding="utf-8",
                                                               errors="replace")]


def test_expected_error_blocks_are_derived_and_drift_is_refuted():
    live = refuted = 0
    drifted = []
    for path in _expected_error_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rep = _compile_report(path)
        oracle = [_study_reproduces(block, rep) for block in _study_blocks(text)]
        entries = [e for e in knowledge.index_text(str(path), text, rep, CODES)
                   if e["kind"] == "expected-error"]
        assert [e["status"] == "live" for e in entries] == oracle, path
        for entry in entries:
            assert entry["tier"] == "derived"
            assert entry["ref"] == {"kind": "diagnostic"}
            assert "body" not in entry     # a reference, not a copy
        live += oracle.count(True)
        refuted += oracle.count(False)
        if False in oracle:
            drifted.append(path.relative_to(ROOT).as_posix())
    print(f"expected-error blocks: {live} reproduced (live), {refuted} refuted")
    assert live + refuted >= 150, "the corpus went missing; this test measures nothing"
    # the study's five, on the tree it measured; they still drift here
    assert set(drifted) >= {"examples/rejections/host_method_not_on_surface.rvl",
                            "examples/rejections/g4_spawn_widens_budget.rvl"}


def test_check_reports_a_refuted_comment_before_anything_loads():
    path = ROOT / "examples" / "rejections" / "host_method_not_on_surface.rvl"
    checked = _call("revl_check", {"files": [str(path)]})
    assert checked["ok"] is False
    refuted = checked["knowledge"]["refutedEntries"]
    assert refuted and refuted[0]["kind"] == "expected-error"
    assert refuted[0]["anchor"]["path"] == str(path)


def test_classification_rules():
    blocks = knowledge.comment_blocks(
        "// REFUSED: G4, the swap would widen it\nservice A { fn f() -> Int }\n\n"
        "// see docs/holes.md for the obligation\nservice B { fn f() -> Int }\n\n"
        "// why this is a separate service: it is swapped on its own\n"
        "service C { fn f() -> Int }\n")
    kinds = [knowledge.classify(b, CODES) for b in blocks]
    assert [k["tier"] for k in kinds] == ["derived", "served", "doc"]
    assert kinds[1]["ref"] == {"kind": "served", "docs": ["docs/holes.md"]}
    assert "body" in kinds[2] and "body" not in kinds[1]


def test_fingerprints_ignore_comments_and_layout():
    text = NOTES.read_text(encoding="utf-8")
    entries = {e["id"]: e for e in knowledge.index_text("n.rvl", text, None, CODES)}
    reformatted = format_source(text, "n.rvl")
    again = {e["id"]: e for e in knowledge.index_text("n.rvl", reformatted, None, CODES)}
    assert set(entries) == set(again)
    assert knowledge.counts(knowledge.merge(list(entries.values()),
                                            list(again.values())))["stale"] == 0


# ------------------------------------------------ staleness on NotesHttp


def _index(text: str) -> list[dict]:
    return knowledge.index_text(str(NOTES), text, None, CODES)


def _notes_vs(text: str) -> dict:
    return {"source": None, "files": [str(NOTES)],
            "files_content": {str(NOTES): text}, "modules": {}}


def test_editing_notes_http_stales_exactly_its_entries():
    """The study's exit, on the file itself: replace one method of NotesHttp
    and merge the index. What goes stale is anchored to NotesHttp or to that
    method, and nothing else is."""
    from revl.mcp import symbols

    text = NOTES.read_text(encoding="utf-8")
    before = _index(text)
    _buffer, edited, _echo = symbols.replace(
        _notes_vs(text), "NotesHttp.create_note",
        "fn create_note(note) {\n"
        "  let row = { id: \"fixed\", title: note.title, body: note.body }\n"
        "  emit store.create(row)\n"
        "  return Ok(row)\n"
        "}")
    merged = knowledge.merge(before, _index(edited))
    stale = {e["anchor"]["symbol"] for e in merged if e["status"] == "stale"}
    assert stale == {"NotesHttp", "NotesHttp.notes_api.create_note"}
    untouched = [e for e in merged if e["anchor"]["symbol"] == "NotesHttp.notes_api.get_note"]
    assert untouched and all(e["status"] == "live" for e in untouched)


def test_a_reformat_of_notes_stales_nothing():
    text = NOTES.read_text(encoding="utf-8")
    reformatted = format_source(text, str(NOTES))
    assert reformatted != text
    assert knowledge.counts(knowledge.merge(_index(text), _index(reformatted)))["stale"] == 0


def test_a_comment_only_edit_of_notes_stales_nothing():
    text = NOTES.read_text(encoding="utf-8")
    edited = text.replace("// The HTTP component wires the endpoints to the store.",
                          "// The HTTP component connects the endpoints to the store.")
    assert edited != text
    assert knowledge.counts(knowledge.merge(_index(text), _index(edited)))["stale"] == 0


# ------------------------------------------------ riding on a live session

SOURCE = ("service Clock { fn now() -> Int\n"
          "                fn zone() -> Str }\n"
          "\n"
          "// A fixed clock for tests: the time never moves, so a test that\n"
          "// reads it twice sees the same value.\n"
          "component FixedClock provides clock: Clock {\n"
          "  provide clock {\n"
          "    // the instant every test agrees on\n"
          "    fn now() = 7\n"
          "\n"
          "    // UTC, so no test depends on the machine's zone (see docs/holes.md)\n"
          "    fn zone() = \"UTC\"\n"
          "  }\n"
          "}\n")


@pytest.fixture
def clock_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "clock.rvl"
    path.write_text(SOURCE, encoding="utf-8")
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        loaded = _call("revl_load", {"files": [str(path)]})
        assert loaded["ok"] is True, loaded
        yield loaded
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.SESSION.proposals = {}
        server_mod.AUTHORING = old


def _anchors(entries) -> set:
    return {e["anchor"]["symbol"] for e in entries}


@needs_runtime
def test_the_edit_response_names_the_entries_it_staled(clock_loaded):
    assert clock_loaded["knowledge"] == {"entries": 3, "live": 3, "stale": 0,
                                         "refuted": 0}
    edited = _call("revl_edit", {"edits": [{"symbol": "FixedClock.now",
                                            "replacement": "fn now() = 8"}]})
    assert edited["swapped"] is True, edited
    riding = edited["knowledge"]
    assert riding["touched"] == ["FixedClock.clock.now"]
    assert _anchors(riding["stale"]) == {"FixedClock", "FixedClock.clock.now"}
    # the sibling's served entry is still live, and not about this edit
    zone = _call("revl_source", {"symbol": "FixedClock.zone",
                                 "with": ["knowledge"]})["knowledge"]
    assert [e["tier"] for e in zone["live"]] == ["served"]
    assert zone["live"][0]["ref"] == {"kind": "served", "docs": ["docs/holes.md"]}
    assert _call("revl_state", {})["knowledge"]["stale"] == 2


@needs_runtime
def test_a_live_comment_only_edit_stales_nothing(clock_loaded):
    edited = _call("revl_edit", {"edits": [{
        "anchor": "// the instant every test agrees on",
        "replacement": "// the one instant every test agrees on"}]})
    assert edited["swapped"] is True, edited
    assert _call("revl_state", {})["knowledge"]["stale"] == 0


@needs_runtime
def test_an_edited_file_keeps_its_operators_assets():
    """Found while building this: editing any file that declares an `asset`
    failed, because the edited buffer is in-memory and an in-memory module
    resolves assets only through the sources map. notes.rvl declares two."""
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(ROOT),))
    try:
        assert _call("revl_load", {"files": [str(NOTES)]})["ok"] is True
        proposed = _call("revl_change", {"edit": {"edits": [{
            "anchor": "// The HTTP component wires the endpoints to the store.",
            "replacement": "// The HTTP component connects the endpoints."}]}})
        assert proposed["ok"] is True and proposed["verified"]["admission"] == "passed", \
            proposed
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.proposals = {}
        server_mod.AUTHORING = old
