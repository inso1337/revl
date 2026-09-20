"""Every rule in tools/check_vision_claims.py, seen to fail and seen to pass.

The discipline is the one tests/test_roadmap_gate_bites.py states and
tests/test_roadmap_claims_gate.py follows: a gate nobody has watched fail is
not known to work. Three gates measured in this repo this month passed while
checking nothing, and the two that read text with regular expressions are the
easiest kind to write so that they can never fire.

So each rule gets a PAIR. One fixture is the claim as a rename or a move leaves
it; the other is the SAME fixture with the one thing changed that should
silence it, which pins the escape hatch as well: what an author has to do to
satisfy the gate honestly rather than by deleting the sentence.

The last tests run against the REAL docs/vision.md and the REAL tree, because
every fixture here is synthetic and a collector whose regex stopped matching
the live document would still pass all of the pairs above.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_roadmap_claims as claims  # noqa: E402
import check_vision_claims as gate  # noqa: E402
import docgen  # noqa: E402


# --------------------------------------------------------------------------
# A tiny tree on disk: the checker resolves abbreviated paths by suffix over
# the real file list, and reads a Makefile and a package.json.
# --------------------------------------------------------------------------
_FILES = {
    "Makefile": "docs-gen:\n\tpython3 tools/docgen.py --write\n\nformal:\n\tsh formal/scripts/run_gate.sh\n",
    "formal/scripts/run_gate.sh": "echo gate\n",
    "backends/go/test_emit_go.py": "def test_go():\n    pass\n",
    "backends/python/setup.sh": "echo setup\n",
    "backends/python/tests/test_semantics.py": "def test_py():\n    pass\n",
    "backends/typescript/package.json": (
        '{"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "4.1.11"}}\n'
    ),
    "demo/bridge_pypy.py": "print(1)\n",
    "docs/conformance.md": "# c\n",
    "docs/vision.md": "# v\n",
}


@pytest.fixture
def tree(tmp_path: Path) -> claims.Tree:
    for rel, body in _FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return claims.Tree(tmp_path, sorted(_FILES))


def _findings(source: str, tree: claims.Tree, rule: str) -> list:
    _, findings = gate.run(source, tree, [rule], doc_rel="docs/vision.md")
    return findings


def _one(source: str, tree: claims.Tree, rule: str) -> str:
    findings = _findings(source, tree, rule)
    assert len(findings) == 1, findings
    return findings[0][1]


def _silent(source: str, tree: claims.Tree, rule: str) -> None:
    assert _findings(source, tree, rule) == []


# --------------------------------------------------------------------------
# (command) The seven rows of the vision gate table, each falsifiable by the
# rename or the move that the issue says nothing catches today.
# --------------------------------------------------------------------------
def test_a_pytest_row_naming_a_renamed_test_file_bites(tree):
    verdict = _one("| go | `pytest backends/go/test_emit_golang.py -q` |", tree, "command")
    assert "test_emit_golang.py" in verdict


def test_the_same_pytest_row_at_the_real_file_passes(tree):
    _silent("| go | `pytest backends/go/test_emit_go.py -q` |", tree, "command")


def test_a_cd_into_a_moved_directory_bites(tree):
    verdict = _one("`cd backends/py && .venv/bin/pytest -q`", tree, "command")
    assert "backends/py" in verdict


def test_a_bare_pytest_run_in_a_directory_with_no_tests_bites(tree):
    """`cd backends/python && .venv/bin/pytest -q` names no file, so the claim
    it makes is about what pytest would collect there. Moving the suite out
    leaves the command intact and the claim false, which is the one case a
    path-resolving gate would otherwise walk straight past."""
    empty = {k: v for k, v in _FILES.items() if "backends/python/tests" not in k}
    stripped = claims.Tree(tree.root, sorted(empty))
    verdict = _one(
        "`cd backends/python && .venv/bin/pytest -q`", stripped, "command")
    assert "no `test_*.py` to collect" in verdict


def test_the_same_bare_pytest_run_passes_where_the_suite_is(tree):
    _silent("`cd backends/python && .venv/bin/pytest -q`", tree, "command")


def test_a_make_target_the_makefile_no_longer_defines_bites(tree):
    verdict = _one("`make formal-gate`", tree, "command")
    assert "formal-gate" in verdict


def test_a_make_target_that_exists_passes(tree):
    _silent("`make formal`", tree, "command")


def test_a_sh_script_that_moved_bites(tree):
    verdict = _one("`sh formal/run_gate.sh`", tree, "command")
    assert "formal/run_gate.sh" in verdict


def test_an_npx_tool_the_package_no_longer_declares_bites(tree):
    verdict = _one(
        "`cd backends/typescript && npm ci && npx jest run`", tree, "command")
    assert "jest" in verdict


def test_the_shipped_npx_tool_passes(tree):
    _silent("`cd backends/typescript && npm ci && npx vitest run`", tree, "command")


def test_a_one_word_span_naming_a_tool_is_not_read_as_a_command(tree):
    """`npx` in prose is the name of a tool, not a command with a working
    directory. Judging it reddened the real document on its own explanation."""
    _silent("the package an `npx` tool comes from", tree, "command")


def test_a_command_narrating_its_own_rename_is_not_judged(tree):
    _silent(
        "`pytest backends/go/test_emit_golang.py -q` was renamed to the current "
        "spelling when the go tier landed",
        tree, "command",
    )


# --------------------------------------------------------------------------
# (link) Relative markdown links.
# --------------------------------------------------------------------------
def test_a_relative_link_to_a_moved_document_bites(tree):
    verdict = _one("see [conformance](conformance-matrix.md)", tree, "link")
    assert "docs/conformance-matrix.md" in verdict


def test_a_relative_link_that_resolves_passes(tree):
    _silent("see [conformance](conformance.md)", tree, "link")


def test_an_absolute_url_is_never_judged(tree):
    _silent("[stc-go](https://github.com/0xdenny218/stc-go)", tree, "link")


def test_an_anchor_is_stripped_before_resolving(tree):
    _silent("[the sweep](conformance.md#emit-sweep)", tree, "link")


# --------------------------------------------------------------------------
# (path) Backticked paths, abbreviations and globs.
# --------------------------------------------------------------------------
def test_a_backticked_path_the_tree_does_not_have_bites(tree):
    verdict = _one("the ledger `formal/STATUS.md` names each gap", tree, "path")
    assert "formal/STATUS.md" in verdict


def test_an_abbreviated_filename_resolves_by_suffix(tree):
    _silent("needs `setup.sh`", tree, "path")


def test_the_same_abbreviation_after_a_rename_bites(tree):
    verdict = _one("needs `bootstrap.sh`", tree, "path")
    assert "bootstrap.sh" in verdict


def test_a_glob_that_matches_nothing_bites(tree):
    """`demo/bridge_*` is the document's own example of a claim with no test
    behind it. The glob is still a claim that the scripts are there."""
    verdict = _one("demonstrated by the `demo/span_*` scripts", tree, "path")
    assert "matches no tracked file" in verdict


def test_a_glob_that_matches_passes(tree):
    _silent("demonstrated by the `demo/bridge_*` scripts", tree, "path")


def test_a_bare_extension_is_not_read_as_a_file(tree):
    _silent("One `.rvl` source, one IR, six emitters.", tree, "path")


# --------------------------------------------------------------------------
# (tier) The generated table against the conformance register.
# --------------------------------------------------------------------------
_TIER_DOC = """# v

<!-- docgen:vision-tiers begin -->
%s
<!-- docgen:vision-tiers end -->
"""

_REGISTER = """# Conformance

**Per tier** (61 constructs emitted; 1 rejected by the frontend, below):

| tier | ok | deliberate limit | real gap |
|---|---|---|---|
| py | 61 | 0 | 0 |
| ts | 61 | 0 | 0 |
| rust | 61 | 0 | 0 |
| java | 60 | 1 | 0 |
| wasm | 51 | 10 | 0 |
| go | 61 | 0 | 0 |
| revl (self-host) | 24 | 37 | 0 |
"""


@pytest.fixture
def register(tmp_path: Path) -> claims.Tree:
    files = dict(_FILES)
    files["docs/conformance.md"] = _REGISTER
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return claims.Tree(tmp_path, sorted(files))


def _write_vision(tree: claims.Tree, body: str) -> str:
    source = _TIER_DOC % body
    (tree.root / "docs" / "vision.md").write_text(source, encoding="utf-8")
    return source


def test_the_generated_tier_table_regenerates_to_itself(register):
    body = docgen.block_vision_tiers("", root=register.root)
    source = _write_vision(register, body)
    _silent(source, register, "tier")


def test_a_hand_edited_count_in_the_tier_table_bites(register):
    """The six-tier table used to be wholly hand-written beside a generated
    conformance matrix. Editing a count back by hand is the drift issue #1204
    is about, and it has to be red rather than merely wrong."""
    body = docgen.block_vision_tiers("", root=register.root).replace(
        "51 ok / 10 limit / 0 gap", "61 ok / 0 limit / 0 gap")
    source = _write_vision(register, body)
    verdict = _one(source, register, "tier")
    assert "docs/conformance.md" in verdict


def test_a_guarantee_moving_in_the_register_reds_the_vision_table(register):
    """The register is the source. A construct moving from `ok` to a
    deliberate limit on one tier moves docs/conformance.md, and the vision
    table it never used to be compared against now moves with it."""
    body = docgen.block_vision_tiers("", root=register.root)
    source = _write_vision(register, body)
    _silent(source, register, "tier")
    (register.root / "docs" / "conformance.md").write_text(
        _REGISTER.replace("| go | 61 | 0 | 0 |", "| go | 60 | 1 | 0 |"),
        encoding="utf-8",
    )
    verdict = _one(source, register, "tier")
    assert "disagrees with the per-tier totals" in verdict


def test_the_human_columns_are_carried_across_a_regeneration(register):
    first = docgen.block_vision_tiers("", root=register.root)
    assert "TODO: say what this tier proves" in first
    edited = first.replace(
        "TODO: say what this tier proves", "the same IR runs unchanged", 1)
    again = docgen.block_vision_tiers(edited, root=register.root)
    assert "the same IR runs unchanged" in again
    assert docgen.block_vision_tiers(again, root=register.root) == again


def test_a_new_register_tier_with_no_name_is_a_loud_failure(register):
    (register.root / "docs" / "conformance.md").write_text(
        _REGISTER + "| zig | 3 | 0 | 0 |\n", encoding="utf-8")
    with pytest.raises(docgen.VisionTierError) as exc:
        docgen.block_vision_tiers("", root=register.root)
    assert "zig" in str(exc.value)


def test_a_register_with_no_per_tier_table_is_a_loud_failure(register):
    (register.root / "docs" / "conformance.md").write_text("# c\n", encoding="utf-8")
    with pytest.raises(docgen.VisionTierError):
        docgen.block_vision_tiers("", root=register.root)


# --------------------------------------------------------------------------
# The real document and the real tree. Every fixture above is synthetic.
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real() -> claims.Tree:
    return claims.Tree.from_git(ROOT)


def test_every_rule_finds_claims_in_the_real_vision_document(real):
    source = (ROOT / "docs" / "vision.md").read_text(encoding="utf-8")
    denominator, _ = gate.run(source, real, gate.RULES)
    for rule in gate.RULES:
        assert denominator[rule] > 0, f"{rule} collected nothing from docs/vision.md"


def test_all_seven_gate_table_commands_are_collected(real):
    """The issue counts seven backticked commands in the "a claim gets a
    command or it gets softened" table. A collector that drifted off that
    table would still pass every synthetic pair above."""
    source = (ROOT / "docs" / "vision.md").read_text(encoding="utf-8")
    collected = {c.key for c in gate.collect_command_claims(source)}
    for command in (
        "cd backends/python && .venv/bin/pytest -q",
        "cd backends/typescript && npm ci && npx vitest run",
        "pytest backends/wasm/test_v3_emit.py tests/test_wasm_backend.py -q",
        "pytest backends/rust/test_emit_rust.py backends/java/test_emit_java.py -q",
        "pytest backends/go/test_emit_go.py -q",
        "make formal",
        "sh formal/scripts/run_gate.sh",
    ):
        assert command in collected, command


def test_the_real_document_is_green(real):
    source = (ROOT / "docs" / "vision.md").read_text(encoding="utf-8")
    _, findings = gate.run(source, real, gate.RULES)
    assert findings == [], findings


def test_a_stale_command_planted_in_the_REAL_document_is_caught(real):
    source = (ROOT / "docs" / "vision.md").read_text(encoding="utf-8")
    broken = source.replace(
        "pytest backends/go/test_emit_go.py -q",
        "pytest backends/go/test_emit_golang.py -q",
    )
    assert broken != source
    _, findings = gate.run(broken, real, ["command"])
    assert len(findings) == 1, findings


def test_the_self_test_passes():
    """The gate's own `--self-test`, which CI runs ahead of `--check`."""
    assert gate.self_test(ROOT) == 0


def test_the_cli_reports_and_exits_zero_on_the_real_document():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_vision_claims.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "claims checked over 4 rules" in proc.stdout
