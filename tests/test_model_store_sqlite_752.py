"""The memory -> sqlite driver swap — the remaining exit criterion of #752.

Roadmap item 465 (issue #752), design
docs/design/527-database-persistence-alignment.md.

examples/model_store.rvl shipped a driver-independent persistence store over an
in-memory driver (`MemoryStore`). Design 527's remaining exit criterion is the
DRIVER SWAP: "a driver swap (memory to sqlite) requiring no change to model or
query code, and an injected-fault write reverting residue-free on the new
driver." examples/model_store_sqlite.rvl is the second driver — a REAL embedded
sqlite backing (Python's stdlib `sqlite3`, held as an `acquire`d connection)
that provides the SAME `Store` service, so choosing memory vs sqlite is a
composition-time choice and nothing in the model or query code moves.

Two check layers, mirroring examples/model_store.rvl's own split
(tests/test_model_store_752.py):

* the frontend/IR assertions always run — the sqlite driver declares the SAME
  model and `Store` surface as the memory driver BYTE-FOR-BYTE, the shared
  lifecycle query bodies are identical (only the `load` target changed, which
  IS the swap), `create` is still store-owned key assignment returning the id,
  the row write still lowers to a revertible `effect` whose inverse is the
  natural `remove` counterpart, and no raw SQL leaks into the model or query
  code (it is confined to the driver's `@py` extern bodies); and
* the lifecycle assertions run on the real cordis-py runtime when it is
  installed (`sh backends/python/setup.sh`): the sqlite driver reverts
  residue-free on teardown and on an injected fault (`abort`), and — the teeth
  — a broken inverse leaves the write behind so the revert assertion FAILS.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

MEMORY = ROOT / "examples" / "model_store.rvl"
SQLITE = ROOT / "examples" / "model_store_sqlite.rvl"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


def _provide_method(ir: dict, component: str, provision: str, method: str) -> dict:
    comp = next(c for c in ir["components"] if c["name"] == component)
    provide = next(
        s for s in comp["body"]
        if s.get("step") == "provide" and s.get("name") == provision)
    return next(m for m in provide["methods"] if m["name"] == method)


def _decl(source: str, header: str) -> str:
    """The single-line `type ...` / `service ...` declaration (a service spans
    to its matching close brace) that begins with `header`, comments stripped."""
    lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("//")]
    for i, ln in enumerate(lines):
        if ln.startswith(header):
            if "{" not in ln or ln.rstrip().endswith("}"):
                return ln.rstrip()
            block = [ln.rstrip()]
            for cont in lines[i + 1:]:
                block.append(cont.rstrip())
                if cont.startswith("}"):
                    break
            return "\n".join(block)
    raise AssertionError(f"no declaration starting {header!r}")


def _lifecycle_block(source: str, title: str) -> str:
    """The body of `lifecycle test "<title>" { ... }`, brace-matched."""
    marker = f'lifecycle test "{title}" {{'
    start = source.index(marker)
    depth = 0
    for j in range(start + len(marker) - 1, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
    raise AssertionError(f"unterminated lifecycle block {title!r}")


# The three lifecycle tests examples/model_store.rvl and the sqlite example
# share verbatim (the fourth, the injected-fault abort, is sqlite-only).
_SHARED_TITLES = (
    "a created row is readable under its store-assigned key, and reverts residue-free",
    "list returns every created row, and keys are never reused",
    "a reloaded store starts empty",
)


# ---------------------------------------------------------------------------
# 1. the swap: identical model + query code, driver swapped (always runs)
# ---------------------------------------------------------------------------

def test_the_model_and_store_service_are_byte_identical_across_drivers():
    """No change to the MODEL: the row types and the `Store` service surface in
    the sqlite example are the same text as examples/model_store.rvl's."""
    mem = MEMORY.read_text(encoding="utf-8")
    sql = SQLITE.read_text(encoding="utf-8")
    for header in ("type Note =", "type Draft =", "service Store {"):
        assert _decl(mem, header) == _decl(sql, header), header


def test_the_shared_query_bodies_differ_only_by_the_driver_name():
    """No change to QUERY CODE: each shared lifecycle test's body is identical
    once the driver component name is normalized — the only thing the swap
    touches is the `load`/`unload` target."""
    mem = MEMORY.read_text(encoding="utf-8")
    sql = SQLITE.read_text(encoding="utf-8")
    for title in _SHARED_TITLES:
        mem_block = _lifecycle_block(mem, title).replace("MemoryStore", "STORE")
        sql_block = _lifecycle_block(sql, title).replace("SqliteStore", "STORE")
        assert mem_block == sql_block, title


def test_both_drivers_provide_the_same_store_service():
    """The compiled surface is the same for both drivers: identical `Store`
    methods, identical emission/return shape."""
    mem_ir = compile_files([str(MEMORY)])
    sql_ir = compile_files([str(SQLITE)])
    assert mem_ir["services"]["Store"] == sql_ir["services"]["Store"]
    comps = {c["name"] for c in sql_ir["components"]}
    assert "SqliteStore" in comps
    prov = next(c for c in sql_ir["components"] if c["name"] == "SqliteStore")
    assert prov["provides"] == {"store": "Store"}


def test_sqlite_create_is_store_owned_key_assignment_returning_the_id():
    """Gap 1 still holds on the new driver: `create` takes a keyless `Draft` and
    returns the store-assigned `Str` id (Minato autoInc + insert-returning-id)."""
    ir = compile_files([str(SQLITE)])
    create = ir["services"]["Store"]["methods"]["create"]
    assert create["emission"] is True
    assert create["returns"] == "Str"
    params = create.get("params") or create.get("args") or []
    assert len(params) == 1
    param_types = json.dumps(params)
    assert "Draft" in param_types and "Note" not in param_types


def test_sqlite_create_lowers_its_row_write_to_a_revertible_effect_with_a_remove_inverse():
    """create/remove is still a revertible effect on the sqlite driver: the row
    write is an INSERT extern carrying an `undo` that DELETEs it, so the row
    written on activation reverts in LIFO order on teardown/abort."""
    ir = compile_files([str(SQLITE)])
    create = _provide_method(ir, "SqliteStore", "store", "create")
    effects = [s for s in create["body"] if s.get("step") == "effect"]
    assert len(effects) == 1, "the row write must be exactly one revertible effect"
    eff = effects[0]
    # the acquire inserts, the undo removes (the natural CRUD inverse pair)
    assert eff["acquire"]["name"] == "sq_insert", eff["acquire"]
    assert eff.get("undo") is not None, "the row write carries no inverse"
    assert eff["undo"]["name"] == "sq_remove", eff["undo"]


def test_no_raw_sql_in_the_model_or_query_code():
    """The #752 exit criterion, on the new driver: the MODEL and QUERY CODE
    carry zero raw SQL. SQL is legitimate INSIDE the sqlite driver's `@py`
    bodies (that is what a sqlite driver is), but it must not appear in the row
    types, the `Store` service, or any lifecycle query body."""
    src = SQLITE.read_text(encoding="utf-8")
    model_regions = [
        _decl(src, "type Note ="),
        _decl(src, "type Draft ="),
        _decl(src, "service Store {"),
    ] + [_lifecycle_block(src, t) for t in _SHARED_TITLES] + [
        _lifecycle_block(src, "an aborted write reverts residue-free on the sqlite driver")]
    haystack = "\n".join(model_regions).lower()
    for tok in ("select ", "insert into", "delete from", "create table",
                "query(", " sql:"):
        assert tok not in haystack, f"raw SQL leaked into the model/query code: {tok!r}"


# ---------------------------------------------------------------------------
# 2. executed on the real cordis-py runtime
# ---------------------------------------------------------------------------

needs_runtime = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _revl_test(path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(path)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


@needs_runtime
def test_the_sqlite_driver_runs_and_reverts_residue_free():
    result = _revl_test(SQLITE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("PASS a created row is readable under its store-assigned key, "
            "and reverts residue-free") in result.stdout
    assert "PASS list returns every created row, and keys are never reused" in result.stdout
    assert "PASS a reloaded store starts empty" in result.stdout
    assert ("PASS an aborted write reverts residue-free on the sqlite driver"
            in result.stdout)
    assert "[py] pass: 4 test(s) passed" in result.stdout


# A file-backed sqlite driver whose db OUTLIVES the component (the `acquire`
# inverse re-opens the file, it never deletes it), so an aborted write can be
# OBSERVED after re-loading the store. This is the teeth for the injected-fault
# revert: the `:memory:` example proves the composition tears down clean, and
# this proves the row inverse actually ran. {db} is a real temp path.
_TEETH_TEMPLATE = r'''
type Note = {{ id: Str, body: Str }}
type Draft = {{ body: Str }}

service Store {{
  fn get(id: Str) -> Opt[Note]
  emission fn create(draft: Draft) -> Str
}}

extern pure fn sq_open() -> Str = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, body TEXT)")
    conn.commit(); conn.close()
    return "{db}"
}}
extern pure fn sq_get(id: Str) -> Opt[Note] = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    row = conn.execute("SELECT id, body FROM notes WHERE id = ?", (id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return {{"id": row[0], "body": row[1]}}
}}
extern pure fn sq_insert(id: Str, body: Str) -> Unit = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    conn.execute("INSERT INTO notes (id, body) VALUES (?, ?)", (id, body))
    conn.commit(); conn.close()
    return
}}
extern pure fn sq_remove(id: Str) -> Unit = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    {remove_body}
    return
}}

component SqliteStore provides store: Store {{
  let db = effect sq_open() undo sq_open()

  provide store {{
    fn get(id) = sq_get(id)
    fn create(draft) {{
      effect sq_insert("note-0", draft.body)
      undo sq_remove("note-0")
      return "note-0"
    }}
  }}
}}

lifecycle test "an aborted write is observably gone on re-open" {{
  load SqliteStore
  call store.create({{ body: "doomed" }})
  abort
  load SqliteStore
  let seen = call store.get("note-0")
  assert seen == None
  unload SqliteStore
  assert no_residue
}}
'''

_REMOVE_OK = ('conn.execute("DELETE FROM notes WHERE id = ?", (id,))\n'
              '    conn.commit(); conn.close()')
_REMOVE_BROKEN = 'conn.close()'  # the inverse does nothing — the write survives


@needs_runtime
def test_the_injected_fault_write_revert_is_observable(tmp_path):
    """Positive teeth: write a row, ABORT, re-open the store on the same file —
    the row is gone, because `create`'s inverse (the DELETE) replayed."""
    db = (tmp_path / "notes.db").as_posix()
    src = _TEETH_TEMPLATE.format(db=db, remove_body=_REMOVE_OK)
    path = tmp_path / "teeth_ok.rvl"
    path.write_text(src, encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS an aborted write is observably gone on re-open" in result.stdout


@needs_runtime
def test_a_broken_inverse_leaves_the_write_and_is_caught(tmp_path):
    """Negative teeth: break the inverse so the aborted write survives — the
    in-language `assert seen == None` then FAILS. An assertion that can only
    pass is not an assertion."""
    db = (tmp_path / "notes.db").as_posix()
    src = _TEETH_TEMPLATE.format(db=db, remove_body=_REMOVE_BROKEN)
    path = tmp_path / "teeth_broken.rvl"
    path.write_text(src, encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL an aborted write is observably gone on re-open" in result.stdout
    assert "assertion failed" in result.stdout
