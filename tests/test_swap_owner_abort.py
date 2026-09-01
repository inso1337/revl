"""A swap must scope the session commit-state owner around the successor
generation's load (item 245, the swap-owner-scoping fix).

`Session.load` installs a fresh `SessionOwner`, seeds it, and makes it the
process-global owner BEFORE `_load`, so every activation Frame built during the
load joins the owner's live-frame registry — the gate target the session
`abort`/`commit` verbs iterate. `Session.swap` (and its `_abort_swap` rollback)
tore the old generation down and loaded the successor WITHOUT installing an
owner for it, so the successor's frames captured the cleared ambient owner
(`None`): they never `register_frame`, and at teardown took the pre-245 implicit
-commit path instead of the session verdict. Result: a witnessed mutation made
after a swap could not be aborted — the abort reported success and restored
nothing, so the mutation became permanent (the same data-loss class as items
246/369, reached through the swap door).

These tests prove the closed loop through the live cordis-py runtime:

  * HEADLINE — a witnessed mutation made after a swap is RESTORED on abort.
  * MORE-THAN-ONCE — the restore holds across successive swap generations and
    across successive sessions in one process (the "abort works exactly once
    per process" symptom is gone).
  * ADDITIVITY — a non-witnessed, non-instance swap is transparent: it adds no
    deferral-queue entries, no prompts, and leaves teardown residue-free.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session commit protocol is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

# A per-call witnessed rename (class a): `stash` renames p -> p.bak and persists
# on commit / reverts to p on abort. The stateless, instance-free composition
# makes every swap a plain non-instance swap.
_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "swap_owner_abort.rvl")

# A trivial, witnessed-free composition for the additivity check: swapping it
# must be byte-transparent (nothing to escrow, nothing to prompt).
_PLAIN_SOURCE = (
    "service Noop {\n"
    "  emission fn ping()\n"
    "}\n"
    "component Quiet provides noop: Noop {\n"
    "  provide noop {\n"
    "    fn ping() { }\n"
    "  }\n"
    "}\n"
)
_PLAIN_BASE = compile_source(_PLAIN_SOURCE, "swap_plain.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _plain_ir() -> dict:
    return copy.deepcopy(_PLAIN_BASE)


def _session():
    from revl.mcp.session import Session
    return Session()


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    return str(p)


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path: str) -> bool:
    return os.path.exists(path) and not os.path.exists(path + ".bak")


def _sole_registered_frame(session):
    """The one activation frame in the current generation's owner registry —
    the gate target the abort/commit verbs iterate. Read from the registry, not
    `_frame_for_ctx` (a method-only component's activation ctx is not the frame
    key), because the registry membership is exactly what the fix restores."""
    ((frame,),) = (session._owner._registry,)
    return frame


# ---------------------------------------------------------------------------
# HEADLINE: a witnessed mutation made AFTER a swap is restored on abort.
# ---------------------------------------------------------------------------

@needs_cordis
def test_abort_after_swap_restores_the_post_swap_mutation(artifact):
    session = _session()
    session.load(_ir(), record=True)

    # swap to a fresh successor generation (a plain, non-instance swap).
    session.swap(_ir())

    # the successor's frame MUST have joined the new generation's owner registry
    # — the exact regression: without the owner scoping it captured None.
    frame = _sole_registered_frame(session)
    assert frame._owner is session._owner, (
        "the post-swap frame did not join the generation's session owner — its "
        "witnessed mutations would take the pre-245 implicit-commit path")
    assert frame in session._owner._registry

    # a witnessed mutation on the SUCCESSOR generation.
    session.call("ops", "stash", [artifact])
    assert _mutated(artifact), "witnessed mutation did not apply on the call"

    result = session.abort()
    assert result["aborted"]

    # THE PROOF: the post-swap mutation is REVERTED, not permanent.
    assert _pristine(artifact), (
        "abort after a swap restored NOTHING — the post-swap witnessed mutation "
        "was made permanent (the swap-owner-scoping data-loss bug)")
    assert result["noResidue"], result["checks"]


# ---------------------------------------------------------------------------
# MORE-THAN-ONCE: the restore holds across multiple swap generations in one
# session, and across successive sessions in one process. The "abort works
# exactly once per process, then silently succeeds" symptom is gone.
# ---------------------------------------------------------------------------

@needs_cordis
def test_abort_restores_across_multiple_swap_generations(artifact):
    session = _session()
    session.load(_ir(), record=True)
    session.swap(_ir())   # generation 2
    session.swap(_ir())   # generation 3

    frame = _sole_registered_frame(session)
    assert frame in session._owner._registry

    session.call("ops", "stash", [artifact])
    assert _mutated(artifact)

    result = session.abort()
    assert result["aborted"]
    assert _pristine(artifact), "abort after two swaps restored nothing"
    assert result["noResidue"], result["checks"]


@needs_cordis
def test_abort_after_swap_works_more_than_once_per_process(tmp_path):
    # Two independent load -> swap -> mutate -> abort cycles in ONE process.
    # Pre-fix, the post-swap abort restores nothing every time; the point of
    # this test is that BOTH cycles restore (not "the first works, the rest
    # silently succeed while losing data").
    for i in range(2):
        path = tmp_path / f"artifact_{i}.txt"
        path.write_text(f"deliverable {i}", encoding="utf-8")
        target = str(path)

        session = _session()
        session.load(_ir(), record=True)
        session.swap(_ir())
        session.call("ops", "stash", [target])
        assert _mutated(target), f"cycle {i}: mutation did not apply"

        result = session.abort()
        assert result["aborted"]
        assert _pristine(target), (
            f"cycle {i}: abort after a swap restored nothing — the failure "
            f"recurs, so it is not a one-time-per-process artifact")


# ---------------------------------------------------------------------------
# ADDITIVITY: a non-witnessed, non-instance swap is transparent.
# ---------------------------------------------------------------------------

@needs_cordis
def test_non_witnessed_swap_is_transparent(artifact):
    session = _session()
    session.load(_plain_ir())
    session.swap(_plain_ir())

    # the owner is scoped, but with nothing witnessed there is nothing to escrow,
    # nothing queued, and no prompt raised — the scoping is inert.
    owner = session._owner
    assert owner is not None
    assert owner._queue == []
    assert owner._escrow == []
    assert owner.prompts == {"commit": 0, "perCall": 0, "residue": 0}

    # a call fires normally, then a clean unload is residue-free (the implicit
    # terminal commit still works — the owner scoping did not perturb it).
    session.call("noop", "ping", [])
    report = session.unload()
    assert report["unloaded"]
    assert report["noResidue"], report["checks"]
