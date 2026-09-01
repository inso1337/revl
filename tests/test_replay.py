"""Backwards replay over the effect accumulator (docs/replay.md).

Two layers, both driving the real pipeline — revl source -> frontend IR ->
the cordis-py emitter -> the recorder -> the timeline.

The first layer runs against :class:`FakeContext`, a stand-in implementing the
slice of the cordis context protocol an emitted component actually uses.  It
is a stub of the *documented contract*, not of cordis's internals, and it is
deliberately strict about the two properties the engine leans on: an effect's
yielded disposers run LIFO, and disposal is single-flight.  It runs on any
interpreter, so the engine stays testable with no runtime installed.

The second layer (`@needs_cordis`, at the bottom) runs the production path —
`revl.mcp.Session`, a real `cordis.Context`, real fibers — and asserts the
same properties, plus the ones only a real runtime can show: that `unload`
after a step-back still leaves no residue, and that recording does not perturb
a mid-body failure.  Run it with an interpreter that has the runtime:

    backends/python/.venv/bin/python -m pytest tests/test_replay.py -q

See docs/replay.md §7 for exactly what each layer establishes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# imported under their canonical names on purpose: emitted modules do
# `from runtime import ...`, so an aliased copy would be a *different* module
# object and the trace fixture would observe nothing.
import emit as py_emit  # noqa: E402
import replay  # noqa: E402
import runtime as runtime_mod  # noqa: E402


# --------------------------------------------------------------------- sources

USER_CACHE = (ROOT / "examples" / "user_cache.rvl").read_text(encoding="utf-8")
PG_CONFIG = {"PgDatabase": {"url": "postgres://replay-test"}}

NOTES = """
service Notes {
  fn get(key: Str) -> Opt[Str]
  fn put(key: Str, value: Str)
}
component N provides notes: Notes {
  let store = effect Map.new() undo store.drop()
  provide notes {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""

COMPENSATED = """
service Bus { emission fn send(line: Str) }
service Ping { emission fn go(line: Str) }
component B provides bus: Bus {
  let out = effect Map.new() undo out.drop()
  provide bus { fn send(line) { effect out.insert(line, line) undo out.remove(line) } }
}
component P requires bus: Bus provides ping: Ping {
  let seen = effect Map.new() undo seen.drop()
  provide ping {
    fn go(line) {
      effect seen.insert(line, line) undo seen.remove(line)
      emit bus.send(line) compensate bus.send("compensated")
    }
  }
}
"""

AWAITING = """
component W {
  let a = effect Map.new() undo a.drop()
  await Job.run("w")
  effect a.insert("k", "v") undo a.remove("k")
}
"""


# ------------------------------------------------------------- the fake runtime


class _Root:
    """The shared provision store + effect registry of one composition."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.effects: list = []


class FakeContext:
    """A protocol-faithful stand-in for ``cordis.Context``.

    Implements exactly what an emitted component and ``runtime.Frame`` use:

    * ``effect(fn, label)`` runs the generator to completion at registration,
      collects its yielded disposers, and returns a **single-flight** disposer
      that runs them newest-first (the per-effect LIFO the cordis README
      guarantees);
    * ``provide(key)`` returns the withdrawal disposer;
    * ``set(key, value)`` publishes, and attribute access resolves an
      injected key from the same store.

    Nothing else — if the engine ever needed more of cordis than this, the
    test would fail rather than quietly pass.
    """

    def __init__(self, root: _Root) -> None:
        self._root = root

    def effect(self, fn, label=None):
        disposers: list = []
        if inspect.isasyncgenfunction(fn):
            agen = fn()

            async def drive():
                async for value in agen:
                    if value is not None:
                        disposers.append(value)

            asyncio.run(drive())
        else:
            for value in fn():
                if value is not None:
                    disposers.append(value)

        state = {"disposed": False}

        async def dispose():
            if state["disposed"]:
                return None
            state["disposed"] = True
            for disposer in reversed(disposers):
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            return None

        dispose.label = label
        self._root.effects.append(dispose)
        return dispose

    def provide(self, key):
        store = self._root.store
        store.setdefault(key, None)

        def withdraw():
            store.pop(key, None)

        return withdraw

    def set(self, key, value):
        self._root.store[key] = value

    def __getattr__(self, name):
        try:
            return self._root.store[name]
        except KeyError:
            raise AttributeError(name) from None


class Harness:
    """source -> IR -> emitted module -> recorded activation."""

    def __init__(self, source: str, config: dict | None = None,
                 record: bool = True) -> None:
        self.ir = compile_source(source, "<replay-test>.rvl")
        self.source = py_emit.emit(self.ir)
        self.filename = "<replay-test-emitted>"
        self.module = types.ModuleType("revl_replay_emitted")
        sys.modules[self.module.__name__] = self.module
        exec(compile(self.source, self.filename, "exec"), self.module.__dict__)

        self.recorder = None
        if record:
            self.recorder = replay.Recorder(self.ir)
            self.recorder.register_source(self.filename, self.source)
            self.recorder.instrument(self.module, self.ir)

        self.root = _Root()
        config = config or {}
        order = (self.ir.get("manifest") or {}).get("loadOrder") or []
        for name in order:
            plugin = getattr(self.module, name)
            plugin["apply"](FakeContext(self.root), config.get(name, {}))

    # -- driving -----------------------------------------------------------

    def get(self, key: str):
        return self.root.store[key]

    def call(self, key: str, method: str, *args):
        if self.recorder is not None:
            self.recorder.set_origin({"phase": "call", "key": key,
                                      "method": method, "args": list(args)})
        try:
            return getattr(self.root.store[key], method)(*args)
        finally:
            if self.recorder is not None:
                self.recorder.activation_origin()

    def timeline(self, name: str):
        return self.recorder.timeline(name)

    def step_back(self, name: str, to: int, force: bool = False) -> dict:
        return asyncio.run(self.timeline(name).step_back(to, force=force))

    def teardown(self) -> None:
        """What the fiber's own unload would do: dispose every effect,
        newest first."""
        async def run():
            for dispose in reversed(self.root.effects):
                await dispose()

        asyncio.run(run())


@pytest.fixture
def trace():
    events: list = []
    runtime_mod.set_trace(events.append)
    yield events
    runtime_mod.set_trace(None)


def kinds(timeline) -> list:
    return [step.kind for step in timeline.steps]


def ops(events: list) -> list:
    """Trace events with instance serials stripped ('map#3.drop' -> 'map.drop');
    the runtime's serials are process-global, so they are not stable per test."""
    return [re.sub(r"#\d+", "", event) for event in events]


# ------------------------------------------------------------------- recording


def test_the_timeline_records_every_accumulator_step_in_order():
    harness = Harness(USER_CACHE, PG_CONFIG)
    db = harness.timeline("PgDatabase")
    assert kinds(db) == ["effect", "provision", "hinge"]
    assert db.steps[0].source == "yield lambda: pool.close()"
    assert db.steps[0].site.startswith("<replay-test-emitted>:")
    assert db.steps[1].label == "provide db"
    assert db.steps[1].detail == {"key": "db"}
    # every step names the effect it was accumulated into
    assert db.steps[0].effect == "PgDatabase/body"


def test_a_provision_is_identified_by_identity_not_by_guesswork():
    harness = Harness(USER_CACHE, PG_CONFIG)
    db = harness.timeline("PgDatabase")
    provisions = [s for s in db.steps if s.kind == "provision"]
    assert [s.detail["key"] for s in provisions] == ["db"]
    # ...and the hinge is never mistaken for an author step
    hinge = [s for s in db.steps if s.kind == "hinge"]
    assert len(hinge) == 1 and hinge[0].revertible is False


def test_an_emission_is_recorded_and_marked_as_having_no_inverse():
    harness = Harness(USER_CACHE, PG_CONFIG)
    harness.call("cache", "put", "k", "v")
    cache = harness.timeline("UserCache")
    assert kinds(cache) == ["effect", "provision", "hinge", "effect", "emission"]
    emission = cache.steps[4]
    assert emission.label == "db.execute"
    assert emission.revertible is False
    assert emission.detail["service"] == "Database"
    assert emission.detail["args"] == ["INSERT INTO cache_log VALUES (k)"]
    assert "no inverse" in emission.note
    # and the effect that ran just before it does have one
    assert cache.steps[3].revertible is True
    assert cache.steps[3].origin == {"phase": "call", "key": "cache",
                                     "method": "put", "args": ["k", "v"]}


def test_a_non_emission_call_is_not_a_timeline_step():
    harness = Harness(USER_CACHE, PG_CONFIG)
    before = len(harness.timeline("UserCache").steps)
    harness.call("cache", "get", "k")  # `get` is not an emission
    assert len(harness.timeline("UserCache").steps) == before


def test_the_await_boundary_is_recorded_with_nothing_to_undo():
    harness = Harness(AWAITING)
    timeline = harness.timeline("W")
    assert kinds(timeline) == ["effect", "boundary", "effect", "hinge"]
    boundary = timeline.steps[1]
    assert boundary.revertible is False
    assert "A1" in boundary.note


def test_instrumenting_copies_the_plugin_rather_than_mutating_it():
    """A recorded load must not leave the emitted module altered for anyone
    else holding a reference to the original dict."""
    ir = compile_source(NOTES, "<replay-test>.rvl")
    module = types.ModuleType("revl_replay_untouched")
    exec(compile(py_emit.emit(ir), "<x>", "exec"), module.__dict__)
    original = module.N
    original_apply = original["apply"]
    replay.Recorder(ir).instrument(module, ir)
    assert module.N is not original
    assert original["apply"] is original_apply  # the old dict is intact


# ----------------------------------------------------------------- stepping back


def test_stepping_back_over_plain_effects_restores_state(trace):
    """The load-bearing case: an effect's inverse ran, and the state it
    guarded is observably back."""
    harness = Harness(NOTES)
    notes = harness.get("notes")
    harness.call("notes", "put", "k", "v")
    assert notes.get("k") == "v"

    timeline = harness.timeline("N")
    assert kinds(timeline) == ["effect", "provision", "hinge", "effect"]
    report = harness.step_back("N", 2)

    assert [s["label"] for s in report["inversesRan"]] == ["N.notes.put#1/effect"]
    assert report["emissionsCrossed"] == []
    assert report["failed"] == []
    assert notes.get("k") is None            # store.remove(key) really ran
    assert "map.remove k" in ops(trace)
    assert "map.drop" not in ops(trace)      # only the tail was unwound


def test_stepping_back_runs_the_inverses_newest_first(trace):
    harness = Harness(NOTES)
    harness.call("notes", "put", "k", "v")
    del trace[:]
    report = harness.step_back("N", -1)
    # LIFO: the call's effect, then the provision, then the activation effect
    assert [s["kind"] for s in report["inversesRan"]] == [
        "effect", "provision", "effect"]
    assert ops(trace) == ["map.remove k", "map.drop"]


def test_stepping_back_leaves_the_component_live_not_torn_down():
    harness = Harness(NOTES)
    harness.call("notes", "put", "a", "1")
    harness.call("notes", "put", "b", "2")
    timeline = harness.timeline("N")
    assert len(timeline.steps) == 5

    harness.step_back("N", 3)  # undo only the second put

    notes = harness.get("notes")               # still provided
    assert notes.get("b") is None
    assert notes.get("a") == "1"
    notes.put("c", "3")                        # ...and still usable
    assert notes.get("c") == "3"
    assert kinds(timeline)[-1] == "effect"     # the new work is recorded too


def test_stepping_back_past_a_provision_withdraws_it():
    harness = Harness(NOTES)
    harness.step_back("N", 0)
    assert "notes" not in harness.root.store
    report = harness.timeline("N").inspect(0)
    assert report["activeProvisions"] == []
    assert report["withdrawnProvisions"] == ["notes"]


def test_an_out_of_range_step_is_refused():
    harness = Harness(NOTES)
    with pytest.raises(replay.ReplayError) as caught:
        harness.step_back("N", 99)
    assert "out of range" in str(caught.value)
    with pytest.raises(replay.ReplayError):
        harness.step_back("N", -2)


def test_an_inverse_never_runs_twice(trace):
    """Replay and the runtime's own teardown share one once-only inverse, so
    a stepped-back `store.drop()` cannot become a use-after-free."""
    harness = Harness(NOTES)
    harness.step_back("N", -1)
    assert ops(trace).count("map.drop") == 1
    harness.teardown()                    # the fiber unloads afterwards
    assert ops(trace).count("map.drop") == 1  # ...and does not double-free
    timeline = harness.timeline("N")
    assert all(s.undone_by == "step_back"
               for s in timeline.steps if s.undone)


def test_the_runtimes_own_teardown_is_visible_in_the_timeline():
    harness = Harness(NOTES)
    harness.teardown()
    timeline = harness.timeline("N")
    undone = [s for s in timeline.steps if s.undone]
    assert [s.undone_by for s in undone] == ["runtime"] * len(undone)
    assert len(undone) == 2  # the Map effect and the provision


# ------------------------------------------------------------------- emissions


def test_stepping_back_over_an_uncompensated_emission_is_refused():
    """An emission has no inverse. Refusing by default is the honest answer:
    silently skipping it would report a state the unwind did not reach."""
    harness = Harness(USER_CACHE, PG_CONFIG)
    harness.call("cache", "put", "k", "v")
    with pytest.raises(replay.IrreversibleStep) as caught:
        harness.step_back("UserCache", 2)
    message = str(caught.value)
    assert "uncompensated emission" in message
    assert "db.execute" in message
    assert "force" in message
    assert caught.value.emissions[0]["kind"] == "emission"
    # nothing ran: the refusal is total, not partial
    assert harness.get("cache").get("k") == "v"


def test_forcing_the_unwind_reports_exactly_what_was_crossed():
    harness = Harness(USER_CACHE, PG_CONFIG)
    harness.call("cache", "put", "k", "v")
    report = harness.step_back("UserCache", 2, force=True)

    assert [s["label"] for s in report["emissionsCrossed"]] == ["db.execute"]
    assert [s["label"] for s in report["inversesRan"]] == ["UserCache.cache.put#1/effect"]
    assert "still out in the world" in report["warning_emissions"]
    assert report["guarantee"] == replay.GUARANTEE
    assert "restored" not in report          # no claim the runtime cannot make
    assert harness.get("cache").get("k") is None


def test_a_compensated_emission_steps_back_as_compensation_not_inversion():
    harness = Harness(COMPENSATED)
    harness.call("ping", "go", "hello")
    timeline = harness.timeline("P")
    assert kinds(timeline) == ["effect", "provision", "hinge",
                               "effect", "emission", "compensation"]
    emission, compensation = timeline.steps[4], timeline.steps[5]
    assert emission.compensation == compensation.index
    assert compensation.detail == {"for": emission.index}
    assert "§6.1" in compensation.note

    # a compensated emission does not block the unwind, and is reported in
    # its own bucket: crossed-and-offset is not the same as crossed-bare
    report = harness.step_back("P", 3)
    assert report["emissionsCrossed"] == []
    assert [s["label"] for s in report["emissionsCompensated"]] == ["bus.send"]
    assert [s["label"] for s in report["compensationsRan"]] == ["compensate bus.send"]
    assert "not inversion" in report["warning"]
    assert "warning_emissions" not in report

    # ...and running the compensation was itself a boundary crossing, which
    # the timeline now shows as a new emission rather than hiding
    assert kinds(timeline)[-1] == "emission"
    assert harness.get("bus")  # the bus took the compensating send
    assert timeline.steps[-1].detail["args"] == ["compensated"]


def test_inspect_reports_the_composition_at_a_step():
    harness = Harness(USER_CACHE, PG_CONFIG)
    harness.call("cache", "put", "k", "v")
    view = harness.timeline("UserCache").inspect(2)

    assert view["at"] == 2
    assert view["activeProvisions"] == ["cache"]
    # the accumulator at step 2, newest first, hinge excluded
    assert [s["label"] for s in view["accumulated"]] == [
        "provide cache", "UserCache/body/effect"]
    assert [s["label"] for s in view["aheadOfHere"]] == [
        "UserCache.cache.put#1/effect", "db.execute"]
    assert view["emissionsSoFar"] == []

    view_after = harness.timeline("UserCache").inspect(4)
    assert [s["label"] for s in view_after["emissionsSoFar"]] == ["db.execute"]


# ---------------------------------------------------------------- replay forward


def test_replay_forward_re_runs_the_calls_that_produced_the_tail():
    harness = Harness(NOTES)
    harness.call("notes", "put", "a", "1")
    harness.call("notes", "put", "b", "2")
    harness.step_back("N", 3)
    assert harness.get("notes").get("b") is None

    plan = harness.timeline("N").forward_plan(3)
    assert plan["replay"] == [{"kind": "call", "key": "notes", "method": "put",
                               "args": ["b", "2"]}]
    assert plan["notReplayable"] == []

    for item in plan["replay"]:
        harness.call(item["key"], item["method"], *item["args"])
    assert harness.get("notes").get("b") == "2"
    assert harness.get("notes").get("a") == "1"


def test_replay_forward_refuses_to_pretend_about_activation_steps():
    """The emitted body is one generator; its tail cannot be re-entered
    without re-running its head, so the plan says so instead of faking it."""
    harness = Harness(NOTES)
    harness.call("notes", "put", "a", "1")
    harness.step_back("N", -1)
    plan = harness.timeline("N").forward_plan(-1)

    assert plan["replay"] == [{"kind": "call", "key": "notes", "method": "put",
                               "args": ["a", "1"]}]
    reasons = {entry["label"]: entry["reason"] for entry in plan["notReplayable"]}
    assert set(reasons) == {"UserCache/body/effect".replace("UserCache", "N"),
                            "provide notes"}
    assert all("cannot be re-entered" in reason for reason in reasons.values())


def test_replay_forward_deduplicates_a_repeated_call():
    harness = Harness(NOTES)
    harness.call("notes", "put", "a", "1")
    harness.call("notes", "put", "a", "1")
    harness.step_back("N", 2, force=True)
    plan = harness.timeline("N").forward_plan(2)
    assert plan["replay"] == [{"kind": "call", "key": "notes", "method": "put",
                               "args": ["a", "1"]}]


# ------------------------------------------------------------------- bisect
#
# git-bisect for an execution: binary-search the recorded timeline for the
# FIRST step at which an asserted predicate flips, in log2(N) evaluations.


def _notes_timeline(n: int):
    harness = Harness(NOTES)
    for i in range(n):
        harness.call("notes", "put", f"k{i}", str(i))
    return harness, harness.timeline("N")


def _has(args):
    # a predicate that flips once the insert carrying `args` is accumulated
    return (f"any((s.get('origin') or {{}}).get('args') == {args!r} "
            "for s in accumulated)")


def test_bisect_finds_the_flip_in_log2_evaluations():
    import math

    harness, timeline = _notes_timeline(32)
    n = len(timeline.steps)
    # the insert of k20 sits at a known step; find the first step it is present
    target = [s for s in timeline.steps
              if (s.origin or {}).get("args") == ["k20", "20"]][0]

    report = timeline.bisect(_has(["k20", "20"]))

    assert report["flipped"] is True
    assert report["found"] == target.index
    # the headline claim: far fewer evaluations than the naive scan of N
    assert report["evaluations"] <= math.ceil(math.log2(n)) + 2
    assert report["evaluations"] < n
    # and the found index really is the boundary: false at found-1, true at found
    assert timeline.inspect(report["found"] - 1)
    scope = _has(["k20", "20"])
    assert _eval_over_view(timeline, report["found"], scope) is True
    assert _eval_over_view(timeline, report["found"] - 1, scope) is False


def _eval_over_view(timeline, k, predicate):
    view = dict(timeline.inspect(k))
    view["step"] = timeline.steps[k].as_dict() if k >= 0 else None
    return bool(eval(predicate,  # noqa: S307
                     {"__builtins__": replay._SAFE_BUILTINS}, view))


def test_bisect_returns_the_full_record_of_the_found_step():
    """who ran, what it touched, which realm."""
    harness = Harness(USER_CACHE, PG_CONFIG)
    harness.call("cache", "put", "a", "1")
    timeline = harness.timeline("UserCache")

    report = timeline.bisect("len(emissionsSoFar) > 0")

    assert report["flipped"] is True
    record = report["record"]
    assert record["realm"] == "UserCache"                    # which realm
    assert record["whoRan"] == "cache.put('a', '1')"         # who ran
    assert record["touched"]["service"] == "Database"        # what it touched
    assert record["touched"]["args"] == ["INSERT INTO cache_log VALUES (a)"]
    assert report["step"]["kind"] == "emission"


def test_bisect_reports_the_unverified_status_of_the_bisect_path():
    """The bound that must be told: bisect trusts the recorded inverses, and
    `verified effect` (roadmap item 26) is not built, so the effects on the
    path are unverified."""
    _harness, timeline = _notes_timeline(8)
    report = timeline.bisect(_has(["k5", "5"]))

    verified = report["verified"]
    assert verified["status"] == "unverified"
    assert verified["effectsOnPath"] > 0
    assert verified["verifiedOnPath"] == 0
    assert "roadmap item 26" in verified["explanation"]
    # no recorded step carries a `verified` marker today
    assert all("verified" not in s.as_dict() for s in timeline.steps)


def test_bisect_when_the_predicate_never_flips():
    _harness, timeline = _notes_timeline(6)
    report = timeline.bisect("False")

    assert report["flipped"] is False
    assert report["found"] is None
    assert report["valueThroughout"] is False
    assert "never flips" in report["reason"]


def test_bisect_when_the_predicate_flips_at_step_zero():
    _harness, timeline = _notes_timeline(6)
    # true at and after step 0, false only before activation (-1)
    report = timeline.bisect("at >= 0")

    assert report["flipped"] is True
    assert report["found"] == 0
    assert report["fromValue"] is False
    assert report["toValue"] is True


def test_bisect_on_an_empty_timeline_flips_nothing():
    empty = replay.Timeline("Nothing")
    report = empty.bisect("True")
    assert report["flipped"] is False
    assert report["found"] is None
    assert report["evaluations"] == 0


def test_bisect_reports_a_bad_predicate_as_a_replay_error():
    _harness, timeline = _notes_timeline(4)
    with pytest.raises(replay.ReplayError, match="predicate"):
        timeline.bisect("no_such_name_here")


def test_bisect_never_mutates_the_timeline():
    """It reconstructs each probe with inspect; nothing is unwound, so the
    recording is byte-for-byte identical before and after."""
    _harness, timeline = _notes_timeline(10)
    before = [s.as_dict() for s in timeline.steps]
    timeline.bisect(_has(["k7", "7"]))
    after = [s.as_dict() for s in timeline.steps]
    assert before == after
    assert not any(s.undone for s in timeline.steps)


# ------------------------------------------------------------------ honesty


def test_no_report_ever_claims_state_was_restored():
    """The guarantee string is the only thing the engine asserts, and it
    asserts the inverses ran — not that state came back."""
    harness = Harness(USER_CACHE, PG_CONFIG)
    harness.call("cache", "put", "k", "v")
    report = harness.step_back("UserCache", -1, force=True)
    assert "the inverses registered" in report["guarantee"]
    assert "application's own equivalence" in report["guarantee"]
    for key in report:
        assert "restor" not in key.lower()
    assert harness.timeline("UserCache").as_dict()["guarantee"] == replay.GUARANTEE


def test_an_inverse_that_raises_does_not_strand_the_rest_of_the_accumulator():
    """G7 teardown is best-effort and keeps unwinding; so does a step-back,
    and the failure is reported rather than swallowed."""
    harness = Harness(NOTES)
    harness.call("notes", "put", "k", "v")
    timeline = harness.timeline("N")

    boom = timeline.steps[3]
    original = boom.undo

    def exploding(*args, **kwargs):
        raise RuntimeError("undo refused")

    boom.undo = exploding

    report = harness.step_back("N", -1)
    assert [s["label"] for s in report["failed"]] == ["N.notes.put#1/effect"]
    assert "undo refused" in report["failed"][0]["error"]
    # the steps below it still unwound
    assert [s["kind"] for s in report["inversesRan"]] == ["provision", "effect"]
    assert original is not None


# ------------------------------------------------------------------ MCP surface


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def test_the_replay_tools_are_advertised_with_honest_annotations():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    for name in ("revl_timeline", "revl_inspect_step", "revl_step_back",
                 "revl_replay_forward", "revl_replay_bisect"):
        assert name in tools
    assert tools["revl_timeline"]["annotations"]["readOnlyHint"] is True
    assert tools["revl_inspect_step"]["annotations"]["readOnlyHint"] is True
    # stepping back runs inverses against a live system
    assert tools["revl_step_back"]["annotations"]["destructiveHint"] is True
    assert tools["revl_replay_forward"]["annotations"]["destructiveHint"] is True
    # bisect reconstructs each probe (inspect) and never mutates the timeline
    assert tools["revl_replay_bisect"]["annotations"]["readOnlyHint"] is True
    assert tools["revl_replay_bisect"]["annotations"]["destructiveHint"] is False
    assert "record" in tools["revl_load"]["inputSchema"]["properties"]


def test_replay_tools_say_recording_must_be_switched_on_at_load():
    # Deterministic precondition instead of relying on prior tests' shared
    # global session: a fresh session loaded WITHOUT record, so revl_timeline
    # reports that recording must be switched on regardless of test order.
    import revl.mcp.server as _server
    from revl.mcp.session import Session as _Session
    _server.SESSION = _Session()
    _call("revl_load", {"source": (
        "service Notes { fn put(k: Str, v: Str) }\n"
        "component N provides notes: Notes {\n"
        "  let m = effect Map.new() undo m.drop()\n"
        "  provide notes { fn put(k, v) { effect m.insert(k, v) undo m.remove(k) } }\n"
        "}"
    )})
    payload = _call("revl_timeline", {})
    assert payload["ok"] is False
    assert "record" in payload["diagnostics"][0]["message"]


def test_replay_tools_validate_their_arguments():
    assert _call("revl_step_back", {})["diagnostics"][0]["message"].startswith("`to`")
    assert _call("revl_inspect_step", {})["diagnostics"][0]["message"].startswith("`at`")
    assert _call("revl_replay_forward", {})["diagnostics"][0]["message"].startswith("`from`")
    assert _call("revl_replay_bisect", {})["diagnostics"][0]["message"].startswith("`assert`")


# ------------------------------------------------------------------ CLI surface


def test_the_repl_replay_commands_parse():
    from revl.run import _replay_command

    assert _replay_command(":timeline") == ("timeline", None, False, None)
    assert _replay_command(":timeline UserCache") == ("timeline", None, False,
                                                      "UserCache")
    assert _replay_command(":inspect 3") == ("inspect", 3, False, None)
    assert _replay_command(":back -1") == ("back", -1, False, None)
    assert _replay_command(":back 2 !") == ("back", 2, True, None)
    assert _replay_command(":forward 2 N") == ("forward", 2, False, "N")
    # :bisect carries a free-form predicate in the `at` slot, not an index
    assert _replay_command(":bisect len(emissionsSoFar) > 0") == (
        "bisect", "len(emissionsSoFar) > 0", False, None)
    assert _replay_command(":bisect @UserCache 'db' in activeProvisions") == (
        "bisect", "'db' in activeProvisions", False, "UserCache")
    # anything else is an ordinary REPL expression, not a command
    assert _replay_command("cache.get('k')") is None
    assert _replay_command("") is None


def test_a_replay_command_missing_its_index_says_so():
    from revl.run import _replay_command

    with pytest.raises(ValueError, match="needs a step index"):
        _replay_command(":back")
    with pytest.raises(ValueError, match="integer step index"):
        _replay_command(":inspect middle")
    with pytest.raises(ValueError, match="predicate expression"):
        _replay_command(":bisect")


def test_run_accepts_record_without_a_runtime_installed():
    """`--record` is a plain flag on `revl run`, and the frontend stays pure:
    `--plan` must still work with no cordis anywhere."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "revl", "run", "--record", "--plan",
         str(ROOT / "examples" / "user_cache.rvl")],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        check=False)
    assert result.returncode == 0, result.stderr
    assert "load order" in result.stdout


# ------------------------------------------------- against the real cordis-py
#
# Everything above runs against FakeContext. This section runs the *production*
# path — revl.mcp.Session, a real cordis.Context, real fibers — and asserts the
# same properties. It skips where cordis-py is absent (the root .venv); it
# executes under backends/python/.venv, which has it.

# NOT a module-level `pytest.importorskip`: that skips the whole file, which
# would silently drop the 28 stub-context tests above wherever cordis-py is
# absent. The marker skips only the tests that genuinely need a real runtime.
try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")

# safe without cordis: session.py imports the runtime lazily, inside _backend()
from revl.mcp.session import Session, SessionError  # noqa: E402


@pytest.fixture
def session():
    live = Session()
    yield live
    if live.loaded:
        live.unload()


def real(live, source: str, config: dict | None = None) -> dict:
    return live.load(compile_source(source, "<replay-test>.rvl"), config,
                     record=True)


def step_kinds(payload: dict) -> list:
    return [step["kind"] for step in payload["steps"]]


@needs_cordis
def test_real_cordis_records_the_same_timeline(session):
    """The classification is not an artifact of the stub: a real
    `ctx.provide` disposer is still identified, by identity, as a provision."""
    real(session, NOTES)
    session.call("notes", "put", ["k", "v"])
    payload = session.timeline("N")
    assert step_kinds(payload) == ["effect", "provision", "hinge", "effect"]
    assert payload["steps"][0]["source"] == "yield lambda: store.drop()"
    assert payload["steps"][1]["label"] == "provide notes"
    assert payload["steps"][3]["origin"] == {"phase": "call", "key": "notes",
                                             "method": "put", "args": ["k", "v"]}


@needs_cordis
def test_real_cordis_step_back_restores_state_and_leaves_it_live(session):
    real(session, NOTES)
    session.call("notes", "put", ["k", "v"])
    assert session.call("notes", "get", ["k"])["result"] == "v"

    report = session.step_back("N", 2)
    assert [s["label"] for s in report["inversesRan"]] == ["N.notes.put#1/effect"]
    assert report["providedKeys"] == ["notes"]
    assert session.call("notes", "get", ["k"])["result"] is None

    # not torn down: the fiber is still ACTIVE and the service still works
    assert session.state()["components"] == [{"name": "N", "state": "ACTIVE"}]
    session.call("notes", "put", ["z", "9"])
    assert session.call("notes", "get", ["z"])["result"] == "9"


@needs_cordis
def test_real_cordis_step_back_then_unload_still_leaves_no_residue(session):
    """The once-only inverse against the real fiber: replay ran `store.drop()`
    early, and the runtime's own unload must neither double-free it nor skip
    anything else. R4 has to survive time travel."""
    real(session, NOTES)
    session.call("notes", "put", ["k", "v"])
    session.step_back("N", -1)

    report = session.unload()
    assert report["noResidue"] is True
    assert report["checks"] == {"registry": True, "provisions": True,
                                "effects": True, "listeners": True}


@needs_cordis
def test_real_cordis_refuses_then_forces_across_an_emission(session):
    real(session, USER_CACHE, PG_CONFIG)
    session.call("cache", "put", ["k", "v"])
    assert step_kinds(session.timeline("UserCache")) == [
        "effect", "provision", "hinge", "effect", "emission"]

    with pytest.raises(SessionError, match="uncompensated emission"):
        session.step_back("UserCache", 2)
    assert session.call("cache", "get", ["k"])["result"] == "v"  # nothing ran

    report = session.step_back("UserCache", 2, force=True)
    assert [s["label"] for s in report["emissionsCrossed"]] == ["db.execute"]
    assert "still out in the world" in report["warning_emissions"]
    assert session.call("cache", "get", ["k"])["result"] is None


@needs_cordis
def test_real_cordis_compensation_appends_a_new_emission(session):
    real(session, COMPENSATED)
    session.call("ping", "go", ["hello"])
    assert step_kinds(session.timeline("P")) == [
        "effect", "provision", "hinge", "effect", "emission", "compensation"]

    report = session.step_back("P", 3)
    assert [s["label"] for s in report["compensationsRan"]] == ["compensate bus.send"]
    assert [s["label"] for s in report["emissionsCompensated"]] == ["bus.send"]
    assert report["emissionsCrossed"] == []
    # stepping back over a compensated emission GROWS the emission record
    assert step_kinds(session.timeline("P"))[-1] == "emission"


@needs_cordis
def test_real_cordis_records_the_a1_boundary_of_an_async_body(session):
    """The riskiest wrapper: an `await` body compiles to an async generator,
    and the recorder has to re-wrap it as one without disturbing A1."""
    state = real(session, AWAITING)
    assert state["components"] == [{"name": "W", "state": "ACTIVE"}]
    assert step_kinds(session.timeline("W")) == [
        "effect", "boundary", "effect", "hinge"]
    report = session.step_back("W", 0)
    assert [s["label"] for s in report["inversesRan"]] == ["W/body/effect"]


@needs_cordis
def test_real_cordis_withdraws_a_provision_without_tearing_the_fiber_down(session):
    real(session, COMPENSATED)
    report = session.step_back("P", 0)
    assert report["providedKeys"] == ["bus"]          # `ping` is gone
    view = session.inspect_step("P", 0)
    assert view["activeProvisions"] == []
    assert view["withdrawnProvisions"] == ["ping"]
    # ...and P is still ACTIVE — withdrawn is not disposed
    assert {c["name"]: c["state"] for c in session.state()["components"]} == {
        "B": "ACTIVE", "P": "ACTIVE"}


@needs_cordis
def test_real_cordis_replay_forward_re_runs_the_call(session):
    real(session, COMPENSATED)
    session.call("ping", "go", ["one"])
    session.step_back("P", 3, force=True)
    before = len(session.timeline("P")["steps"])

    plan = session.replay_forward("P", 3)
    assert plan["replay"] == [{"kind": "call", "key": "ping", "method": "go",
                               "args": ["one"]}]
    assert plan["notReplayable"] == []
    assert plan["replayed"][0]["key"] == "ping"
    # the re-run accumulated fresh steps rather than resurrecting the old ones
    assert len(session.timeline("P")["steps"]) > before


@needs_cordis
def test_real_cordis_bisect_finds_the_first_emission_step(session):
    """Through the production path — a real cordis.Context, real fibers —
    bisect binary-searches the recorded timeline and finds the step where the
    predicate flips, reporting it unverified without ever mutating the
    timeline."""
    real(session, USER_CACHE, PG_CONFIG)
    session.call("cache", "put", ["a", "1"])
    before = session.timeline("UserCache")["steps"]

    report = session.bisect("UserCache", "len(emissionsSoFar) > 0")

    assert report["flipped"] is True
    assert report["step"]["kind"] == "emission"
    assert report["record"]["realm"] == "UserCache"
    assert report["record"]["whoRan"] == "cache.put('a', '1')"
    assert report["verified"]["status"] == "unverified"
    # read-only: the recording is untouched, so a later step_back still works
    assert session.timeline("UserCache")["steps"] == before
    # the log2 advantage is proved on a large timeline in the stub layer; on
    # this 5-step one it is at worst a wash, never worse than the naive scan
    assert report["evaluations"] <= len(before)


@needs_cordis
def test_real_cordis_bisect_is_exposed_as_an_mcp_tool():
    import revl.mcp.server as _server
    from revl.mcp.session import Session as _Session
    _server.SESSION = _Session()
    _call("revl_load", {"source": USER_CACHE, "config": PG_CONFIG,
                        "record": True})
    _call("revl_call", {"key": "cache", "method": "put", "args": ["a", "1"]})
    payload = _call("revl_replay_bisect",
                    {"component": "UserCache", "assert": "len(emissionsSoFar) > 0"})
    assert payload["ok"] is True
    assert payload["flipped"] is True
    assert payload["verified"]["status"] == "unverified"


# -- recording must not change what it observes ----------------------------

SYNC_FAIL = """
component F {
  config { url: Str }
  let a = effect Map.new() undo a.drop()
  let p = effect Pool.open(config.url, 2) undo p.close()
  let b = effect Map.new() undo b.drop()
}
"""

ASYNC_FAIL = """
component G {
  config { url: Str }
  let a = effect Map.new() undo a.drop()
  await Job.run("g")
  let p = effect Pool.open(config.url, 2) undo p.close()
  let b = effect Map.new() undo b.drop()
}
"""


def _fail_outcome(source: str, component: str, record: bool) -> tuple:
    live = Session()
    try:
        state = live.load(compile_source(source, "<replay-test>.rvl"),
                          {component: {"url": "boom://nope"}}, record=record)
        states = {c["name"]: c["state"] for c in state["components"]}
        return states, live.unload()["noResidue"]
    finally:
        if live.loaded:
            live.unload()


@needs_cordis
def test_recording_does_not_perturb_a_sync_mid_body_failure():
    """A8: the acquisition refuses, the completed inverses run LIFO, the fiber
    lands FAILED and leaves no residue — with recording on, identically."""
    plain, plain_clean = _fail_outcome(SYNC_FAIL, "F", record=False)
    recorded, recorded_clean = _fail_outcome(SYNC_FAIL, "F", record=True)
    assert plain == {"F": "FAILED"}
    assert recorded == plain
    assert plain_clean is True and recorded_clean is True


@needs_cordis
def test_recording_does_not_perturb_an_async_mid_body_failure():
    """cordis-py routes an *async* body's mid-body failure to the effect guard
    rather than the fiber's error slot, so the fiber does not land FAILED (a
    known cordis-py gap, reported independently by the fault-injection work).

    This test does not assert which state that is — it asserts the property
    this recorder owns: recording observes the failure without changing it.
    """
    plain, plain_clean = _fail_outcome(ASYNC_FAIL, "G", record=False)
    recorded, recorded_clean = _fail_outcome(ASYNC_FAIL, "G", record=True)
    assert recorded == plain
    assert plain_clean is True and recorded_clean is True


@needs_cordis
def test_the_timeline_witnesses_a_partial_unwind_after_a_failure():
    """The accumulator is the honest record of a failed activation: the steps
    that completed, and who undid them."""
    live = Session()
    try:
        live.load(compile_source(SYNC_FAIL, "<replay-test>.rvl"),
                  {"F": {"url": "boom://nope"}}, record=True)
        steps = live.timeline("F")["steps"]
        # only the first effect ever made it into the accumulator, and the
        # runtime — not a step_back — is what unwound it
        assert [s["kind"] for s in steps] == ["effect"]
        assert steps[0]["undone"] is True
        assert steps[0]["undoneBy"] == "runtime"
    finally:
        if live.loaded:
            live.unload()
