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


@pytest.fixture(autouse=True)
def _restore_revl_module_graph():
    """Put the `revl.*` module graph back exactly as this test found it.

    `rescore.load_compiler` deliberately EVICTS every `revl`/`revl.*` entry
    from `sys.modules` and re-imports a fresh set, so the grader it hands back
    is a pristine compiler with no state a prior run could have poisoned (issue
    #478). That is correct for a one-shot script, but in a shared test process
    it leaks: after eviction `sys.modules["revl.parser"]` (and every sibling)
    is a BRAND-NEW module object with brand-new class objects, while every
    already-imported test module still holds names bound to the ORIGINAL ones.
    A later test whose import-time `compile_files` is the old module then parses
    with the old `parser.HostRef` yet reaches a `lower.py` that resolves
    `revl.parser` freshly from `sys.modules` and sees the NEW `HostRef`; the
    `isinstance(body, HostRef)` guard silently turns False and lowering blows up
    with `'HostRef' object has no attribute 'text'`.

    Snapshot the `revl.*` entries before the test and restore that exact
    mapping after, dropping any fresh modules `load_compiler` installed, so the
    process-wide graph the next test sees is byte-identical to before.
    """
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "revl" or name.startswith("revl.")}
    saved_path = list(sys.path)
    try:
        yield
    finally:
        for name in [n for n in sys.modules
                     if n == "revl" or n.startswith("revl.")]:
            del sys.modules[name]
        sys.modules.update(saved)
        sys.path[:] = saved_path


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
