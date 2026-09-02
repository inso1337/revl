"""Composition layers: the fold (roadmap item 426, slice S2).

The design is `docs/design/426-composition-layers.md`; the exit tests it names
are in §12 and the slice split is §11. S2 is "the fold, buildable after S1,
depends on nothing new": the four operations (§3.2), the four levels (§3.1),
the pure fold (§3.3), peer refusal with layer provenance (§3.4), address
resolution with the two spellings (§2.3) and refusal-never-no-op (§2.4), and
`revl layer check` over headers only.

Which of 426 §12's exit tests this file covers:

  5  an address resolving to nothing   COVERED for a layer's `key(...)` and
                                       label addresses, which is the half S1
                                       could not reach
  6  peer conflicts refuse             COVERED, including that permuting the
                                       stack changes NOTHING — not the verdict,
                                       not the message, not the file and line
  7  site resolution works             COVERED
  8  the fold never calls the gate     COVERED for the order half:
                                       `remove`-then-`add` and
                                       `add`-then-`remove` agree. The soundness
                                       half is structural — `_link` runs G2/G3
                                       over the compiled result unchanged.
  9  `configure` is typed              COVERED, including `configure` against a
                                       non-config row
 12  header-only checking              COVERED (`revl layer check`)

Item 424 R2's third rule (`granted` is never writable in a stack layer), which
S1 could not enforce because it needs layers, is covered here.

 10, 11             need S3 (incremental admission)
 13, 14, 15, 16, 17 need S4/S5/S6 (confinement, the panel, distribution)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402
from revl.composition import resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402

SERVICES = """
service Database { fn query(sql: Str) -> Str }
service Cache    { fn get(key: Str) -> Str }
service Metrics  { fn tick() -> Int }
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
component MemCache provides cache: Cache {
  provide cache { fn get(key) = key }
}
"""

METRICS = """
use "services.rvl" { }
component LocalMetrics provides metrics: Metrics {
  provide metrics { fn tick() = 1 }
}
"""

OTEL = """
use "services.rvl" { }
component OtelMetrics provides metrics: Metrics {
  provide metrics { fn tick() = 2 }
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


def _project(tmp_path: Path, *stack: str, site: str | None = None,
             **layers: str) -> Path:
    """A project with the four sources, a base composition naming `stack`
    (and optionally `site`), and each named layer written to `layers/`."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "services.rvl").write_text(SERVICES)
    (tmp_path / "sqlite.rvl").write_text(SQLITE)
    (tmp_path / "postgres.rvl").write_text(POSTGRES)
    (tmp_path / "cache.rvl").write_text(CACHE)
    (tmp_path / "metrics.rvl").write_text(METRICS)
    (tmp_path / "otel.rvl").write_text(OTEL)
    (tmp_path / "layers").mkdir(exist_ok=True)
    for name, text in layers.items():
        (tmp_path / "layers" / f"{name}.rvl").write_text(text)
    clauses = "".join(f'  stack "layers/{name}.rvl"\n' for name in stack)
    if site is not None:
        clauses += f'  site "layers/{site}.rvl"\n'
    doc = tmp_path / "base.rvl"
    doc.write_text(BASE % clauses)
    return doc


def _labels(table) -> list[str]:
    return [row.qualified for row in table.rows]


PG_SWAP = """
layer PgSwap for Demo {
  replace key("db") with row @db from "../postgres.rvl" provides db
    config { url: "postgres://primary:5432/app" }
}
"""


# ----------------------------------------------------------- the four operations

def test_replace_swaps_the_component_and_keeps_the_label(tmp_path):
    """426 §3.2: the LABEL is preserved — the replacement is a new
    implementation of the same row, not a new row."""
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    table = resolve_file(str(doc), str(tmp_path))

    assert _labels(table) == [".::@db", ".::@cache"]
    assert table.rows[0].component == "PgDatabase"
    assert table.rows[0].config == {"url": "postgres://primary:5432/app"}
    assert table.rows[0].provenance == [(0, "<base>", "row"),
                                        (1, "PgSwap", "replace")]


def test_add_introduces_a_row_and_remove_withdraws_one(tmp_path):
    doc = _project(tmp_path, "obs", obs="""
layer Obs for Demo {
  add row @metrics from "../metrics.rvl" provides metrics
  remove key("cache")
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert _labels(table) == [".::@db", ".::@metrics"]


def test_configure_merges_into_the_rows_config(tmp_path):
    doc = _project(tmp_path, "tune", tune="""
layer Tune for Demo {
  configure @db with { pool: 32 }
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    # the base's `url` survives: `configure` MERGES, it does not overwrite the
    # block
    assert table.rows[0].config == {"url": "sqlite://local", "pool": 32}


def test_replace_must_preserve_the_claim_set(tmp_path):
    """426 §3.2's second determinism lever: a replacement claiming exactly what
    it replaced can never create or destroy a provision conflict."""
    doc = _project(tmp_path, "widen", widen="""
layer Widen for Demo {
  replace key("db") with row @db from "../cache.rvl" provides cache
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "claiming a different set" in str(caught.value)
    assert "remove` plus `add" in str(caught.value)


def test_there_is_no_positional_operation(tmp_path):
    """No `insert before`, no priority, no ordering: load order is derived from
    the wiring, so a position operation would invent a concept the gate does
    not have (426 §3.2)."""
    doc = _project(tmp_path, "pos", pos="""
layer Pos for Demo {
  insert @db before @cache
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "four operations and no more" in str(caught.value)


# ------------------------------------------------------------------ addressing

def test_a_key_address_that_resolves_to_nothing_refuses(tmp_path):
    """426 exit test 5, the half S1 could not reach. Never a no-op: the refusal
    names the address, the layer, and what is there instead."""
    doc = _project(tmp_path, "ghost", ghost="""
layer Obs for Demo {
  configure key("logger") with { level: "debug" }
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    message = str(caught.value)
    assert 'layer `Obs` addresses key("logger")' in message
    assert 'key("db")' in message, "what IS claimed is named"


def test_a_label_address_that_resolves_to_nothing_refuses(tmp_path):
    doc = _project(tmp_path, "ghost", ghost="""
layer Obs for Demo {
  remove @logger
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "addresses row `@logger`" in str(caught.value)


def test_both_spellings_reach_the_same_row(tmp_path):
    """426 §2.3: `key("db")` follows the contract, `.::@db` names the row. Two
    layers using the two spellings on one row are seen as one target, which is
    what makes the peer rules meaningful."""
    doc = _project(tmp_path, "a", "b", a="""
layer A for Demo { configure key("db") with { pool: 1 } }
""", b="""
layer B for Demo { configure .::@db with { pool: 2 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "layer conflict on row `.::@db`" in str(caught.value)


def test_a_realm_is_part_of_the_address(tmp_path):
    """`key("kv", realm: "a")` is a different address from `key("kv")`, so it
    resolves to nothing here rather than silently hitting the shared row."""
    doc = _project(tmp_path, "r", r="""
layer R for Demo { configure key("db", realm: "tenant_a") with { pool: 1 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert 'key("db", realm: "tenant_a")' in str(caught.value)


# ------------------------------------------------------------- peer conflicts

CORP_SWAP = """
layer CorpSqlite for Demo {
  replace key("db") with row @db from "../sqlite.rvl" provides db
    config { url: "sqlite://corp" }
}
"""


def test_two_stack_layers_replacing_one_row_refuses(tmp_path):
    """426 exit test 6. Neither layer is preferred: precedence never chooses a
    provider (decision 4)."""
    doc = _project(tmp_path, "pg", "corp", pg=PG_SWAP, corp=CORP_SWAP)
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    message = str(caught.value)
    assert "`CorpSqlite` and `PgSwap` both replace it" in message
    assert "neither layer is preferred" in message


def test_permuting_the_stack_changes_nothing(tmp_path):
    """426 exit test 6's second half, and the property the whole design turns
    on: not the verdict, not the message, and not the file and line it is
    reported at."""
    # one project, two orderings of the same two layers, so nothing but the
    # order can differ.
    doc = _project(tmp_path, "pg", "corp", pg=PG_SWAP, corp=CORP_SWAP)
    with pytest.raises(RevlError) as first:
        resolve_file(str(doc), str(tmp_path))

    doc.write_text(doc.read_text().replace(
        '  stack "layers/pg.rvl"\n  stack "layers/corp.rvl"\n',
        '  stack "layers/corp.rvl"\n  stack "layers/pg.rvl"\n'))
    with pytest.raises(RevlError) as second:
        resolve_file(str(doc), str(tmp_path))

    assert str(first.value) == str(second.value)


def test_two_adds_claiming_one_key_refuse(tmp_path):
    doc = _project(tmp_path, "a", "b", a="""
layer AcmeM for Demo { add row @m_acme from "../metrics.rvl" provides metrics }
""", b="""
layer CorpM for Demo { add row @m_corp from "../otel.rvl" provides metrics }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    message = str(caught.value)
    assert 'layer conflict on key("metrics")' in message
    assert "resolve key(\"metrics\") to" in message, "the remedy is spelled out"


def test_disjoint_configures_of_one_row_merge(tmp_path):
    """426 §3.4: two stack layers configuring one row's DISJOINT fields is a
    merge, and it is commutative."""
    doc = _project(tmp_path, "a", "b", a="""
layer A for Demo { configure @db with { pool: 16 } }
""", b="""
layer B for Demo { configure @db with { url: "sqlite://b" } }
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[0].config == {"url": "sqlite://b", "pool": 16}


def test_the_same_field_set_two_ways_refuses(tmp_path):
    doc = _project(tmp_path, "a", "b", a="""
layer A for Demo { configure @db with { pool: 16 } }
""", b="""
layer B for Demo { configure @db with { pool: 32 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "set config field `pool`" in str(caught.value)


def test_two_removes_of_one_row_are_idempotent(tmp_path):
    doc = _project(tmp_path, "a", "b", a="""
layer A for Demo { remove key("cache") }
""", b="""
layer B for Demo { remove @cache }
""")
    assert _labels(resolve_file(str(doc), str(tmp_path))) == [".::@db"]


def test_a_remove_and_a_patch_of_one_row_refuse(tmp_path):
    doc = _project(tmp_path, "a", "b", a="""
layer Drop for Demo { remove key("db") }
""", b="""
layer Tune for Demo { configure key("db") with { pool: 8 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "withdraw and configure it" in str(caught.value)


# ----------------------------------------------------------- the site layer

def test_the_site_layer_resolves_a_peer_conflict(tmp_path):
    """426 exit test 7: the same pair resolves when the site layer names both
    sides, and the resolved provider is the one named."""
    doc = _project(tmp_path, "a", "b", site="ops", a="""
layer AcmeM for Demo { add row @m_acme from "../metrics.rvl" provides metrics }
""", b="""
layer CorpM for Demo { add row @m_corp from "../otel.rvl" provides metrics }
""", ops="""
site layer Ops for Demo {
  resolve key("metrics") to .::@m_acme over .::@m_corp
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert _labels(table) == [".::@db", ".::@cache", ".::@m_acme"]
    assert table.rows[2].component == "LocalMetrics"
    assert table.rows[2].provenance == [(1, "AcmeM", "add"),
                                        (2, "Ops", "resolve")]


def test_the_site_layer_wins_over_a_stack_layer(tmp_path):
    """The operator is not a peer of the layers; the operator is the person the
    refusal is shown to (426 §3.4)."""
    doc = _project(tmp_path, "pg", site="ops", pg=PG_SWAP, ops="""
site layer Ops for Demo {
  configure @db with { pool: 64 }
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[0].config["pool"] == 64
    assert table.rows[0].provenance[-1] == (2, "Ops", "configure")


def test_resolve_is_refused_in_a_stack_layer(tmp_path):
    doc = _project(tmp_path, "a", a="""
layer A for Demo { resolve key("db") to .::@db over .::@cache }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "`resolve` is a SITE layer operation" in str(caught.value)


def test_a_resolve_that_decides_nothing_refuses(tmp_path):
    """An operator who wrote one believed there was a conflict. If there is
    not, that belief is worth correcting rather than silently honouring."""
    doc = _project(tmp_path, site="ops", ops="""
site layer Ops for Demo {
  resolve key("db") to .::@cache over .::@db
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "did not decide anything" in str(caught.value)


def test_two_site_layers_are_refused(tmp_path):
    doc = _project(tmp_path)
    doc.write_text(doc.read_text().replace(
        "  row @cache", '  site "layers/a.rvl"\n  site "layers/b.rvl"\n  row @cache'))
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "declares a second `site` layer" in str(caught.value)


# ---------------------------------------------------------------- determinism

def test_remove_then_add_and_add_then_remove_agree(tmp_path):
    """426 exit test 8's order half. Admitting per operation would make one
    order succeed and the other refuse on provision disjointness at an
    intermediate state; the fold applies withdrawals before admissions, so that
    state never exists."""
    swap = """
layer Swap for Demo {
  %s
}
"""
    remove_first = swap % ('remove key("db")\n  add row @db2 from '
                           '"../postgres.rvl" provides db\n    '
                           'config { url: "postgres://x" }')
    add_first = swap % ('add row @db2 from "../postgres.rvl" provides db\n    '
                        'config { url: "postgres://x" }\n  remove key("db")')

    one = _project(tmp_path / "a", "swap", swap=remove_first)
    two = _project(tmp_path / "b", "swap", swap=add_first)
    first = resolve_file(str(one), str(tmp_path / "a"))
    second = resolve_file(str(two), str(tmp_path / "b"))

    assert _labels(first) == _labels(second) == [".::@cache", ".::@db2"]
    assert first.wiring() == second.wiring()


def test_an_add_over_a_claim_the_base_holds_refuses(tmp_path):
    """Without a matching `remove` in the same delta, an addition that collides
    with a row already there is a refusal naming the row it collides with."""
    doc = _project(tmp_path, "a", a="""
layer A for Demo {
  add row @db2 from "../postgres.rvl" provides db
    config { url: "postgres://x" }
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "row `.::@db` already claims" in str(caught.value)


def test_the_folded_table_carries_no_absolute_path(tmp_path):
    """426 exit test 18, extended to the fold: two machines folding the same
    composition and layers produce a byte-identical table."""
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    rendered = json.dumps(resolve_file(str(doc), str(tmp_path)).to_ir())
    assert str(tmp_path) not in rendered


def test_a_composition_with_no_layers_is_unchanged(tmp_path):
    """The S1 document, byte for byte: `provenance` appears only once a layer
    has actually touched a row."""
    doc = _project(tmp_path)
    document = resolve_file(str(doc), str(tmp_path)).to_ir()
    assert all("provenance" not in row for row in document["rows"])


# ------------------------------------------------------------------- typed config

def test_a_wrongly_typed_configure_does_not_admit(tmp_path):
    """426 exit test 9: the refusal names the field and the declared type."""
    doc = _project(tmp_path, "typo", typo="""
layer Typo for Demo { configure key("db") with { pool: "big" } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    message = str(caught.value)
    assert "config field `pool`" in message and "declared `Int`" in message


def test_configure_of_an_unknown_field_refuses(tmp_path):
    doc = _project(tmp_path, "typo", typo="""
layer Typo for Demo { configure key("db") with { poool: 4 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "`poool` is not a config field" in str(caught.value)


def test_configure_of_a_non_config_row_refuses(tmp_path):
    """426 exit test 9's second half. A patch system that happily writes a key
    nothing reads is how a layered composition breaks silently."""
    doc = _project(tmp_path, "cfg", cfg="""
layer Cfg for Demo { configure key("cache") with { size: 10 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "declares no config" in str(caught.value)


# ------------------------------------------------- what a layer document may be

def test_a_layer_may_not_declare_a_component(tmp_path):
    """426 §6.1. Without this rule a layer is a component-authoring surface,
    which is the surface the confinement slice exists to profile."""
    doc = _project(tmp_path, "evil", evil="""
component Sneak provides x: Cache { provide x { fn get(k) = k } }
layer Evil for Demo { remove key("cache") }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "ONLY layer operations" in str(caught.value)


def test_a_missing_layer_file_refuses(tmp_path):
    doc = _project(tmp_path, "absent")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "which does not exist" in str(caught.value)


def test_a_layer_patching_another_composition_refuses(tmp_path):
    doc = _project(tmp_path, "other", other="""
layer Other for Elsewhere { remove key("cache") }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "patches composition `Elsewhere`" in str(caught.value)


def test_a_layer_beside_the_composition_refuses(tmp_path):
    """A layer in the base document would be silently unapplied, and 426 §2.4
    has no silent no-ops."""
    doc = _project(tmp_path)
    doc.write_text(doc.read_text() + "\nlayer Stray for Demo { remove @cache }\n")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "and also layer `Stray`" in str(caught.value)


def test_configure_with_a_brace_names_the_host_body_trap(tmp_path):
    """The finding S1 recorded, now a diagnostic: `@db {` never reaches the
    parser as three tokens — the lexer consumed it as a verbatim host body."""
    doc = _project(tmp_path, "old", old="""
layer Old for Demo { configure @db { pool: 4 } }
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    message = str(caught.value)
    assert "is a HOST BODY, not a row address" in message
    assert "configure @db with { ... }" in message


# ------------------------------------------------------------------- `granted`

def test_a_stack_layer_may_not_write_granted(tmp_path):
    """Item 424 R2's third rule, the one that needed layers to exist: no layer
    raises its own authority."""
    doc = _project(tmp_path, "sneaky", sneaky="""
layer Sneaky for Demo {
  add row @metrics from "../metrics.rvl" provides metrics
    granted { db }
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "writes a `granted` clause" in str(caught.value)


def test_the_site_layer_may_write_granted(tmp_path):
    doc = _project(tmp_path, site="ops", ops="""
site layer Ops for Demo {
  add row @metrics from "../metrics.rvl" provides metrics
    granted { }
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.rows[-1].granted == []


# --------------------------------------------------------------------- touches

def test_touches_is_enforced(tmp_path):
    doc = _project(tmp_path, "t", t="""
layer T for Demo {
  touches key("db")
  configure @cache with { size: 1 }
}
""")
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path))
    assert "`touches` clause does not list" in str(caught.value)


def test_touches_admits_what_it_lists(tmp_path):
    doc = _project(tmp_path, "t", t="""
layer T for Demo {
  touches key("db")
  configure @db with { pool: 12 }
}
""")
    assert resolve_file(str(doc), str(tmp_path)).rows[0].config["pool"] == 12


# ----------------------------------------------------------- invocation overlay

def test_the_overlay_carries_values(tmp_path):
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    table = resolve_file(str(doc), str(tmp_path), {("db", "pool"): 99})
    assert table.rows[0].config["pool"] == 99
    assert table.rows[0].provenance[-1] == (3, "<invocation>", "configure")


def test_the_overlay_is_typed_like_every_other_config_value(tmp_path):
    doc = _project(tmp_path)
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path), {("db", "pool"): "lots"})
    assert "declared `Int`" in str(caught.value)


def test_the_overlay_cannot_name_a_row_that_is_not_there(tmp_path):
    """Values only, never structure: the overlay reaches a field of a row that
    already exists (426 §3.1)."""
    doc = _project(tmp_path)
    with pytest.raises(RevlError) as caught:
        resolve_file(str(doc), str(tmp_path), {("ghost", "pool"): 1})
    assert "is not in the composition" in str(caught.value)


# -------------------------------------------------------------------- the CLI

def test_layer_check_renders_the_fold_header_only(tmp_path, capsys):
    """426 exit test 12: every row id resolves and the whole wiring renders
    with no component body lowered."""
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    assert main(["layer", "check", str(doc), "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "PgDatabase" in out
    assert "replace by `PgSwap` (L1)" in out
    assert "header-only: no component body was lowered" in out


def test_layer_check_json_carries_the_provenance(tmp_path, capsys):
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    assert main(["layer", "check", str(doc), "--root", str(tmp_path),
                 "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert rows[0]["provenance"][-1] == {"level": 1, "layer": "PgSwap",
                                         "op": "replace"}


def test_layer_check_exits_nonzero_on_a_refusal(tmp_path, capsys):
    doc = _project(tmp_path, "pg", "corp", pg=PG_SWAP, corp=CORP_SWAP)
    assert main(["layer", "check", str(doc), "--root", str(tmp_path)]) == 1
    assert "layer conflict" in capsys.readouterr().err


def test_composition_set_is_the_invocation_overlay(tmp_path, capsys):
    doc = _project(tmp_path)
    assert main(["composition", str(doc), "--root", str(tmp_path), "--json",
                 "--set", "@db.pool=77"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert rows[0]["config"]["pool"] == 77


def test_a_malformed_set_is_refused(tmp_path, capsys):
    doc = _project(tmp_path)
    assert main(["composition", str(doc), "--root", str(tmp_path),
                 "--set", "db.pool"]) == 1
    assert "is not `@row.field=value`" in capsys.readouterr().err


# ------------------------------------------------------------ admission is real

def test_admit_compiles_the_folded_rows(tmp_path, capsys):
    """The fold produces the file list `compile_files` already takes, so
    `_link` runs G2 and G3 over the PATCHED composition unchanged — which is
    what keeps the fold off the trusted path (426 §3.3)."""
    doc = _project(tmp_path, "pg", pg=PG_SWAP)
    assert main(["composition", str(doc), "--root", str(tmp_path),
                 "--admit"]) == 0
    out = capsys.readouterr().out
    assert "ADMITTED" in out and "PgDatabase" in out
    assert "SqliteDatabase" not in out
