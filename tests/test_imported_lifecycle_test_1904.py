"""issue #1904 — importing a module that holds a lifecycle test of its own component.

A `use` edge carries a module's pure declarations into the importer: its `fn`s,
its types, and — for the importer's convenience — the `test`/`prop test` blocks
that need nothing but those declarations. A `lifecycle test` is not one of
those: it NAMES the components it `load`s, and components are never imported
(the components/services merge in `compile_files` takes the root modules only,
and the fault-test merge says it outright: "fault tests name a component, and
components are never imported").

Collecting an imported module's lifecycle test anyway put a `load` of a
component the importer never declares into the importer's program, so
`revl test b.rvl` refused with `unknown component` — and because test bodies are
lowered inside the IMPORTER's merged program, it refused with `b.rvl:12` where
line 12 is `a.rvl`'s.

The rule these tests pin is "a test travels with the things it needs":

  * a `lifecycle test` (like a `fault test`) needs a component, so it rides with
    the composition's own modules — `revl test a.rvl` collects it, `revl test
    b.rvl` does not, and `revl test a.rvl b.rvl` does, because both files are
    roots there (the issue's own workaround);
  * a plain `test` needs only pure declarations, so it keeps crossing the `use`
    edge exactly as it did before this fix;
  * a diagnostic raised inside an imported module's test names THAT module's
    file and line, never the importer's file with the imported file's line.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

# The issue's own reproducer: `a.rvl` declares a service and a component plus a
# `pub fn`, and holds a `lifecycle test` of its own component.
A_LIFECYCLE = """service P { fn ping(tag: Str) -> Str }

component C provides p: P {
  provide p { fn ping(tag) = tag }
}

pub fn twice(s: Str) -> Str {
  return s + s
}

lifecycle test "C answers" {
  load C
  let r = call p.ping("x")
  assert r == "x"
  unload C
  assert no_residue
}
"""

# `use "./a.rvl" { twice }` names a FUNCTION. b.rvl has 5 lines and `load C` sits
# on `a.rvl:12`, which is why the pre-fix message `b.rvl:12` is the signature of
# the provenance half of this bug.
B_IMPORTS_TWICE = """use "./a.rvl" { twice }

test "twice" {
  assert twice("a") == "aa"
}
"""

B3_IMPORTS_BROKEN = """use "./a3.rvl" { twice }

test "twice" {
  assert twice("a") == "aa"
}
"""

B4_IMPORTS_BROKEN = """use "./a4.rvl" { twice }

test "twice" {
  assert twice("a") == "aa"
}
"""

A_PLAIN = """pub fn twice(s: Str) -> Str {
  return s + s
}

test "plain a test" {
  assert twice("a") == "aa"
}
"""

# A module whose own lifecycle test loads a component it does NOT declare: a
# genuine error inside the imported module, at importable line 16.
A_LIFECYCLE_BROKEN = """service P { fn ping(tag: Str) -> Str }

component C provides p: P {
  provide p { fn ping(tag) = tag }
}

pub fn twice(s: Str) -> Str {
  return s + s
}

lifecycle test "C answers" {
  load C
  let r = call p.ping("x")
  assert r == "x"
  unload C
  load Ghost
}
"""
BROKEN_LIFECYCLE_LINE = 16

# The same shape for the plain-test half: the error is inside the IMPORTED
# module's test, at line 7.
A_PLAIN_BROKEN = """pub fn twice(s: Str) -> Str {
  return s + s
}

test "plain with a real error" {
  let r = twice("a")
  assert nope("x")
}
"""
BROKEN_PLAIN_LINE = 7


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """`revl test` run FROM the directory holding the fixtures, with bare file
    names — how the issue reproduces it, and what keeps the diagnostic-location
    assertions exact (`a3.rvl:16`, not a longer path ending the same way)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "revl", "test", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


# --------------------------------------------------------------------------- #
# half 1: a module that tests its own component stays importable              #
# --------------------------------------------------------------------------- #


def test_importer_compiles_and_collects_only_its_own_tests(tmp_path):
    """`revl test b.rvl` compiles: the imported module's lifecycle test is not
    the importer's to run. Before the fix this exited 1 with
    `error: b.rvl:12: unknown component `C``."""
    _write(tmp_path, "a.rvl", A_LIFECYCLE)
    _write(tmp_path, "b.rvl", B_IMPORTS_TWICE)

    result = _cli(tmp_path, "b.rvl", "--list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["twice", "1 test(s) collected"]
    assert result.stderr == ""


def test_importer_ir_carries_the_imported_fn_but_not_the_lifecycle_test(tmp_path):
    """The same answer at the compilation boundary: `twice` is linked in (the
    importer calls it), `C answers` is not, and no component was imported."""
    ir = compile_files(["b.rvl"], sources={"b.rvl": B_IMPORTS_TWICE, "a.rvl": A_LIFECYCLE})
    assert [t["name"] for t in ir["tests"]] == ["twice"]
    assert [fn["name"] for fn in ir["functions"]] == ["twice"]
    assert ir["components"] == []


def test_the_module_that_owns_the_component_still_runs_its_lifecycle_test(tmp_path):
    """The test is not lost: `revl test a.rvl` — that module's own run — collects
    it, with the component it loads declared."""
    _write(tmp_path, "a.rvl", A_LIFECYCLE)

    result = _cli(tmp_path, "a.rvl", "--list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["C answers", "1 test(s) collected"]


def test_plain_test_blocks_keep_crossing_the_import_edge(tmp_path):
    """The other half of the rule, unchanged by this fix: a plain `test` needs
    only pure declarations, so an importer still collects it."""
    _write(tmp_path, "a.rvl", A_PLAIN)
    _write(tmp_path, "b.rvl", B_IMPORTS_TWICE)

    result = _cli(tmp_path, "b.rvl", "--list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["twice", "plain a test", "2 test(s) collected"]


def test_the_two_file_workaround_still_compiles_and_collects_both(tmp_path):
    """The issue's workaround — `revl test a.rvl b.rvl` — is not regressed: with
    both files roots, both files' tests are collected, and the run gets past
    compilation to a tier (which tier is what `--backend py` needs cordis-py
    for, a preflight skip documented elsewhere in this suite)."""
    _write(tmp_path, "a.rvl", A_LIFECYCLE)
    _write(tmp_path, "b.rvl", B_IMPORTS_TWICE)

    listed = _cli(tmp_path, "a.rvl", "b.rvl", "--list")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert listed.stdout.splitlines() == ["C answers", "twice", "2 test(s) collected"]

    ran = _cli(tmp_path, "a.rvl", "b.rvl")
    assert "unknown component" not in ran.stderr, ran.stderr
    assert "error:" not in ran.stderr, ran.stderr


# --------------------------------------------------------------------------- #
# half 2: a diagnostic raised in an imported module names that module         #
# --------------------------------------------------------------------------- #


def test_diagnostic_in_an_imported_lifecycle_test_names_the_imported_file(tmp_path):
    """The imported module's lifecycle test loads a component IT does not
    declare, at `a3.rvl:16`. Pre-fix the message was `error: b3.rvl:16` — the
    importer's file with the imported file's line."""
    _write(tmp_path, "a3.rvl", A_LIFECYCLE_BROKEN)
    _write(tmp_path, "b3.rvl", B3_IMPORTS_BROKEN)

    result = _cli(tmp_path, "b3.rvl", "a3.rvl", "--list")
    assert result.returncode == 1
    assert result.stderr.startswith(
        f"error: a3.rvl:{BROKEN_LIFECYCLE_LINE}: unknown component `Ghost`"), result.stderr
    assert "b3.rvl" not in result.stderr, result.stderr
    assert "unknown component `C`" not in result.stderr, result.stderr


def test_diagnostic_in_an_imported_plain_test_names_the_imported_file(tmp_path):
    """Same rule for a plain test block, which IS collected across the edge: the
    error lives at `a4.rvl:7` and the message names it. Pre-fix it was
    `error: b4.rvl:7` — the importer's file with the imported file's line."""
    _write(tmp_path, "a4.rvl", A_PLAIN_BROKEN)
    _write(tmp_path, "b4.rvl", B4_IMPORTS_BROKEN)

    result = _cli(tmp_path, "b4.rvl", "--list")
    assert result.returncode == 1
    assert result.stderr.startswith(f"error: a4.rvl:{BROKEN_PLAIN_LINE}: "), result.stderr
    assert "is not declared in this function" in result.stderr, result.stderr
    assert "b4.rvl" not in result.stderr, result.stderr
