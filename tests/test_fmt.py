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
# Operator spacing: compound assignment, prefix-unary, and unary sign
# (issue 545: the formatter split `x += 1` -> `x + = 1`, `!x` -> `! x`,
#  and unary `-1` -> `- 1`).  The lexer has no compound-assign / unary token,
# so the token stream is identical either way and the IR gate cannot see the
# mangling -- these tests pin the SPACING directly.
# --------------------------------------------------------------------------

# (messy source, expected substring in the canonical output).
_SPACING_CASES = [
    # compound assignment binds the operator to its `=`
    ("fn f() { var x = 0  x + = 1 }", "x += 1"),
    ("fn f() { var x = 0  x -= 1 }", "x -= 1"),
    ("fn f() { var x = 2  x * = 2 }", "x *= 2"),
    ("fn f() { var x = 2  x / = 2 }", "x /= 2"),
    ("fn f() { var x = 2  x % = 2 }", "x %= 2"),
    # prefix-only operators bind tight to their operand
    ("fn f(a) = ! a", "= !a"),
    ("fn f() = ~ 1", "= ~1"),
    # unary sign binds tight to the value it signs, in every position
    ("fn f() = - 1", "= -1"),
    ("fn f(b) = - b", "= -b"),
    ("fn f(a) = f(- 1)", "f(-1)"),
    ("fn f() = [- 1]", "[-1]"),
    ("fn f(a) = a + - 1", "a + -1"),
    # ...but the BINARY operator keeps its spaces
    ("fn f(a) = a - 1", "a - 1"),
    ("fn f(a) = a + 1", "a + 1"),
    ("fn g(xs, i) = xs[i - 1]", "xs[i - 1]"),
    ("fn h() = g() - 1", "g() - 1"),
]


def test_operator_spacing_is_canonical():
    for messy, want in _SPACING_CASES:
        out = format_source(messy, "spacing.rvl")
        assert want in out, f"{want!r} not in {out!r} (from {messy!r})"


def test_operator_spacing_is_idempotent():
    for messy, _want in _SPACING_CASES:
        once = format_source(messy, "spacing.rvl")
        assert format_source(once, "spacing.rvl") == once, f"not idempotent: {messy!r}"


def test_operator_spacing_preserves_ir():
    # Each rewrite must also clear the self-proving gate: the token stream is
    # identical, so the IR must be byte-identical (never REFUSED).
    for messy, _want in _SPACING_CASES:
        out = format_source(messy, "spacing.rvl")
        result = ir_equivalent(messy, out, "spacing.rvl")
        assert result.admitted, f"gate refused {messy!r}: {result.reason}"


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
    # the meaning differs.
    tampered = original.replace("fn healthy() = true", "fn healthy() = false")

    result = ir_equivalent(original, tampered, "registry.rvl")
    assert not result.admitted
    # Caught by the token check, which runs first and names the exact token
    # that moved. (Before issue 309 this was caught only by the IR arm, and
    # only for a file the compiler could build standalone.)
    assert "token stream" in result.reason
    assert "'true'" in result.reason and "'false'" in result.reason


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
    # Since item 203, the legacy source COMPILES on its own (`$key` is now a
    # literal), so migrating it to a `${key}` template is a deliberate
    # literal->interpolation meaning change. The migrate gate
    # (token_preserving=False) admits that intended semantic upgrade rather
    # than refusing it as a meaning change, while a formatter reformat with the
    # same IR delta would still be refused.
    f = tmp_path / "cache.rvl"
    f.write_text(MIGRATE_PROGRAM)
    # The original really does compile as a literal now (the premise above).
    compile_source(MIGRATE_PROGRAM, "cache.rvl")
    assert main(["fmt", "--migrate", str(f)]) == 0
    text = f.read_text()
    assert "emit db.execute(`INSERT INTO cache_log VALUES (${key})`)" in text
    # Migrated output still compiles, now as an interpolating template.
    compile_source(text, "cache.rvl")


def test_migrate_gate_admits_the_literal_to_template_upgrade():
    # The gate the CLI uses for --migrate: the same rewrite the formatter would
    # be REFUSED for (an IR change on a compiling original) is ADMITTED for
    # migration, because token_preserving=False marks it a deliberate upgrade.
    original = MIGRATE_PROGRAM
    migrated = original.replace(
        'emit db.execute("INSERT INTO cache_log VALUES ($key)")',
        "emit db.execute(`INSERT INTO cache_log VALUES (${key})`)",
    )
    # formatter policy (token_preserving=True) refuses this IR change...
    assert not ir_equivalent(original, migrated, "cache.rvl").admitted
    # ...but the migrate policy admits it as the intended semantic upgrade.
    assert ir_equivalent(
        original, migrated, "cache.rvl", token_preserving=False).admitted


def test_migrate_gate_still_refuses_a_rewrite_that_breaks_compilation():
    # The one guarantee migration keeps: a mechanical pass that corrupted the
    # source so it no longer compiles is REFUSED, even under the relaxed policy.
    original = MIGRATE_PROGRAM
    broken = original.replace("fn put(key, value)", "fn put(key, value")  # drop `)`
    result = ir_equivalent(original, broken, "cache.rvl", token_preserving=False)
    assert not result.admitted
    assert "no longer compiles" in result.reason


def test_migrate_gate_holds_on_identity():
    # A file with no legacy `$` migrates to itself; the gate proves IR identity.
    src = "service K {\n  fn get() -> Bool\n}\n"
    from revl.fmt import migrate_source

    migrated, _ = migrate_source(src, "k.rvl")
    assert migrated == src
    assert ir_equivalent(src, migrated, "k.rvl").admitted


# --------------------------------------------------------------------------
# Issue 309: the gate proves meaning preservation for EVERY construct
#
# `revl fmt` used to rewrite a `use`-bearing file with no proof at all. The IR
# arm compiled with `compile_source`, which refuses a `use` for want of a
# module directory, so every such file fell through to a "token identity"
# fall-back that compared the FORMATTER'S OWN scanner with itself -- and that
# scanner had drifted from the lexer. Both sides were mis-scanned the same
# way, the comparison passed, and the corrupted rewrite was written in place.
# --------------------------------------------------------------------------

# Constructs the drifted scanner got wrong. Each is a one-line function body
# whose meaning the formatter must not touch.
DRIFTED_CONSTRUCTS = [
    ('fn f() -> Str { return """abc""" }', "triple-quoted string"),
    ("fn f() -> Str { return 'abc' }", "single-quoted string (item 382)"),
    ('fn f() -> Str { return "a\\"b" }', "escaped quote inside a string"),
    ("fn f() -> Int { return 1_000 }", "digit-group separator (item 381)"),
    ("fn f() -> Int { return 0xFF }", "hexadecimal literal (item 381)"),
    ("fn f() -> Int { return 0b1010 }", "binary literal (item 381)"),
    ("fn f() -> Int { return 0o17 }", "octal literal (item 381)"),
    ("fn f(a: Int32, b: Int32) -> Int32 { return a & b }", "bitwise and (item 366)"),
    ("fn f(a: Int32, b: Int32) -> Int32 { return a ^ b }", "bitwise xor (item 366)"),
    ("fn f(a: Int32, b: Int32) -> Int32 { return a << b }", "bitwise shift (item 366)"),
    ("fn f(a: Int32) -> Int32 { return ~a }", "bitwise not (item 366)"),
]


def _module_pair(tmp_path, body):
    """A `use`-bearing file next to the module it imports, on disk."""
    (tmp_path / "lib.rvl").write_text("pub fn one() -> Int { return 1 }\n")
    target = tmp_path / "use_it.rvl"
    target.write_text('use "lib.rvl" as lib\n\n' + body + "\n")
    return target


def test_use_bearing_file_round_trips_with_identical_ir(tmp_path):
    # The headline claim, proven by COMPILING both texts with `use` resolved
    # and comparing IR -- not by eyeballing the formatted text.
    import os
    from revl.compiler import compile_files

    for body, label in DRIFTED_CONSTRUCTS:
        target = _module_pair(tmp_path, body)
        original = target.read_text()
        # Messy input, so the formatter really does rewrite something.
        messy = original.replace("fn f(", "fn  f(").replace("{ return", "{   return")
        target.write_text(messy)

        formatted = format_source(messy, str(target))
        gate = ir_equivalent(messy, formatted, str(target))
        assert gate.admitted, f"{label}: refused ({gate.reason})"

        path = os.path.abspath(str(target))
        before = _canonical_ir(compile_files([path], sources={path: messy}))
        after = _canonical_ir(compile_files([path], sources={path: formatted}))
        assert before == after, f"{label}: formatting changed the compiled IR"


def test_gate_runs_the_ir_arm_for_a_use_bearing_file(tmp_path):
    # Not just "admitted": the proof must be the IR comparison. Before the fix
    # this file reached the vacuous token fall-back instead.
    target = _module_pair(tmp_path, 'fn f() -> Str { return """abc""" }')
    messy = target.read_text().replace("fn f()", "fn  f( )")
    formatted = format_source(messy, str(target))
    gate = ir_equivalent(messy, formatted, str(target))
    assert gate.admitted
    assert gate.proof == "IR byte-identical"


def test_token_check_uses_the_reference_lexer_not_the_formatter_scanner():
    # The check must be able to see a difference the formatter's own scanner
    # cannot. A scanner that knows only `"` reads `"""abc"""` as `""`, `abc`,
    # `""` -- the same three pieces it reads from `"" "abc" ""`, which is a
    # DIFFERENT program. The reference lexer tells them apart.
    from revl.formatter import _token_signature

    one = 'fn f() -> Str { return """abc""" }'
    other = 'fn f() -> Str { return "" "abc" "" }'
    assert _token_signature(one, "a.rvl") != _token_signature(other, "a.rvl")


def test_gate_refuses_a_rewrite_that_changes_tokens_in_an_uncompilable_file():
    # No IR baseline (this does not type-check), so only the token check can
    # speak -- and it must refuse rather than wave the rewrite through.
    original = "fn f() -> Str { return \"\"\"abc\"\"\" }\n"
    tampered = 'fn f() -> Str { return "" "abc" "" }\n'
    result = ir_equivalent(original, tampered, "<source>")
    assert not result.admitted
    assert "token stream" in result.reason


def test_gate_refuses_source_that_does_not_lex():
    # Nothing can be proven about a file the lexer rejects, so nothing is
    # written. Fail closed rather than fall back to a weaker check.
    result = ir_equivalent("fn f() { return `unterminated }\n", "anything\n",
                           "<source>")
    assert not result.admitted
    assert "does not lex" in result.reason


def test_host_body_brace_inside_a_string_is_not_a_terminator():
    # A naive brace count truncated the body at the `"}"` string (this is the
    # shape `stdlib/json.rvl` carries).
    src = ('extern fn dumps(v: Str) -> Str\n'
           '@ts {\n'
           '  const closer = "}"\n'
           '  return closer\n'
           '}\n')
    assert format_source(src, "j.rvl") == src


def test_cli_refuses_rather_than_writing_a_corrupted_use_bearing_file(tmp_path):
    # End to end through the CLI: whatever happens, the file on disk after
    # `revl fmt` compiles to the same IR as the file before it.
    import os
    from revl.compiler import compile_files

    target = _module_pair(tmp_path, 'fn f() -> Str { return """abc""" }')
    messy = target.read_text().replace("fn f()", "fn  f( )")
    target.write_text(messy)
    path = os.path.abspath(str(target))
    before = _canonical_ir(compile_files([path], sources={path: messy}))

    assert main(["fmt", str(target)]) == 0
    after_text = target.read_text()
    assert after_text != messy, "the formatter should have tidied this file"
    assert _canonical_ir(compile_files([path], sources={path: after_text})) == before
