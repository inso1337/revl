"""The self-host compile ledger is measured, stale by construction, and total.

`tools/selfhost_matrix.py` records, per self-host file and per tier, whether
that tier's reference emitter compiles the file (issue #2899). The measurement
is minutes long, so it is a committed ledger with a cheap gate rather than a
walk in CI. This file holds the gate's refusals, each constructed rather than
hoped for:

  * a ledger whose sources or emitters have moved since it was measured;
  * a cell that does not emit and is not acknowledged anywhere;
  * an acknowledgement whose cell emits now (the ratchet's other direction);
  * an acknowledgement with no issue and no recorded scope decision;
  * an acknowledgement with no reason naming the construct.
  * an acknowledgement with no reason naming the construct;
  * NOT refused: a self-host source, stdlib file or emitter that moved since the
    measurement while no cell changed. That is reported as a fact, because
    refusing it would red main on every such merge and force every unrelated
    pull request to re-measure;
  * refused again at the pre-merge gate (`--check --base REF`): a branch whose
    own diff edits a measured input while the ledger digest is stale, because
    that branch has the reason to re-measure.

`test_the_committed_ledger_is_current` is the control: it passes on the tree
every refusal below fails on.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import selfhost_matrix as sm  # noqa: E402


@pytest.fixture(scope="module")
def ledger():
    data = sm.load()
    assert data is not None, (
        "tools/selfhost_compile_matrix.json is missing; measure it with "
        "`python3 tools/selfhost_matrix.py --measure`")
    return data


def _changed_problems(ledger, gaps, **kwargs):
    kwargs.setdefault("expected_digest", ledger["digest"])
    return sm.problems(ledger, gaps, **kwargs)


def _a_cell(ledger, kind):
    for name, row in ledger["cells"].items():
        for tier, cell in row.items():
            if cell["kind"] == kind:
                return f"{name}::{tier}"
    raise AssertionError(f"the committed ledger has no {kind} cell")


# ----------------------------------------------------------------- the control

def test_the_committed_ledger_is_current():
    """THE CONTROL: the gate passes on the tree every refusal below fails on."""
    assert sm.problems() == []
    assert sm.main(["--check"]) == 0


def test_the_ledger_covers_every_self_host_file_and_tier(ledger):
    assert ledger["files"] == [path.name for path in sm.sources()]
    assert ledger["tiers"] == list(sm.tiers())
    for name in ledger["files"]:
        assert set(ledger["cells"][name]) == set(ledger["tiers"]), name


# ------------------------------------------------------------------- staleness

def test_a_moved_source_or_emitter_marks_the_ledger_stale(ledger):
    found = sm.problems(ledger, sm.load_gaps(), expected_digest="0" * 64)
    assert any("different self-host, stdlib or tier emitter sources" in problem
               for problem in found)
def _tree(root: Path) -> dict[str, Path]:
    (root / "selfhost").mkdir()
    (root / "stdlib").mkdir()
    (root / "backends" / "go").mkdir(parents=True)
    (root / "backends" / "typescript").mkdir(parents=True)
    (root / "backends" / "wasm").mkdir(parents=True)
    paths = {"corpus": root / "selfhost" / "a.rvl",
             "stdlib": root / "stdlib" / "s.rvl",
             "emitter": root / "backends" / "go" / "emit.py",
             "sibling emit module": root / "backends" / "typescript" / "emit_temporal.py",
             "wasm canonical": root / "backends" / "wasm" / "canonical.py"}
    for name, path in paths.items():
        path.write_text(name, encoding="utf-8")
    return paths


def test_the_digest_covers_every_kind_of_source_the_measurement_reads(tmp_path):
    """A digest that quietly stopped hashing an emitter or a stdlib file would
    make the gate stop noticing the change it exists to notice."""
    (tmp_path / "selfhost").mkdir()
    (tmp_path / "stdlib").mkdir()
    (tmp_path / "backends" / "go").mkdir(parents=True)
    corpus = tmp_path / "selfhost" / "a.rvl"
    stdlib = tmp_path / "stdlib" / "s.rvl"
    emitter = tmp_path / "backends" / "go" / "emit.py"
    for path, text in ((corpus, "corpus"), (stdlib, "stdlib"), (emitter, "emitter")):
        path.write_text(text, encoding="utf-8")
    """The digest is what says which measurement a ledger is. One that quietly
    stopped hashing an emitter, a sibling emit module, the wasm canonical-ABI
    module or a stdlib file would report a stale ledger as current."""
    paths = _tree(tmp_path)
    seen = {sm.digest(tmp_path)}
    for path, text in ((emitter, "emitter2"), (stdlib, "stdlib2"), (corpus, "corpus2")):
        path.write_text(text, encoding="utf-8")
    for name, path in paths.items():
        path.write_text(name + "2", encoding="utf-8")
        seen.add(sm.digest(tmp_path))
    assert len(seen) == len(paths) + 1


def test_the_digest_ignores_files_the_measurement_does_not_read(tmp_path):
    paths = _tree(tmp_path)
    before = sm.digest(tmp_path)
    (tmp_path / "backends" / "go" / "test_emit_go.py").write_text("t", encoding="utf-8")
    (tmp_path / "backends" / "go" / "runtime.py").write_text("r", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "frontend.py").write_text("f", encoding="utf-8")
    assert sm.digest(tmp_path) == before
    assert paths  # the measured files themselves were untouched


# -------------------------------------------------------------- acknowledgement

def test_an_unacknowledged_non_emitting_cell_is_reported(ledger):
    cell = _a_cell(ledger, sm.REFUSED)
    gaps = [entry for entry in sm.load_gaps() if entry["cell"] != cell]
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "no acknowledgement" in problem
               for problem in found), found


def test_an_acknowledgement_whose_cell_emits_now_is_stale(ledger):
    cell = _a_cell(ledger, sm.OK)
    gaps = sm.load_gaps() + [{"cell": cell, "issue": 1, "reason": "planted"}]
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "needs no acknowledgement now" in problem
               for problem in found), found


def test_an_acknowledgement_needs_an_issue_or_a_scope_decision(ledger):
    cell = _a_cell(ledger, sm.REFUSED)
    gaps = copy.deepcopy(sm.load_gaps())
    entry = next(entry for entry in gaps if entry["cell"] == cell)
    entry.pop("issue", None)
    entry.pop("out_of_scope", None)
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "integer `issue`" in problem for problem in found)


def test_an_acknowledgement_needs_a_reason(ledger):
    cell = _a_cell(ledger, sm.REFUSED)
    gaps = copy.deepcopy(sm.load_gaps())
    entry = next(entry for entry in gaps if entry["cell"] == cell)
    entry["reason"] = ""
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "`reason`" in problem for problem in found)


def test_a_frontend_refusal_is_unmeasured_and_needs_no_acknowledgement(ledger):
    cell = _a_cell(ledger, sm.OK)
    name, tier = cell.split("::")
    changed = copy.deepcopy(ledger)
    changed["cells"][name][tier] = {"kind": sm.FRONTEND, "message": "planted"}
    gaps = [entry for entry in sm.load_gaps() if entry["cell"] != cell]
    assert _changed_problems(changed, gaps) == []


def test_an_acknowledgement_for_a_cell_the_measurement_lost_is_reported(ledger):
    gaps = sm.load_gaps() + [
        {"cell": "not-a-selfhost-file.rvl::go", "issue": 1, "reason": "planted"}]
    found = _changed_problems(ledger, gaps)
    assert any("not-a-selfhost-file.rvl::go" in problem and
               "acknowledged but not measured" in problem for problem in found)


# ------------------------------------------------------------ the table derives

def test_a_tier_with_a_frontend_cell_is_unmeasured_not_a_count(ledger):
    changed = copy.deepcopy(ledger)
    tier = changed["tiers"][0]
    changed["cells"][changed["files"][0]][tier] = {"kind": sm.FRONTEND,
                                                   "message": "planted"}
    assert sm.tier_counts(changed, tier) is None


def test_tier_counts_follow_the_ledger_cells(ledger):
    for tier in ledger["tiers"]:
        kinds = [row[tier]["kind"] for row in ledger["cells"].values()]
        expected = (sum(1 for kind in kinds if kind == sm.OK), len(kinds))
        assert sm.tier_counts(ledger, tier) == expected


# ------------------------------------------------------------- the branch gate

def _git(root: Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    *args], cwd=root, check=True, capture_output=True)


def test_the_branch_that_edits_a_measured_input_must_re_measure(tmp_path, ledger):
    paths = _tree(tmp_path)
    measured = copy.deepcopy(ledger)
    measured["digest"] = sm.digest(tmp_path)
    paths["emitter"].write_text("emitter, edited\n", encoding="utf-8")

    refused = sm.branch_problems(measured, ["backends/go/emit.py", "README.md"],
                                 current=sm.digest(tmp_path))
    assert refused and "backends/go/emit.py" in refused[0]
    assert "--measure" in refused[0]


def test_the_branch_gate_passes_when_the_branch_touches_no_input(tmp_path, ledger):
    """Main may have let the ledger go stale; an unrelated branch is not
    answerable for it."""
    _tree(tmp_path)
    measured = copy.deepcopy(ledger)
    measured["digest"] = "0" * 64   # stale
    assert sm.branch_problems(measured, ["README.md", "tools/other.py",
                                         "backends/go/runtime.py"],
                              current=sm.digest(tmp_path)) == []


def test_the_branch_gate_passes_when_the_branch_re_measured(tmp_path, ledger):
    paths = _tree(tmp_path)
    paths["emitter"].write_text("emitter, edited\n", encoding="utf-8")
    measured = copy.deepcopy(ledger)
    measured["digest"] = sm.digest(tmp_path)   # the branch re-measured
    assert sm.branch_problems(measured, ["backends/go/emit.py"],
                              current=sm.digest(tmp_path)) == []


def test_touched_inputs_matches_exactly_what_the_digest_reads():
    changed = ["selfhost/lower.rvl", "stdlib/str.rvl", "backends/go/emit.py",
               "backends/typescript/emit_temporal.py", "backends/wasm/canonical.py",
               "backends/go/test_emit.py", "backends/go/runtime.py",
               "src/revl/lower.py", "docs/conformance.md"]
    assert sm.touched_inputs(changed) == [
        "backends/go/emit.py", "backends/typescript/emit_temporal.py",
        "backends/wasm/canonical.py", "selfhost/lower.rvl", "stdlib/str.rvl"]


def test_changed_paths_reads_the_branch_diff_against_the_merge_base(tmp_path):
    """Commits that landed on the base after the branch point are not this
    branch's changes, and an uncommitted edit is."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "selfhost").mkdir()
    (tmp_path / "selfhost" / "a.rvl").write_text("a", encoding="utf-8")
    (tmp_path / "other.txt").write_text("o", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "branch")
    (tmp_path / "branch.txt").write_text("b", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "branch work")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "selfhost" / "a.rvl").write_text("moved on main", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "main moves an input")
    _git(tmp_path, "checkout", "-q", "branch")
    (tmp_path / "other.txt").write_text("dirty", encoding="utf-8")

    changed = sm.changed_paths("main", tmp_path)
    assert "branch.txt" in changed and "other.txt" in changed
    assert "selfhost/a.rvl" not in changed


def test_check_base_refuses_when_the_diff_cannot_be_read(
        ledger, monkeypatch, capsys):
    import subprocess

    def broken(base_ref, root=None):
        raise subprocess.CalledProcessError(128, ["git", "diff"])

    monkeypatch.setattr(sm, "changed_paths", broken)
    assert sm.main(["--check", "--base", "no-such-ref"]) == 1
    assert "cannot read this branch's diff" in capsys.readouterr().err


def test_check_base_exits_one_for_an_input_edit_on_a_stale_ledger(
        ledger, monkeypatch, capsys):
    monkeypatch.setattr(sm, "changed_paths",
                        lambda base_ref, root=None: ["backends/go/emit.py"])
    monkeypatch.setattr(sm, "digest", lambda root=None: "f" * 64)
    assert sm.main(["--check", "--base", "origin/main"]) == 1
    assert "this branch changes a measured input" in capsys.readouterr().err


def test_base_without_check_is_a_usage_error():
    with pytest.raises(SystemExit):
        sm.main(["--base", "origin/main"])


# ------------------------------------------- the table says which measurement

def test_the_support_table_names_the_digest_it_was_measured_at():
    import tier_guarantees as tg

    shown = tg.support_markdown(
        {"tiers": [], "rows": [], "selfhost_emit_digest": "ab" * 32})
    assert "recorded at source digest `abababababab`" in shown
    assert "abababababab" + "ab" not in shown  # the short form, not the digest

    without = tg.support_markdown(
        {"tiers": [], "rows": [], "selfhost_emit_digest": None})
    assert "source digest" not in without


def test_the_committed_support_block_names_the_committed_ledger_digest(ledger):
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    assert f"recorded at source digest `{ledger['digest'][:12]}`" in doc
