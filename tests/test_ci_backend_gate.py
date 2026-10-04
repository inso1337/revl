"""The six backend jobs skip on a pull request only when they cannot observe it (issue #1817).

`changes` runs `tools/affected_tests.py --ci-backends` on a pull request, and
the six `backend-*` jobs skip when it answers none. The rule is deny-by-default
(`ci_backends` and the note above it): only documentation, a few trees no
backend file reads, and top-level `tests/` or `tools/` files no backend file,
backend job step or backend-run root test names, leave the jobs out. Main and
the merge queue run all six whatever the answer.

These tests pin the decision on real paths, and pin the two facts the "blind"
classes rest on against the tree itself, so a backend file that starts reading
one of those trees turns this file red instead of silently skipping its job.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import affected_tests as sel  # noqa: E402

ALL = list(sel.CI_BACKEND_JOBS)


def _answer(changed):
    return sel.ci_backends(changed, sel.select(changed, ROOT), ROOT)


@pytest.fixture(scope="module")
def corpus():
    return sel.ci_backend_corpus(ROOT)


def _code_lines(text):
    """Lines that are not comments; a comment citing a path reads nothing."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(("#", "//", "*", "/*")):
            yield line


# ------------------------------------------------------------- the decision

@pytest.mark.parametrize("changed", [
    ["docs/process.md"],
    ["README.md", "docs/census-artifact.json"],
    ["CHANGELOG.md", "formal/STATUS.md"],
    ["formal/RevL/Semantics.lean"],
    ["tests/test_mcp_http_transport.py"],
    ["tools/affected_tests.py", "tests/test_affected_tests.py"],
])
def test_a_change_no_backend_job_can_observe_skips_them(changed):
    assert _answer(changed) == []


@pytest.mark.parametrize("changed, why", [
    ([], "an empty changed set is not evidence of anything"),
    (["src/revl/peer_pool.py"], "backend tests run `python -m revl` as a subprocess"),
    (["backends/wasm/emit.py"], "the tiers' tests read one another"),
    (["selfhost/checker.rvl"], "not a blind tree"),
    (["bench/blast_radius/blast_radius.py"], "backends/typescript runs a bench script"),
    (["tests/test_wasm_backend.py"], "backend-wasm runs it"),
    (["tests/test_selfhost_lower.py"], "backend-rust's test_gate_crate_admit imports it"),
    (["tools/validate.py"], "backend go tests import it"),
    (["docs/process.md", "src/revl/peer_pool.py"], "one observable path is enough"),
    (["some/new/tree/file.txt"], "an unknown path"),
    (["crates/revl-gate/README.md"], "the gate crate tests read it"),
    (["backends/python/README.md"], "nothing under backends/ is blind"),
])
def test_anything_else_runs_all_six(changed, why):
    assert _answer(changed) == ALL, why


# ------------------------------------------- what the blind classes rest on

@pytest.mark.parametrize("tree", [t.rstrip("/") for t in sel.CI_BACKEND_BLIND_TREES
                                  if t != "docs/"])
def test_no_backend_file_or_job_step_names_a_blind_tree(corpus, tree):
    # A path segment (`ROOT / "site"`, `Path("site")`, `"site/x"`, `site/x`),
    # not the same word as a JSON key (`"site": step.site`).
    t = re.escape(tree)
    pattern = re.compile(
        rf"""(?:/\s*|\()["']{t}["']|["']{t}/|(?<![A-Za-z0-9_./-]){t}/""")
    hits = [line.strip() for line in _code_lines(corpus) if pattern.search(line)]
    assert not hits, (
        f"a backend file or backend job step names {tree}/, so a change there can "
        f"break a backend job: take it off CI_BACKEND_BLIND_TREES. {hits[:3]}")


def test_no_backend_file_builds_a_path_into_docs(corpus):
    """docs/ is cited in prose and messages (`see docs/records.md §6`), never
    built into a path the way `ROOT / "bench"` is."""
    hits = [line.strip() for line in _code_lines(corpus)
            if re.search(r"""["']docs["']""", line)]
    assert not hits, hits[:3]


def test_no_backend_file_opens_a_markdown_file_outside_its_own_tree(corpus):
    """A string literal that ends in `.md` is a document path a test could
    read; prose like `contract.md's` is not. The only ones are the gate crate's
    own README (crates/ is never blind), which is why only a TOP-LEVEL *.md or
    one under a blind tree is skippable."""
    hits = [line.strip() for line in _code_lines(corpus)
            if re.search(r"""["'][^"'\s]*\.md["']""", line)]
    others = [h for h in hits
              if not ('CRATE / "README.md"' in h or h.startswith('"Cargo.toml",'))]
    assert not others, others[:3]
    assert hits, "anti-vacuity: the gate crate README read is no longer seen"


def test_the_corpus_holds_the_backend_job_steps_and_the_root_tests_they_run(corpus):
    """Anti-vacuity: a corpus that missed the ci.yml job blocks, or the root
    tests a backend job runs and their imports, would clear paths it should
    not."""
    assert "regen_goldens.py --check" in corpus           # backend-go's step
    assert "cargo test --manifest-path crates/revl-lsp" in corpus  # backend-rust's
    root_test = (ROOT / "tests" / "test_gate_crate_admit.py").read_text(encoding="utf-8")
    assert root_test in corpus
    assert "import test_selfhost_lower" in root_test


def test_untracked_files_under_backends_are_not_read(tmp_path):
    """Issue #1886: frontend-cordis runs backends/python/setup.sh, which
    clones the cordis-py fork into backends/python/.cordis-py. That clone's
    `pyproject.toml` names its own README.md, which is not this repository's
    code reading a document. Only tracked files enter the corpus."""
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(name, None)
    (tmp_path / "backends" / "python").mkdir(parents=True)
    (tmp_path / "backends" / "python" / "emit.py").write_text("X = 1\n")
    clone = tmp_path / "backends" / "python" / ".cordis-py"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('readme = "README.md"\n')
    (tmp_path / "backends" / "python" / "stray.py").write_text('P = "formal/x"\n')
    for args in (["init", "-q"], ["add", "backends/python/emit.py"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       env=env, capture_output=True)
    files = sel._ci_backend_files(tmp_path)
    assert files == [tmp_path / "backends" / "python" / "emit.py"]
    corpus = sel.ci_backend_corpus(tmp_path)
    assert "README.md" not in corpus and "formal/" not in corpus


def test_without_git_the_setup_clone_is_still_skipped(tmp_path):
    (tmp_path / "backends" / "python" / ".cordis-py").mkdir(parents=True)
    (tmp_path / "backends" / "python" / ".cordis-py" / "pyproject.toml").write_text(
        'readme = "README.md"\n')
    (tmp_path / "backends" / "python" / "emit.py").write_text("X = 1\n")
    assert sel._ci_backend_files(tmp_path) == [tmp_path / "backends" / "python" / "emit.py"]


# ------------------------------------------------------------- the CI wiring

def test_every_backend_job_is_gated_on_the_answer_and_fails_open():
    import yaml  # noqa: PLC0415

    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml")
                          .read_text(encoding="utf-8"))["jobs"]
    assert jobs["changes"]["outputs"]["backends"] == "${{ steps.backends.outputs.backends }}"
    step = next(s for s in jobs["changes"]["steps"] if s.get("id") == "backends")
    assert step.get("continue-on-error") is True
    assert "affected_tests.py --ci-backends" in step["run"]
    assert 'echo "backends=false"' in step["run"]
    for tier in sel.CI_BACKEND_JOBS:
        spec = jobs[f"backend-{tier}"]
        assert set(spec["needs"]) == {"lint", "changes"}, tier
        cond = spec["if"]
        assert "!cancelled()" in cond and "needs.lint.result == 'success'" in cond, tier
        assert "needs.changes.outputs.backends != 'false'" in cond, tier


def test_the_cli_prints_one_line():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "affected_tests.py"), "--ci-backends",
         "--base", "HEAD", "--root", str(ROOT)],
        capture_output=True, text=True, timeout=300, check=True)
    line = proc.stdout.strip()
    assert line.split()[0] == "CI_BACKENDS" and "\n" not in line, proc.stdout
    assert set(line.split()[1:]) <= set(sel.CI_BACKEND_JOBS)
