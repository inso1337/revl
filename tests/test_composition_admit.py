"""Incremental admission of a composition's layers (roadmap item 426, slice S3).

The design is `docs/design/426-composition-layers.md`; §11 states S3 as "the
fold's delta admitted against the base as the running manifest, buildable after
S2, depends on nothing new", and §5.1 is the mechanism. The exit test this file
carries is §12's number 10:

  10  incremental admission  applying a layer recompiles ONLY the delta, and the
                             verdict and the resulting manifest are byte-
                             identical to a full re-compile.

The recompile set is the structural delta the fold introduces (the rows it added
or replaced); every row the delta did not touch is CARRIED from the base
manifest rather than recompiled, which is the incremental win and is exactly how
`tests/test_manifest.py:44` admits a lone hot-swap provider against a running
composition. Activation stays whole-generation (§5.2): this admits a patch, it
does not hot-swap a fiber, so exit test 11 is deliberately not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import admit_layers  # noqa: E402

SERVICES = """
service Database { fn query(sql: Str) -> Str }
service Cache    { fn get(key: Str) -> Str }
service Metrics  { fn tick() -> Int }
"""

METRICS = """
use "services.rvl" { }
component LocalMetrics provides metrics: Metrics {
  provide metrics { fn tick() = 1 }
}
"""

SQLITE = """
use "services.rvl" { }
component SqliteDatabase provides db: Database {
  config { url: Str, pool: Int = 4 }
  provide db { fn query(sql) = sql }
}
"""

POSTGRES = """
use "services.rvl" { }
component PgDatabase provides db: Database {
  config { url: Str, pool: Int = 8 }
  provide db { fn query(sql) = sql }
}
"""

CACHE = """
use "services.rvl" { }
component MemCache requires db: Database provides cache: Cache {
  provide cache { fn get(key) = db.query(key) }
}
"""

BASE = """
composition Demo {
  use "services.rvl"
  row @db from "sqlite.rvl" provides db
    config { url: "sqlite://local" }
  row @cache from "cache.rvl" provides cache
%s}
"""

PG_SWAP = """
layer PgSwap for Demo {
  replace key("db") with row @db from "../postgres.rvl" provides db
    config { url: "postgres://primary:5432/app" }
}
"""


def _project(tmp_path: Path, *stack: str, **layers: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "services.rvl").write_text(SERVICES)
    (tmp_path / "sqlite.rvl").write_text(SQLITE)
    (tmp_path / "postgres.rvl").write_text(POSTGRES)
    (tmp_path / "metrics.rvl").write_text(METRICS)
    (tmp_path / "cache.rvl").write_text(CACHE)
    (tmp_path / "layers").mkdir(exist_ok=True)
    for name, text in layers.items():
        (tmp_path / "layers" / f"{name}.rvl").write_text(text)
    clauses = "".join(f'  stack "layers/{name}.rvl"\n' for name in stack)
    doc = tmp_path / "base.rvl"
    doc.write_text(BASE % clauses)
    return doc


def _names(document) -> set[str]:
    return {entry["name"] for entry in document["manifest"]["components"]}


# ----------------------------------------------------------------- exit test 10

def test_replace_recompiles_only_the_delta_and_matches_a_full_admit(tmp_path):
    """426 exit test 10. A `replace` layer swaps `@db` while `@cache` is
    untouched. The incremental admit recompiles only the replacing row
    (PgDatabase), CARRIES MemCache from the running manifest, and produces a
    manifest byte-identical to a full re-admission."""
    doc = _project(tmp_path, "pg", pg=PG_SWAP)

    incremental = admit_layers(str(doc), str(tmp_path))
    full = admit_layers(str(doc), str(tmp_path), full=True)

    # the verdict and the resulting manifest are identical to a full re-compile
    assert _names(incremental) == _names(full) == {"PgDatabase", "MemCache"}
    assert incremental["manifest"]["loadOrder"] == full["manifest"]["loadOrder"]
    assert incremental["manifest"]["rows"] == full["manifest"]["rows"]

    # ONLY the delta was recompiled: the unchanged row is carried, the replaced
    # provider's old name is withdrawn.
    record = incremental["admission"]
    assert record["mode"] == "incremental"
    assert record["recompiled"] == ["PgDatabase"]
    assert record["carried"] == ["MemCache"]
    assert record["withdrawn"] == ["SqliteDatabase"]

    # the delta document's own `components` are the newly compiled ones only
    assert [c["name"] for c in incremental["components"]] == ["PgDatabase"]


def test_full_admit_recompiles_the_whole_table(tmp_path):
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    full = admit_layers(str(doc), str(tmp_path), full=True)
    assert full["admission"]["mode"] == "full"
    assert sorted(full["admission"]["recompiled"]) == ["MemCache", "PgDatabase"]
    assert {c["name"] for c in full["components"]} == {"PgDatabase", "MemCache"}


# ------------------------------------------------- the four delta shapes admit

def test_add_only_layer_carries_the_base_and_admits_the_new_row(tmp_path):
    """An `add` layer introduces a row. Only the added row is recompiled, the
    base rows are carried, and the manifest gains the new component."""
    doc = _project(tmp_path, "obs", obs="""
layer Obs for Demo {
  add row @metrics from "../metrics.rvl" provides metrics
}
""")
    incremental = admit_layers(str(doc), str(tmp_path))
    full = admit_layers(str(doc), str(tmp_path), full=True)
    assert _names(incremental) == _names(full)
    # an independent added row has a free position in the topological order, so
    # the load SET is what must agree (the replace test pins byte-identity where
    # a dependency edge fixes the order).
    assert set(incremental["manifest"]["loadOrder"]) == \
        set(full["manifest"]["loadOrder"])
    assert incremental["admission"]["recompiled"] == ["LocalMetrics"]
    assert set(incremental["admission"]["carried"]) == {"SqliteDatabase", "MemCache"}
    assert incremental["admission"]["withdrawn"] == []


def test_configure_only_layer_recompiles_nothing(tmp_path):
    """Config is typed at resolution and never reaches a body, so a configure-
    only patch recompiles NOTHING and changes no wiring (§5.3). The manifest is
    unchanged from the base; the folded config rides in the IR `rows`."""
    doc = _project(tmp_path, "tune", tune="""
layer Tune for Demo {
  configure @db with { pool: 32 }
}
""")
    incremental = admit_layers(str(doc), str(tmp_path))
    full = admit_layers(str(doc), str(tmp_path), full=True)

    assert incremental["admission"]["mode"] == "incremental"
    assert incremental["admission"]["recompiled"] == []
    assert _names(incremental) == _names(full) == {"SqliteDatabase", "MemCache"}
    # the configured value is present in the folded rows the admit carries
    db_row = next(r for r in incremental["rows"]["rows"] if r["label"] == "db")
    assert db_row["config"]["pool"] == 32


def test_no_layers_folds_to_a_full_admit(tmp_path):
    """A composition with no layers admits as a full compile — there is no delta
    to be incremental about, and the result equals `--full`."""
    doc = _project(tmp_path)
    document = admit_layers(str(doc), str(tmp_path))
    assert document["admission"]["mode"] == "full"
    assert _names(document) == {"SqliteDatabase", "MemCache"}
