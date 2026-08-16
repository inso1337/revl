"""The composition manifest and the runtime-admission gate (DESIGN §4:
"dynamically-added components re-run the link check against the running
manifest before admission — the same checker, not a separate weaker one").

Also the demo agent's friction finding #1: a hot-swap must be able to
compile a lone changed file, which cannot redeclare shared services —
ambient services from the running manifest are what make that possible.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl.__main__ import _boundary, main  # noqa: E402

EXAMPLES = ROOT / "examples"
DEMO = ROOT / "demo"


# ---------------------------------------------------------------- manifest

def test_manifest_is_cordisc_shaped():
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    manifest = ir["manifest"]
    assert set(manifest) == {"components", "loadOrder"}
    for entry in manifest["components"]:
        assert set(entry) == {"name", "file", "inject", "provides"}
    assert manifest["loadOrder"] == ["PgDatabase", "UserCache"], "providers first"


def test_components_carry_provenance():
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    assert all(c["source"].endswith("user_cache.rvl") for c in ir["components"])


# ---------------------------------------------------------------- admission

def test_hot_swap_compiles_a_lone_file_against_the_running_manifest():
    """Friction #1 resolved: the changed provider file (which declares no
    services of its own) links against the running composition."""
    running = compile_files([
        str(DEMO / "components" / "services.rvl"),
        str(DEMO / "components" / "pg_database.rvl"),
        str(DEMO / "components" / "user_cache.rvl"),
    ])
    admitted = compile_files(
        [str(DEMO / "variants" / "pg_database.hotswap.rvl")],
        manifest=running,
    )
    # only the new component is compiled; the manifest spans the composition
    assert [c["name"] for c in admitted["components"]] == ["PgDatabase"]
    names = [e["name"] for e in admitted["manifest"]["components"]]
    assert set(names) == {"PgDatabase", "UserCache"}
    assert admitted["manifest"]["loadOrder"] == ["PgDatabase", "UserCache"]


def test_admission_rejects_a_conflicting_provider():
    running = compile_files([str(EXAMPLES / "user_cache.rvl")])
    rogue = """
    component SqliteDatabase provides db: Database {
      let pool = effect Pool.open("sqlite://", 1) undo pool.close()
      provide db {
        fn query(sql) = pool.query(sql)
        fn execute(sql) = pool.execute(sql)
      }
    }
    """
    with pytest.raises(RevlError, match="provided by both PgDatabase and SqliteDatabase"):
        _admit_source(rogue, running)


def test_admission_rejects_interface_drift():
    running = compile_files([str(EXAMPLES / "user_cache.rvl")])
    drifted = """
    service Database { fn query(sql: Str, timeout: Int) -> List[Row] }
    component Probe requires db: Database {
      let rows = effect db.query("SELECT 1", 5) undo rows.drop()
    }
    """
    with pytest.raises(RevlError, match="differs from the running manifest"):
        _admit_source(drifted, running)


def test_admission_detects_a_cycle_through_the_running_composition():
    running = compile_files([str(EXAMPLES / "beacon.rvl")])
    closer = """
    service Kv {
      fn get(k: Int) -> Int
      fn set(k: Int, v: Int)
    }
    component KvBridge requires status: Status provides kv: Kv {
      provide kv {
        fn get(k) = status.shared()
        fn set(k, v) = status.shared()
      }
    }
    """
    with pytest.raises(RevlError, match="dependency cycle"):
        _admit_source(closer, running)


def _admit_source(source: str, running: dict):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".rvl", delete=False) as handle:
        handle.write(source)
        path = handle.name
    return compile_files([path], manifest=running)


# ---------------------------------------------------------------- audit (G8)

def test_boundary_report():
    ir = compile_files([str(EXAMPLES / "user_cache.rvl"), ])
    boundary = _boundary(ir)
    assert boundary["PgDatabase"] == {"emissions": [], "compensated": 0, "awaits": 0}
    assert boundary["UserCache"]["emissions"] == ["db.execute"]


def test_audit_cli_json(capsys):
    code = main(["audit", str(EXAMPLES / "migrator.rvl"), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    migrator = report["boundary"]["Migrator"]
    assert migrator["emissions"] == ["db.execute"]
    assert migrator["compensated"] == 1
    assert migrator["awaits"] == 1
    assert report["manifest"]["loadOrder"] == ["Migrator"]


def test_boundary_counts_teardown_position_emissions():
    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Quiet requires bus: Bus {
          effect Map.new() undo bus.send("goodbye")
        }
        """
    )
    assert _boundary(ir)["Quiet"]["emissions"] == ["bus.send"]
