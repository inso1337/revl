"""A marked note block in an adopted export copy stays the author's
(issue #2577).

Since #2550 a marked block read back from a file under `.revl/export/` is an
untrusted agent's. Adoption is the operator copying such a file out of the
export directory into the project. The copy then held "bytes no MCP verb
wrote", so on the next load each block the author had changed or added
became an operator note, and a changed body of a recorded note an operator
supersede carrying the author's text, with nothing shown to the operator.

`revl_export` records the SHA-256 of every file it writes in its root's
manifest, and every digest the manifest no longer holds in the digest record
beside it. A loaded file outside the export directory whose text, as a load
reads it, is a recorded export is an adopted copy, so through the real
dispatch:

* the author rewrites a recorded note's body and exports; the copy is
  adopted byte for byte (over the project's file, or into a nested
  directory) and loaded: the new record is an untrusted agent's, names the
  export it came from (`adoptedFrom`), and rides with its body withheld;
* the same for a block with no record;
* the load result lists each adopted block (`notesAdopted`): its note, its
  symbol, its file, the export, and whether it changed a recorded body;
* the sidecar export of the adopted note keeps the label;
* a later export rewriting the copy neither ends the adoption nor, after an
  export with knowledge, records the same block again on reload;
* an export copy with `\\r\\n` line ends adopted byte for byte matches;
* a damaged digest record is ignored, never fatal, reported by the load,
  and started again;
* no export target has the manifest's or the digest record's name as any
  part below the export directory, so no export can make either name a
  directory;
* the digest record is written before the copies it covers: a record write
  that fails fails the export, naming the record, with no copy written and
  the manifest keeping the digest it would have replaced;
* the manifest is written last: a manifest write that fails fails the
  export, naming the manifest and each copy that landed;
* a directory prepared at a write's old fixed temporary name stops no
  export: each write goes through a fresh name and leaves none behind;
* an adopted block whose recorded note is the operator's never supersedes
  it: it becomes a note of its own beside it, naming the note it would have
  changed (`changes`), and a reload records nothing more;
* control: a copy the operator edited before loading (one line outside
  every block, or the block's body) is the operator's, as before, and the
  load lists nothing adopted;
* control: an unchanged block in an adopted copy keeps its record and that
  record's trust, and is not listed.

Every test needs the cordis-py runtime and skips without it: with it
installed this module collects 26 items and all 26 pass; a run that reports
26 skipped (or 0 passed) has proved nothing. Before the fix the 22 adoption
items fail and the 4 controls pass.
"""

import errno
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import notes as notes_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from test_notes_import_trust_2550 import (  # noqa: E402
    AUTHOR_TEXT, BODY, DECL, OPERATOR, PLAIN, UNRECORDED, UNTRUSTED,
    _author_export, _block, _call, _copy, _load, _reset, _riding, _with_body)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="notes anchor to a loaded composition, which needs the cordis-py "
           "runtime; install it with `sh backends/python/setup.sh`")

# the export copy every adopted file here matches, relative to the root
EXPORTED = os.path.join(".revl", "export", "vault.rvl")
ADOPTED = {**UNTRUSTED, "adoptedFrom": EXPORTED}
OPERATOR_TEXT = "the operator reviewed get and keeps it answering None"
WHERE = ["root", "nested"]
# a line of `Vault` a later export changes
GET = "provide kv { fn get(k) = None }"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """`vault.rvl` in a sanctioned root, under the default closed trust."""
    root = tmp_path / "root"
    root.mkdir()
    path = root / "vault.rvl"
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


def _recorded_then_rewritten(vault: Path) -> tuple[Path, str]:
    """A note on `Vault` recorded in the project's sidecar, whose body the
    untrusted author then rewrites in the held source before exporting with
    knowledge. Returns the export copy and the recorded note's id."""
    assert server_mod.AUTHORING.profile() is not None
    _load(vault)
    added = _call("revl_knowledge", {"op": "add", "symbol": "Vault",
                                     "kind": "purpose", "body": BODY})
    assert added["ok"] is True, added
    note_id = added["note"]["id"]
    edited = _call("revl_edit", {"edits": [{
        "anchor": DECL, "replacement": _block(note_id, AUTHOR_TEXT) + DECL,
        "count": 1}], "commit": True})
    assert edited["ok"] is True, {k: edited.get(k) for k in ("diagnostics", "note")}
    wrote = _call("revl_export", {"with_knowledge": True})
    assert wrote["ok"] is True, wrote
    copy = _copy(vault)
    text = copy.read_text(encoding="utf-8")
    assert _block(note_id, AUTHOR_TEXT) in text and BODY not in text
    assert (vault.parent / ".revl" / "knowledge" / f"{note_id}.json").is_file()
    _reset()
    return copy, note_id


def _adopt(copy: Path, vault: Path, where: str) -> Path:
    """The operator copies the export `copy` byte for byte into the project:
    over `vault.rvl` at the root, or into a nested directory, taking the
    project's sidecar records along. Returns the adopted file."""
    root = vault.parent
    if where == "root":
        target = vault
    else:
        target = root / "pkg" / vault.name
        target.parent.mkdir()
        knowledge = root / ".revl" / "knowledge"
        if knowledge.is_dir():
            shutil.copytree(knowledge, target.parent / ".revl" / "knowledge")
    shutil.copyfile(copy, target)
    assert target.read_bytes() == copy.read_bytes()
    return target


def _same_file(a, b) -> bool:
    return Path(a).resolve() == Path(b).resolve()


# ------------------------------------------------ an adopted copy's blocks


@pytest.mark.parametrize("where", WHERE)
def test_an_adopted_copys_changed_body_is_an_untrusted_supersede(vault, where):
    copy, note_id = _recorded_then_rewritten(vault)
    _load(_adopt(copy, vault, where))
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == ADOPTED
    assert found[0]["supersedes"] == note_id
    riding = [n for n in _riding("Vault") if n["id"] == found[0]["id"]]
    assert len(riding) == 1, riding
    assert riding[0]["author"] == ADOPTED
    assert "body" not in riding[0] and "bodyWithheld" in riding[0]


@pytest.mark.parametrize("where", WHERE)
def test_an_adopted_copys_unrecorded_block_is_untrusted(vault, where):
    copy = _author_export(vault)
    _load(_adopt(copy, vault, where))
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == ADOPTED
    assert found[0].get("supersedes") is None
    riding = [n for n in _riding("Vault") if n["id"] == found[0]["id"]]
    assert len(riding) == 1, riding
    assert "body" not in riding[0] and "bodyWithheld" in riding[0]


# ------------------------------------------------ what the load reports


@pytest.mark.parametrize("where", WHERE)
def test_the_load_result_lists_each_adopted_block(vault, where):
    copy, note_id = _recorded_then_rewritten(vault)
    adopted = _adopt(copy, vault, where)
    loaded = _load(adopted)
    new_id = _with_body(AUTHOR_TEXT)[0]["id"]
    listed = loaded.get("notesAdopted")
    assert isinstance(listed, list) and len(listed) == 1, loaded
    entry = listed[0]
    assert _same_file(entry.pop("file"), adopted), entry
    assert entry == {"id": new_id, "symbol": "Vault", "adoptedFrom": EXPORTED,
                     "block": "changed", "supersedes": note_id}
    assert AUTHOR_TEXT not in json.dumps(listed)


def test_the_load_result_lists_an_adopted_unrecorded_block(vault):
    copy = _author_export(vault)
    adopted = _adopt(copy, vault, "root")
    loaded = _load(adopted)
    listed = loaded.get("notesAdopted")
    assert isinstance(listed, list) and len(listed) == 1, loaded
    entry = listed[0]
    assert _same_file(entry.pop("file"), adopted), entry
    assert entry == {"id": _with_body(AUTHOR_TEXT)[0]["id"], "symbol": "Vault",
                     "adoptedFrom": EXPORTED, "block": "unrecorded"}
    assert UNRECORDED not in [n["id"] for n in listed]


def test_the_sidecar_export_of_an_adopted_note_keeps_the_label(vault):
    copy = _author_export(vault)
    _load(_adopt(copy, vault, "root"))
    note_id = _with_body(AUTHOR_TEXT)[0]["id"]
    exported = _call("revl_export", {"with_knowledge": True})
    assert exported["ok"] is True, exported
    records = [p for p in vault.parent.rglob(f"{note_id}.json")
               if p.parent.name == "knowledge"]
    assert len(records) == 1, records
    written = json.loads(records[0].read_text(encoding="utf-8"))
    assert written["body"] == AUTHOR_TEXT
    assert written["author"] == ADOPTED


# ------------------------------------------------ controls


@pytest.mark.parametrize("edit", ["outside-every-block", "in-the-block"])
def test_a_copy_the_operator_edited_stays_operator(vault, edit):
    """The documented limit: once the operator changes the copy, its bytes
    are no export's, and its blocks are the operator's, as before."""
    copy, note_id = _recorded_then_rewritten(vault)
    adopted = _adopt(copy, vault, "root")
    text = adopted.read_text(encoding="utf-8")
    if edit == "outside-every-block":
        text, body = text + "// reviewed by the operator\n", AUTHOR_TEXT
    else:
        text, body = text.replace(AUTHOR_TEXT, OPERATOR_TEXT), OPERATOR_TEXT
    adopted.write_text(text, encoding="utf-8")
    loaded = _load(adopted)
    assert "notesAdopted" not in loaded, loaded["notesAdopted"]
    found = _with_body(body)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == OPERATOR
    assert found[0]["supersedes"] == note_id
    assert [n.get("body") for n in _riding("Vault")
            if n["id"] == found[0]["id"]] == [body]


@pytest.mark.parametrize("where", WHERE)
def test_an_unchanged_block_in_an_adopted_copy_keeps_its_record(vault, where):
    _load(vault)
    added = _call("revl_knowledge", {"op": "add", "symbol": "Vault",
                                     "kind": "purpose", "body": BODY})
    assert added["ok"] is True, added
    note_id = added["note"]["id"]
    assert _call("revl_export", {"with_knowledge": True})["ok"] is True
    _reset()
    loaded = _load(_adopt(_copy(vault), vault, where))
    assert "notesAdopted" not in loaded, loaded["notesAdopted"]
    current = notes_mod.notes(server_mod.SESSION)
    assert sorted(current) == [note_id]
    assert current[note_id]["author"] == UNTRUSTED
    assert current[note_id]["status"] == "live"
    assert len(notes_mod._records(server_mod.SESSION)) == 1


# ------------------------------------------------ a later export, line ends


def _export_again(copy: Path) -> None:
    """The author changes code in the loaded adopted copy and exports it, so
    the export copy, and the digest its manifest keeps, are replaced."""
    before = copy.read_bytes()
    edited = _call("revl_edit", {"edits": [{
        "anchor": GET, "replacement": GET + "  // v2", "count": 1}],
        "commit": True})
    assert edited["ok"] is True, {k: edited.get(k) for k in ("diagnostics", "note")}
    assert _call("revl_export", {})["ok"] is True
    assert copy.read_bytes() != before


def _record_of(path: Path):
    """The digest record at `path` as JSON, or None when it is not JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def test_a_reexport_after_adoption_neither_grows_nor_promotes(vault):
    """Adopted load, export with knowledge (which rewrites the export copy),
    reload of the unchanged adopted file: the same records, none an
    operator's, nothing listed as adopted again."""
    copy, note_id = _recorded_then_rewritten(vault)
    adopted = _adopt(copy, vault, "root")
    _load(adopted)
    first = sorted(notes_mod._records(server_mod.SESSION))
    assert len(first) == 2 and note_id in first, first
    held = adopted.read_bytes()
    exported = _call("revl_export", {"with_knowledge": True})
    assert exported["ok"] is True, exported
    assert adopted.read_bytes() == held and copy.read_bytes() != held
    _reset()
    loaded = _load(adopted)
    records = notes_mod._records(server_mod.SESSION)
    assert sorted(records) == first, sorted(records)
    assert [r["id"] for r in records.values()
            if r.get("author", {}).get("trust") == "operator"] == []
    assert "notesAdopted" not in loaded, loaded["notesAdopted"]


def test_a_later_export_of_the_copy_does_not_end_the_adoption(vault):
    copy = _author_export(vault)
    adopted = _adopt(copy, vault, "root")
    _load(adopted)
    assert _with_body(AUTHOR_TEXT)[0]["author"] == ADOPTED
    _export_again(copy)
    _reset()
    loaded = _load(adopted)
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == ADOPTED
    assert [e["block"] for e in loaded.get("notesAdopted") or []] == ["unrecorded"]


def test_an_export_copy_with_crlf_line_ends_adopted_byte_for_byte_is_untrusted(vault):
    """An inline export writes its source as held, `\\r\\n` included; a load
    reads the adopted copy with `\\n` line ends, which is still the text the
    export wrote."""
    source = PLAIN.replace(DECL, _block(UNRECORDED, AUTHOR_TEXT) + DECL)
    assert _call("revl_load", {"source": source.replace("\n", "\r\n")})["ok"] is True
    copy = _copy(vault)
    wrote = _call("revl_export", {"path": str(copy)})
    assert wrote["ok"] is True, wrote
    assert b"\r\n" in copy.read_bytes()
    _reset()
    adopted = _adopt(copy, vault, "root")
    assert adopted.read_bytes() == copy.read_bytes()
    loaded = _load(adopted)
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == ADOPTED
    assert [e["block"] for e in loaded.get("notesAdopted") or []] == ["unrecorded"]


@pytest.mark.parametrize("damage", ["not-json", "wrong-shape"])
def test_a_damaged_digest_record_is_ignored_and_reported(vault, damage):
    """A record that is not JSON, or not `{"digests": {str: str}}`, records
    nothing: the load succeeds, the manifest still names the copy, the load
    result names the damaged record, and the next export starts it again."""
    copy = _author_export(vault)
    record = copy.parent / "digests.json"
    record.write_text("{not json" if damage == "not-json" else
                      json.dumps({"digests": {"x": 1}, "extra": []}),
                      encoding="utf-8")
    adopted = _adopt(copy, vault, "root")
    loaded = _load(adopted)
    assert _with_body(AUTHOR_TEXT)[0]["author"] == ADOPTED
    damaged = loaded.get("exportRecordDamaged")
    assert isinstance(damaged, list) and len(damaged) == 1, loaded
    assert _same_file(damaged[0], record), damaged
    _export_again(copy)
    rewritten = _record_of(record)
    assert isinstance(rewritten, dict) and set(rewritten) == {"digests"}, rewritten
    _reset()
    reloaded = _load(adopted)
    assert "exportRecordDamaged" not in reloaded, reloaded["exportRecordDamaged"]
    assert _with_body(AUTHOR_TEXT)[0]["author"] == ADOPTED


# ------------------------------------------------ the record is never lost


@pytest.mark.parametrize("name", ["digests.json", "Manifest.JSON"])
def test_a_record_name_is_refused_as_any_part_of_an_export_target(vault, name):
    """An export target under a directory named for the manifest or the
    digest record would make that name a directory, and every later record
    write fail: it is refused, with nothing written."""
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    export = vault.parent / ".revl" / "export"
    wrote = _call("revl_export", {"path": str(export / name / "x.rvl")})
    assert wrote["ok"] is False, wrote
    assert "digest record" in json.dumps(wrote), wrote
    assert not (export / name).exists()


@pytest.mark.parametrize("how", ["write-fails", "a-directory"])
def test_a_record_write_that_fails_fails_the_export(vault, monkeypatch, how):
    """An export that replaces a copy's digest must keep it in the record,
    which it writes before the copy. When that write fails the export says
    so and names the record, writes no copy, and the manifest keeps the
    digest it would have replaced, so the adopted copy still matches."""
    copy = _author_export(vault)
    adopted = _adopt(copy, vault, "root")
    _load(adopted)
    manifest = copy.parent / "manifest.json"
    held = manifest.read_bytes()
    before = copy.read_bytes()
    record = copy.parent / "digests.json"
    write = server_mod._write_atomic
    if how == "a-directory":
        record.mkdir()
    else:
        def failing(path, text):
            if os.path.basename(path) == "digests.json":
                raise OSError(errno.ENOSPC, "No space left on device", path)
            write(path, text)
        monkeypatch.setattr(server_mod, "_write_atomic", failing)
    edited = _call("revl_edit", {"edits": [{
        "anchor": GET, "replacement": GET + "  // v2", "count": 1}],
        "commit": True})
    assert edited["ok"] is True, {k: edited.get(k) for k in ("diagnostics", "note")}
    wrote = _call("revl_export", {})
    assert wrote["ok"] is False, wrote
    assert "digests.json" in json.dumps(wrote), wrote
    assert "nothing was written" in json.dumps(wrote), wrote
    assert copy.read_bytes() == before
    assert manifest.read_bytes() == held
    monkeypatch.setattr(server_mod, "_write_atomic", write)
    _reset()
    _load(adopted)
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == ADOPTED


def test_a_manifest_write_that_fails_names_the_copies_it_left(vault, monkeypatch):
    """The manifest is written last, after the copies. When that write fails
    the export says so, naming the manifest and each copy that landed, which
    no record keeps: loaded once adopted, its blocks would be the
    operator's."""
    copy = _author_export(vault)
    adopted = _adopt(copy, vault, "root")
    _load(adopted)
    manifest = copy.parent / "manifest.json"
    held = manifest.read_bytes()
    before = copy.read_bytes()
    write = server_mod._write_atomic

    def failing(path, text):
        if os.path.basename(path) == "manifest.json":
            raise OSError(errno.ENOSPC, "No space left on device", path)
        write(path, text)
    monkeypatch.setattr(server_mod, "_write_atomic", failing)
    edited = _call("revl_edit", {"edits": [{
        "anchor": GET, "replacement": GET + "  // v2", "count": 1}],
        "commit": True})
    assert edited["ok"] is True, {k: edited.get(k) for k in ("diagnostics", "note")}
    wrote = _call("revl_export", {})
    assert wrote["ok"] is False, wrote
    message = json.dumps(wrote)
    assert "manifest.json" in message, wrote
    assert ("written, not recorded: " in message
            and os.path.join(".revl", "export", "vault.rvl") in message), wrote
    assert copy.read_bytes() != before
    assert manifest.read_bytes() == held


@pytest.mark.parametrize("name", ["digests.json", "manifest.json", "vault.rvl"])
def test_a_directory_at_an_old_temporary_name_stops_no_export(vault, name):
    """A write once went through a fixed temporary name beside its file,
    `.<name>.revl-export`, which an inline export could make a directory,
    after which every export writing that file failed. Each write now goes
    through a fresh name: the export writes the copy and records it, and
    leaves no temporary behind."""
    copy = _author_export(vault)
    export = copy.parent
    prepared = export / f".{name}.revl-export"
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    wrote = _call("revl_export", {"path": str(prepared / "x.rvl")})
    assert wrote["ok"] is True, wrote
    assert prepared.is_dir()
    _reset()
    adopted = _adopt(copy, vault, "root")
    _load(adopted)
    _export_again(copy)
    assert isinstance(_record_of(export / "digests.json"), dict)
    assert [p.name for p in export.iterdir()
            if p.name.endswith(".revl-export") and p != prepared] == []
    _reset()
    _load(adopted)
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(notes_mod.notes(server_mod.SESSION))
    assert found[0]["author"] == ADOPTED


# ------------------------------------------------ a note trusted further


def test_an_adopted_block_never_supersedes_an_operators_note(vault):
    """The recorded note is the operator's; the author rewrites its block and
    exports, and the copy is adopted. The operator's note stays live, and the
    author's text is a note of its own beside it, naming the note it would
    have changed (`changes`), with its body withheld."""
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    _load(vault)
    added = _call("revl_knowledge", {"op": "add", "symbol": "Vault",
                                     "kind": "purpose", "body": BODY})
    assert added["ok"] is True, added
    note_id = added["note"]["id"]
    assert added["note"]["author"]["trust"] == "operator"
    assert _call("revl_export", {"with_knowledge": True})["ok"] is True
    _reset()
    server_mod.AUTHORING = old
    _load(vault)
    edited = _call("revl_edit", {"edits": [{
        "anchor": DECL, "replacement": _block(note_id, AUTHOR_TEXT) + DECL,
        "count": 1}], "commit": True})
    assert edited["ok"] is True, {k: edited.get(k) for k in ("diagnostics", "note")}
    assert _call("revl_export", {"with_knowledge": True})["ok"] is True
    copy = _copy(vault)
    assert _block(note_id, AUTHOR_TEXT) in copy.read_text(encoding="utf-8")
    _reset()
    adopted = _adopt(copy, vault, "root")
    loaded = _load(adopted)
    current = notes_mod.notes(server_mod.SESSION)
    assert current[note_id]["status"] == "live"
    assert current[note_id]["author"]["trust"] == "operator"
    found = _with_body(AUTHOR_TEXT)
    assert len(found) == 1, sorted(current)
    assert found[0]["author"] == ADOPTED
    assert found[0].get("supersedes") is None
    assert found[0]["changes"] == note_id
    riding = {n["id"]: n for n in _riding("Vault")}
    assert riding[note_id].get("body") == BODY
    assert riding[found[0]["id"]]["changes"] == note_id
    assert "body" not in riding[found[0]["id"]]
    listed = loaded.get("notesAdopted")
    assert isinstance(listed, list) and len(listed) == 1, loaded
    entry = listed[0]
    assert _same_file(entry.pop("file"), adopted), entry
    assert entry == {"id": found[0]["id"], "symbol": "Vault",
                     "adoptedFrom": EXPORTED, "block": "changed",
                     "changes": note_id}
    # the sibling is kept, and a reload records nothing more
    first = sorted(notes_mod._records(server_mod.SESSION))
    assert _call("revl_export", {"with_knowledge": True})["ok"] is True
    _reset()
    reloaded = _load(adopted)
    assert sorted(notes_mod._records(server_mod.SESSION)) == first
    assert "notesAdopted" not in reloaded, reloaded["notesAdopted"]
