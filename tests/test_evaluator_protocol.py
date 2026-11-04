"""The evaluator as one enumerated set, and the protocol identity (issue #2802).

The judges of the evolution loop were three hand-written path lists, and the
modules that assemble the scorecard were on none of them. These tests hold the
three things the one enumeration has to be worth:

  1. THE ENUMERATION IS REAL. Every `EVALUATOR_PATHS` entry exists in the tree,
     and the three named views (`KERNEL_PATHS`, `AUTHORITY_FENCE`,
     `HELD_OUT_FENCE`) partition it. A view naming a file that is not there
     protects nothing, and a fourth list appearing beside the three is the drift
     that produced issue #2802.
  2. THE FENCE IS THE JUDGE, NOT THE SUBJECT. `src/revl/lower.py`,
     `selfhost/lower.rvl` and the rest of the compiler stay refused by nothing:
     a candidate improving the gate has to be able to change them.
  3. THE DIGEST IS OVER THE PROTOCOL AND NOT OVER THE BUILD. One byte of
     `tools/evolution_progress.py` changes it; a build artifact under `formal/`
     does not; a member that is not tracked is a refusal rather than a silent
     omission.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402
from revl import kernel_boundary as kb  # noqa: E402
import evaluator_protocol as protocol  # noqa: E402

#: The files that are the evaluator but were on none of the three old lists.
#: Named here so the enumeration growing is a decision and not a side effect.
PROTOCOL_MODULES = (
    "tools/evolution_reward.py",
    "tests/test_evolution_reward.py",
    "tools/evolution_progress.py",
    "tests/test_evolution_progress.py",
)


# --------------------------------------------------------------- a checkout

def _git(tree: Path, *args):
    proc = subprocess.run(
        ["git", "-C", str(tree)] + list(args),
        capture_output=True, text=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
             "HOME": str(tree), "PATH": "/usr/bin:/bin:/usr/local/bin",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _tracked_evaluator_files():
    """The tracked evaluator files, the same set `protocol_digest` hashes."""
    done = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", *kb.EVALUATOR_PATHS],
        capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return sorted(p for p in done.stdout.decode("utf-8").split("\0") if p)


def staged_checkout(root: Path) -> Path:
    """A git checkout holding only the evaluator's tracked files.

    A copy rather than an edit of the live tree: the byte-change arm has to
    modify `tools/evolution_progress.py`, and the suite does not modify the
    checkout it is running in. Built from `git ls-files`, so the staged tree is
    the same tracked set the digest reads and a build directory does not ride
    along.
    """
    for rel in _tracked_evaluator_files():
        src = ROOT / rel
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "evaluator")
    return root


# ------------------------------------------------------- 1. the enumeration

def test_every_evaluator_path_exists_in_the_tree():
    missing = [p for p in kb.EVALUATOR_PATHS if not (ROOT / p).exists()]
    assert not missing, (
        f"EVALUATOR_PATHS names {missing}, which is not in the tree. An "
        f"enumeration that protects a path nothing writes to protects nothing.")
    assert len(kb.EVALUATOR_PATHS) == len(set(kb.EVALUATOR_PATHS)), (
        "EVALUATOR_PATHS names a path twice; a member is a decision and not a "
        "duplicate row")


def test_the_named_views_partition_the_evaluator_set():
    """Three views cover the whole set, and no row is kernel and authority.

    Three rows deliberately appear in TWO views. The census tool and its
    baseline are kernel paths and decide the held-out allowance; the enumeration
    itself defines the held-out fence, so it is on that fence too. Refusing a
    path under both names is the decision, not a duplicate row. The held-out
    files the controller also refuses are the same kind of double membership in
    the other pair.
    """
    kernel = set(kb.KERNEL_PATHS)
    authority = set(protocol.AUTHORITY_FENCE)
    held_out = set(protocol.HELD_OUT_FENCE)
    assert kernel | authority | held_out == set(kb.EVALUATOR_PATHS)
    assert not (kernel & authority)
    assert kernel & held_out == {
        "src/revl/kernel_boundary.py",
        "tools/gate_reference_census.py",
        "tools/gate_reference_census_baseline.json",
    }
    assert held_out & authority == {
        "tools/heldout_scoring.py",
        "tests/test_heldout_scoring.py",
        "tools/build_gate_crate.py",
        "tests/test_selfhost_lower.py",
        "tools/evaluator_protocol.py",
    }


def test_the_fences_are_derived_from_the_enumeration_not_restated():
    assert set(protocol.AUTHORITY_FENCE) == set(kb.evaluator_fence("authority"))
    assert set(protocol.HELD_OUT_FENCE) == set(kb.evaluator_fence("held_out"))
    assert set(kb.KERNEL_PATHS) == set(kb.evaluator_fence("kernel"))
    assert set(protocol.AUTHORITY_FENCE) <= set(kb.EVALUATOR_PATHS)
    assert set(protocol.HELD_OUT_FENCE) <= set(kb.EVALUATOR_PATHS)


def _assigned(path: str, name: str):
    """The value of the `name = ...` assignment in a tool, by AST."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    return None


def test_the_tool_fences_are_derived_rather_than_copied():
    """The acceptance is DERIVED, not equal to today's members.

    An equality check passes for a literal copy that matches the current
    enumeration and fails only after the enumeration moves, which is the drift
    issue #2802 exists to prevent. These read the assignment in each tool and
    hold it to the derivation call, so a tuple literal is refused here even
    while its members are correct.
    """
    controller = _assigned("tools/evolution_controller.py", "AUTHORITY_FENCE")
    assert isinstance(controller, ast.Call), ast.dump(controller) if controller else None
    assert isinstance(controller.func, ast.Name)
    assert controller.func.id == "evaluator_fence"
    assert [a.value for a in controller.args
            if isinstance(a, ast.Constant)] == ["authority"]

    held_out = _assigned("tools/heldout_scoring.py", "HELD_OUT_FENCE")
    assert isinstance(held_out, ast.Attribute), ast.dump(held_out) if held_out else None
    assert held_out.attr == "HELD_OUT_FENCE"
    assert isinstance(held_out.value, ast.Name)
    assert held_out.value.id == "_PROTOCOL"


def test_the_scorecard_assemblers_are_fenced_by_name():
    """Issue #2802's measured gap: these files were on none of the three lists."""
    for path in PROTOCOL_MODULES:
        assert path in kb.EVALUATOR_PATHS, path
        assert path in protocol.AUTHORITY_FENCE, path


def test_every_evaluator_fence_member_exists_in_the_tree():
    for path in protocol.AUTHORITY_FENCE + protocol.HELD_OUT_FENCE:
        assert (ROOT / path).exists(), path


# ------------------------------------------------------- 2. not the subject

def test_the_fence_does_not_forbid_the_subject():
    """The compiler is what the loop evolves; fencing it forbids the work."""
    subject = ("src/revl/lower.py", "selfhost/lower.rvl",
               "backends/python/emit.py", "tests/test_gate_crate_admit.py")
    for path in subject:
        assert path not in protocol.AUTHORITY_FENCE
        assert path not in protocol.HELD_OUT_FENCE


# ------------------------------------------------------- 3. the digest

def test_the_digest_is_deterministic_and_names_its_algorithm(tmp_path):
    root = staged_checkout(tmp_path / "checkout")
    first = kb.protocol_digest(root)
    assert first == kb.protocol_digest(root)
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_one_byte_of_evolution_progress_changes_the_digest(tmp_path):
    """The field the scorecard did not have (issue #2802, acceptance)."""
    root = staged_checkout(tmp_path / "checkout")
    before = kb.protocol_digest(root)
    path = root / "tools" / "evolution_progress.py"
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 0x01]))
    assert kb.protocol_digest(root) != before


def test_a_build_artifact_under_a_fenced_directory_is_not_hashed(tmp_path):
    """`formal/.lake` is generated by `lake`; two rounds must not differ by it."""
    root = staged_checkout(tmp_path / "checkout")
    before = kb.protocol_digest(root)
    build = root / "formal" / ".lake" / "build"
    build.mkdir(parents=True)
    (build / "RevL.olean").write_bytes(b"build output\n")
    assert kb.protocol_digest(root) == before


def test_a_member_that_is_not_tracked_is_a_refusal(tmp_path):
    """An untracked evaluator file must not silently leave the digest."""
    root = staged_checkout(tmp_path / "checkout")
    with pytest.raises(FileNotFoundError):
        kb.protocol_digest(root, ("tools/not_an_evaluator.py",))


def test_an_empty_path_list_is_refused(tmp_path):
    """`git ls-files` with no pathspec hashes the whole tree; an empty view is
    a typo, not a protocol."""
    root = staged_checkout(tmp_path / "checkout")
    with pytest.raises(ValueError):
        kb.protocol_digest(root, ())


def test_the_scorecard_digest_follows_the_protocol_it_was_scored_under(
        tmp_path, monkeypatch):
    """The scorecard JSON carries `protocol_digest`, and the byte arm moves it.

    The checkout the reward reads `ROOT` from is the staged one, so this is the
    same code path a scorer runs in and not a comparison of the digest against
    itself. Changing one byte of `tools/evolution_progress.py` moves the field.
    """
    reward = load_by_path("evolution_reward",
                          ROOT / "tools" / "evolution_reward.py")
    root = staged_checkout(tmp_path / "scorer")
    monkeypatch.setattr(reward, "ROOT", root)
    candidate = reward.load_candidate(
        {"tree": str(root), "base": "HEAD", "scope": ["**"]})

    def card():
        return reward.Scorecard(candidate, []).as_dict()

    before = card()["protocol_digest"]
    assert before == kb.protocol_digest(root)
    path = root / "tools" / "evolution_progress.py"
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 0x01]))
    assert card()["protocol_digest"] != before


def test_a_held_out_refusal_record_carries_the_protocol_digest():
    """Every record names its protocol, refusals included."""
    spec = load_by_path("heldout_scoring",
                        ROOT / "tools" / "heldout_scoring.py")
    record = spec.refusal_record("no-seed")
    assert record["protocol_digest"] == kb.protocol_digest(ROOT)
