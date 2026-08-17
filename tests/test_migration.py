"""Migration §9 tests: `revl fmt --migrate` and the new template syntax."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import migrate_source  # noqa: E402
from revl.__main__ import main  # noqa: E402


def test_migrate_string_interpolation():
    source = 'emit db.execute("INSERT INTO cache_log VALUES ($key)")\n'
    migrated, warnings = migrate_source(source, "user_cache.rvl")
    assert migrated == 'emit db.execute(`INSERT INTO cache_log VALUES (${key})`)\n'
    assert warnings == []


def test_migrate_doubled_dollar_only_becomes_plain_string():
    source = 'emit bus.send("cost: $$9.99")\n'
    migrated, warnings = migrate_source(source, "pricer.rvl")
    assert migrated == 'emit bus.send("cost: $9.99")\n'
    assert warnings == []


def test_migrate_is_idempotent():
    source = (
        'emit db.execute("INSERT INTO cache_log VALUES ($key)")\n'
        'emit bus.send("cost: $$9.99")\n'
    )
    once, _ = migrate_source(source, "mixed.rvl")
    twice, warnings = migrate_source(once, "mixed.rvl")
    assert twice == once
    assert warnings == []


def test_migrate_skips_backtick_templates():
    source = 'emit db.execute(`INSERT INTO cache_log VALUES (${key})`)\n'
    migrated, warnings = migrate_source(source, "already.rvl")
    assert migrated == source
    assert warnings == []


def test_fmt_migrate_cli_rewrites_in_place(tmp_path):
    path = tmp_path / "user_cache.rvl"
    path.write_text('emit db.execute("INSERT INTO cache_log VALUES ($key)")\n', encoding="utf-8")

    assert main(["fmt", "--migrate", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == 'emit db.execute(`INSERT INTO cache_log VALUES (${key})`)\n'


def test_fmt_migrate_cli_is_idempotent(tmp_path):
    path = tmp_path / "mixed.rvl"
    path.write_text(
        'emit db.execute("INSERT INTO cache_log VALUES ($key)")\n'
        'emit bus.send("cost: $$9.99")\n',
        encoding="utf-8",
    )

    assert main(["fmt", "--migrate", str(path)]) == 0
    first = path.read_text(encoding="utf-8")
    assert main(["fmt", "--migrate", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == first
