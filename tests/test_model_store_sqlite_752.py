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

Issue #3001 (slice R6) completes the typed CRUD surface on both drivers:
`update` (an overwrite whose inverse restores the value the row held) and
`delete` (a remove whose inverse re-inserts the row), beside `create` and the
`get`/`list` select verbs. The swap still touches only the `load` target, and
the sqlite driver adds the two properties a real driver owes on top of the
memory one: every VALUE is a bound `?` parameter (never statement text), and
every IDENTIFIER is declared and validated against the model before it can
reach statement text (identifiers cannot be bound).

Two check layers, mirroring examples/model_store.rvl's own split
(tests/test_model_store_752.py):

* the frontend/IR assertions always run — the sqlite driver declares the SAME
  model and `Store` surface as the memory driver BYTE-FOR-BYTE, the shared
  lifecycle query bodies are identical (only the `load` target changed, which
  IS the swap), `create` is still store-owned key assignment returning the id,
  every mutation still lowers to a revertible `effect` whose inverse is the
  exact CRUD counterpart, no raw SQL leaks into the model or query code (it is
  confined to the driver's `@py` extern bodies), a value that looks like SQL is
  bound and stored verbatim, and the driver's table/column identifiers are
  declared and validated; and
* the lifecycle assertions run on the real cordis-py runtime when it is
  installed (`sh backends/python/setup.sh`): the sqlite driver reverts
  residue-free on teardown and on an injected fault (`abort`) after a create,
  an update and a delete, and — the teeth — a broken inverse leaves the write
  behind so the revert assertion FAILS.
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


# The five lifecycle tests examples/model_store.rvl and the sqlite example
# share verbatim (the three abort tests, one per mutation, are sqlite-only).
_SHARED_TITLES = (
    "a created row is readable under its store-assigned key, and reverts residue-free",
    "list returns every created row, and keys are never reused",
    "a reloaded store starts empty",
    "update overwrites the row and delete removes it, and both revert residue-free",
    "update and delete of an absent key are typed no-ops",
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
    methods, identical emission/return shape, including the R6 update/delete
    verbs the swap must not change."""
    mem_ir = compile_files([str(MEMORY)])
    sql_ir = compile_files([str(SQLITE)])
    assert mem_ir["services"]["Store"] == sql_ir["services"]["Store"]
    assert sorted(sql_ir["services"]["Store"]["methods"]) == [
        "create", "delete", "get", "list", "update"]
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


def test_sqlite_update_and_delete_lower_to_effects_with_exact_restore_inverses():
    """Issue #3001 on the fresh driver: `update` is an overwrite whose inverse
    restores the previous row (`sq_restore`, never `sq_remove`), and `delete`
    is a remove whose inverse re-inserts the removed row. Both carry the value
    the read observed, so a missing key never registers an effect."""
    ir = compile_files([str(SQLITE)])
    update = _provide_method(ir, "SqliteStore", "store", "update")
    delete = _provide_method(ir, "SqliteStore", "store", "delete")
    upd_effects = [s for s in update["body"] if s.get("step") == "effect"]
    del_effects = [s for s in delete["body"] if s.get("step") == "effect"]
    assert len(upd_effects) == 1 and len(del_effects) == 1
    assert upd_effects[0]["acquire"]["name"] == "sq_update"
    assert upd_effects[0]["undo"]["name"] == "sq_restore"
    assert del_effects[0]["acquire"]["name"] == "sq_delete"
    assert del_effects[0]["undo"]["name"] == "sq_restore"


def test_sqlite_table_and_column_identifiers_are_declared_and_validated():
    """Issue #3001: identifiers cannot be bound as parameters, so the driver
    declares the table/columns of its model and validates each one against a
    plain-SQL-name rule BEFORE it builds the schema from them. The declared
    columns are exactly the `type Note` fields, so the model and the schema
    cannot drift apart."""
    ir = compile_files([str(SQLITE)])
    open_extern = next(e for e in ir["externs"] if e["name"] == "sq_open")
    body = open_extern["bodies"]["py"]
    # the declared model, pinned to the row type's fields
    assert '"notes": ("id", "body")' in body
    assert '"seq": ("n",)' in body
    assert set(ir["types"]["Note"]["fields"]) == {"id", "body"}
    # the validation itself, and its use in the schema statement
    assert "refused table or column identifier" in body
    assert "ident(" in body
    assert "CREATE TABLE" in body


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
        _lifecycle_block(src, t) for t in (
            "an aborted write reverts residue-free on the sqlite driver",
            "an aborted update reverts residue-free on the sqlite driver",
            "an aborted delete reverts residue-free on the sqlite driver",
        )]
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
    for title in _SHARED_TITLES:
        assert f"PASS {title}" in result.stdout, title
    for title in ("an aborted write reverts residue-free on the sqlite driver",
                  "an aborted update reverts residue-free on the sqlite driver",
                  "an aborted delete reverts residue-free on the sqlite driver"):
        assert f"PASS {title}" in result.stdout, title
    assert "[py] pass: 8 test(s) passed" in result.stdout


@needs_runtime
def test_a_value_that_looks_like_sql_is_bound_and_stored_verbatim(tmp_path):
    """Issue #3001: a VALUE is a bound `?` parameter, never statement text. A
    body that looks like SQL round-trips byte for byte, and the notes table
    still answers afterwards; had the value been interpolated, the DROP would
    have run instead."""
    src = SQLITE.read_text(encoding="utf-8") + r'''
lifecycle test "a hostile value is data, not SQL" {
  load SqliteStore
  let id = call store.create({ body: "x'); DROP TABLE notes; --" })
  let hit = call store.get(id)
  assert hit == Some({ id: "note-0", body: "x'); DROP TABLE notes; --" })
  let all = call store.list()
  assert all == [{ id: "note-0", body: "x'); DROP TABLE notes; --" }]
  unload SqliteStore
  assert no_residue
}
'''
    path = tmp_path / "hostile_value.rvl"
    path.write_text(src, encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS a hostile value is data, not SQL" in result.stdout


def test_an_undeclared_update_field_is_a_type_error():
    """Issue #3001: the update payload is typed against the declared model, so
    a field the row type does not declare is refused by the front end, not
    discovered by the driver at runtime."""
    from revl import RevlError, compile_source

    src = SQLITE.read_text(encoding="utf-8") + r'''
lifecycle test "an undeclared update field is refused" {
  load SqliteStore
  call store.update("note-0", { bogus: "x" })
  unload SqliteStore
  assert no_residue
}
'''
    with pytest.raises(RevlError) as exc:
        compile_source(src, filename="undeclared_update_field.rvl")
    assert "bogus" in str(exc.value)


# A file-backed sqlite driver whose db OUTLIVES the component (the `acquire`
# inverse re-opens the file, it never deletes it), so an aborted write can be
# OBSERVED after re-loading the store. This is the teeth for the injected-fault
# reverts: the `:memory:` example proves the composition tears down clean, and
# this proves each mutation's inverse actually ran. `sq_open` seeds one row
# outside the activation's effects, so an aborted update or delete has a
# pre-existing value whose restoration can be observed. {db} is a real temp
# path and {remove_body} is the create inverse's body (broken in the negative
# test).
_TEETH_TEMPLATE = r'''
type Note = {{ id: Str, body: Str }}
type Draft = {{ body: Str }}

service Store {{
  fn get(id: Str) -> Opt[Note]
  emission fn create(draft: Draft) -> Str
  emission fn update(id: Str, draft: Draft) -> Opt[Note]
  emission fn delete(id: Str) -> Opt[Note]
}}

extern pure fn sq_open() -> Str = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, body TEXT)")
    # a row that exists BEFORE the activation: INSERT OR IGNORE keeps the
    # restored value an aborted update/delete writes back.
    conn.execute("INSERT OR IGNORE INTO notes (id, body) VALUES ('seed', 'original')")
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
extern pure fn sq_update(id: Str, body: Str) -> Unit = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body, id))
    conn.commit(); conn.close()
    return
}}
extern pure fn sq_restore(id: Str, body: Str) -> Unit = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    conn.execute("INSERT OR REPLACE INTO notes (id, body) VALUES (?, ?)", (id, body))
    conn.commit(); conn.close()
    return
}}
extern pure fn sq_remove(id: Str) -> Unit = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    {remove_body}
    return
}}
extern pure fn sq_delete(id: Str) -> Unit = @py {{
    import sqlite3
    conn = sqlite3.connect("{db}")
    conn.execute("DELETE FROM notes WHERE id = ?", (id,))
    conn.commit(); conn.close()
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
    fn update(id, draft) {{
      let cur = sq_get(id)
      if (cur == None) {{ return None }}
      let old = match cur {{ Some(v) => v, None => {{ id: id, body: "" }} }}
      effect sq_update(id, draft.body)
      undo sq_restore(id, old.body)
      return Some({{ id: id, body: draft.body }})
    }}
    fn delete(id) {{
      let cur = sq_get(id)
      if (cur == None) {{ return None }}
      let old = match cur {{ Some(v) => v, None => {{ id: id, body: "" }} }}
      effect sq_delete(id)
      undo sq_restore(id, old.body)
      return cur
    }}
  }}
}}

lifecycle test "an aborted create is observably gone on re-open" {{
  load SqliteStore
  call store.create({{ body: "doomed" }})
  abort
  load SqliteStore
  let seen = call store.get("note-0")
  assert seen == None
  unload SqliteStore
  assert no_residue
}}

lifecycle test "an aborted update restores the previous body on re-open" {{
  load SqliteStore
  call store.update("seed", {{ body: "edited" }})
  abort
  load SqliteStore
  let seen = call store.get("seed")
  assert seen == Some({{ id: "seed", body: "original" }})
  unload SqliteStore
  assert no_residue
}}

lifecycle test "an aborted delete restores the removed row on re-open" {{
  load SqliteStore
  call store.delete("seed")
  abort
  load SqliteStore
  let seen = call store.get("seed")
  assert seen == Some({{ id: "seed", body: "original" }})
  unload SqliteStore
  assert no_residue
}}
'''

_REMOVE_OK = ('conn.execute("DELETE FROM notes WHERE id = ?", (id,))\n'
              '    conn.commit(); conn.close()')
_REMOVE_BROKEN = 'conn.close()'  # the inverse does nothing — the write survives


@needs_runtime
def test_the_injected_fault_write_revert_is_observable(tmp_path):
    """Positive teeth: create, update and delete each ABORT, then re-open the
    store on the same file. The create's row is gone, and the update/delete
    inverses restored the seeded row, because each inverse replayed."""
    db = (tmp_path / "notes.db").as_posix()
    src = _TEETH_TEMPLATE.format(db=db, remove_body=_REMOVE_OK)
    path = tmp_path / "teeth_ok.rvl"
    path.write_text(src, encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS an aborted create is observably gone on re-open" in result.stdout
    assert "PASS an aborted update restores the previous body on re-open" in result.stdout
    assert "PASS an aborted delete restores the removed row on re-open" in result.stdout


@needs_runtime
def test_a_broken_inverse_leaves_the_write_and_is_caught(tmp_path):
    """Negative teeth: break the inverse so the aborted create survives — the
    in-language `assert seen == None` then FAILS. An assertion that can only
    pass is not an assertion."""
    db = (tmp_path / "notes.db").as_posix()
    src = _TEETH_TEMPLATE.format(db=db, remove_body=_REMOVE_BROKEN)
    path = tmp_path / "teeth_broken.rvl"
    path.write_text(src, encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL an aborted create is observably gone on re-open" in result.stdout
    assert "assertion failed" in result.stdout
