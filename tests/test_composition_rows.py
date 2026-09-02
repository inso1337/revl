"""Composition rows: the row table (roadmap item 426, slice S1).

The design is `docs/design/426-composition-layers.md`; the exit tests it names
are in §12 and the slice split is §11. S1 is "the row table, buildable first,
depends on nothing": the composition document, label declaration with origin
scoping, the claim assertion checked against the component header, header-only
resolution, and the row table in the IR and the manifest.

Item 424's residual R2 (the `granted` clause, `docs/design/424-dsh-language-gaps.md`
§1.2 and slice A1) folds in here, which is where 424 itself files it.

Which of 426 §12's exit tests this file covers, and which it cannot yet:

  1  label survives a surface addition   COVERED (the layer half needs S2)
  2  label survives a rename             COVERED (empty wiring diff)
  3  a removed provision is a refusal    COVERED
  4  two origins, one bare label         COVERED
  5  an address resolving to nothing     COVERED for a row's own `from` and
                                         `component` addresses; the layer's
                                         `key(...)` address needs S2
  9  `configure` is typed                COVERED for the BASE composition's
                                         `config` clause; the layer operation
                                         needs S2
 12  header-only checking                COVERED
 18  resolution is reproducible          COVERED (no absolute path in the IR)

  6, 7, 8, 10, 11    need S2/S3 (layers, the fold, incremental admission)
 13, 14, 15, 16, 17  need S4/S5/S6 (confinement, the panel, distribution)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import (  # noqa: E402
    compile_composition, origin_of, resolve_file)
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402

SERVICES = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}

service Metrics {
  fn tick() -> Int
}
"""

DB = """
use "services.rvl" { }

component PgDatabase provides db: Database {
  config { url: Str, pool_size: Int = 10 }
  let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
"""

CACHE = """
use "services.rvl" { }

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
"""


def _project(tmp_path: Path, composition: str, **extra: str) -> Path:
    """A minimal project: the three sources plus a composition document."""
    (tmp_path / "services.rvl").write_text(SERVICES)
    (tmp_path / "db.rvl").write_text(DB)
    (tmp_path / "cache.rvl").write_text(CACHE)
    for name, text in extra.items():
        (tmp_path / f"{name}.rvl").write_text(text)
    doc = tmp_path / "base.rvl"
    doc.write_text(composition)
    return doc


BASE = """
composition Demo {
  use "services.rvl"
  row @db from "db.rvl" provides db
    config { url: "postgres://primary:5432/app", pool_size: 8 }
  row @cache from "cache.rvl" provides cache
}
"""


# ------------------------------------------------------- the row table itself

def test_the_row_table_resolves_header_only(tmp_path):
    """426 exit test 12: every row id resolves and the whole wiring renders
    with no component body lowered."""
    doc = _project(tmp_path, BASE)
    table = resolve_file(str(doc), str(tmp_path))

    assert [row.label for row in table.rows] == ["db", "cache"]
    assert [row.qualified for row in table.rows] == [".::@db", ".::@cache"]
    # `component` is resolved from the header, and it is PROVENANCE (§1.5)
    assert [row.component for row in table.rows] == ["PgDatabase", "UserCache"]
    assert table.rows[1].requires == ["db"]
    # the file list `compile_files` takes, composition-declared rather than
    # hand-listed: this is what §6 point 3's bootstrap shrink buys
    assert table.paths() == ["services.rvl", "db.rvl", "cache.rvl"]


def test_the_row_table_reaches_the_ir_and_the_manifest(tmp_path):
    """S1's deliverable: the base row table emitted into the IR and into the
    manifest, so the composition is an object the gate's output names."""
    doc = _project(tmp_path, BASE)
    document = compile_composition(str(doc), str(tmp_path))

    assert document["manifest"]["loadOrder"] == ["PgDatabase", "UserCache"]
    assert document["rows"] == document["manifest"]["rows"]
    rows = {row["label"]: row for row in document["rows"]["rows"]}
    assert rows["db"]["claims"] == [{"key": "db"}]
    assert rows["db"]["config"] == {"url": "postgres://primary:5432/app",
                                    "pool_size": 8}
    assert rows["cache"]["requires"] == ["db"]


def test_the_row_table_carries_no_absolute_path(tmp_path):
    """426 exit test 18: resolution is reproducible. Every path in the table is
    recorded relative to the project root, so two machines with the same
    composition produce a byte-identical row table."""
    doc = _project(tmp_path, BASE)
    rendered = json.dumps(resolve_file(str(doc), str(tmp_path)).to_ir())
    assert str(tmp_path) not in rendered


# ------------------------------------------------------------------ identity

def test_label_survives_a_component_rename(tmp_path):
    """426 exit test 2. Renaming a component upstream, with no other change,
    leaves the WIRING projection untouched — `component` is provenance, never
    identity (§1.4, §1.5)."""
    doc = _project(tmp_path, BASE)
    before = resolve_file(str(doc), str(tmp_path))

    (tmp_path / "db.rvl").write_text(DB.replace("PgDatabase", "PostgresDatabase"))
    after = resolve_file(str(doc), str(tmp_path))

    assert after.wiring() == before.wiring(), "a rename is a non-event"
    assert after.rows[0].label == "db"
    assert after.rows[0].component == "PostgresDatabase"


def test_label_survives_an_upstream_surface_addition(tmp_path):
    """426 exit test 1. Upstream adds a provision to a pinned component: the row
    keeps its label, every address still resolves, and the addition is LOUD (it
    shows up as an unasserted claim) rather than silent."""
    doc = _project(tmp_path, BASE)
    grown = DB.replace(
        "provides db: Database {",
        "provides db: Database, metrics: Metrics {").replace(
        "  provide db {",
        "  provide metrics { fn tick() = 1 }\n  provide db {")
    (tmp_path / "db.rvl").write_text(grown)

    table = resolve_file(str(doc), str(tmp_path))
    row = table.rows[0]
    assert row.label == "db", "the label survives; a claim-set id would not"
    assert row.claims == [("db", None)]
    assert row.extra_claims == [("metrics", None)]
    assert 'key("metrics")' in table.wiring()[".::@db"]["claims"]


def test_a_removed_provision_is_a_refusal(tmp_path):
    """426 exit test 3, and §1.4's other direction: the assertion the document
    made no longer holds, so resolution refuses naming the row, the lost key and
    the source that dropped it."""
    doc = _project(tmp_path, BASE)
    (tmp_path / "db.rvl").write_text(DB.replace("provides db: Database",
                                                "provides store: Database"))
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    message = str(excinfo.value)
    assert "@db" in message and "provides db" in message
    assert "PgDatabase" in message and "db.rvl" in message


def test_two_labels_in_one_origin_are_refused_at_parse(tmp_path):
    """426 exit test 4, first half — the same shape as a duplicate component
    name, and it fires in the PARSER, before anything is resolved."""
    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition Demo {
  row @db from "db.rvl" provides db
  row @db from "cache.rvl" provides cache
}
""", "base.rvl").parse()
    assert "duplicate row label `@db`" in str(excinfo.value)


def test_two_origins_may_declare_the_same_bare_label(tmp_path):
    """426 exit test 4, second half. `.::@db` and `acme_pg::@db` are two
    different qualified labels, so there is nothing to arbitrate and no
    squatting policy to write (§1.2)."""
    doc = _project(tmp_path, BASE)
    vendored = tmp_path / "trucs" / "acme_pg"
    vendored.mkdir(parents=True)
    (vendored / "services.rvl").write_text(SERVICES)
    (vendored / "component.rvl").write_text(DB)
    (vendored / "composition.rvl").write_text("""
composition AcmeDemo {
  row @db from "component.rvl" provides db config { url: "x" }
}
""")

    ours = resolve_file(str(doc), str(tmp_path))
    theirs = resolve_file(str(vendored / "composition.rvl"), str(tmp_path))
    assert ours.rows[0].qualified == ".::@db"
    assert theirs.rows[0].qualified == "acme_pg::@db"
    assert ours.rows[0].label == theirs.rows[0].label == "db"


def test_origin_comes_from_the_vendor_directory(tmp_path):
    """§1.2: the origin namespace is the `[trucs]` table, which is also the
    vendor directory name. Trust class is a DISTRIBUTION fact (§4.1), so it is
    read off where the document lives, never written by the document."""
    assert origin_of(str(tmp_path / "src" / "base.rvl"), str(tmp_path)) == "."
    assert origin_of(str(tmp_path / "trucs" / "acme_pg" / "layer.rvl"),
                     str(tmp_path)) == "acme_pg"


# ---------------------------------------------------------------- addressing

def test_a_row_pointing_at_nothing_is_a_refusal(tmp_path):
    """426 §2.4: an address that resolves to nothing is a REFUSAL, never a
    no-op. This is the sharpest single difference from DSH, where a patch whose
    target vanished does nothing and the operator learns at runtime."""
    doc = _project(tmp_path, """
composition Demo {
  row @db from "gone.rvl" provides db
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    assert "@db" in str(excinfo.value) and "gone.rvl" in str(excinfo.value)


def test_an_unknown_component_name_is_a_refusal_listing_what_is_there(tmp_path):
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db component Postgres config { url: "x" }
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    assert "`Postgres`" in str(excinfo.value)
    assert "`PgDatabase`" in str(excinfo.value)


def test_an_ambiguous_source_asks_for_the_component_clause(tmp_path):
    doc = _project(tmp_path, """
composition Demo {
  row @db from "both.rvl" provides db config { url: "x" }
}
""", both=DB + CACHE)
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    assert "2 components" in str(excinfo.value)
    assert "component <Name>" in str(excinfo.value)


def test_two_rows_claiming_one_key_are_refused_naming_both_rows(tmp_path):
    """G2 (provision disjointness) seen at the ROW level. The refusal names the
    rows, which is what an operator can act on after layering (§3.4); `_link`
    still runs G2 unchanged over the compiled result, so this only buys a better
    message and cannot admit anything the linker would refuse."""
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: "x" }
  row @db2 from "other.rvl" provides db config { url: "y" }
}
""", other=DB.replace("PgDatabase", "OtherDatabase"))
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    message = str(excinfo.value)
    assert '.::@db' in message and '.::@db2' in message
    assert 'key("db")' in message


def test_two_rows_in_different_realms_do_not_collide(tmp_path):
    """The sanctioned multi-provider shape: the claim set G2 keys on is
    `(key, realm)`, so two rows claiming `kv` in two realms resolve. The realm
    is read from `isolate` in the parse tree, with no body lowered."""
    realmed = """
service Kv { fn get(k: Str) -> Opt[Str] }

component TenantA provides kv: Kv {
  isolate kv in realm("tenant_a")
  let store = effect Map.new() undo store.drop()
  provide kv { fn get(k) = store.get(k) }
}

component TenantB provides kv: Kv {
  isolate kv in realm("tenant_b")
  let store = effect Map.new() undo store.drop()
  provide kv { fn get(k) = store.get(k) }
}
"""
    doc = _project(tmp_path, """
composition Tenants {
  row @a from "tenants.rvl" provides kv component TenantA
  row @b from "tenants.rvl" provides kv component TenantB
}
""", tenants=realmed)
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[0].claims == [("kv", "tenant_a")]
    assert table.rows[1].claims == [("kv", "tenant_b")]
    assert table.wiring()[".::@a"]["claims"] == ['key("kv", realm: "tenant_a")']


# -------------------------------------------------------------------- config

def test_a_wrongly_typed_config_value_does_not_admit(tmp_path):
    """426 exit test 9 / §3.2, at the base composition. In DSH the equivalent
    typo reaches runtime; here it is a refusal naming the field and the declared
    type, before any body is lowered."""
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: 7 }
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    message = str(excinfo.value)
    assert "`url`" in message and "`Str`" in message and "`Int`" in message


def test_an_unknown_config_field_is_a_refusal_listing_the_real_ones(tmp_path):
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: "x", timeout: 5 }
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    assert "`timeout` is not a config field" in str(excinfo.value)
    assert "`url`, `pool_size`" in str(excinfo.value)


def test_missing_required_config_is_a_refusal(tmp_path):
    """A config field with no default must be supplied by the composition — the
    same rule `load C with { ... }` already enforces (lower.py:4197)."""
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    assert "missing required config `url`" in str(excinfo.value)


# ------------------------------------------------- item 424 R2: `granted`

def test_granted_defaults_to_empty_and_an_ungranted_require_is_refused(tmp_path):
    """424 R2 / slice A1, completing 426 §9.3 Part 2. `granted` is what
    `AdmissionProfile.untrusted_author(granted)` takes, and something has to
    produce it. It defaults to EMPTY, never to "whatever the row requires", so a
    row cannot grant itself authority by needing it."""
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: "x" }
  row @cache from "cache.rvl" provides cache granted { }
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    message = str(excinfo.value)
    assert "@cache" in message and "requires `db`" in message
    assert "granted" in message


def test_a_granted_row_whose_requires_are_listed_admits(tmp_path):
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: "x" }
  row @cache from "cache.rvl" provides cache granted { db }
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[1].granted == ["db"]
    assert table.rows[1].to_ir()["granted"] == ["db"]


def test_an_empty_granted_on_a_row_that_requires_nothing_admits(tmp_path):
    """424 §1.3 slice A1's exit test, third clause."""
    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: "x" } granted { }
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[0].granted == []


def test_a_row_without_a_granted_clause_is_unconfined(tmp_path):
    """The clause is absent, not empty: wiring the untrusted-author profile per
    row is 426 S4 and waits on 425 F1's decision, so an unwritten clause must
    not silently confine a first-party row."""
    doc = _project(tmp_path, BASE)
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[1].granted is None
    assert "granted" not in table.rows[1].to_ir()


# ------------------------------------------------------------------- grammar

def test_a_row_must_assert_what_it_claims(tmp_path):
    """§1.3: the assertion is what makes the document unable to lie about the
    wiring, so it is required. A sink row writes `provides nothing`, which is
    the ordinary case under a label scheme, not a special one."""
    with pytest.raises(RevlError) as excinfo:
        Parser('composition Demo {\n  row @db from "db.rvl"\n}\n', "base.rvl").parse()
    assert "expected `provides`" in str(excinfo.value)

    program = Parser(
        'composition Demo {\n  row @routes from "r.rvl" provides nothing\n}\n',
        "base.rvl").parse()
    assert program.compositions[0].rows[0].claims == []


def test_a_layer_clause_is_not_grammar_yet(tmp_path):
    """`open`, `reach`, `place` and `variant` are S2/S5 surface. Until they are
    built, writing one is a parse error rather than a silently ignored clause —
    the fail-closed direction."""
    for clause in ("open { max_steps }", "reach { pg_connect: host(\"x\") }",
                   "place @db on process \"provider\""):
        with pytest.raises(RevlError):
            Parser(f'composition Demo {{\n  row @db from "db.rvl" provides db '
                   f'{clause}\n}}\n', "base.rvl").parse()


def test_composition_is_a_contextual_keyword():
    """`composition` heads a declaration only in the shape `composition NAME {`,
    so a program that already uses it as an ordinary name still compiles and the
    self-hosted lexer's KEYWORDS table needs no sync."""
    program = Parser("fn composition(x: Int) -> Int { return x }\n",
                     "m.rvl").parse()
    assert [fn.name for fn in program.fn_decls] == ["composition"]
    assert program.compositions == []


def test_one_composition_per_document(tmp_path):
    """The document IS the composition's origin scope (§1.2)."""
    doc = _project(tmp_path, "composition A { }\ncomposition B { }\n")
    with pytest.raises(RevlError) as excinfo:
        resolve_file(str(doc), str(tmp_path))
    assert "2 compositions" in str(excinfo.value)


# ----------------------------------------------------------------------- CLI

def test_cli_renders_the_row_table_header_only(tmp_path, capsys):
    from revl.__main__ import main

    doc = _project(tmp_path, BASE)
    assert main(["composition", str(doc), "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "COMPOSITION  Demo" in out
    assert ".::@db" in out and "PgDatabase" in out
    assert "header-only: no component body was lowered" in out


def test_cli_admit_compiles_the_rows(tmp_path, capsys):
    from revl.__main__ import main

    doc = _project(tmp_path, BASE)
    assert main(["composition", str(doc), "--root", str(tmp_path),
                 "--admit", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["composition"] == "Demo"
    assert out["loadOrder"] == ["PgDatabase", "UserCache"]


def test_cli_reports_a_refusal_without_a_traceback(tmp_path, capsys):
    from revl.__main__ import main

    doc = _project(tmp_path, """
composition Demo {
  row @db from "db.rvl" provides db config { url: 7 }
}
""")
    assert main(["composition", str(doc), "--root", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err


def test_the_formatter_keeps_a_row_label_tight(tmp_path):
    """`revl fmt` is token-based, and a bare `@` piece exists only as a row
    label's sigil (a host body is scanned as one verbatim span). It must not
    become `@ db`: that still lexes, so the formatter's IR gate would not catch
    it, and the row table must survive a format unchanged."""
    from revl.formatter import format_source

    doc = _project(tmp_path, BASE)
    before = resolve_file(str(doc), str(tmp_path)).to_ir()

    formatted = format_source(doc.read_text(), str(doc))
    assert "@db" in formatted and "@ db" not in formatted
    doc.write_text(formatted)
    assert resolve_file(str(doc), str(tmp_path)).to_ir() == before
    assert format_source(formatted, str(doc)) == formatted, "idempotent"
