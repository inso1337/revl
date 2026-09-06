"""Guard for the noSelfScore rule wired into the re-scoring path (issue #478).

`bench/rescore.py` carries `assert_model_free`, which enforces the mechanical
core of docs/eval-protocol.md section 3 before any cell is scored: the grader is
the revl compiler, the grading input is a committed file on disk, and the score
is deterministic. This test does NOT pin any compile-rate number (those move
with the checker and the corpora); it asserts the guard accepts a real model-free
setup and rejects each way the setup could stop being model-free.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import pytest  # noqa: E402

import rescore  # noqa: E402

CORPUS = "typed-deepseek-v4-pro"


def _compiler():
    return rescore.load_compiler(ROOT)


def _cells():
    return rescore.collect(CORPUS, attempt=1)


def test_real_rescore_setup_is_model_free():
    """The committed corpus scored by the real compiler passes every check."""
    compile_source, RevlError, classify = _compiler()
    cells = _cells()
    assert cells, "expected a committed corpus to score"
    # Must not raise.
    rescore.assert_model_free(cells, compile_source, RevlError, classify)


def test_rejects_a_grader_that_is_not_the_compiler():
    """A generation driver standing in for the grader is refused (item 2)."""
    _cs, RevlError, classify = _compiler()
    cells = _cells()

    def run_local(code, name):  # a model-driver-shaped callable, not the compiler
        return {"ok": True}

    with pytest.raises(rescore.SelfScoreError):
        rescore.assert_model_free(cells, run_local, RevlError, classify)


def test_rejects_a_grading_input_outside_the_committed_corpus(tmp_path):
    """A file synthesized outside bench/results is not a valid grading input (item 1)."""
    compile_source, RevlError, classify = _compiler()
    stray = tmp_path / "attempt-1.rvl"
    stray.write_text("component X {}\n")
    cells = [("fake-spec", "v2", stray)]

    with pytest.raises(rescore.SelfScoreError):
        rescore.assert_model_free(cells, compile_source, RevlError, classify)


def test_rejects_a_non_deterministic_grader():
    """A grader whose verdict can vary between two runs is a model, not a compiler (item 3)."""
    _cs, RevlError, classify = _compiler()
    cells = _cells()
    flip = {"n": 0}

    def wobbly(code, name):
        flip["n"] += 1
        if flip["n"] % 2 == 0:
            raise ValueError("crashes on the even call")
        # compiles on the odd call

    # Give the stand-in the compiler identity so it clears item 2 and we isolate
    # the determinism check.
    wobbly.__module__ = "revl"
    wobbly.__name__ = "compile_source"

    with pytest.raises(rescore.SelfScoreError):
        rescore.assert_model_free(cells, wobbly, RevlError, classify)
