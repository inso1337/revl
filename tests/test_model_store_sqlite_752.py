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
  an update and a delete, and (the teeth) a file-backed build of the example's
  own driver shows each inverse restored the row's value, while a broken
  create, update or delete inverse leaves the wrong value behind so the revert
  assertion FAILS.
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
    is another plain UPDATE carrying the previous value, and `delete` is a
    remove whose inverse re-inserts the removed row. Both carry the value the
    read observed, so a missing key never registers an effect."""
    ir = compile_files([str(SQLITE)])
    update = _provide_method(ir, "SqliteStore", "store", "update")
    delete = _provide_method(ir, "SqliteStore", "store", "delete")
    upd_effects = [s for s in update["body"] if s.get("step") == "effect"]
    del_effects = [s for s in delete["body"] if s.get("step") == "effect"]
    assert len(upd_effects) == 1 and len(del_effects) == 1
    assert upd_effects[0]["acquire"]["name"] == "sq_update"
    assert upd_effects[0]["undo"]["name"] == "sq_update"
    assert del_effects[0]["acquire"]["name"] == "sq_delete"
    assert del_effects[0]["undo"]["name"] == "sq_restore"


def test_sqlite_table_and_column_identifiers_are_declared_and_validated():
    """Issue #3001: identifiers cannot be bound as parameters, so the driver
    declares the table/columns of its model and validates each one against a
    plain-SQL-name rule BEFORE it builds the schema from them. The declared
    columns are parsed from the host body and compared with the row type's
    fields, so the schema and the model cannot silently drift apart."""
    import ast
    import re

    ir = compile_files([str(SQLITE)])
    open_extern = next(e for e in ir["externs"] if e["name"] == "sq_open")
    body = open_extern["bodies"]["py"]
    match = re.search(r"declared = (\{[^\n]*\})\n", body)
    assert match, "the driver declares no table/column identifiers"
    declared = ast.literal_eval(match.group(1))
    # the declared table's columns ARE the `type Note` fields
    assert set(declared["notes"]) == set(ir["types"]["Note"]["fields"])
    assert set(declared["notes"]) == {"id", "body"}
    # the validation itself, and its use in the schema statement
    assert "refused table or column identifier" in body
    assert "ident(" in body


def _is_ident_call(node) -> bool:
    """`conn.ident(...)`, or one item of it (`conn.ident(...)[0]`)."""
    import ast

    if isinstance(node, ast.Subscript):
        node = node.value
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ident")


def _execute_statements(py_body: str) -> list:
    """The statement argument of each `conn.execute(...)` in a `@py` extern
    body, as `(text, stray)`. The statement must be a string constant or a `+`
    chain of operands; `text` joins the constant operands in order with each
    name bound from `conn.ident` rendered as `?ident?`, and `stray` lists every
    operand that is neither (a caller's value, another call, a format)."""
    import ast
    import textwrap

    tree = ast.parse("def _body():\n" + textwrap.indent(py_body, "    "))
    admitted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_ident_call(node.value):
            for target in node.targets:
                elts = target.elts if isinstance(target, ast.Tuple) else [target]
                admitted |= {e.id for e in elts if isinstance(e, ast.Name)}

    def operands(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return operands(node.left) + operands(node.right)
        return [node]

    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args):
            continue
        text, stray = "", []
        for op in operands(node.args[0]):
            if isinstance(op, ast.Constant) and isinstance(op.value, str):
                text += op.value
            elif isinstance(op, ast.Name) and op.id in admitted:
                text += "?ident?"
            else:
                stray.append(ast.unparse(op))
        found.append((text, stray))
    return found


def test_every_statement_takes_its_identifiers_from_the_declared_check():
    """Issue #3001: every `conn.execute` statement in the driver is built only
    from string constants and names bound from `conn.ident`: no caller value,
    call or format reaches statement text (values travel as bound `?`
    parameters). The constants, joined as the statement joins them, never
    spell a declared table or column name, so every identifier comes from the
    declared check. Every statement-bearing extern is counted."""
    import ast
    import re

    ir = compile_files([str(SQLITE)])
    open_body = next(e for e in ir["externs"] if e["name"] == "sq_open")["bodies"]["py"]
    declared = ast.literal_eval(
        re.search(r"declared = (\{[^\n]*\})\n", open_body).group(1))
    names = set(declared) | {c for cols in declared.values() for c in cols}
    word = re.compile(r"\b(" + "|".join(sorted(names)) + r")\b")
    statements = 0
    for ext in ir["externs"]:
        body = (ext.get("bodies") or {}).get("py") or ""
        found = _execute_statements(body)
        if not found:
            continue
        statements += 1
        for text, stray in found:
            assert stray == [], (ext["name"], stray)
            assert not word.search(text), (ext["name"], text)
    # sq_open, sq_next_id, sq_get, sq_list, sq_insert, sq_remove, sq_update,
    # sq_restore, sq_delete
    assert statements == 9


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
                "update notes", " where ", "drop table", "query(", " sql:"):
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
def test_an_invalid_table_identifier_is_refused_at_open(tmp_path):
    """Behavioral teeth for the identifier check: rewrite the driver's declared
    table to a name that is not a plain SQL identifier. The run must fail at
    open with the refusal, rather than reaching statement text."""
    src = SQLITE.read_text(encoding="utf-8").replace(
        'declared = {"notes": ("id", "body"), "seq": ("n",)}',
        'declared = {"notes; DROP TABLE notes": ("id", "body"), "seq": ("n",)}')
    assert '"notes; DROP TABLE notes"' in src
    path = tmp_path / "bad_identifier.rvl"
    path.write_text(src, encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "refused table or column identifier" in (result.stdout + result.stderr)


@needs_runtime
@pytest.mark.parametrize("names, refused", [
    # a plain column sqlite has on every table, but `notes` does not declare
    ('"notes", "id", "rowid"', "'notes.rowid'"),
    # a plain table sqlite always has, outside the declaration
    ('"sqlite_master", "name", "sql"', "'sqlite_master'"),
])
def test_a_plain_but_undeclared_identifier_is_refused(tmp_path, names, refused):
    """Issue #3001: the identifier check is against the DECLARATION, not only
    the plain-name rule. Point the driver's `list` statement at a plain name
    sqlite would accept but the model does not declare; the run must fail with
    the refusal, rather than run the statement."""
    src = SQLITE.read_text(encoding="utf-8")
    start = src.index("extern pure fn sq_list(")
    end = src.index("\n}\n", start)
    head, body, tail = src[:start], src[start:end], src[end:]
    old = 'conn.ident("notes", "id", "body")'
    assert body.count(old) == 1
    path = tmp_path / "undeclared_identifier.rvl"
    path.write_text(head + body.replace(old, f"conn.ident({names})") + tail,
                    encoding="utf-8")
    result = _revl_test(path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert ("refused table or column identifier: " + refused
            in result.stdout + result.stderr), result.stdout + result.stderr


@needs_runtime
def test_a_value_that_looks_like_sql_is_bound_and_stored_verbatim(tmp_path):
    """Issue #3001: a VALUE is a bound `?` parameter, never statement text. A
    body that looks like SQL round-trips byte for byte through create, update,
    read and delete, and the notes table still answers afterwards; had any of
    those values been interpolated, the DROP or DELETE would have run. The same
    kind of text sent as the KEY to get, update and delete matches no row and
    changes nothing: the row set afterwards is exactly the one kept row. An
    interpolated key would raise on the DROP text or match every row on the
    OR text."""
    src = SQLITE.read_text(encoding="utf-8") + r'''
lifecycle test "a hostile value is data, not SQL" {
  load SqliteStore
  let id = call store.create({ body: "x'); DROP TABLE notes; --" })
  let hit = call store.get(id)
  assert hit == Some({ id: "note-0", body: "x'); DROP TABLE notes; --" })
  let edited = call store.update(id, { body: "y'); DELETE FROM notes; --" })
  assert edited == Some({ id: "note-0", body: "y'); DELETE FROM notes; --" })
  let reread = call store.get(id)
  assert reread == Some({ id: "note-0", body: "y'); DELETE FROM notes; --" })
  let removed = call store.delete(id)
  assert removed == Some({ id: "note-0", body: "y'); DELETE FROM notes; --" })
  let gone = call store.get(id)
  assert gone == None
  let kept = call store.create({ body: "kept" })
  assert kept == "note-1"
  let by_drop = call store.get("x'); DROP TABLE notes; --")
  assert by_drop == None
  let by_or = call store.get("x' OR '1'='1")
  assert by_or == None
  let upd_drop = call store.update("x'); DROP TABLE notes; --", { body: "z" })
  assert upd_drop == None
  let upd_or = call store.update("x' OR '1'='1", { body: "z" })
  assert upd_or == None
  let del_drop = call store.delete("x'); DROP TABLE notes; --")
  assert del_drop == None
  let del_or = call store.delete("x' OR '1'='1")
  assert del_or == None
  let rows = call store.list()
  assert rows == [{ id: "note-1", body: "kept" }]
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


# The file-backed teeth for the injected-fault reverts. The driver is the
# EXAMPLE'S OWN source (everything above its lifecycle section), turned
# file-backed by two mechanical rewrites: the connection opens a real temp file
# instead of `:memory:`, and the schema statements gain `IF NOT EXISTS` so a
# re-open keeps the rows. Every CRUD body and every inverse is the shipped one,
# so the example and the tested code cannot drift apart: a wrong inverse in
# examples/model_store_sqlite.rvl turns these tests red. The Python side seeds
# one row per mutation BEFORE the run (`seed-u` for update, `seed-d` for
# delete), so each aborted write has a pre-existing value whose restoration is
# observable, both by the in-language assertion after re-loading the store and
# by reading the file after the run. Seeding in `sq_open` instead would
# recreate the row on the post-abort load and make the delete check vacuous.
_LIFECYCLE_MARKER = (
    "// ---------------------------------------------------------------- lifecycle")

_TEETH_TESTS = r'''
lifecycle test "an aborted create is observably gone on re-open" {
  load SqliteStore
  call store.create({ body: "doomed" })
  abort
  load SqliteStore
  let seen = call store.get("note-0")
  assert seen == None
  unload SqliteStore
  assert no_residue
}

lifecycle test "an aborted update restores the previous body on re-open" {
  load SqliteStore
  call store.update("seed-u", { body: "edited" })
  abort
  load SqliteStore
  let seen = call store.get("seed-u")
  assert seen == Some({ id: "seed-u", body: "original" })
  unload SqliteStore
  assert no_residue
}

lifecycle test "an aborted delete restores the removed row on re-open" {
  load SqliteStore
  call store.delete("seed-d")
  abort
  load SqliteStore
  let seen = call store.get("seed-d")
  assert seen == Some({ id: "seed-d", body: "original" })
  unload SqliteStore
  assert no_residue
}
'''

_TEETH_TITLES = {
    "create": "an aborted create is observably gone on re-open",
    "update": "an aborted update restores the previous body on re-open",
    "delete": "an aborted delete restores the removed row on re-open",
}

# Each shipped inverse, and a broken one that restores nothing: the aborted
# create's row survives (an UPDATE in place of the DELETE), the aborted
# update keeps the new body (the inverse writes the new body again), and the
# deleted row stays gone (an UPDATE of a row that no longer exists).
_SHIPPED_INVERSE = {
    "create": "undo sq_remove(db, id)",
    "update": "undo   sq_update(db, id, old.body)",
    "delete": "undo   sq_restore(db, id, old.body)",
}
_BROKEN_INVERSE = {
    "create": "undo sq_update(db, id, draft.body)",
    "update": "undo   sq_update(db, id, draft.body)",
    "delete": "undo   sq_update(db, id, old.body)",
}

# The rows the file holds after the run: what each shipped inverse leaves, and
# what each broken one leaves instead (None = the row is absent).
_ROWS_RESTORED = {"note-0": None, "seed-u": "original", "seed-d": "original"}
_ROWS_BROKEN = {
    "create": ("note-0", "doomed"),
    "update": ("seed-u", "edited"),
    "delete": ("seed-d", None),
}


def _swap_once(src: str, old: str, new: str) -> str:
    assert src.count(old) == 1, f"expected exactly one {old!r} in the example"
    return src.replace(old, new)


def _file_backed_example(db: str, broken: str | None = None) -> str:
    """examples/model_store_sqlite.rvl's driver, opened on the file `db`, with
    the teeth lifecycle tests in place of the example's own. `broken` names a
    mutation whose shipped inverse is replaced by one that restores nothing."""
    src = SQLITE.read_text(encoding="utf-8")
    driver = src[:src.index(_LIFECYCLE_MARKER)]
    driver = _swap_once(
        driver, 'sqlite3.connect(":memory:", factory=DeclaredConn)',
        f"sqlite3.connect({json.dumps(db)}, factory=DeclaredConn)")
    assert driver.count('"CREATE TABLE "') == 2, "the example's schema changed shape"
    driver = driver.replace('"CREATE TABLE "', '"CREATE TABLE IF NOT EXISTS "')
    if broken is not None:
        driver = _swap_once(
            driver, _SHIPPED_INVERSE[broken], _BROKEN_INVERSE[broken])
    return driver + _TEETH_TESTS


def _seed_db(path: Path) -> None:
    """Create the file's `notes` table and the two pre-existing rows before
    the activation. Seeding here rather than in `sq_open` means the post-abort
    re-open cannot recreate a row, so an update or delete whose inverse did not
    run fails its assertion."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, body TEXT)")
    for key in ("seed-u", "seed-d"):
        conn.execute(
            "INSERT OR REPLACE INTO notes (id, body) VALUES (?, 'original')", (key,))
    conn.commit()
    conn.close()


def _rows_after(path: Path) -> dict:
    """The body each teeth key holds in the file after the run (None = absent)."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        found = dict(conn.execute("SELECT id, body FROM notes").fetchall())
    finally:
        conn.close()
    return {key: found.get(key) for key in _ROWS_RESTORED}


def _run_teeth(tmp_path: Path, broken: str | None) -> tuple:
    db_path = tmp_path / "notes.db"
    _seed_db(db_path)
    src = _file_backed_example(db_path.as_posix(), broken)
    path = tmp_path / f"teeth_{broken or 'ok'}.rvl"
    path.write_text(src, encoding="utf-8")
    return _revl_test(path), _rows_after(db_path)


@needs_runtime
def test_the_injected_fault_write_revert_is_observable(tmp_path):
    """Positive teeth, on the example's own driver: the Python side seeds the
    pre-existing rows, then create, update and delete each ABORT and the store
    is re-opened on the same file. The create's row is gone and the update and
    delete inverses put back the seeded value, seen both in-language after the
    re-load and by reading the file after the run."""
    result, rows = _run_teeth(tmp_path, None)
    assert result.returncode == 0, result.stdout + result.stderr
    for title in _TEETH_TITLES.values():
        assert f"PASS {title}" in result.stdout, title
    assert rows == _ROWS_RESTORED


@needs_runtime
@pytest.mark.parametrize("broken", sorted(_TEETH_TITLES))
def test_a_broken_inverse_leaves_the_write_and_is_caught(tmp_path, broken):
    """Negative teeth: replace one of the example's shipped inverses with one
    that restores nothing. The aborted write then survives with the wrong value
    in the row: the in-language assertion for that mutation FAILS, the other
    two still pass, and the file holds the value the broken inverse left. An
    assertion that can only pass is not an assertion."""
    result, rows = _run_teeth(tmp_path, broken)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"FAIL {_TEETH_TITLES[broken]}" in result.stdout, result.stdout
    assert "assertion failed" in result.stdout
    for other, title in _TEETH_TITLES.items():
        if other != broken:
            assert f"PASS {title}" in result.stdout, title
    key, left = _ROWS_BROKEN[broken]
    assert rows == {**_ROWS_RESTORED, key: left}
