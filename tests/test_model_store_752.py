"""Persistence bound to a driver-independent model — the memory-driver slice.

Roadmap item 465 (issue #752), design
docs/design/527-database-persistence-alignment.md.

examples/model_store.rvl is the first slice of a Cordis-Database-shaped
persistence store: typed CRUD over a declared row type, with NO raw-SQL
strings (contrast the v0 examples/user_cache.rvl `query(sql: Str)` sketch that
predates Minato), and every mutation lowered as a revertible `effect` whose
derived inverse is the natural CRUD counterpart (`create` carries `undo
remove`).

Two checks, mirroring the split used elsewhere in the suite:

* the frontend/IR assertions always run — they prove the surface compiles, the
  store carries no SQL string, and `create` lowers to an insert effect whose
  undo is the `remove` inverse; and
* the lifecycle assertion runs on the real cordis-py runtime when it is
  installed (`sh backends/python/setup.sh`), proving the created row is
  readable and that teardown reverts it residue-free — the same driver harness
  as tests/test_lifecycle_exec.py.
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


def _find_method(ir: dict, component: str, provision: str, method: str) -> dict:
    comp = next(c for c in ir["components"] if c["name"] == component)
    provide = next(
        s for s in comp["body"]
        if s.get("step") == "provide" and s.get("name") == provision)
    return next(m for m in provide["methods"] if m["name"] == method)


def test_model_store_compiles_with_typed_crud():
    ir = compile_files([str(EXAMPLE)])
    # The declared model row type is carried on the IR.
    assert "User" in ir["types"]
    # The store service exposes typed CRUD verbs, and `create` is an emission
    # (a witnessed mutation), while `get` is a pure read.
    store = ir["services"]["Store"]["methods"]
    assert store["create"]["emission"] is True
    assert store["get"]["emission"] is False
    assert store["get"]["returns"] == "Opt[User]"


def test_no_raw_sql_anywhere():
    """The #752 exit criterion: persistence carries zero raw-SQL strings. The
    CRUD is typed method calls over the model, never a `query(sql: Str)`."""
    # Scan executable source only — the doc comment deliberately names the
    # `query(sql: Str)` sketch it replaces.
    code = "\n".join(
        line.split("//", 1)[0]
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines())
    lowered = code.lower()
    for sql_tok in ("query(sql", "insert into", "delete from", "select * from", " sql:"):
        assert sql_tok not in lowered, f"raw SQL leaked into the store: {sql_tok!r}"
    ir = compile_files([str(EXAMPLE)])
    assert "sql" not in json.dumps(ir).lower()


def test_create_lowers_to_a_revertible_effect_with_the_remove_inverse():
    """Every persistence mutation is a revertible effect whose inverse is the
    natural CRUD counterpart: `create` (an insert) carries `undo remove`."""
    ir = compile_files([str(EXAMPLE)])
    create = _find_method(ir, "MemoryStore", "store", "create")
    effects = [s for s in create["body"] if s.get("step") == "effect"]
    assert len(effects) == 1, "create must be exactly one revertible effect"
    eff = effects[0]
    # forward: an insert of the row under its declared primary field
    assert eff["acquire"]["method"] == "insert"
    # derived inverse: remove — so the row written on activation is reverted
    # in LIFO order on teardown/divert, residue-free.
    assert eff["undo"]["method"] == "remove"


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
    assert "PASS a created row is readable, and reverts residue-free" in result.stdout
    assert "PASS a reloaded store starts empty" in result.stdout
    assert "[py] pass: 2 test(s) passed" in result.stdout
