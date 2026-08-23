"""Tests for the canonical formatter and its IR-equivalence gate (item 35).

`revl fmt` produces a canonical formatting and admits it only when compiling
the original and the formatted text yields byte-identical IR.  These tests
cover idempotence, the messy-to-canonical path with identical IR, that the
gate refuses a meaning-changing rewrite, and the retrofit onto `--migrate`.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.formatter import (  # noqa: E402
    format_source,
    ir_equivalent,
    _canonical_ir,
)
from revl.__main__ import main  # noqa: E402

EXAMPLES = sorted(glob.glob(str(ROOT / "examples" / "**" / "*.rvl"), recursive=True))


# --------------------------------------------------------------------------
# Canonical formatting: idempotence and IR preservation across the corpus
# --------------------------------------------------------------------------

def test_formatter_is_idempotent_on_corpus():
    for path in EXAMPLES:
        src = Path(path).read_text()
        once = format_source(src, path)
        twice = format_source(once, path)
        assert once == twice, f"not idempotent: {path}"


def test_gate_admits_every_example():
    # Every shipped example must survive the gate: either IR byte-identical,
    # newly admissible, or (for the intentionally-rejected files) token-stream
    # identical -- never refused.
    for path in EXAMPLES:
        src = Path(path).read_text()
        out = format_source(src, path)
        result = ir_equivalent(src, out, path)
        assert result.admitted, f"gate refused {path}: {result.reason}"


def test_messy_program_formats_to_canonical_with_identical_ir():
    messy = (
        "component   RegistryStore   provides reg: Registry {\n"
        "      let store = effect Map.new()    undo store.drop()\n"
        "  provide reg {\n"
        "        fn lookup(id)   =   store.get(id)\n"
        "fn healthy()=true\n"
        "  }\n"
        "}\n"
    )
    prelude = "service Registry {\n  fn lookup(id: Str) -> Opt[Str]\n  fn healthy() -> Bool\n}\n"
    src = prelude + "\n" + messy

    out = format_source(src, "registry.rvl")

    # Canonical: two-space indentation, single spaces, no trailing blanks.
    assert "      let store" not in out
    assert "fn healthy() = true" in out
    assert "fn lookup(id) = store.get(id)" in out
    assert out.endswith("}\n")

    # The gate proves the reformat changed nothing the compiler sees.
    result = ir_equivalent(src, out, "registry.rvl")
    assert result.admitted
    assert result.proof == "IR byte-identical"
    assert _canonical_ir(compile_source(src)) == _canonical_ir(compile_source(out))


def test_format_is_stable_second_run():
    src = Path(EXAMPLES[0]).read_text()
    out = format_source(src)
    assert format_source(out) == out


# --------------------------------------------------------------------------
# The self-proving gate refuses a meaning-changing rewrite
# --------------------------------------------------------------------------

def test_gate_refuses_meaning_changing_rewrite():
    # A hand-constructed "rewrite" that renames a method call: this is exactly
    # what the formatter must never do, and the gate must catch it.
    original = (
        "service Registry {\n"
        "  fn lookup(id: Str) -> Opt[Str]\n"
        "  fn healthy() -> Bool\n"
        "}\n\n"
        "component RegistryStore provides reg: Registry {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide reg {\n"
        "    fn lookup(id) = store.get(id)\n"
        "    fn healthy() = true\n"
        "  }\n"
        "}\n"
    )
    # Change the healthy() result from `true` to `false` -- compiles fine but
    # the IR differs.
    tampered = original.replace("fn healthy() = true", "fn healthy() = false")

    result = ir_equivalent(original, tampered, "registry.rvl")
    assert not result.admitted
    assert "IR changed" in result.reason


def test_gate_refuses_rewrite_that_breaks_compilation():
    original = (
        "service Registry {\n"
        "  fn healthy() -> Bool\n"
        "}\n\n"
        "component RegistryStore provides reg: Registry {\n"
        "  provide reg {\n"
        "    fn healthy() = true\n"
        "  }\n"
        "}\n"
    )
    broken = original.replace("fn healthy() = true", "fn healthy() = nonexistent_call()")
    result = ir_equivalent(original, broken, "registry.rvl")
    assert not result.admitted


def test_gate_reports_unchanged_for_noop():
    src = "service K {\n  fn get() -> Bool\n}\n"
    result = ir_equivalent(src, src, "k.rvl")
    assert result.admitted
    assert result.proof == "unchanged"


# --------------------------------------------------------------------------
# CLI wiring: default formatting, --check, and the --migrate retrofit
# --------------------------------------------------------------------------

def test_cli_formats_in_place(tmp_path):
    f = tmp_path / "reg.rvl"
    f.write_text(
        "service Registry {\n  fn healthy() -> Bool\n}\n\n"
        "component S provides reg: Registry {\n"
        "  provide reg {\n    fn healthy()=true\n  }\n}\n"
    )
    assert main(["fmt", str(f)]) == 0
    assert "fn healthy() = true" in f.read_text()
    # Second run is a no-op (already canonical).
    before = f.read_text()
    assert main(["fmt", str(f)]) == 0
    assert f.read_text() == before


def test_cli_check_flags_noncanonical(tmp_path):
    f = tmp_path / "reg.rvl"
    f.write_text(
        "service Registry {\n  fn healthy() -> Bool\n}\n\n"
        "component S provides reg: Registry {\n"
        "  provide reg {\n    fn healthy()=true\n  }\n}\n"
    )
    original = f.read_text()
    assert main(["fmt", "--check", str(f)]) == 1
    assert f.read_text() == original  # --check never writes


MIGRATE_PROGRAM = (
    "service Database {\n"
    "  emission fn execute(sql: Str) -> Int\n"
    "}\n\n"
    "service Cache {\n"
    "  emission fn put(key: Str, value: Str)\n"
    "}\n\n"
    "component UserCache requires db: Database provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache {\n"
    "    fn put(key, value) {\n"
    "      effect store.insert(key, value)\n"
    "      undo store.remove(key)\n"
    '      emit db.execute("INSERT INTO cache_log VALUES ($key)")\n'
    "    }\n"
    "  }\n"
    "}\n"
)


def test_cli_migrate_still_works_and_is_gated(tmp_path):
    # The 1.x source does not compile (the 2.0 lexer rejects the legacy `$`);
    # after migration it does, so the gate admits it as newly admissible.
    f = tmp_path / "cache.rvl"
    f.write_text(MIGRATE_PROGRAM)
    assert main(["fmt", "--migrate", str(f)]) == 0
    text = f.read_text()
    assert "emit db.execute(`INSERT INTO cache_log VALUES (${key})`)" in text
    # Migrated output now compiles.
    compile_source(text, "cache.rvl")


def test_migrate_gate_holds_on_identity():
    # A file with no legacy `$` migrates to itself; the gate proves IR identity.
    src = "service K {\n  fn get() -> Bool\n}\n"
    from revl.fmt import migrate_source

    migrated, _ = migrate_source(src, "k.rvl")
    assert migrated == src
    assert ir_equivalent(src, migrated, "k.rvl").admitted
