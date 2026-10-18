"""A session loaded from an export copy reads the sidecar of the file the copy
copies, and writes only its own (issue #2575).

`revl_export {with_knowledge: true}` writes a loaded file's text, with each
note rendered as a marked block, to its copy under `<root>/.revl/export/`, and
each note's record to `.revl/knowledge/` beside the loaded file. The sidecar
of a session used to be found beside its FIRST LOADED FILE only, so a session
loaded from the copy read `<root>/.revl/export/.revl/knowledge`, which holds
none of the original's records: every block in the copy imported again as a
new note with a fresh id, and the next export rendered each note a second
time. Every round trip grew the module by one copy of each note.

The copy's session now reads the original's sidecar too, but its records
stay inside the export area: they are the author's, and reach the operator's
sidecar only by adoption. And no author confirms, retires or supersedes a
note whose author is trusted further.

Through the real dispatch, for a file at the root and in a subdirectory (and
a case alias of the export directory where the filesystem folds case):

* a load from the copy finds the existing record: the same note id, and no
  new record;
* two round trips from the copy (load it, export with knowledge) write
  nothing: the copy's bytes, its one block, the records and the session's
  note ids are all unchanged;
* a note added from the copy is recorded in the copy's own sidecar, keeps
  its id when the copy is loaded again, and is not in the operator's;
* an authoring session loaded from the copy cannot retire or supersede an
  operator's note: the operator's next load still has it live, and nothing
  new is in any sidecar outside the export area;
* an untrusted session loaded from the operator's own file cannot retire,
  supersede or confirm an operator's note either;
* a composition loaded from the export area writes no record outside it
  (`export_jail.check_write_back`);
* control: a load of the operator's own file finds the same sidecar, before
  and after the fix.

Every test needs the cordis-py runtime and skips without it: with it
installed this module collects 23 items and all 23 pass on a case-folding
filesystem (19 pass and the 4 case aliases skip on a case-sensitive one). On
the base without the fix, 17 fail and 6 pass (the 2 controls and the 4 copy
sessions that could not see the operator's note). A run that reports 23
skipped (or 0 passed) has proved nothing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import export_jail  # noqa: E402
from revl.mcp import notes as notes_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="notes anchor to a loaded composition, which needs the cordis-py "
           "runtime; install it with `sh backends/python/setup.sh`")

PLAIN = ("service Kv {\n  fn get(k: Str) -> Opt[Str]\n}\n"
         "component Vault provides kv: Kv {\n"
         "  provide kv { fn get(k) = None }\n}\n")
BODY = "get answers None for every key, so callers must not rely on a hit"
SECOND = "a miss is never cached, so a hot missing key costs a lookup each time"
REPLACED = "every key is present, so a caller may treat a miss as a bug"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _reset():
    session = server_mod.SESSION
    if session.loaded:
        session.unload()
    notes_mod.clear(session)
    session.draft = None
    session.proposals = {}


@pytest.fixture(params=["top", "sub"])
def vault(request, tmp_path, monkeypatch):
    """`vault.rvl` at a sanctioned root, or in a subdirectory of it, under the
    default closed trust."""
    root = tmp_path / "root"
    path = root / "vault.rvl" if request.param == "top" else root / "sub" / "vault.rvl"
    path.parent.mkdir(parents=True)
    path.write_text(PLAIN, encoding="utf-8")
    monkeypatch.chdir(root)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(root),))
    _reset()
    try:
        yield path
    finally:
        _reset()
        server_mod.AUTHORING = old


def _root() -> Path:
    return Path(server_mod._file_roots()[0])


def _copy(vault: Path, spelling: str = "canonical") -> Path:
    """The export copy of `vault`, under its export directory as spelled."""
    export = (".revl", "export") if spelling == "canonical" else (".REVL", "EXPORT")
    copy = _root().joinpath(*export, vault.resolve().relative_to(_root()))
    if not copy.exists():
        pytest.skip("a case-sensitive filesystem: the alias names no file")
    return copy


def _load(path: Path) -> dict:
    loaded = _call("revl_load", {"files": [str(path)]})
    assert loaded["ok"] is True, loaded
    return loaded


def _note_ids() -> list:
    return sorted(notes_mod.notes(server_mod.SESSION))


def _records() -> set:
    """The id of every knowledge record on disk, in any sidecar of the root."""
    return {p.stem for p in _root().rglob("*.json")
            if p.parent.name == "knowledge"}


def _operator_records() -> set:
    """The id of every record in a sidecar outside the export area."""
    export = _root() / ".revl" / "export"
    return {p.stem for p in _root().rglob("*.json")
            if p.parent.name == "knowledge" and export not in p.parents}


def _minted(vault: Path, *, trusted: bool = False) -> str:
    """One note added and exported with knowledge: its block is in the copy,
    its record in the sidecar beside `vault`. `trusted` adds it as the
    operator's. Returns its id, under the closed trust again."""
    if trusted:
        server_mod.set_authoring_trust(host_code=True)
        assert server_mod.AUTHORING.profile() is None
    _load(vault)
    added = _call("revl_knowledge", {"op": "add", "symbol": "Vault",
                                     "kind": "purpose", "body": BODY})
    assert added["ok"] is True, added
    note_id = added["note"]["id"]
    assert added["note"]["author"]["trust"] == ("operator" if trusted else "untrusted")
    exported = _call("revl_export", {"with_knowledge": True})
    assert exported["ok"] is True, exported
    assert f"// [{note_id}]" in _copy(vault).read_text(encoding="utf-8")
    assert (vault.parent / ".revl" / "knowledge" / f"{note_id}.json").is_file()
    assert _records() == {note_id}
    _reset()
    server_mod.set_authoring_trust(host_code=False)
    assert server_mod.AUTHORING.profile() is not None
    return note_id


# ------------------------------------------------ a load from the copy


@pytest.mark.parametrize("spelling", ["canonical", "case-alias"])
def test_a_load_from_the_export_copy_finds_the_existing_record(vault, spelling):
    note_id = _minted(vault)
    _load(_copy(vault, spelling))
    assert _note_ids() == [note_id]
    assert sorted(notes_mod._records(server_mod.SESSION)) == [note_id]
    assert notes_mod.notes(server_mod.SESSION)[note_id]["body"] == BODY


@pytest.mark.parametrize("spelling", ["canonical", "case-alias"])
def test_round_trips_from_the_copy_keep_the_note_count_and_ids(vault, spelling):
    note_id = _minted(vault)
    copy = _copy(vault, spelling)
    before = copy.read_bytes()
    for _cycle in range(2):
        _load(copy)
        assert _note_ids() == [note_id]
        exported = _call("revl_export", {"with_knowledge": True})
        assert exported["ok"] is True, exported
        assert exported["written"] == [], exported
        text = copy.read_text(encoding="utf-8")
        assert text.count("// note k_") == 1, text
        assert copy.read_bytes() == before
        assert _records() == {note_id}
        _reset()


def test_a_note_added_from_the_copy_stays_in_the_copys_sidecar(vault):
    first = _minted(vault)
    copy = _copy(vault)
    _load(copy)
    added = _call("revl_knowledge", {"op": "add", "symbol": "Vault",
                                     "kind": "trap", "body": SECOND})
    assert added["ok"] is True, added
    second = added["note"]["id"]
    exported = _call("revl_export", {"with_knowledge": True})
    assert exported["ok"] is True, exported
    assert (copy.parent / ".revl" / "knowledge" / f"{second}.json").is_file()
    assert _operator_records() == {first}
    rendered = copy.read_bytes()
    _reset()
    _load(copy)                               # both records found, none minted
    assert _note_ids() == sorted([first, second])
    assert _call("revl_export", {"with_knowledge": True})["written"] == []
    assert copy.read_bytes() == rendered
    _reset()
    _load(vault)                              # the author's note is not adopted
    assert _note_ids() == [first]


# ------------------------------------------------ an operator's note


@pytest.mark.parametrize("op", ["retire", "supersede"])
def test_a_copy_session_cannot_take_an_operators_note_away(vault, op):
    note_id = _minted(vault, trusted=True)
    _load(_copy(vault))
    changed = _call("revl_knowledge", {"op": op, "id": note_id,
                                       "reason": "stale", "body": REPLACED})
    assert changed["ok"] is False, changed
    assert _call("revl_export", {"with_knowledge": True})["ok"] is True
    _reset()
    assert _operator_records() == {note_id}
    _load(vault)
    current = notes_mod.notes(server_mod.SESSION)
    assert sorted(current) == [note_id]
    assert current[note_id]["status"] == "live"
    assert current[note_id]["body"] == BODY


@pytest.mark.parametrize("op", ["retire", "supersede", "confirm"])
def test_an_untrusted_session_cannot_change_an_operators_note(vault, op):
    note_id = _minted(vault, trusted=True)
    _load(vault)
    changed = _call("revl_knowledge", {"op": op, "id": note_id,
                                       "reason": "stale", "body": REPLACED})
    assert changed["ok"] is False, changed
    message = changed["diagnostics"][0]["message"]
    assert "author trust `operator`" in message and f"cannot {op}" in message, message
    assert len(notes_mod._records(server_mod.SESSION)) == 1
    assert notes_mod.notes(server_mod.SESSION)[note_id]["status"] == "live"


def test_a_copy_session_writes_no_record_outside_the_export_area(tmp_path):
    record = "k_0123456789ab.json"
    inside = tmp_path / "root" / ".revl" / "export" / ".revl" / "knowledge"
    outside = tmp_path / "root" / ".revl" / "knowledge"
    export_jail.check_write_back([], [str(inside / record)], str(inside),
                                 jailed=True)
    export_jail.check_write_back([], [str(outside / record)], str(outside),
                                 jailed=False)
    with pytest.raises(export_jail.ExportRefused, match="outside the export area"):
        export_jail.check_write_back([], [str(outside / record)], str(outside),
                                     jailed=True)


# ------------------------------------------------ control


def test_a_load_of_the_operators_file_keeps_its_sidecar(vault):
    note_id = _minted(vault)
    copy = _copy(vault)
    before = copy.read_bytes()
    _load(vault)
    assert _note_ids() == [note_id]
    exported = _call("revl_export", {"with_knowledge": True})
    assert exported["ok"] is True, exported
    assert exported["written"] == [], exported
    assert copy.read_bytes() == before
    assert _records() == {note_id}
