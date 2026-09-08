"""The exemplary revl web application — the DIFFERENTIATED HALF (item 462, #725).

525's second claim (design docs/design/525-exemplary-web-app.md): the complexity
is reserved for the guarantee revl uniquely provides — a live-reconfigurable,
effect-governed service that is hot-swapped through admission, with VISIBLE
admission, cancellation and residue-free recovery. `examples/app/notes.rvl` grows
ONE such service: a pluggable ranking/scoring component (`Ranker`) that records
engagement signals into effect-created state and scores a note under a strategy;
the strategy is what gets hot-swapped at runtime.

This file proves 525 acceptance bar 4 — the hot-swap scenario demonstrates
admission -> cancellation -> residue-free recovery against the documented service
lifecycle contract (item 460, docs/lifecycle-contract.md) — the way
tests/test_issue_723_lifecycle_contract.py proves the contract itself: by
EXECUTION on the real cordis-py runtime through the same `Session.swap` an agent
drives over the MCP bridge, with each named lifecycle state observed through the
contract's own surface (`Session.state` fiber states + `providedKeys`,
`teardown_disposition`, the `recover` verdict). It uses the REAL admission/swap
machinery (`compile_source(manifest=, replacing=)` + `Session.swap`); it invents
none.

The split mirrors the boring-half suite:

* frontend/IR assertions ALWAYS run — the app declares the `Ranker` service and a
  `TrendingRanker` component that carries a `handoff` (the state contract), whose
  `bump` lowers to a revertible effect (insert/undo remove); and the swap-candidate
  helper below is structurally FAITHFUL to that app component, so the runtime legs
  swap the same thing the app ships;

* the lifecycle walk runs on the real cordis-py runtime when it is installed
  (`sh backends/python/setup.sh`), driving every transition of the contract:
  serving -> admitted swap (state survives, new code live) -> refused swap
  (retained ownership, gen N keeps serving) -> cancel requested/completed
  (released, no residue) -> owned-after-failure (unresolved, stranded) ->
  recovery-resume (roll-forward) / residue-free roll-back. Without the runtime
  these skip with a reason, never reported as a pass.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backends" / "python"
sys.path.insert(0, str(ROOT / "src"))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402
from revl.recovery import recover  # noqa: E402

APP = ROOT / "examples" / "app" / "notes.rvl"

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the differentiated half is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and run "
           "this file under `backends/python/.venv/bin/python -m pytest`",
)


# The swap-candidate source of truth. It is the SAME ranking component the app
# ships (a fidelity assertion below proves the structural equivalence), lifted to
# a standalone composition and parameterized on the two axes a hot-swap moves: the
# scoring `strategy`/`weight` (the CODE that swaps) and the declared `handoff`
# shape (the state contract admission checks). It carries no `use`, so it compiles
# as a bare source string, exactly as test_state_handoff_exec.py drives its cache.
def _ranker(strategy: str = "recency", weight: int = 1, *,
            handoff: str = "Map[Str, Str]", declare_handoff: bool = True,
            requires_missing: bool = False) -> str:
    missing = "service Missing { fn probe() -> Int }\n" if requires_missing else ""
    req = "requires dep: Missing " if requires_missing else ""
    hl = f"  handoff ranking: {handoff}\n" if declare_handoff else ""
    return f"""
{missing}service Ranker {{
  fn strategy() -> Str
  fn signals() -> Int
  fn score(id: Str) -> Int
  fn bump(id: Str)
}}
component TrendingRanker {req}provides ranking: Ranker {{
{hl}  let log = effect Map.new() undo log.drop()
  provide ranking {{
    fn strategy() = "{strategy}"
    fn signals() = log.size()
    fn bump(id) {{
      let k = `sig-${{log.size()}}`
      effect log.insert(k, id)
      undo   log.remove(k)
    }}
    fn score(id) {{
      var n = 0
      for (k of log.keys()) {{
        let hit = match log.get(k) {{ Some(v) => v == id, None => false }}
        if (hit) {{ n = n + {weight} }}
      }}
      return n
    }}
  }}
}}
"""


# --------------------------------------------------------------- frontend / IR

def _ranker_shape(comp: dict) -> dict:
    """The structural fingerprint of a ranking component: its state contract, the
    ranked method surface, and the revertible effect its `bump` lowers to."""
    provide = next(s for s in comp["body"]
                   if s.get("step") == "provide" and s.get("name") == "ranking")
    bump = next(m for m in provide["methods"] if m["name"] == "bump")
    effects = [s for s in bump["body"] if s.get("step") == "effect"]
    return {
        "handoff": comp["handoff"],
        "methods": sorted(m["name"] for m in provide["methods"]),
        "bump_effect": [(e["acquire"]["method"], e["undo"]["method"])
                        for e in effects],
    }


def _app_ranker() -> dict:
    ir = compile_files([str(APP)])
    return next(c for c in ir["components"] if c["name"] == "TrendingRanker")


def test_app_declares_the_hot_swappable_ranker_service():
    """The differentiated half is a first-class service in the app, not a bolt-on:
    the `Ranker` service is declared with its ranked surface, all plain reads/verbs
    (the swap moves the scoring CODE, not the interface)."""
    ir = compile_files([str(APP)])
    assert "Ranker" in ir["services"]
    methods = ir["services"]["Ranker"]["methods"]
    assert set(methods) == {"strategy", "signals", "score", "bump"}
    assert methods["strategy"]["returns"] == "Str"
    assert methods["score"]["returns"] == "Int"
    assert methods["score"]["params"] == [{"name": "id", "type": "Str"}]


def test_ranker_declares_a_state_handoff_contract():
    """A hot-swap must not silently drop live state, so the component declares a
    `handoff`: the SHAPE its engagement log carries across a swap. That declaration
    is what admission points the §5 compatibility relation at (item 53)."""
    comp = _app_ranker()
    assert comp["handoff"] == {"key": "ranking", "type": "Map[Str, Str]"}


def test_bump_lowers_to_a_revertible_effect_with_the_remove_inverse():
    """Every recorded signal is a revertible effect (insert) whose derived inverse
    is the model-store-natural counterpart (remove), so a signal recorded during
    activation reverts in LIFO order on teardown/divert — residue-free."""
    comp = _app_ranker()
    provide = next(s for s in comp["body"]
                   if s.get("step") == "provide" and s.get("name") == "ranking")
    bump = next(m for m in provide["methods"] if m["name"] == "bump")
    effects = [s for s in bump["body"] if s.get("step") == "effect"]
    assert len(effects) == 1, "bump must be exactly one revertible effect"
    assert effects[0]["acquire"]["method"] == "insert"
    assert effects[0]["undo"]["method"] == "remove"


def test_swap_candidate_helper_is_faithful_to_the_app_component():
    """The runtime legs below swap sources built by `_ranker(...)`; this pins that
    helper to what the app actually ships — same state contract, same ranked
    surface, same revertible effect — so the conformance walk proves the deployed
    component, not a divergent stand-in."""
    from_app = _ranker_shape(_app_ranker())
    from_helper = _ranker_shape(
        next(c for c in compile_source(_ranker(), "ranker.rvl")["components"]
             if c["name"] == "TrendingRanker"))
    assert from_app == from_helper


# --------------------------------------------------------------- runtime (gated)

def _loaded(src: str, **load_kw) -> Session:
    session = Session()
    session.load(compile_source(src, "ranker.rvl"), origin={"source": src},
                 **load_kw)
    return session


async def _native_fault():
    raise RuntimeError("native disposer fault")


@needs_cordis
def test_1_serving_a_live_ranking_provider():
    """Serving: a key is serving iff loaded, its provider fiber is ACTIVE, and the
    key resolves to a live provider in ROOT (`providedKeys`). The baseline
    `recency` build answers, weighing each signal 1."""
    session = _loaded(_ranker("recency", 1))
    try:
        for note in ("note-0", "note-0", "note-1"):
            session.call("ranking", "bump", [note])
        state = session.state()
        assert state["loaded"] is True
        assert {c["name"]: c["state"] for c in state["components"]} == {
            "TrendingRanker": "ACTIVE"}
        assert state["providedKeys"] == ["ranking"]
        assert session.call("ranking", "strategy", [])["result"] == "recency"
        assert session.call("ranking", "signals", [])["result"] == 3
        assert session.call("ranking", "score", ["note-0"])["result"] == 2
    finally:
        if session.loaded:
            session.unload()


@needs_cordis
def test_2_admitted_swap_carries_state_and_runs_new_code():
    """Admission (the accepted path): a `weighted` successor is proposed against
    the running manifest, admitted, and swapped in atomically. Because it declares
    a compatible `handoff`, the recorded signals SURVIVE (a warm start, reported by
    the swap), while `strategy()` and the re-weighted `score()` prove the new code
    is live — serving never observed a half-swapped composition."""
    session = _loaded(_ranker("recency", 1))
    try:
        for note in ("note-0", "note-0", "note-1"):
            session.call("ranking", "bump", [note])

        successor = _ranker("weighted", 3)
        candidate = compile_source(successor, "ranker.rvl",
                                   manifest=session.ir, replacing=("TrendingRanker",))
        state = session.swap(candidate, origin={"source": successor})

        # the swap reports the state it carried across (warm, 1 resource).
        assert state.get("handoff") == {
            "ranking": {"component": "TrendingRanker", "migrated": True,
                        "resources": 1}}
        # new code is live ...
        assert session.call("ranking", "strategy", [])["result"] == "weighted"
        # ... and the signals survived: 3 still recorded, re-scored at weight 3.
        assert session.call("ranking", "signals", [])["result"] == 3
        assert session.call("ranking", "score", ["note-0"])["result"] == 6
        assert session.call("ranking", "score", ["note-1"])["result"] == 3
    finally:
        if session.loaded:
            session.unload()


@needs_cordis
def test_3_refused_swap_retains_ownership_and_keeps_serving():
    """Admission (the refused path) — a documented refusal with RETAINED ownership,
    preferred over an uncertain replacement (contract §3). Two independent refusals,
    each leaving generation N untouched and still serving:

      * an incompatible `handoff` shape is refused at admission time (dropping live
        state would be residue), and
      * a successor whose activation is not healthy (an unmet requirement leaves its
        fiber PENDING) is refused by the post-activation health gate, which rolls
        the swap back to gen N.
    """
    session = _loaded(_ranker("recency", 1))
    try:
        session.call("ranking", "bump", ["note-0"])

        # refusal A: incompatible hand-off shape — admission refuses before any
        # teardown; the change is cancelled, not the service (contract §2, a
        # refused swap is a completed cancellation of the CHANGE).
        with pytest.raises(RevlError, match="state hand-off on `ranking` differs"):
            compile_source(_ranker("weighted", 3, handoff="Map[Str, Int]"),
                           "ranker.rvl", manifest=session.ir,
                           replacing=("TrendingRanker",))

        # refusal B: an unhealthy successor — the post-activation health gate
        # refuses and rolls back to gen N (contract §3, the refused swap).
        unhealthy = compile_source(_ranker("weighted", 3, requires_missing=True),
                                   "ranker.rvl", manifest=session.ir,
                                   replacing=("TrendingRanker",))
        with pytest.raises(SessionError) as excinfo:
            session.swap(unhealthy, origin={"source": "unhealthy"})
        assert "swap rejected" in str(excinfo.value)

        # retained ownership: gen N (the ORIGINAL recency build) is untouched and
        # still serving, its recorded signal intact.
        state = session.state()
        assert state["loaded"] is True
        assert state["providedKeys"] == ["ranking"]
        assert session.call("ranking", "strategy", [])["result"] == "recency"
        assert session.call("ranking", "signals", [])["result"] == 1
    finally:
        if session.loaded:
            session.unload()


@needs_cordis
def test_4_cancellation_completes_releasing_ownership_with_no_residue():
    """Cancellation: two transitions, not one. The disposal is INVOKED (requested),
    then the attempt settles RELEASED (completed) — ownership dropped, no residue,
    and `teardown_disposition` reads back `released`. This is the residue-free
    teardown a hot-swap depends on, read at the session seam."""
    session = _loaded(_ranker("recency", 1))
    session.call("ranking", "bump", ["note-0"])

    result = asyncio.run(session.aclose())

    # requested: the disposal was actually invoked and returned.
    assert result["disposal"] == {"invoked": True, "returned": True,
                                   "failed": None, "cancelled": False}
    # completed: settled clean, ownership released, no residue.
    assert result["closed"] is True
    assert result["settled"] is True
    assert result["releaseOwnership"] is True
    assert result["noResidue"] is True
    assert {r["component"]: r["outcome"] for r in result["resources"]} == {
        "TrendingRanker": "returned"}
    assert session.loaded is False
    assert session.teardown_disposition()["attempt"] == "released"


@needs_cordis
def test_5_a_native_disposer_fault_retains_ownership_and_names_what_is_owed():
    """Owned-after-failure: when teardown does NOT complete cleanly, the resource it
    could not release stays OWNED by the session — not dropped, not silently retried.
    A native disposer fault settles the attempt `unresolved`; the session stays
    loaded and inspectable, and `strand_teardown` then accepts the owed state as
    stranded (the runtime's affirmation that a documented refusal beats an uncertain
    cleanup)."""
    session = _loaded(_ranker("recency", 1))
    session.call("ranking", "bump", ["note-0"])
    session._driver.fibers["TrendingRanker"].dispose = _native_fault

    result = asyncio.run(session.aclose())

    assert result["closed"] is False
    assert result["settled"] is False
    assert result["releaseOwnership"] is False
    assert result["nativeCleanupComplete"] is False
    assert {r["component"]: r["outcome"] for r in result["resources"]} == {
        "TrendingRanker": "failed"}
    assert result["unresolved"]["ownedResources"] == ["TrendingRanker"]
    assert "native disposer fault" in result["unresolved"]["error"]

    # retained: still loaded, ownership NOT dropped, inspectable.
    assert session.loaded is True
    disposition = session.teardown_disposition()
    assert disposition["attempt"] == "unresolved"
    assert (disposition["settlement"]["unresolved"]["ownedResources"]
            == ["TrendingRanker"])

    # the host's explicit terminal move: accept the owed state as stranded.
    stranded = session.strand_teardown()
    assert stranded["stranded"] is True
    assert stranded["unresolved"]["ownedResources"] == ["TrendingRanker"]


@needs_cordis
def test_6_recovery_rolls_a_completed_activation_forward_and_resumes(tmp_path):
    """Recovery-resume: `activation-complete` present, so `recover` rolls the
    persisted generation FORWARD, re-admitting it through item 15's restore. The
    verdict resumes and the fresh session comes up loaded — the same admission gate
    a live restore runs."""
    import replay  # noqa: PLC0415 — backends/python, cordis-only

    src = _ranker("recency", 1)
    session = _loaded(src, record=True)
    session.call("ranking", "bump", ["note-0"])
    snap = session.snapshot()
    session.unload()   # the process "died"; only the snapshot + WAL survive

    path = str(tmp_path / "done.wal")
    with replay.WriteAheadLog(path, ir=session.ir or {}, generation=1) as wal:
        wal.commit_activation(["TrendingRanker"])

    fresh = Session()
    try:
        report = recover(path, session=fresh, snapshot=snap)
        assert report["verdict"] == "rolled-forward"
        assert report["resumed"] is True
        assert report["resume"]["resumedForCrashRecovery"] is True
        assert fresh.loaded
    finally:
        if fresh.loaded:
            fresh.unload()


@needs_cordis
def test_6b_a_crash_mid_activation_rolls_back_residue_free(tmp_path):
    """Recovery, the roll-back branch: `activation-complete` absent, so the crash
    happened mid-activation. Every one of the ranker's boundary inverses is an
    in-process Map inverse (memory gone with the process), so recovery reports the
    honest residue-FREE verdict — nothing unreconstructible, the world holding only
    what was deliberately committed — rather than pretending an undo ran."""
    import replay  # noqa: PLC0415 — backends/python, cordis-only

    src = _ranker("recency", 1)
    session = _loaded(src, record=True)
    session.call("ranking", "bump", ["note-0"])

    path = str(tmp_path / "crash.wal")
    tl = session.recorder.timeline("TrendingRanker")
    with replay.WriteAheadLog(path, ir=session.ir, generation=1) as wal:
        wal.append_timeline(tl)   # NO commit -> crashed mid-activation
    session.unload()

    loaded = replay.WriteAheadLog.read(path)
    assert loaded["complete"] is False
    report = recover(path)
    assert report["verdict"] == "rolled-back"
    assert report["unreconstructible"] == []
    assert report["residue"]["clean"] is True
    assert report["residue"]["outstanding"] == []
