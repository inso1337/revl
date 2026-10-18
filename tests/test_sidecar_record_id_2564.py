"""A knowledge sidecar record id is a name, never a path (issue #2564).

`revl_export {with_knowledge: true}` writes one JSON file per note record
under `.revl/knowledge/`, named after the record's id. The server mints ids
as `k_` and twelve lower-case hex digits, but a record read back from a file
already in that directory carried whatever id the file held, and the export
built the file name from it.

The rule these tests hold, through the real dispatch:

* a record file whose id is not `k_` and twelve hex digits is ignored at
  load, and no export builds a file name from such an id;
* a record that reaches the store with such an id anyway is skipped and
  listed in `skipped`, never written;
* sidecar targets pass the same export checks as every other target: an
  existing file is never replaced (issue #2534) and a link is refused;
* the server's own records still land in the sidecar, and a well-formed
  record file round-trips unchanged.
"""

import importlib.util
import json
import os
import re
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
RECORD_NAME = re.compile(r"k_[0-9a-f]{12}\.json")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _message(result: dict) -> str:
    return " | ".join(str(d.get("message", ""))
                      for d in result.get("diagnostics") or [])


def _reset():
    session = server_mod.SESSION
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    session.note_records = {}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A sanctioned root under the default, closed authoring trust."""
    base = tmp_path / "root"
    base.mkdir()
    monkeypatch.chdir(base)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(base),))
    assert server_mod.AUTHORING.profile() is not None
    _reset()
    try:
        yield base
    finally:
        _reset()
        server_mod.AUTHORING = old


def _sidecar(base: Path) -> Path:
    return base / ".revl" / "knowledge"


def _in_sidecar(path: str) -> bool:
    """Whether `path` lies directly in a `.revl/knowledge/` directory, however
    the root is spelled."""
    return Path(path).parent.parts[-2:] == (".revl", "knowledge")


def _export_dir(base: Path) -> Path:
    return base / ".revl" / "export"


def _files_tree(base: Path) -> set:
    return {str(p.relative_to(base)) for p in base.rglob("*")
            if p.is_file() or p.is_symlink()}


def _load(path: Path) -> dict:
    loaded = _call("revl_load", {"files": [str(path)]})
    assert loaded["ok"] is True, loaded
    return loaded


def _loaded_notes(path: Path) -> int:
    return _load(path)["knowledge"]["notes"]


def _minted(base: Path) -> tuple[Path, dict]:
    """A composition with one note the server minted and exported: the clock
    file, and the record as its sidecar file holds it."""
    path = base / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    _load(path)
    added = _call("revl_knowledge", {"op": "add", "symbol": "FixedClock",
                                     "kind": "rationale", "body": "why"})
    assert added["ok"] is True, added
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is True, out
    record_file = _sidecar(base) / f"{added['note']['id']}.json"
    assert [Path(p).name for p in out["written"]
            if _in_sidecar(p)] == [record_file.name], out
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["id"] == added["note"]["id"]
    _reset()
    return path, record


def _forged_ids(base: Path) -> dict:
    return {"dotdot": os.path.join("..", "..", "escaped"),
            "separator": os.path.join("sub", "escaped"),
            "absolute": str(base / "escaped")}


def _only_export_area(new: set) -> list:
    """What an export added outside its export directory."""
    return sorted(p for p in new
                  if not p.startswith(os.path.join(".revl", "export") + os.sep))


# ---------------------------------------- a malformed id is not a file name


@pytest.mark.parametrize("kind", ["dotdot", "separator", "absolute"])
def test_a_record_file_with_a_path_for_an_id_is_ignored(root, kind):
    path, record = _minted(root)
    assert _loaded_notes(path) == 1
    _reset()
    forged = {**record, "id": _forged_ids(root)[kind]}
    (_sidecar(root) / "forged.json").write_text(json.dumps(forged), encoding="utf-8")

    loaded = _loaded_notes(path)
    kept = forged["id"] in server_mod.SESSION.note_records
    before = _files_tree(root)
    out = _call("revl_export", {"with_knowledge": True})
    after = _files_tree(root)
    assert _only_export_area(after - before) == [], (after - before, out)
    assert not any("escaped" in p for p in after), after
    assert out["ok"] is True and "escaped" not in json.dumps(out["written"]), out
    assert loaded == 1 and not kept               # dropped at load


def test_a_record_in_the_store_with_a_path_for_an_id_is_skipped(root):
    """Whatever route a record took into the store, an export builds no file
    name from an id that is not one, and says it skipped it."""
    path, record = _minted(root)
    _load(path)
    bad = os.path.join("..", "..", "escaped")
    server_mod.SESSION.note_records[bad] = {**record, "id": bad, "seq": 99}
    before = _files_tree(root)
    out = _call("revl_export", {"with_knowledge": True})
    after = _files_tree(root)
    assert not (root / "escaped.json").exists()
    assert _only_export_area(after - before) == [], (after - before, out)
    assert out["ok"] is True, out
    assert any(bad in str(entry) for entry in out.get("skipped", [])), out


def test_a_record_in_the_store_with_a_malformed_id_is_not_rendered(root):
    """The export renders a comment block only for a record id: an id holding
    a line break never puts a line of its own into the exported module."""
    path, record = _minted(root)
    _load(path)
    bad = "k_bad\n// injected"
    server_mod.SESSION.note_records[bad] = {**record, "id": bad, "seq": 99}
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is True, out
    copy = _export_dir(root).resolve() / "clock.rvl"
    text = copy.read_text(encoding="utf-8")
    assert f"// [{record['id']}]" in text, text   # the minted note still renders
    assert "injected" not in text and "k_bad" not in text, text


def test_a_well_formed_record_file_round_trips_unchanged(root):
    """The positive control: a record file named and keyed by a record id is
    loaded, served, and never rewritten."""
    path, record = _minted(root)
    other = {**record, "id": "k_0123456789ab", "body": "another reason"}
    other_file = _sidecar(root) / "k_0123456789ab.json"
    other_file.write_text(json.dumps(other, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    records = {p: p.read_bytes() for p in _sidecar(root).iterdir()}

    assert _loaded_notes(path) == 2
    found = _call("revl_knowledge", {"op": "query", "id": "k_0123456789ab"})
    assert found["ok"] is True and found["note"]["body"] == "another reason", found
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is True, out
    assert {p: p.read_bytes() for p in _sidecar(root).iterdir()} == records
    assert not any(_in_sidecar(p) for p in out["written"]), out


# ------------------------------------ sidecar targets pass the export checks


def test_a_sidecar_target_that_appeared_is_never_replaced(root, monkeypatch):
    """Issue #2534 holds: the new-file-only writer refuses a file that is at a
    sidecar target by the time the write runs."""
    path = root / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    _load(path)
    assert _call("revl_knowledge", {"op": "add", "symbol": "FixedClock",
                                    "kind": "rationale", "body": "why"})["ok"]
    planned = server_mod._export_plan
    raced = []

    def racing(held, arguments, skipped=None):
        plan = planned(held, arguments, skipped)
        for target, _ in plan:
            if _in_sidecar(target):
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_text("operator bytes\n", encoding="utf-8")
                raced.append(Path(target))
        return plan

    monkeypatch.setattr(server_mod, "_export_plan", racing)
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is False, out
    assert raced and all(p.read_text(encoding="utf-8") == "operator bytes\n"
                         for p in raced)


def test_a_sidecar_target_that_is_a_link_is_refused(root):
    path = root / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    _load(path)
    added = _call("revl_knowledge", {"op": "add", "symbol": "FixedClock",
                                     "kind": "rationale", "body": "why"})
    assert added["ok"] is True, added
    _sidecar(root).mkdir(parents=True)
    link = _sidecar(root) / f"{added['note']['id']}.json"
    link.symlink_to(root / "elsewhere.json")
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is False and "symbolic link" in _message(out), out
    assert link.is_symlink() and not (root / "elsewhere.json").exists()
    assert not _export_dir(root).exists()


def test_the_plan_marks_each_sidecar_entry(root):
    """The new-file-only writer is chosen by a mark on the plan entry, not by
    where the path lies."""
    path = root / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    _load(path)
    assert _call("revl_knowledge", {"op": "add", "symbol": "FixedClock",
                                    "kind": "rationale", "body": "why"})["ok"]
    held = server_mod._edit.running_source(server_mod.SESSION)
    plan = server_mod._export_plan(held, {"with_knowledge": True})
    marks = {(_in_sidecar(entry[0]), entry.new_only)
             for entry in plan}
    assert marks == {(True, True), (False, False)}, plan


# ------------------------------------------------------- the trusted control


def test_the_servers_own_records_land_in_the_sidecar(root):
    path = root / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    _load(path)
    added = _call("revl_knowledge", {"op": "add", "symbol": "FixedClock",
                                     "kind": "rationale", "body": "why"})
    assert added["ok"] is True, added
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is True, out
    names = sorted(p.name for p in _sidecar(root).iterdir())
    assert names == [f"{added['note']['id']}.json"]
    assert all(RECORD_NAME.fullmatch(n) for n in names)
    record = json.loads((_sidecar(root) / names[0]).read_text(encoding="utf-8"))
    assert record["id"] == added["note"]["id"] and record["body"] == "why"
    _reset()
    assert _loaded_notes(path) == 1
