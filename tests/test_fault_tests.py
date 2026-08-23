"""`fault test`: the declarable L-Raise experiment (docs/fault-tests.md).

Three layers, gated separately so a missing runtime never looks like a pass:

* **frontend** — syntax, the injection scheme, the compile-time refusals, the
  IR shape.  No runtime needed; these always run.
* **backend contract** — the py tier lowers the section, the other four refuse
  it by name.  No runtime needed; these always run.
* **judging** — the residue/LIFO/emission verdicts and their wording, driven
  against a fabricated outcome so the *failure* paths are covered without
  having to break a real runtime on purpose.  No runtime needed.
* **execution** — real activations on a real `cordis.Context`.  Skipped with a
  reason when cordis-py is absent (`sh backends/python/setup.sh`).
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402


def _emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"revl_fault_{tier}_emit", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# a two-component composition: `Sink` provides the emission target, `Store`
# accumulates map -> (emit) -> pool -> provision, so every kind of inverse a
# body can yield is represented before the injection point
SOURCE = '''
service Log { emission fn write(line: Str) }
service Cache { fn put(k: Str, v: Str) }

component Sink provides log: Log {
  provide log { fn write(line) { } }
}

component Store requires log: Log provides cache: Cache {
  config { url: Str = "postgres://primary/app" }
  let scratch = effect Map.new() undo scratch.drop()
  emit log.write("store coming up")
  let pool = effect Pool.open(config.url, 2) undo pool.close()
  provide cache {
    fn put(k, v) { effect scratch.insert(k, v) undo scratch.remove(k) }
  }
}
'''


def _with(fault_tests: str) -> dict:
    return compile_source(SOURCE + fault_tests, "fault.rvl")


# ---------------------------------------------------------------- frontend


def test_fault_test_lowers_to_an_ir_section():
    ir = _with('''
fault test "db dies mid-activation" for Store {
  fail at step 3
  assert failed
  assert no residue
  assert inverses lifo
  assert siblings unaffected
}
''')
    assert ir["fault_tests"] == [{
        "name": "db dies mid-activation",
        "component": "Store",
        "at": {"step": 3},
        "assert": ["failed", "no-residue", "inverses-lifo", "siblings-unaffected"],
    }]
    # a fault-test section is a v3 feature: the version itself is the guard
    assert ir["ir_version"] == 3


def test_named_effect_resolves_to_a_step_index_and_keeps_the_name():
    """`fail at effect X` is sugar — one addressing scheme reaches the
    backends, but the diagnostics keep the name the author wrote."""
    ir = _with('fault test "t" for Store { fail at effect pool  assert failed }')
    assert ir["fault_tests"][0]["at"] == {"step": 3, "effect": "pool"}


def test_config_rides_with_the_fault_test():
    ir = _with('''
fault test "t" for Store with { url: "postgres://replica/app" } {
  fail at step 1
  assert failed
}
''')
    assert ir["fault_tests"][0]["config"] == {"url": "postgres://replica/app"}


def test_fault_is_a_contextual_keyword_not_a_reserved_word():
    """Adding this form must not break a program that already says `fault`."""
    ir = compile_source(
        'pub fn blame(fault: Int) -> Int { let fault2 = fault + 1  return fault2 }',
        "contextual.rvl")
    assert ir["functions"][0]["name"] == "blame"


def test_documents_without_fault_tests_carry_no_section():
    assert "fault_tests" not in compile_source(SOURCE, "plain.rvl")


@pytest.mark.parametrize("block, expected", [
    ('fault test "t" for Nope { fail at step 1  assert failed }',
     "names unknown component"),
    ('fault test "t" for Store { fail at step 9  assert failed }',
     "is past the end of"),
    ('fault test "t" for Store { fail at effect nope  assert failed }',
     "has no `let … effect` step bound to"),
    ('fault test "t" for Store { fail at step 0  assert failed }',
     "1-based"),
    ('fault test "t" for Store { assert failed }',
     "has no `fail at …` injection point"),
    ('fault test "t" for Store { fail at step 1 }',
     "asserts nothing"),
    ('fault test "t" for Store { fail at step 1  assert nonsense }',
     "unknown fault-test assertion"),
    ('fault test "t" for Store { fail at step 1  assert no bananas }',
     "expected `residue` or `emissions`"),
    ('fault test "t" for Store with { nope: 1 } { fail at step 1  assert failed }',
     "has no config field"),
    ('fault test "t" for Store { fail at step 1  fail at step 2  assert failed }',
     "already has an injection point"),
    ('fault test "t" for Store { fail at step 1  assert failed }\n'
     'fault test "t" for Store { fail at step 2  assert failed }',
     "duplicate fault test"),
    ('fault test "t" Store { fail at step 1  assert failed }',
     "expected `for <component>`"),
])
def test_refusals(block, expected):
    with pytest.raises(RevlError) as caught:
        _with(block)
    assert expected in str(caught.value)


# ------------------------------------------------------- backend contract


_ONE = 'fault test "t" for Store { fail at step 3  assert no residue }'


def test_python_tier_lowers_the_section_into_the_module():
    module = _emitter("python").emit(_with(_ONE))
    assert "REVL_FAULT_TESTS" in module
    assert "'component': 'Store'" in module
    assert "'step': 3" in module


@pytest.mark.parametrize("tier", ["typescript", "rust", "java", "wasm"])
def test_other_tiers_refuse_the_section_by_name(tier):
    """Requirement: never a *silent* mis-emit. Each tier names the fault
    tests it cannot lower and points at the doc."""
    emit = _emitter(tier)
    with pytest.raises(emit.EmitError) as caught:
        emit.emit(_with(_ONE))
    message = str(caught.value)
    assert "fault tests do not lower" in message
    assert "'t'" in message
    assert "docs/fault-tests.md" in message


# wasm is absent here for a reason unrelated to fault tests: this fixture's
# services take `Str` params, which the wasm tier does not lower (i32 only).
@pytest.mark.parametrize("tier", ["typescript", "rust", "java"])
def test_other_tiers_still_emit_the_same_document_without_the_section(tier):
    """The refusal is about the section, not about the document."""
    emit = _emitter(tier)
    assert emit.emit(compile_source(SOURCE, "plain.rvl"))


def test_test_runner_strips_the_section_for_a_foreign_tier(capsys):
    from revl.test import _fault_note, _without_fault_tests

    ir = _with(_ONE)
    assert _without_fault_tests(ir).get("fault_tests") is None
    assert ir.get("fault_tests"), "the original document must not be mutated"
    note = _fault_note(ir, "ts")
    assert "1 fault test(s) skipped" in note
    assert "py reference tier only" in capsys.readouterr().out


# -------------------------------------------------------- static reading


def test_injection_replaces_the_named_step():
    ir = _with(_ONE)
    unit = fault_mod.fault_units(ir)[0]
    mutated = fault_mod._inject(ir, unit)
    body = next(c["body"] for c in mutated["components"] if c["name"] == "Store")
    original = next(c["body"] for c in ir["components"] if c["name"] == "Store")
    assert body[2] == {"step": "fail", "message": {
        "kind": "lit", "value": 'fault test "t": injected failure'}}
    # steps 1..N-1 survive untouched; step N is pushed past the failure and
    # therefore never runs
    assert body[:2] == original[:2]
    assert body[3:] == original[2:]
    assert "fault_tests" not in mutated
    assert ir["components"][1]["body"] == original, "the source IR must not be mutated"


def test_inverse_labels_name_every_kind_of_yielding_step():
    body = next(c["body"] for c in _with(_ONE)["components"] if c["name"] == "Store")
    assert fault_mod._inverse_labels(body, 4) == [
        "step 1 (undo of effect `scratch`)",
        "step 3 (undo of effect `pool`)",
        "step 4 (withdrawal of provision `cache`)",
    ]
    # `emit` without a `compensate` accumulates nothing, so step 2 is absent


def test_inverse_labels_degrade_rather_than_guess_under_a_conditional():
    """Surface revl forbids anything but `fail` inside a component `if` (G6),
    so this guard is for IR that did not come from a .rvl file (imported WIT,
    hand-written documents). It must degrade to ordinals, never guess."""
    body = [
        {"step": "if", "cond": {"kind": "lit", "value": True},
         "then": [{"step": "let-effect", "bind": "m",
                   "acquire": {"kind": "host", "fn": "Map.new", "args": []},
                   "undo": {"kind": "lit", "value": None}}]},
        {"step": "let-effect", "bind": "p",
         "acquire": {"kind": "host", "fn": "Map.new", "args": []},
         "undo": {"kind": "lit", "value": None}},
    ]
    assert fault_mod._inverse_labels(body, len(body)) == []
    outcome = fault_mod._Outcome()
    assert fault_mod._label(outcome, 2) == "inverse #2"


def test_a_conditional_emission_is_found_and_marked_conditional():
    body = [
        {"step": "if", "cond": {"kind": "lit", "value": True},
         "then": [{"step": "emit", "expr": {
             "kind": "call", "target": {"kind": "req", "name": "log"},
             "method": "write", "args": [{"kind": "lit", "value": "hi"}]}}]},
    ]
    assert fault_mod._emissions_before(body, len(body)) == [
        ("step 1: emit log.write('hi')", False, True)]


def test_emissions_before_the_failure_point_are_found_and_described():
    body = next(c["body"] for c in _with(_ONE)["components"] if c["name"] == "Store")
    assert fault_mod._emissions_before(body, 2) == [
        ("step 2: emit log.write('store coming up')", False, False)]
    # nothing has emitted yet at step 1
    assert fault_mod._emissions_before(body, 0) == []


# ------------------------------------------------------------- judging
#
# The verdicts are exercised against a fabricated outcome: breaking a real
# runtime on purpose is not reproducible, but the *wording* of a failure is
# the deliverable (a bare assertion failure is not good enough), so it is
# tested directly.


class _FiberState:
    FAILED = "FAILED"
    ACTIVE = "ACTIVE"


def _outcome(**overrides):
    outcome = fault_mod._Outcome()
    outcome.state = _FiberState.FAILED
    outcome.siblings = {"Sink": _FiberState.ACTIVE}
    outcome.baseline = {"registry": 1, "provisions": ["log"], "effects": 1, "listeners": {}}
    outcome.unwound = {"registry": 2, "provisions": ["log"], "effects": 2, "listeners": {}}
    outcome.settled = {"registry": 1, "provisions": ["log"], "effects": 1, "listeners": {}}
    outcome.accumulated = 2
    outcome.ran = [2, 1]
    outcome.labels = ["step 1 (undo of effect `scratch`)", "step 3 (undo of effect `pool`)"]
    for key, value in overrides.items():
        setattr(outcome, key, value)
    return outcome


_ALL = {"name": "t", "component": "Store", "step": 3,
        "assert": ["failed", "no-residue", "inverses-lifo", "siblings-unaffected"]}


def test_a_clean_unwind_passes_every_assertion():
    assert fault_mod._judge(_ALL, _outcome(), _FiberState) == []


def test_leaked_provision_is_named():
    problems = fault_mod._judge(
        _ALL, _outcome(unwound={"registry": 2, "provisions": ["cache", "log"],
                                "effects": 2, "listeners": {}}), _FiberState)
    assert any("provision(s) `cache` survived the unwind" in p for p in problems)


def test_an_inverse_that_never_ran_is_named():
    problems = fault_mod._judge(_ALL, _outcome(ran=[2], never_ran=[1]), _FiberState)
    assert any("1 of 2 accumulated inverse(s) never ran — "
               "step 1 (undo of effect `scratch`)" in p for p in problems)


def test_out_of_lifo_order_names_both_inverses():
    outcome = _outcome(ran=[1, 2], lifo_violation=(1, 1, 2))
    problems = fault_mod._judge(_ALL, outcome, _FiberState)
    assert any("unwind position 1 ran step 1 (undo of effect `scratch`), "
               "expected step 3 (undo of effect `pool`)" in p for p in problems)


def test_left_over_effects_after_the_handle_is_dropped_are_named():
    problems = fault_mod._judge(
        _ALL, _outcome(settled={"registry": 1, "provisions": ["log"],
                                "effects": 4, "listeners": {}}), _FiberState)
    assert any("residue in the effect stack: 4 disposable(s)" in p for p in problems)


def test_leaked_listener_is_named():
    problems = fault_mod._judge(
        _ALL, _outcome(unwound={"registry": 2, "provisions": ["log"],
                                "effects": 2, "listeners": {"tick": 1}}), _FiberState)
    assert any("residue in the event hooks" in p for p in problems)


def test_a_harmed_sibling_is_named():
    problems = fault_mod._judge(
        _ALL, _outcome(siblings={"Sink": "PENDING"}), _FiberState)
    assert any("Sink is PENDING" in p for p in problems)


def test_not_landing_failed_is_named():
    problems = fault_mod._judge(_ALL, _outcome(state=_FiberState.ACTIVE), _FiberState)
    assert any("did not land FAILED — it is ACTIVE" in p for p in problems)


def test_an_async_body_that_stays_active_gets_the_known_divergence_hint():
    problems = fault_mod._judge(
        _ALL, _outcome(state=_FiberState.ACTIVE, async_body=True), _FiberState)
    assert any("compiles to an async generator" in p and "inverses DID run" in p
               for p in problems)


def test_no_emissions_reports_the_emission_it_found():
    unit = dict(_ALL, **{"assert": ["no-emissions"]})
    outcome = _outcome(emissions=[("step 2: emit log.write('x')", False, False)])
    problems = fault_mod._judge(unit, outcome, _FiberState)
    assert any("an emission cannot be reverted" in p and "step 2" in p for p in problems)


def test_emissions_are_reported_as_irreversible_even_on_a_pass():
    """`assert no residue` must never read as "the emission was undone"."""
    outcome = _outcome(emissions=[("step 2: emit log.write('x')", False, False)])
    assert fault_mod._judge(_ALL, outcome, _FiberState) == []   # residue-clean
    notes = fault_mod._notes(outcome)
    assert any("irreversible" in n and "was NOT reverted by the unwind" in n for n in notes)


def test_a_compensated_emission_is_not_called_reverted():
    outcome = _outcome(emissions=[("step 2: emit bus.publish('x')", True, False)])
    note = " ".join(fault_mod._notes(outcome))
    assert "`compensate` ran" in note and "compensation is not inversion" in note


def test_a_conditional_emission_is_flagged_as_maybe():
    outcome = _outcome(emissions=[("step 2: emit log.write('x')", False, True)])
    assert "may not have run" in " ".join(fault_mod._notes(outcome))


def test_the_failed_registration_is_reported_as_not_residue():
    note = " ".join(fault_mod._notes(_outcome()))
    assert 'registry 1 -> 2' in note and '"error recorded", not residue' in note


def test_probe_lifo_bookkeeping():
    """FaultProbe is the only observer of the two orders; check its algebra
    directly (it lives in the backend runtime and needs no cordis)."""
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime as runtime_mod

    probe = runtime_mod.FaultProbe("C")
    probe.accumulated = [1, 2, 3]
    probe.ran = [3, 2, 1]
    assert probe.lifo_violation() is None
    assert probe.never_ran() == []

    probe.ran = [3, 1, 2]
    assert probe.lifo_violation() == (2, 1, 2)

    probe.ran = [3, 2]
    assert probe.lifo_violation() == (3, None, 1)
    assert probe.never_ran() == [1]


# ------------------------------------------------------------ execution

# `importorskip` at module level would skip *everything* above, including the
# frontend and judging tests, which need no runtime at all.
_HAS_CORDIS = importlib.util.find_spec("cordis") is not None
needs_cordis = pytest.mark.skipif(
    not _HAS_CORDIS,
    reason="fault tests activate for real; they need the cordis-py runtime "
           "(sh backends/python/setup.sh)")


def _run(block: str, capsys):
    from revl.test import test_command

    code = test_command(_with(block), "py")
    return code, capsys.readouterr().out


@needs_cordis
def test_a_real_activation_unwinds_lifo_with_no_residue(capsys):
    code, out = _run('''
fault test "db dies mid-activation" for Store {
  fail at step 3
  assert failed
  assert no residue
  assert inverses lifo
  assert siblings unaffected
}
''', capsys)
    assert code == 0, out
    assert "PASS db dies mid-activation [Store dies at step 3]" in out
    assert "irreversible: step 2: emit log.write('store coming up')" in out
    assert "NOT reverted by the unwind" in out


@needs_cordis
def test_a_real_activation_at_the_first_step_accumulates_nothing(capsys):
    code, out = _run('''
fault test "dies immediately" for Store with { url: "postgres://replica/app" } {
  fail at effect scratch
  assert failed
  assert no residue
  assert inverses lifo
  assert no emissions
}
''', capsys)
    assert code == 0, out
    assert "PASS dies immediately [Store dies at step 1 (effect `scratch`)]" in out


@needs_cordis
def test_a_real_activation_after_the_provision_withdraws_it(capsys):
    """The hardest inverse to observe: a provision's withdrawal is the
    runtime's own disposer and produces no host-trace event at all."""
    from revl.test import test_command

    ir = compile_source('''
service Cache { fn put(k: Str, v: Str) }

component Store provides cache: Cache, spare: Cache {
  let scratch = effect Map.new() undo scratch.drop()
  provide cache {
    fn put(k, v) { effect scratch.insert(k, v) undo scratch.remove(k) }
  }
  provide spare {
    fn put(k, v) { effect scratch.insert(k, v) undo scratch.remove(k) }
  }
}

fault test "dies after providing" for Store {
  fail at step 3
  assert failed
  assert no residue
  assert inverses lifo
  assert no emissions
}
''', "provide.rvl")
    code = test_command(ir, "py")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS dies after providing [Store dies at step 3]" in out
    # the composition had no provisions at all before the target activated,
    # so `cache` surviving the unwind would have shown up as registry residue
    assert "residue in the" not in out


@needs_cordis
def test_no_emissions_really_fails_when_the_body_emitted(capsys):
    code, out = _run('''
fault test "claims it never emitted" for Store {
  fail at step 3
  assert no emissions
}
''', capsys)
    assert code == 1
    assert "FAIL claims it never emitted" in out
    assert "an emission cannot be reverted" in out


@needs_cordis
def test_fault_tests_and_plain_test_blocks_coexist(capsys):
    code, out = _run('''
test "arithmetic" { assert 1 + 1 == 2 }

fault test "db dies" for Store { fail at step 3  assert no residue }
''', capsys)
    assert code == 0, out
    assert "PASS arithmetic" in out
    assert "PASS db dies" in out


@needs_cordis
def test_an_await_body_reports_the_known_cordis_py_divergence(capsys):
    """Regression lock on a *reported upstream defect*, not on desired behaviour.

    A body with an `await` step compiles to an async generator, and cordis-py
    routes an async setup failure to its effect guard (auto-dispose) instead of
    the fiber's error slot: the inverses run LIFO and leave no residue, but the
    fiber stays ACTIVE instead of landing FAILED. See docs/fault-tests.md §8.

    This asserts the *report* is precise, and it is meant to go red the day
    cordis-py fixes it — at which point delete the xfail framing here and the
    §8 entry, not the `assert failed` clause.
    """
    from revl.test import test_command

    ir = compile_source('''
service Cache { fn put(k: Str, v: Str) }

component Migrator provides cache: Cache {
  let scratch = effect Map.new() undo scratch.drop()
  let pool = effect Pool.open("postgres://primary/app", 2) undo pool.close()
  await Job.run("migrations")
  provide cache {
    fn put(k, v) { effect scratch.insert(k, v) undo scratch.remove(k) }
  }
}

fault test "async body dies at the pool" for Migrator {
  fail at effect pool
  assert failed
  assert no residue
  assert inverses lifo
}
''', "async.rvl")
    code = test_command(ir, "py")
    out = capsys.readouterr().out
    assert code == 1, out
    # exactly one clause failed, and it is the state clause
    assert "did not land FAILED — it is ACTIVE" in out
    assert "compiles to an async generator" in out
    assert "the inverses DID run" in out
    # `no residue` and `inverses lifo` still held: the unwind itself is correct
    assert "residue in the" not in out
    assert "out of LIFO order" not in out

# ------------------------------------------------- R1: host-trace residue
#
# The fault-path `assert no residue` reads the same host trace the lifecycle
# harness's R1 accounting reads (findings-uxprobe2: a Map stub acquired with
# a non-inverse undo used to PASS a fault test while the identical component
# failed the lifecycle one, because the four runtime counters cannot see an
# acquisition whose undo is not its inverse).

def test_pairing_releases_a_matching_drop():
    events = ["map#1.new", "map#1.insert k", "map#1.drop"]
    assert fault_mod._unreleased_host_resources(events) == []


def test_pairing_flags_an_unpaired_new():
    got = fault_mod._unreleased_host_resources(["map#1.new", "map#1.insert leak"])
    assert got == ["map#1 (new() with no drop())"]


def test_pairing_handles_pools_and_order():
    events = ["pool#1.open postgres://primary/app",
              "map#2.new",
              "map#2.drop",
              "pool#1.close postgres://primary/app"]
    assert fault_mod._unreleased_host_resources(events) == []
    events[3] = "pool#1.query SELECT 1"          # not the inverse of open
    assert fault_mod._unreleased_host_resources(events) == \
        ["pool#1 (open() with no close())"]


def test_non_host_events_are_ignored():
    events = ["store.config {url=..}", "fiber#7.dispose", "cache.put k"]
    assert fault_mod._unreleased_host_resources(events) == []


def test_judge_names_the_unreleased_resource():
    outcome = _outcome(events=["map#1.new", "map#1.insert leak"])
    problems = fault_mod._judge(_ALL, outcome, _FiberState)
    hit = [p for p in problems if "residue in the host" in p]
    assert len(hit) == 1
    assert "map#1 (new() with no drop())" in hit[0]
    assert "(R1)" in hit[0]


def test_judge_stays_quiet_when_the_trace_pairs():
    outcome = _outcome(events=["map#1.new", "map#1.drop"])
    assert fault_mod._judge(_ALL, outcome, _FiberState) == []


# ------------------------------------------------------------ execution
# The uxprobe2 repro, as a regression. A component whose setup acquires a
# Map stub with a NON-INVERSE undo used to PASS a fault test while the
# identical component failed the lifecycle one; `assert no residue` must
# now read the host trace and refuse.

_LEAKY_BODY = '''service Ping { fn ping(tag: Str) -> Str }

component Fragile provides p: Ping {
  let scratch = effect Map.new() undo scratch.insert("leak", "1")
  fail "deliberate activation fault"
  provide p { fn ping(tag) = tag }
}
'''

_FAULT_ON_LEAKY = _LEAKY_BODY + '''
fault test "mid-activation failure reverts its acquisition" for Fragile {
  fail at step 2
  assert no residue
}
'''

_FAULT_ON_HONEST = (_LEAKY_BODY
                    .replace('undo scratch.insert("leak", "1")',
                             'undo scratch.drop()')
                    + '''
fault test "clean mid-activation failure" for Fragile {
  fail at step 2
  assert no residue
}
''')


def _run_src(src: str, capsys):
    from revl.test import test_command

    code = test_command(compile_source(src, "fault.rvl"), "py")
    return code, capsys.readouterr().out


@needs_cordis
def test_a_non_inverse_undo_fails_the_fault_test(capsys):
    """Before the R1 accounting this exact document passed."""
    code, out = _run_src(_FAULT_ON_LEAKY, capsys)
    assert code == 1, out
    assert "FAIL mid-activation failure reverts its acquisition" in out
    assert "residue in the host" in out
    # the tag carries the runtime's global Map serial, which depends on how
    # many stubs earlier tests created - assert the shape, not the number
    assert re.search(r"map#\d+ \(new\(\) with no drop\(\)\)", out)
    assert "(R1)" in out


@needs_cordis
def test_an_inverse_undo_still_passes_the_fault_test(capsys):
    """Positive control: the same shape with the real inverse keeps passing —
    the trace must not grow false positives."""
    code, out = _run_src(_FAULT_ON_HONEST, capsys)
    assert code == 0, out
    assert "PASS clean mid-activation failure" in out


# The two tests above fail the component with an explicit `fail` statement in
# its body. That is not how a fault test faults a component: `fail at step N`
# arms the runtime's fault probe and the injected raise takes a different exit
# from the activation than a body-level `fail` does. The review counterexample
# below keeps the non-inverse undo but lets the *probe* kill the activation —
# and on this branch it still PASSes, so the host trace the judge reads is not
# fed on the injected-fault path. Constructed live during review from the
# original findings-uxprobe2 asymmetry repro; kept verbatim.

_LEAKY_UNDER_INJECTION = '''extern pure fn now() -> Int = @py { import time; return int(time.time()) }
service Fragile { fn work(n: Int) -> Int }
component Fragile provides f: Fragile {
  let scratch = effect Map.new() undo scratch.insert("leak", "1")
  provide f {
    fn work(n) { return now() + n }
  }
}
fault test "mid-activation failure with a non-inverse undo" for Fragile {
  fail at step 1
  assert no residue
}
'''


@needs_cordis
def test_a_non_inverse_undo_fails_under_an_injected_fault(capsys):
    """The R1 accounting must hold when the *probe* faults the activation,
    not only when a `fail` statement in the body does."""
    code, out = _run_src(_LEAKY_UNDER_INJECTION, capsys)
    assert code == 1, out
    assert "FAIL mid-activation failure with a non-inverse undo" in out
    assert "residue in the host" in out
    assert re.search(r"map#\d+ \(new\(\) with no drop\(\)\)", out)
    assert "(R1)" in out


@needs_cordis
def test_an_inverse_undo_still_passes_under_an_injected_fault(capsys):
    """Positive control for the injected path: the identical construction
    with the real inverse must keep passing."""
    src = _LEAKY_UNDER_INJECTION.replace(
        'undo scratch.insert("leak", "1")', 'undo scratch.drop()')
    code, out = _run_src(src, capsys)
    assert code == 0, out
    assert "PASS mid-activation failure with a non-inverse undo" in out



