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


def _failing_ir() -> dict:
    """The same composition with a deliberate activation fault in the
    component's body. Item 372 lands the fiber FAILED without raising, so the
    item-334 health gate is what rejects the swap, and `_abort_swap` reloads the
    predecessor as a new generation."""
    ir = copy.deepcopy(_BASE)
    ir["components"][0]["body"].insert(
        0, {"step": "fail",
            "message": {"kind": "lit", "value": "deliberate activation fault"}})
    return ir


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
    session.swap(_ir(), agent_authored=False)

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
    session.swap(_ir(), agent_authored=False)   # generation 2
    session.swap(_ir(), agent_authored=False)   # generation 3

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
        session.swap(_ir(), agent_authored=False)
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
    session.swap(_plain_ir(), agent_authored=False)

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


# ---------------------------------------------------------------------------
# ABORT AFTER A ROLLED-BACK SWAP: `_abort_swap` reloads the predecessor as a
# new generation, so it too must install an owner (issue #2732). A session
# abort after that rollback must still restore what the predecessor changed.
# ---------------------------------------------------------------------------

@needs_cordis
def test_abort_after_a_rolled_back_swap_restores_the_predecessor_mutation(artifact):
    """Load, make a witnessed change, attempt a swap whose successor fails its
    activation, let the health gate roll it back through `_abort_swap`, then
    abort. The predecessor's witnessed change is escrowed across the rollout,
    carried into the reloaded generation's owner, and replayed by the abort.

    The issue's report is that nothing covers this third load path: `swap` and
    `_abort_swap` each reload a generation, and only the abort-after-a-clean-
    swap case was pinned before."""
    from revl.mcp.session import SessionError

    session = _session()
    session.load(_ir(), record=True)
    session.call("ops", "stash", [artifact])
    assert _mutated(artifact), "the predecessor's witnessed change did not apply"

    with pytest.raises(SessionError) as caught:
        session.swap(_failing_ir(), agent_authored=False)
    assert "swap rejected" in str(caught.value)

    # The rollback reloaded the predecessor. The change must still be in the
    # world (the swap rollback is not the session verdict), and the reloaded
    # generation's frame must have joined a live owner whose escrow carries the
    # predecessor's undischarged entry.
    assert _mutated(artifact), (
        "the rolled-back swap replayed the predecessor's inverse by itself")
    frame = _sole_registered_frame(session)
    assert frame._owner is session._owner
    owner = session._owner
    assert owner._escrow, (
        "the rolled-back swap dropped the predecessor's escrow, so the later "
        "abort would restore nothing")

    result = session.abort()
    assert result["aborted"]
    assert _pristine(artifact), (
        "abort after a rolled-back swap restored NOTHING; the predecessor's "
        "witnessed mutation was made permanent")
    assert owner._escrow == [], "the abort left the escrow unreplayed"
    assert result["noResidue"], result["checks"]


# ---------------------------------------------------------------------------
# STRUCTURAL: every path that boots a generation installs a session owner
# before it prepares the module. The admit-turn path is the one caller of
# `_prepare_module` that is NOT a generation load: it plugs components into the
# live generation and rightly keeps the current owner, so it calls neither
# `_install_session_owner` nor the driver's `_load`.
# ---------------------------------------------------------------------------

def test_every_generation_load_installs_a_session_owner_before_preparing():
    """A later load path cannot skip the owner install by convention. Parsed
    from `src/revl/mcp/session.py` per method: a method that calls
    `_prepare_module` and boots a module through the driver's `_load` is a
    generation load, and its `_install_session_owner` call must come first.
    `_boot` is the load path behind `load`, `_cut_over` is the swap's successor
    and `_abort_swap` the rolled-back predecessor. `_wire_turn` (the admit-turn
    plug path) calls `_prepare_module` but no `_load`, so it is not judged as a
    new generation. The exact set of judged methods is asserted, so adding a
    load path forces a reviewer to say which kind it is."""
    import ast

    source = (_ROOT / "src" / "revl" / "mcp" / "session.py").read_text(
        encoding="utf-8")
    generation_loads, offenders = [], []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [child for child in ast.walk(node)
                 if isinstance(child, ast.Call)
                 and isinstance(child.func, ast.Attribute)]
        names = {call.func.attr for call in calls}
        if "_prepare_module" not in names or "_load" not in names:
            continue
        generation_loads.append(node.name)
        prepares = [c.lineno for c in calls if c.func.attr == "_prepare_module"]
        installs = [c.lineno for c in calls
                    if c.func.attr == "_install_session_owner"]
        if not installs:
            offenders.append(
                f"{node.name} boots a generation through _prepare_module and "
                f"_load without calling _install_session_owner")
        elif min(installs) > min(prepares):
            offenders.append(
                f"{node.name} calls _prepare_module (line {min(prepares)}) "
                f"before _install_session_owner (line {min(installs)})")

    assert sorted(generation_loads) == ["_abort_swap", "_boot", "_cut_over"], (
        "the set of methods that boot a generation changed: "
        f"{sorted(generation_loads)}. A new load path must install a session "
        "owner before preparing its module; if it is a plug into the live "
        "generation (like the admit-turn path), it must be named here as such")
    assert not offenders, "\n".join(offenders)
