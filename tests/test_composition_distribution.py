"""Composition distribution: the composition-owned half of item 426 slice S6.

426 decision 7 (§7): distribution is truc's, semantics are the composition's,
and when they disagree THE COMPOSITION IS SOURCE OF TRUTH while the lock is the
integrity proof. This file covers the semantics the COMPOSITION owns and that
resolve header-only, with no cordis-py runtime:

  15 (second half)  the VENDORED-DIR JAIL: a stack layer whose `from` path
                    resolves outside its own truc's vendored directory is
                    refused (§4.1).
  17                the MANDATORY PIN at resolution: a vendored truc a
                    composition references with no lock pin, or a blank
                    `sourceHash`, is refused (§7, the 428 F3 gate). The
                    `truc assemble` half of F3 is tests/test_truc_lock_pin_required.py
                    (it drives the real `truc` CLI and needs the cordis-py
                    runtime); this is the composition-resolution half.
  18                RESOLUTION IS REPRODUCIBLE: a byte-identical row table
                    across two machines given the same composition, lock and
                    vendored layers, with different registry index contents
                    present on each — the resolver reads vendored bytes and the
                    lock, never the registry index.

The `truc apply` and `truc stack check` distribution VERBS (§11 S6) are truc's
CLI surface (the bootstrapped `.rvl` toolchain), and are not exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from revl.composition import resolve_file
from revl.errors import RevlError

_SERVICES = """
service Db { fn query(q: Str) -> Str }
"""

_PG_COMPONENT = """
use "services.rvl" { }
component PgDb provides db: Db {
  config { url: Str }
  provide db { fn query(q) = q }
}
"""

_BASE = """
composition Demo {
  use "services.rvl"
  row @db from "trucs/pg_database/component.rvl" provides db
    config { url: "postgres://primary:5432/app" }
%(stack)s}
"""


def _base(stack: str = "") -> str:
    return _BASE % {"stack": stack}


def _project(tmp_path: Path, *, lock: dict | None = None) -> Path:
    """A project vendoring one truc, `pg_database`, referenced by the base
    composition. `lock` writes `truc.lock` when given."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "services.rvl").write_text(_SERVICES)
    vendored = tmp_path / "trucs" / "pg_database"
    vendored.mkdir(parents=True, exist_ok=True)
    (vendored / "services.rvl").write_text(_SERVICES)
    (vendored / "component.rvl").write_text(_PG_COMPONENT)
    if lock is not None:
        (tmp_path / "truc.lock").write_text(json.dumps(lock, indent=2))
    doc = tmp_path / "base.rvl"
    doc.write_text(_base())
    return doc


_PINNED = {"lockVersion": 1,
           "trucs": [{"name": "pg_database", "sourceHash": "a" * 64}]}


# --------------------------------------------------------------------------- #
# Exit test 17 — the pin is mandatory.
# --------------------------------------------------------------------------- #

def test_a_vendored_truc_with_no_lock_at_all_is_refused(tmp_path):
    """A composition that references a vendored truc with no `truc.lock` present
    is refused at resolution — the pin is not optional (§7)."""
    doc = _project(tmp_path, lock=None)
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    assert "unpinned truc" in str(exc.value) and "pg_database" in str(exc.value)


def test_a_vendored_truc_absent_from_the_lock_is_refused(tmp_path):
    """A lock that exists but names no row for the referenced truc is 'no pin'."""
    doc = _project(tmp_path, lock={"lockVersion": 1, "trucs": []})
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    assert "unpinned truc" in str(exc.value)


def test_a_blank_source_hash_is_no_pin(tmp_path):
    """A lock row present but whose `sourceHash` is "" is the same exploit
    wearing a lock row: a blank pin is no pin (428 F3)."""
    doc = _project(tmp_path, lock={"lockVersion": 1,
                                   "trucs": [{"name": "pg_database",
                                              "sourceHash": ""}]})
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    assert "unpinned truc" in str(exc.value)


def test_a_pinned_vendored_truc_resolves(tmp_path):
    """With a non-blank pin, the vendored row resolves — the pin's PRESENCE is
    what resolution requires; verifying the bytes hash to it is `truc`'s job
    (§7: the composition is source of truth, the lock is the integrity proof)."""
    doc = _project(tmp_path, lock=_PINNED)
    table = resolve_file(str(doc), str(tmp_path))
    assert [r.label for r in table.rows] == ["db"]
    assert table.rows[0].component == "PgDb"


# --------------------------------------------------------------------------- #
# Exit test 15 (second half) — the vendored-dir jail.
# --------------------------------------------------------------------------- #

def test_a_stack_layer_from_path_outside_its_truc_is_refused(tmp_path):
    """426 exit test 15, second half. A vendored stack layer whose `from` path
    climbs out of its own truc's vendored directory is refused — it cannot read
    the project's own sources (or another truc's) and launder them as its own
    row (§4.1)."""
    doc = _project(tmp_path, lock={
        "lockVersion": 1,
        "trucs": [{"name": "pg_database", "sourceHash": "a" * 64},
                  {"name": "evil", "sourceHash": "b" * 64}]})
    # a secret the project keeps outside any truc
    (tmp_path / "secret.rvl").write_text(_PG_COMPONENT)
    evil = tmp_path / "trucs" / "evil"
    evil.mkdir(parents=True)
    (evil / "layer.rvl").write_text("""
layer Evil for Demo {
  add row @stolen from "../../secret.rvl" provides db
}
""")
    # the base composition names the vendored layer in its stack.
    doc.write_text(_base('  stack "trucs/evil/layer.rvl"\n'))
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    msg = str(exc.value)
    assert "outside" in msg and "trucs/evil/" in msg


def test_a_stack_layer_reading_its_own_truc_is_admitted(tmp_path):
    """The jail admits the honest case: a vendored layer reading a component
    inside its OWN truc directory resolves."""
    doc = _project(tmp_path, lock={
        "lockVersion": 1,
        "trucs": [{"name": "pg_database", "sourceHash": "a" * 64},
                  {"name": "metrics_kit", "sourceHash": "b" * 64}]})
    kit = tmp_path / "trucs" / "metrics_kit"
    kit.mkdir(parents=True)
    (kit / "services.rvl").write_text("service Metrics { fn tick() -> Int }\n")
    (kit / "component.rvl").write_text("""
use "services.rvl" { }
component KitMetrics provides metrics: Metrics {
  provide metrics { fn tick() = 1 }
}
""")
    (kit / "layer.rvl").write_text("""
layer MetricsKit for Demo {
  add row @metrics from "component.rvl" provides metrics
}
""")
    doc.write_text(_base('  stack "trucs/metrics_kit/layer.rvl"\n'))
    table = resolve_file(str(doc), str(tmp_path))
    assert {r.label for r in table.rows} == {"db", "metrics"}


# --------------------------------------------------------------------------- #
# Exit test 18 — resolution is reproducible.
# --------------------------------------------------------------------------- #

def test_row_table_is_byte_identical_across_differing_registry_indexes(tmp_path):
    """426 exit test 18. The resolved row table is byte-identical given the same
    composition, lock and vendored sources, regardless of what a registry index
    on the machine contains — resolution reads vendored bytes and the lock,
    never the registry index."""
    doc = _project(tmp_path, lock=_PINNED)
    registry = tmp_path / "registry"
    registry.mkdir()

    # machine A: one registry index content
    (registry / "index.json").write_text(json.dumps(
        {"pg_database": {"versions": ["2.0.0", "2.1.0"], "note": "machine A"}}))
    first = json.dumps(resolve_file(str(doc), str(tmp_path)).to_ir(),
                       sort_keys=True)

    # machine B: a DIFFERENT registry index content, same composition and lock
    (registry / "index.json").write_text(json.dumps(
        {"pg_database": {"versions": ["9.9.9"], "extra": "machine B differs"}}))
    second = json.dumps(resolve_file(str(doc), str(tmp_path)).to_ir(),
                        sort_keys=True)

    assert first == second
