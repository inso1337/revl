"""Persistence bound to a driver-independent model — the memory-driver slice.

Roadmap item 465 (issue #752), design
docs/design/527-database-persistence-alignment.md.

examples/model_store.rvl is a Cordis-Database-shaped persistence store: typed
CRUD over a declared row type, with NO raw-SQL strings (contrast the v0
examples/user_cache.rvl `query(sql: Str)` sketch that predates Minato), and
every mutation lowered as a revertible `effect` whose inverse is the natural
CRUD counterpart (`create` undoes to `remove`, `update` restores the value the
row held before the overwrite, `delete` re-inserts the row it removed).

Beyond the first slice (#767) it closes the two persistence-ergonomics gaps the
exemplary app's boring half (#777) filed against the pattern:

* STORE-OWNED KEY ASSIGNMENT — `create` takes a keyless `Draft` and mints the
  primary key itself, returning it (Minato autoInc + insert-returning-id). The
  handler no longer mints `"note-" + size().to_str()`, which reuses an id after
  a delete; the store draws the key from a monotonic sequence.
* A DRIVER-OWNED `list` VERB — the `keys()`-then-`get` fold lives once inside
  the driver (Minato `select` with no filter), so a consumer says
  `store.list()` instead of re-rolling the fold at every call site.

Issue #3001 (slice R6) completes the typed CRUD surface: `update` (overwrite)
and `delete` (remove), both keyed and both returning the row they changed (or
`None` for an absent key, a typed no-op), so create/select/update/delete
round-trip through the driver while the swap test in the sqlite suite proves
the model and query code never move.

Two check layers, mirroring the split used elsewhere in the suite:

* the frontend/IR assertions always run — they prove the surface compiles, the
  store carries no SQL string, `create` returns the store-assigned key and
  lowers its row write to an insert effect whose undo is the `remove` inverse,
  `update` lowers to an overwrite effect whose undo branches on the value read
  before the write (issue #1980), `delete` lowers to a remove effect whose undo
  re-inserts the removed row, and `list` returns the typed `List[Note]`; and
* the lifecycle assertion runs on the real cordis-py runtime when it is
  installed (`sh backends/python/setup.sh`), proving the rows are created,
  read, overwritten, deleted and listable under their store-assigned keys and
  that teardown reverts them residue-free, with the same driver harness as
  tests/test_lifecycle_exec.py.
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

EXAMPLE = ROOT / "examples" / "model_store.rvl"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


def _provide_method(ir: dict, component: str, provision: str, method: str) -> dict:
    comp = next(c for c in ir["components"] if c["name"] == component)
    provide = next(
        s for s in comp["body"]
        if s.get("step") == "provide" and s.get("name") == provision)
    return next(m for m in provide["methods"] if m["name"] == method)


def test_model_store_compiles_with_typed_crud():
    ir = compile_files([str(EXAMPLE)])
    # The declared row type is carried on the IR, keyed by `id`; the caller
    # supplies the keyless `Draft`.
    assert "Note" in ir["types"]
    assert "Draft" in ir["types"]
    assert ir["types"]["Draft"]["fields"] == {"body": "Str"}
    # The store service exposes typed CRUD verbs, and `create`/`update`/`delete`
    # are emissions (witnessed mutations), while `get`/`list` are pure reads.
    store = ir["services"]["Store"]["methods"]
    assert store["create"]["emission"] is True
    assert store["update"]["emission"] is True
    assert store["delete"]["emission"] is True
    assert store["get"]["emission"] is False
    assert store["list"]["emission"] is False
    assert store["get"]["returns"] == "Opt[Note]"
    assert store["update"]["returns"] == "Opt[Note]"
    assert store["delete"]["returns"] == "Opt[Note]"
    assert sorted(store) == ["create", "delete", "get", "list", "update"]


def test_no_raw_sql_anywhere():
    """The #752 exit criterion: persistence carries zero raw-SQL strings. The
    CRUD is typed method calls over the model, never a `query(sql: Str)`."""
    # Scan executable source only — the doc comment deliberately names the
    # `query(sql: Str)` sketch it replaces.
    code = "\n".join(
        line.split("//", 1)[0]
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines())
    lowered = code.lower()
    for sql_tok in ("query(sql", "insert into", "delete from", "select * from",
                    "update notes", " where ", "drop table", " sql:"):
        assert sql_tok not in lowered, f"raw SQL leaked into the store: {sql_tok!r}"
    ir = compile_files([str(EXAMPLE)])
    assert "sql" not in json.dumps(ir).lower()


def test_create_is_store_owned_key_assignment_returning_the_id():
    """Gap 1: the store mints the primary key and returns it (Minato autoInc +
    insert-returning-id), so the handler no longer mints keys. `create` takes a
    keyless `Draft` and returns the store-assigned `Str` id."""
    ir = compile_files([str(EXAMPLE)])
    create = ir["services"]["Store"]["methods"]["create"]
    assert create["returns"] == "Str"
    params = create.get("params") or create.get("args") or []
    # exactly one parameter, and it is the keyless draft (not the keyed row)
    assert len(params) == 1
    param_types = json.dumps(params)
    assert "Draft" in param_types and "Note" not in param_types


def test_create_lowers_its_row_write_to_a_revertible_effect_with_the_remove_inverse():
    """Every persistence mutation is a revertible effect whose inverse is the
    natural CRUD counterpart: the row write is an insert carrying `undo
    remove`, so the row written on activation reverts in LIFO order on
    teardown/divert, residue-free."""
    ir = compile_files([str(EXAMPLE)])
    create = _provide_method(ir, "MemoryStore", "store", "create")
    effects = [s for s in create["body"] if s.get("step") == "effect"]
    # locate the effect whose receiver is the `rows` model table (the sequence
    # tick is a second, book-keeping effect)
    row_effects = [
        e for e in effects
        if e["acquire"].get("target", {}).get("id") == "rows"]
    assert len(row_effects) == 1, "the row write must be exactly one revertible effect"
    eff = row_effects[0]
    assert eff["acquire"]["method"] == "insert"
    assert eff["undo"]["method"] == "remove"
    assert eff["undo"]["target"]["id"] == "rows"


def test_list_is_a_driver_owned_typed_listing():
    """Gap 2: listing is a driver-owned `list` verb returning the typed
    `List[Note]` — the `keys()`-then-`get` fold lives inside the driver, not in
    the consumer (Minato `select` with no filter)."""
    ir = compile_files([str(EXAMPLE)])
    assert ir["services"]["Store"]["methods"]["list"]["returns"] == "List[Note]"
    # the fold is inside the provider method: it iterates the model's keys
    lst = _provide_method(ir, "MemoryStore", "store", "list")
    steps = [s.get("step") for s in lst["body"]]
    assert "for" in steps, "the driver owns the fold over the model's keys"


def test_update_lowers_to_an_overwrite_effect_that_restores_the_previous_value():
    """Issue #3001: `update` is an overwrite, so its inverse is the value the
    row held BEFORE the write, admitted as the `Opt` match issue #1980 proves
    exact (`Some(v)` restores, `None` removes), never `remove` on its own."""
    ir = compile_files([str(EXAMPLE)])
    update = _provide_method(ir, "MemoryStore", "store", "update")
    effects = [s for s in update["body"] if s.get("step") == "effect"]
    assert len(effects) == 1
    eff = effects[0]
    assert eff["acquire"]["method"] == "insert"
    assert eff["acquire"]["target"]["id"] == "rows"
    assert eff["undo"]["kind"] == "match"
    assert [a["pattern"] for a in eff["undo"]["arms"]] == ["Some", "None"]
    assert eff["inverse"] == "table", "the Opt-read restore is a proved inverse"


def test_delete_lowers_to_a_remove_effect_that_restores_the_removed_row():
    """Issue #3001: `delete` is a remove whose inverse re-inserts the row it
    removed, so a delete during activation is undone in LIFO order."""
    ir = compile_files([str(EXAMPLE)])
    delete = _provide_method(ir, "MemoryStore", "store", "delete")
    effects = [s for s in delete["body"] if s.get("step") == "effect"]
    assert len(effects) == 1
    eff = effects[0]
    assert eff["acquire"]["method"] == "remove"
    assert eff["acquire"]["target"]["id"] == "rows"
    assert eff["undo"]["method"] == "insert"
    assert eff["undo"]["target"]["id"] == "rows"


def test_update_takes_the_same_keyless_typed_payload_as_create():
    """The update payload is the declared `Draft`, not the keyed `Note`: a
    caller supplies only the mutable fields, and a field the model does not
    declare is a front-end type error (the typed model boundary)."""
    ir = compile_files([str(EXAMPLE)])
    update = ir["services"]["Store"]["methods"]["update"]
    params = {p["name"]: p["type"] for p in update["params"]}
    assert params == {"id": "Str", "draft": "Draft"}
    assert ir["types"]["Draft"]["fields"] == {"body": "Str"}


@pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
def test_store_reverts_residue_free_on_the_runtime():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(EXAMPLE)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("PASS a created row is readable under its store-assigned key, "
            "and reverts residue-free") in result.stdout
    assert "PASS list returns every created row, and keys are never reused" in result.stdout
    assert "PASS a reloaded store starts empty" in result.stdout
    assert ("PASS update overwrites the row and delete removes it, and both "
            "revert residue-free") in result.stdout
    assert "PASS update and delete of an absent key are typed no-ops" in result.stdout
    assert "[py] pass: 5 test(s) passed" in result.stdout
