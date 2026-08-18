"""Backwards replay over the effect accumulator (docs/replay.md).

These tests drive the *real* pipeline — revl source -> frontend IR -> the
cordis-py emitter -> the recorder -> the timeline — against
:class:`FakeContext`, a stand-in that implements the slice of the cordis
context protocol an emitted component actually uses.  cordis-py is not
installed in this checkout (`backends/python/tests` skips for the same
reason), so a stub is what makes the replay engine executable here; it is a
stub of the *documented contract*, not of cordis's internals, and it is
deliberately strict about the two properties the engine leans on: an
effect's yielded disposers run LIFO, and disposal is single-flight.

What that means for the reader: everything below really runs, but the final
hop — the recorder against cordis-py itself — is exercised by the emitted
shape, not by cordis.  See the report in docs/replay.md §"Status".
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
                 "revl_replay_forward"):
        assert name in tools
    assert tools["revl_timeline"]["annotations"]["readOnlyHint"] is True
    assert tools["revl_inspect_step"]["annotations"]["readOnlyHint"] is True
    # stepping back runs inverses against a live system
    assert tools["revl_step_back"]["annotations"]["destructiveHint"] is True
    assert tools["revl_replay_forward"]["annotations"]["destructiveHint"] is True
    assert "record" in tools["revl_load"]["inputSchema"]["properties"]


def test_replay_tools_say_recording_must_be_switched_on_at_load():
    payload = _call("revl_timeline", {})
    assert payload["ok"] is False
    assert "record" in payload["diagnostics"][0]["message"]


def test_replay_tools_validate_their_arguments():
    assert _call("revl_step_back", {})["diagnostics"][0]["message"].startswith("`to`")
    assert _call("revl_inspect_step", {})["diagnostics"][0]["message"].startswith("`at`")
    assert _call("revl_replay_forward", {})["diagnostics"][0]["message"].startswith("`from`")


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
    # anything else is an ordinary REPL expression, not a command
    assert _replay_command("cache.get('k')") is None
    assert _replay_command("") is None


def test_a_replay_command_missing_its_index_says_so():
    from revl.run import _replay_command

    with pytest.raises(ValueError, match="needs a step index"):
        _replay_command(":back")
    with pytest.raises(ValueError, match="integer step index"):
        _replay_command(":inspect middle")


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
