"""`revl promote`: a promotion decided from RECORDED evidence (issue #1192).

The executable spec for the verb `docs/design/558-shadow-scheduling.md`
section 18 adds, and for the two documents it reads.

`tests/test_shadow_promotion_518.py` measures the gate, `tests/test_shadow_
routing_518.py` the schedule, `tests/test_shadow_runtime_518.py` the seam, and
`tests/test_shadow_cutover_1192.py` the arm that moves. Every one of them
builds its plan and its window in PYTHON LOCALS. `test_shadow_cutover_1192.py`
says so itself, in its own `WHAT THIS FILE DOES NOT DO`:

    "the other five tiers' seams, `revl promote`, and the trace's `llm` span"

This file is the measurement of that middle gap: the decision reachable by an
OPERATOR, from two artifacts on disk and nothing else.

What this file's tests are about, in one place. The verb adds no decision
logic, so the tests here are not about the decision: they are about the two
places a verb CAN be wrong where a library cannot.

* The DOCUMENT is now an input a stranger can write. Every member is required
  and nothing is defaulted, because the two defaults that matter -- `live` and
  the threshold -- are the fail-open direction.
* The WORLDS are now derived, not carried. `ShadowLedger.as_window` writes no
  world and `window_from_dict` reads none; the verb rebuilds item 496's two
  recorded worlds from the two COMPOSITIONS, which is what makes the verdict a
  replay comparison over what a generation DECLARES rather than over a
  timeline the document's author asserted.

WHAT THIS FILE DOES NOT DO, stated so nobody infers it from silence: it does
not prove a shadow ran. A correctly-sealed window document is
indistinguishable from a real run's, so every test here is about what the verb
does with a document, never about whether the document is true. It proves
nothing about the five unwired tiers (`cordis`, `java`, `rust`, `ts`, `wasm`),
so issue #1192 stays open. And a `REFUSE` is not evidence that no divergence
exists: it is evidence that this window did not establish one.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_shadow_runtime_518 as live  # noqa: E402
from revl import shadow_promotion as sp  # noqa: E402
from revl import shadow_routing as sr  # noqa: E402
from revl import shadow_runtime as srt  # noqa: E402
from revl import wal  # noqa: E402
from revl.cli.parser import build_parser  # noqa: E402
from revl.cli.promote import _run_promote  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp import canary  # noqa: E402
from revl.mcp.session import replay_module  # noqa: E402

MODULE_PATH = ROOT / "src" / "revl" / "cli" / "promote.py"
MAIN_PATH = ROOT / "src" / "revl" / "__main__.py"
CLI_PARSER_PATH = ROOT / "src" / "revl" / "cli" / "parser.py"

#: The published verb name, spelled once. Every test that runs the verb runs it
#: through `build_parser`, so this is the name a user types and not a helper's.
VERB = "promote"


# ==========================================================================
# the compositions, on disk, because the verb's inputs are FILES
# ==========================================================================

@pytest.fixture(scope="module")
def running_ir():
    return compile_source(live.INCUMBENT_SRC, "incumbent.rvl")


@pytest.fixture(scope="module")
def successor_ir():
    return compile_source(live.CANDIDATE_SAME_SRC, "candidate_same.rvl")


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    """The two generations as `.rvl` files, and the evidence key.

    A verb whose whole point is "decidable from artifacts" has to be measured
    on artifacts. Handing `_run_promote` a namespace built in memory would
    measure the pipeline and skip the one thing that is new."""
    root = tmp_path_factory.mktemp("promote")
    incumbent = root / "incumbent.rvl"
    incumbent.write_text(live.INCUMBENT_SRC, encoding="utf-8")
    successor = root / "candidate.rvl"
    successor.write_text(live.CANDIDATE_SAME_SRC, encoding="utf-8")
    key = root / "evidence.key"
    key.write_bytes(live.KEY)
    return {"incumbent": incumbent, "successor": successor, "key": key,
            "root": root}


# ==========================================================================
# the window document
# ==========================================================================

def crossing_indices(ir, action=None):
    """The step indices this composition's own walk declares ``action`` at.

    `srt.crossing_actions` reads the static walk, which is the same walk the
    gate's stamp check reads. Deriving the fixture's crossings from it rather
    than counting from zero is what keeps a fixture from passing for the wrong
    reason: `summarize`'s crossings start at 2, not 0, because `classify` owns
    the anchor at 1.
    """
    actions = srt.crossing_actions(ir, live.COMPONENT)
    if action is None:
        action = live.ACTION
    return sorted(index for index, name in actions.items() if name == action)


def window_ledger(ir, *, disagree_at=None, slo=None, side=sr.INCUMBENT,
                  live_route=False, realm=live.REALM, steps=None,
                  answer_of=None, recorded_at="2026-01-01T00:00:00Z"):
    """One accumulated window, as the schedule would have left it.

    The records are sealed by `revl.model_evidence`'s own sealer, over the
    same two roles the composition declares, so the gate's `admit` verifies
    them for real. `disagree_at` changes the SUCCESSOR's answer at one
    crossing and leaves every other pair agreeing, which is the smallest
    divergence a window can carry."""
    if steps is None:
        steps = crossing_indices(ir)
    entries = []
    for step in steps:
        same = step != disagree_at
        if answer_of is not None:
            said = answer_of(step)
        else:
            said = f"said-{step}" if same else f"other-{step}"
        entries.append(sr.Entry(
            crossing=(live.COMPONENT, step), component=live.COMPONENT,
            action=live.ACTION, realm=realm, side=side,
            observation=sp.Observation(
                incumbent=live.seal(
                    step_index=step, role="incumbent",
                    placement=live.PLACEMENT_INCUMBENT,
                    answer=live.answer_digest(f"said-{step}"),
                    prompt=f"asked-{step}", recorded_at=recorded_at),
                candidate=live.seal(
                    step_index=step, role="successor",
                    placement=live.PLACEMENT_SUCCESSOR,
                    answer=live.answer_digest(said),
                    prompt=f"asked-{step}", recorded_at=recorded_at),
                slo=dict(slo) if slo is not None else None,
                realm=live.REALM)))
    return sr.ShadowLedger(
        route=live.route(live=live_route, realm=realm),
        entries=tuple(entries),
        served=tuple(sr.Served(crossing=(live.COMPONENT, step), shadowed=True,
                               side=sr.INCUMBENT) for step in steps),
        candidate_calls=len(steps))


def window_document(ledger):
    return ledger.as_window()


def plan_document(ledger, **overrides):
    return live.plan_for(ledger, **overrides).as_dict()


def write_json(path, document):
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


# ==========================================================================
# running the verb
# ==========================================================================

def promote_args(sources, *, window, plan, candidate=None, key=True,
                 files=None):
    """A parsed `revl promote` namespace, built by the real parser.

    Built by `build_parser` and not by hand: the flag names, the `required`
    markers and the exit-status contract are part of what this slice lands,
    and a hand-built namespace would keep working after the parser stopped
    accepting the invocation a user types."""
    argv = [VERB, str(sources["incumbent"])]
    argv += ["--candidate", str(candidate or sources["successor"])]
    argv += ["--plan", str(plan), "--window", str(window)]
    if key:
        argv += ["--evidence-key", str(sources["key"])]
    if files:
        argv += [str(f) for f in files]
    return build_parser().parse_args(argv)


def run_verb(sources, *, window, plan, candidate=None, key=True, files=None,
             json_output=False):
    """``(exit_status, stdout, stderr)`` from one `revl promote` invocation."""
    argv = [VERB, str(sources["incumbent"])]
    argv += ["--candidate", str(candidate or sources["successor"])]
    argv += ["--plan", str(plan), "--window", str(window)]
    if key:
        argv += ["--evidence-key", str(sources["key"])]
    if json_output:
        argv += ["--json"]
    if files:
        argv += [str(f) for f in files]
    return _run_promote(build_parser().parse_args(argv))


@pytest.fixture
def artifacts(sources, tmp_path, running_ir):
    """A written window and plan, and a `run(**overrides)` that writes more.

    Each `run` writes to its OWN filenames, so two windows in one test cannot
    overwrite each other and a test that compares three verdicts is comparing
    three documents."""
    counter = {"n": 0}

    def run(*, disagree_at=None, slo=None, side=sr.INCUMBENT, live_route=False,
            realm=live.REALM, steps=None, key=True, plan_overrides=None,
            plan=None, window=None, candidate=None, json_output=False):
        counter["n"] += 1
        tag = counter["n"]
        ledger = window_ledger(
            running_ir, disagree_at=disagree_at, slo=slo, side=side,
            live_route=live_route, realm=realm, steps=steps)
        if window is None:
            window = write_json(tmp_path / f"window-{tag}.json",
                                ledger.as_window())
        if plan is None:
            plan = write_json(tmp_path / f"plan-{tag}.json",
                              plan_document(ledger, **(plan_overrides or {})))
        return run_verb(sources, window=window, plan=plan, candidate=candidate,
                        key=key, json_output=json_output)

    return run


# ==========================================================================
# 0. NON-VACUITY: the three decisions, from documents alone
# ==========================================================================

def test_three_documents_decide_three_ways_with_three_exit_statuses(
        artifacts, running_ir):
    """The differential, and the falsifier for the exit status.

    One verb, three windows: an agreeing shadow, a shadow whose agreement is
    below the declared threshold, and a LIVE shadow with one attributed
    divergence. `PROMOTE`/`REFUSE`/`REVERT`, and the exit status is 0/1/1 --
    0 only where the comparison RAN and the gate promoted.

    The mutation that proves this is load-bearing: `return 0` in place of the
    verdict-derived status. The two assertions after the first go red."""
    assert artifacts() == 0

    steps = crossing_indices(running_ir)
    assert artifacts(disagree_at=steps[7],
                     plan_overrides={"threshold": 0.99}) == 1

    assert artifacts(live_route=True, disagree_at=steps[7]) == 1


def test_a_promote_prints_the_schedule_and_the_verdict(artifacts, capsys):
    """The report an operator reads, and the two blocks it has to carry.

    `routing.render` prints the schedule first and the gate's verdict second,
    and the schedule block is the one member no other surface prints: the
    count of pairs that carry BOTH recorded worlds. A verdict printed without
    it would be a decision whose evidence an operator cannot see."""
    assert artifacts() == 0
    out = capsys.readouterr().out
    assert sr.ROUTING_KIND in out
    assert sp.PROMOTION_KIND in out
    assert "pairs carrying both recorded worlds: 20 of 20" in out
    assert "decision: PROMOTE" in out
    assert "metric blocks supplied: 0, read: 0" in out


def test_json_is_the_same_verdict_as_a_versioned_document(artifacts, capsys):
    """`--json` is a second rendering of one verdict, not a second decision.

    The document is `verdict.as_dict()`, so the member the exit status is
    derived from is the member a script reads."""
    assert artifacts(json_output=True) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == sp.PROMOTION_KIND
    assert document["decision"] == sp.PROMOTE


# ==========================================================================
# 1. F1: the incumbent's answer is the one used
# ==========================================================================

def test_a_candidate_that_answered_a_not_live_route_is_refused(
        artifacts, capsys):
    """F1. The clause the whole construction rests on.

    A window whose SERVED side is the candidate, over a route that is not
    live, is refused by name (`served-candidate`): on a shadow the
    incumbent's answer is the one used, and a document that says otherwise is
    describing a promotion that already happened without a verdict.

    Mutation that proves it load-bearing: delete the `entry.side == CANDIDATE
    and not route.live` branch in `shadow_routing._check_entries`. The exit
    status stays 1 (the entry would then be admitted and the gate would
    decide on its merits) but the named link disappears -- so the assertion
    that is doing the work is the LINK, not the status."""
    assert artifacts(side=sr.CANDIDATE) == 1
    err = capsys.readouterr().out
    assert sr.SERVED_CANDIDATE in err


def test_a_live_window_whose_served_side_is_the_candidate_is_admitted(
        artifacts):
    """The other half of F1, so the refusal above is about `live` and not
    about the side.

    The SAME window, served by the candidate, promotes once the route is live
    -- because that is what live means. Without this row, the branch above
    would be satisfiable by refusing every candidate-side window."""
    assert artifacts(side=sr.CANDIDATE, live_route=True) == 0


# ==========================================================================
# 2. F2: the attributed divergence reverts, and names the crossing
# ==========================================================================

def test_the_first_attributed_divergence_reverts_and_names_the_crossing(
        artifacts, capsys, running_ir):
    """F2. The revert is item 496's, over the derived worlds.

    A LIVE window with one disagreeing successor answer reverts, and the
    refusal names `(component, realm, step)` -- the exact attribution `revl
    canary` already produces -- plus the field that differed. The agreement
    ratio is printed and is 19/20 = 0.95, exactly the threshold: the revert
    does not consult it.

    Mutation: `_world_divergence` returning `None`. The gate would then see
    twenty agreeing pairs, print `PROMOTE`, and exit 0."""
    steps = crossing_indices(running_ir)
    assert artifacts(live_route=True, disagree_at=steps[7]) == 1
    out = capsys.readouterr().out
    assert "decision: REVERT" in out
    assert sp.DIVERGENCE_ATTRIBUTED in out
    assert f"step {steps[7]}" in out
    assert "chosen_digest" in out


def test_a_divergence_on_a_shadow_only_lowers_agreement(artifacts, capsys):
    """The same divergence, on a NOT-live route, is not a revert.

    A shadow's candidate answer is discarded, so the divergence is a datum
    about agreement and not a reason to revert. Measured rather than asserted
    from the code: the same window and the same plan, one flag apart, and the
    decision changes from `REVERT` to `REFUSE` (below threshold) -- never to
    `PROMOTE`, because 19/20 against a 0.99 threshold is below it."""
    steps = crossing_indices(compile_source(live.INCUMBENT_SRC, "incumbent.rvl"))
    assert artifacts(disagree_at=steps[7],
                     plan_overrides={"threshold": 0.99}) == 1
    out = capsys.readouterr().out
    assert "decision: REFUSE" in out
    assert sp.AGREEMENT_BELOW_THRESHOLD in out


# ==========================================================================
# 3. F3: a verdict, not a numeric flip
# ==========================================================================

def test_a_metric_block_does_not_promote_and_is_not_read(artifacts, capsys,
                                                         running_ir):
    """F3. The rejected alternative, as a measurement.

    A window carrying a PERFECT metric block on every observation, whose
    agreement the replay comparison puts below the stated threshold, is
    REFUSED on the COMPARISON -- and the verdict reports `read: 0`. A
    promotion that a latency or cost metric could carry on its own is the
    alternative this item rejected; the window here is the shape that would
    carry it.

    Mutation: read `observation.slo` in `_resolve_verifier` or in the
    threshold path. `slo_reads` becomes non-zero and the tripwire property in
    `shadow_promotion.Observation` raises before any assertion."""
    steps = crossing_indices(running_ir)
    assert artifacts(slo=live.PERFECT_SLO, disagree_at=steps[7],
                     plan_overrides={"threshold": 0.99}) == 1
    out = capsys.readouterr().out
    assert "decision: REFUSE" in out
    assert sp.AGREEMENT_BELOW_THRESHOLD in out
    assert "metric blocks supplied: 20, read: 0" in out


def test_a_metric_block_on_a_promoting_window_is_still_not_read(
        artifacts, capsys):
    """The promote side of the same claim.

    A window that PROMOTES, carrying the same twenty metric blocks: the
    threshold is met by the comparison and the metric is reported as supplied
    and unread. Without this row F3 would be satisfiable by refusing every
    window that carries a metric, which is a different rule."""
    assert artifacts(slo=live.PERFECT_SLO) == 0
    out = capsys.readouterr().out
    assert "decision: PROMOTE" in out
    assert "metric blocks supplied: 20, read: 0" in out


# ==========================================================================
# 4. F4/F5: the evidence is evidence
# ==========================================================================

def test_one_byte_of_a_sealed_record_refused_the_promotion(
        sources, tmp_path, running_ir, capsys):
    """F5. The seal is checked, over a document rather than in memory.

    One character of one observation's prompt binding is edited after sealing,
    which is what a hand-written or tampered window looks like. The verdict is
    `REFUSE` naming `evidence-unverified` -- the verifier RAN and said no,
    which is a different link from the `evidence-unverifiable` the keyless run
    below names (no verifier could run at all). Both are refusals; neither is
    a promotion.

    Mutation: `verifier=None` at the `runtime.decide` call. The edited record
    would be admitted and the window would promote."""
    ledger = window_ledger(running_ir)
    document = ledger.as_window()
    record = document["entries"][0]["incumbent"]
    digest = record["prompt_binding"]["value"]
    record["prompt_binding"]["value"] = (
        ("0" if digest[0] != "0" else "1") + digest[1:])
    window = write_json(tmp_path / "tampered.json", document)
    plan = write_json(tmp_path / "plan.json", plan_document(ledger))

    assert run_verb(sources, window=window, plan=plan) == 1
    out = capsys.readouterr().out
    assert "decision: REFUSE" in out
    assert sp.EVIDENCE_UNVERIFIED in out


def test_a_window_read_without_a_key_is_refused_not_admitted(
        artifacts, capsys):
    """The absence of a key is fail-closed.

    The verb takes `--evidence-key` as optional, and omitting it must not mean
    "skip the check". `admit` refuses a record it cannot verify, so the run is
    a `REFUSE` naming `evidence-unverifiable` -- the same link the tampered
    window names, for the same reason.

    Mutation: default the verifier to something that returns True when no key
    resolves."""
    assert artifacts(key=False) == 1
    out = capsys.readouterr().out
    assert sp.EVIDENCE_UNVERIFIABLE in out


# ==========================================================================
# 5. F6: the action is DERIVED from the composition, not taken from the stamp
# ==========================================================================

def test_a_stamp_the_composition_does_not_declare_is_refused(
        artifacts, capsys, running_ir):
    """F6. The clause `shadow_runtime.decide` exists to add over
    `shadow_routing.decide`.

    A window stamped `Classifier.summarize` whose crossings are `0..19`
    instead of the composition's own `2..21` is refused, and the refusal names
    the indices the composition DOES declare. A window that agreed with itself
    perfectly is not evidence about this composition.

    Mutation: call `routing.decide` instead of `runtime.decide` in
    `_run_promote`. `routing.decide` reads the stamp and never the walk, so
    the window would promote."""
    assert artifacts(steps=list(range(0, live.WIDTH))) == 1
    out = capsys.readouterr().out
    assert srt.STAMP_UNDERIVED in out
    declared = crossing_indices(running_ir)
    assert ", ".join(str(i) for i in declared) in out


# ==========================================================================
# 6. F7: a document member is required, never defaulted
# ==========================================================================

@pytest.mark.parametrize("member", ["kind", "version", "route", "entries",
                                    "served", "candidate_calls", "refusal"])
def test_a_window_missing_a_member_is_refused_never_defaulted(
        sources, tmp_path, running_ir, capsys, member):
    """F7, over the window document.

    Each top-level member of `revl.shadow-window` is deleted in turn and the
    verb must refuse to READ the document: exit 1, a message on stderr naming
    the member, and no verdict at all. Defaulting is how a promotion comes to
    rest on a window nobody recorded.

    Mutation: `.get(member, default)` in `window_from_dict`. The run reaches
    the gate, and every row here goes red for the reason the row is about."""
    ledger = window_ledger(running_ir)
    document = ledger.as_window()
    del document[member]
    window = write_json(tmp_path / f"missing-{member}.json", document)
    plan = write_json(tmp_path / "plan.json", plan_document(ledger))

    assert run_verb(sources, window=window, plan=plan) == 1
    captured = capsys.readouterr()
    assert member in captured.err
    assert sp.PROMOTION_KIND not in captured.out


@pytest.mark.parametrize("member", ["kind", "version", "route_table",
                                    "authority_diff", "layers",
                                    "compensations", "threshold",
                                    "min_observations"])
def test_a_plan_missing_a_member_is_refused_never_defaulted(
        sources, tmp_path, running_ir, capsys, member):
    """F7, over the plan document, and the member list is the reason it
    matters.

    `threshold` and `min_observations` are the two whose DEFAULT would be a
    promotion. A reader that supplied 0.0 for a missing threshold would turn a
    declaration nobody wrote into a promotion of everything."""
    ledger = window_ledger(running_ir)
    document = live.plan_for(ledger).as_dict()
    del document[member]
    plan = write_json(tmp_path / f"missing-{member}.json", document)
    window = write_json(tmp_path / "window.json", ledger.as_window())

    assert run_verb(sources, window=window, plan=plan) == 1
    captured = capsys.readouterr()
    assert member in captured.err
    assert sp.PROMOTION_KIND not in captured.out


def test_a_plan_that_is_not_a_plan_is_refused(sources, tmp_path, running_ir,
                                             capsys):
    """The window document handed in as the plan.

    Both documents are JSON objects with a `kind` and a `version`, so a
    swapped pair is a plausible operator mistake. It is refused by the reader
    and never reaches the gate.

    Mutation: drop the `kind` check in `plan_from_dict`. The reader would take
    the window's `route` member for a plan route and raise inside the gate
    instead of refusing."""
    ledger = window_ledger(running_ir)
    window = write_json(tmp_path / "window.json", ledger.as_window())
    plan = write_json(tmp_path / "swapped.json", ledger.as_window())

    assert run_verb(sources, window=window, plan=plan) == 1
    captured = capsys.readouterr()
    assert "kind" in captured.err


def test_a_plan_with_a_non_bool_live_is_refused(sources, tmp_path, running_ir,
                                               capsys):
    """`live` is the one member whose default is the fail-open direction.

    `live=False` reports a shadow, where the first attributed divergence only
    lowers agreement. So a `live` that is not a bool is refused rather than
    coerced: `"false"` is truthy in Python, and reading it as True would make
    a shadow's window revert.

    Mutation: `bool(document["live"])` instead of an isinstance check. The
    string `"false"` becomes True and this row goes red."""
    ledger = window_ledger(running_ir)
    document = live.plan_for(ledger).as_dict()
    document["live"] = "false"
    plan = write_json(tmp_path / "live-string.json", document)
    window = write_json(tmp_path / "window.json", ledger.as_window())

    assert run_verb(sources, window=window, plan=plan) == 1
    assert "live" in capsys.readouterr().err


def test_an_unreadable_or_non_object_artifact_is_an_input_failure(
        sources, tmp_path, running_ir, capsys):
    """A missing file, and a JSON array, are not refusals.

    There is nothing yet to decide, so there is no verdict: stderr and exit 1,
    and no verdict document on stdout. This is the one place in the verb where
    "no answer" is the honest answer, and it is distinguished from a REFUSE so
    a caller can tell "your evidence is not evidence" from "your path is
    wrong"."""
    ledger = window_ledger(running_ir)
    window = write_json(tmp_path / "window.json", ledger.as_window())
    plan = write_json(tmp_path / "plan.json", plan_document(ledger))

    assert run_verb(sources, window=tmp_path / "nope.json", plan=plan) == 1
    assert "cannot read" in capsys.readouterr().err

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2, 3]", encoding="utf-8")
    assert run_verb(sources, window=window, plan=not_an_object) == 1
    captured = capsys.readouterr()
    assert "expected a plan document" in captured.err
    assert sp.PROMOTION_KIND not in captured.out


# ==========================================================================
# 7. F8: the composition the window was taken over
# ==========================================================================

def test_a_realm_the_composition_does_not_have_is_refused_and_lists_the_ones_it_has(
        artifacts, capsys):
    """F8. `resolve`, called by the verb on BOTH generations.

    A window served into a realm this composition does not isolate the
    component into is refused by name, and the refusal lists the realms the
    composition DOES have. The list is the useful output of a typo.

    Mutation: drop `_unresolved`'s loop and let `decide` resolve. The
    incumbent's half would still refuse (so the exit status holds), which is
    why the assertion that does the work is the LIST -- the candidate's half
    is the one `decide` cannot make."""
    assert artifacts(realm=live.OTHER_REALM) == 1
    out = capsys.readouterr().out
    assert srt.REALM_UNKNOWN in out
    assert live.REALM in out


def test_a_candidate_that_does_not_declare_the_component_is_refused_before_any_world(
        sources, tmp_path, running_ir, capsys):
    """The candidate's half of `_unresolved`, and why it is checked EARLY.

    A successor generation that does not declare `Classifier` at all. The verb
    must refuse it as a COMPOSITION precondition, and it must do so before
    building a world: `canary.slice_timeline` raises on a missing component,
    and a comparison that could not run must be reported rather than skipped,
    because a skipped comparison reads exactly like a clean one.

    Mutation: build the worlds before resolving. The run dies with
    `slice_timeline`'s exception -- a traceback and no verdict -- instead of a
    named refusal."""
    ledger = window_ledger(running_ir)
    window = write_json(tmp_path / "window.json", ledger.as_window())
    plan = write_json(tmp_path / "plan.json", plan_document(ledger))
    elsewhere = tmp_path / "elsewhere.rvl"
    elsewhere.write_text(live.source(extra=False).replace(
        f"component {live.COMPONENT}", "component Somewhere"), encoding="utf-8")

    assert run_verb(sources, window=window, plan=plan,
                    candidate=elsewhere) == 1
    out = capsys.readouterr().out
    assert srt.COMPONENT_UNKNOWN in out
    assert "the candidate composition" in out


# ==========================================================================
# 8. the two compositions ARE the two worlds, and the document carries none
# ==========================================================================

def test_the_window_document_carries_no_recorded_world(running_ir):
    """The measurement the substrate decision rests on, pinned.

    `ShadowLedger.worlds_recorded` counts the pairs that carried both worlds
    IN MEMORY. The serialized document has no such member and the reader reads
    none: the worlds are DERIVED by the verb from the two compositions.

    The reason is the one measured in section 9: with a shadow attached, the
    candidate's own completion crosses the SAME seam and mints a step index,
    so a run's timeline is not step-for-step comparable with another
    generation's static walk. A document that asserted its own worlds would be
    asserting a comparison nothing can check.

    Measured here in both directions: a window built the way a TEST builds one
    carries no world either, so the count is 0 in memory AND absent on disk --
    and the verb still runs a real comparison (section 8's last row)."""
    ledger = window_ledger(running_ir)
    document = ledger.as_window()
    assert ledger.worlds_recorded == 0
    assert not any("world" in key for key in document)
    assert not any("world" in key
                   for entry in document["entries"] for key in entry)


def test_the_two_compositions_are_the_two_worlds_the_gate_compares(
        running_ir, successor_ir):
    """What the derivation produces, compared with item 496's own comparator.

    The incumbent's world and the successor's world for this slice are
    identical step for step -- the sibling generation differs elsewhere in the
    composition, which is what a promotion is supposed to look like. So the
    comparison the verb runs is a real comparison that found no divergence,
    and not a comparison that never ran."""
    def walk(timeline):
        return [(s.kind, s.label, (s.detail or {}).get("slot"))
                for s in timeline.steps]

    left = srt.world_for(running_ir, live.COMPONENT)
    right = srt.world_for(successor_ir, live.COMPONENT)
    verdict = canary.compare_timelines(left, right)
    assert verdict["diverged"] is False
    assert walk(left) == walk(right)

    # and the two generations really are different compositions, so the
    # equality above is a finding about the world and not about the file
    assert {c["name"] for c in running_ir["components"]} \
        != {c["name"] for c in successor_ir["components"]}


def test_a_world_that_diverges_reverts_a_live_window_that_agrees_on_every_answer(
        sources, tmp_path, running_ir, capsys):
    """The falsifier for the derivation: worlds that differ where the RECORDS
    agree.

    The successor generation here is the incumbent's own `summarize` body with
    ONE extra `model.complete` in front of it. Every one of the window's
    twenty pairs names the same completion on both sides -- so a comparison
    over the RECORDS finds nothing -- and the two generations' recorded worlds
    differ at replay step 2, which is what item 496's walker compares. A live
    route REVERTS and names the field.

    Mutation: compare the records instead of the worlds (`_world_divergence`
    returning `None`). Twenty agreeing pairs, `PROMOTE`, exit 0.

    The window is built with `disagree_at=None`, and the row below asserts the
    agreement directly, so "the records agree" is measured and not assumed."""
    from revl.compiler import compile_files

    extra = tmp_path / "candidate_extra.rvl"
    extra.write_text(
        live.source().replace(
            "    fn summarize(text) {\n",
            '    fn summarize(text) {\n      emit model.complete("extra")'
            ' compensate model.cancel("extra")\n', 1),
        encoding="utf-8")

    # the records agree, pairwise, on all twenty pairs
    ledger = window_ledger(running_ir, live_route=True)
    for entry in ledger.entries:
        assert entry.observation.incumbent["chosen"] \
            == entry.observation.candidate["chosen"]
    assert not any(entry.observation.incumbent["candidates"]
                   != entry.observation.candidate["candidates"]
                   for entry in ledger.entries)

    window = write_json(tmp_path / "window.json", ledger.as_window())
    plan = write_json(tmp_path / "plan.json", plan_document(ledger))

    assert run_verb(sources, window=window, plan=plan, candidate=extra) == 1
    out = capsys.readouterr().out
    assert "decision: REVERT" in out
    assert sp.DIVERGENCE_ATTRIBUTED in out

    # and the two generations' DERIVED worlds really do differ
    verdict = canary.compare_timelines(
        srt.world_for(running_ir, live.COMPONENT),
        srt.world_for(compile_files([str(extra)]), live.COMPONENT))
    assert verdict["diverged"] is True
    assert verdict["atIndex"] == 2


def test_a_generation_that_moves_the_crossings_is_refused_before_any_world(
        sources, tmp_path, running_ir, capsys):
    """The moved generation, and why the ordering in `_unresolved` matters.

    `CANDIDATE_MOVED_SRC` relocates `summarize`'s crossings into `classify`
    WITHOUT changing a kind or a label -- item 496's own finding, and the
    generation whose world diverges while its records agree. It also leaves
    `summarize` crossing no boundary, so the route does not resolve against it
    and the verb refuses it as a COMPOSITION precondition naming
    `action-uncrossed`.

    That refusal is correct and it is also the reason `_unresolved` resolves
    BOTH generations before building either world: a generation that cannot be
    resolved has no world to compare, and a comparison that could not run must
    be named rather than skipped. The world comparison itself is measured in
    the row above, on a generation that does resolve."""
    from revl.compiler import compile_files

    moved = tmp_path / "candidate_moved.rvl"
    moved.write_text(live.CANDIDATE_MOVED_SRC, encoding="utf-8")
    ledger = window_ledger(running_ir)
    window = write_json(tmp_path / "window.json", ledger.as_window())
    plan = write_json(tmp_path / "plan.json", plan_document(ledger))

    assert run_verb(sources, window=window, plan=plan, candidate=moved) == 1
    out = capsys.readouterr().out
    assert srt.ACTION_UNCROSSED in out
    assert "the candidate composition" in out
    assert sp.PROMOTION_KIND in out          # a verdict, not a crash
    assert "decision: REFUSE" in out

    # ... and its world for this slice really is different, so the refusal is
    # not hiding a comparison that would have passed
    assert canary.compare_timelines(
        srt.world_for(running_ir, live.COMPONENT),
        srt.world_for(compile_files([str(moved)]), live.COMPONENT),
    )["diverged"] is True


# ==========================================================================
# 9. the substrate: why the WAL is not the window
# ==========================================================================

def test_the_wal_carries_one_record_per_crossing_and_names_no_side(running_ir,
                                                                  successor_ir,
                                                                  tmp_path):
    """The measurement behind "the window document is the substrate".

    A shadowed drive, with a real WAL attached to the incumbent's recorder,
    writes one `model-decision` record per COMPLETION: twenty-one of them, for
    twenty pairs -- the anchor's plus `summarize`'s twenty. `wal.
    model_decisions` indexes them by `(component, stepIndex)` and no record
    carries a `side` or a `role`, so the index CANNOT tell the incumbent's
    side from the candidate's side of one shadowed crossing.

    The candidate's own completion does not reach this WAL at all: its
    producer runs on its own recorder, which is what keeps the shadow from
    observing itself. So the two sides of one crossing are two records in two
    places, keyed the same way, and the artifact that distinguishes them is
    the window document's `entries`/`served` pair.

    This is a pin on a MEASUREMENT and not on a rule. If a later slice teaches
    the WAL to carry the served side, this row goes red and the substrate
    decision is the thing that has to be revisited."""
    replay = replay_module()
    runtime = srt.tier_runtime()
    runtime.revl_reset_run_trace_state()

    resolution = srt.resolve(running_ir, live.route())
    incumbent, candidate, _metrics, calls = live.sides(running_ir,
                                                       successor_ir)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)

    timeline = replay.Timeline(live.COMPONENT)
    log = replay.WriteAheadLog(str(tmp_path / "run.wal"), running_ir)
    log.open()
    timeline.attach_wal(log, running_ir)
    shadow.attach()
    try:
        for step in canary.slice_timeline(running_ir, live.COMPONENT).steps:
            detail = dict(step.detail) if isinstance(step.detail, dict) else {}
            if step.kind != replay.KIND_EMISSION:
                timeline._add(step.kind, step.label, step.effect, detail=detail)
                continue
            head, _, tail = step.label.partition("(")
            key, _, method = head.partition(".")
            args = tuple(a.strip().strip("'\"")
                         for a in tail.rstrip(")").split(",") if a.strip())

            def make_call(k=key, m=method, a=args):
                timeline.record_emission(k, m, a, "Model", ("shadow.rvl", 1))
                return live.host_return()

            runtime.validate_retry(make_call, budget=0,
                                   schema={"type": "object"},
                                   where=live.COMPONENT)
    finally:
        shadow.detach()
        log.close()

    records = replay.WriteAheadLog.read(str(tmp_path / "run.wal"))["records"]
    decisions = wal.model_decisions(records)
    ledger = shadow.ledger()

    # the successor WAS consulted on every crossing, so this is not a drive
    # that skipped the shadow
    assert len(calls) == live.WIDTH
    assert len(ledger.entries) == live.WIDTH
    assert ledger.worlds_recorded == live.WIDTH

    # ... and the WAL holds one record per completion, none of them sided
    assert len(decisions) == live.WIDTH + 1
    assert sorted(k[1] for k in decisions) \
        == [1] + crossing_indices(running_ir)
    for record in decisions.values():
        assert not any("side" in key or "role" in key for key in record)


# ==========================================================================
# 10. the ratchets: what the verb is not allowed to become
# ==========================================================================

def module_ast(path=None):
    return ast.parse((path or MODULE_PATH).read_text(encoding="utf-8"))


def test_the_verb_that_decides_is_not_the_verb_that_moves():
    """G2, as a property of the source.

    `revl.shadow_register` is the arm that LANDS a promotion. A decision verb
    that imported it could land a promotion as a side effect of printing a
    verdict, which is the one thing this slice must not do: the human merges,
    and the operator decides.

    Mutation: `from .. import shadow_register` anywhere in the module."""
    tree = module_ast()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    assert not any("shadow_register" in name for name in imported)

    called = {node.attr for node in ast.walk(tree)
              if isinstance(node, ast.Attribute)}
    assert not called & {"arm", "land", "promote", "apply", "swap"}


def test_the_verb_defines_no_second_comparator():
    """The comparison is item 496's and there is exactly one.

    A second walker over recorded steps in this module would be a second
    definition of what "diverged" means, and the two would drift. The verb
    calls `runtime.world_for` (which is `canary.slice_timeline`) and lets
    `shadow_promotion._world_divergence` call `canary.compare_timelines`.

    Mutation: a local `_step_key` or `_walk_steps` -- a "small" local
    comparator, which is how the first one starts."""
    defined = {node.name for node in ast.walk(module_ast())
               if isinstance(node, ast.FunctionDef)}
    assert not defined & {"_step_key", "_walk_steps", "compare_timelines",
                          "slice_timeline", "_compare", "_diverged"}
    # and the one walker it does call is imported, not reimplemented
    assert "world_for" not in defined


def test_no_function_in_the_verb_holds_both_an_agreement_figure_and_a_metric():
    """`a verdict, not a numeric flip`, as a property of the source.

    The rejected alternative is promotion by metric threshold. The narrow
    claim this pin makes is the one that can be checked from the syntax tree:
    no function in the verb mentions an agreement figure and a measured metric
    in the same scope. That is not the whole of the invariant -- the gate
    carries the rest -- but it is the part the verb could break on its own.

    Mutation: a `verdict.tally.agreement` comparison against `verdict.
    slo_reads`, or any local that pairs them."""
    metric_names = {"slo", "latency", "cost", "tokens", "refusal_rate",
                    "slo_reads"}
    figure_names = {"agreement", "agreed", "paired", "tally", "threshold",
                    "ratio"}
    for node in ast.walk(module_ast()):
        if not isinstance(node, ast.FunctionDef):
            continue
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute):
                names.add(child.attr)
            elif isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.arg):
                names.add(child.arg)
        assert not (names & metric_names and names & figure_names), \
            f"{node.name} holds both a metric and an agreement figure"


def test_the_verb_invents_no_link_and_no_decision_word():
    """Every link the verb can print is the gate's or the schedule's.

    `_refused` builds its verdict with `promotion.refused`, the gate's own
    constructor for a caller-owned precondition, so the verb cannot invent a
    fourth decision word or a link nobody can look up. The links it names come
    from `resolve` (the composition stage) and from `_check_entries`.

    The check is over the SYNTAX TREE, not the text, because the text contains
    the words in prose: the module defines no string constant of its own
    (`REFUSE = "refuse"` is the shape this refuses), and the one comparison
    against a decision word is against the gate's own attribute.

    Mutation: `REFUSE = "refuse"` at module level, or a link literal such as
    `"served-candidate"`."""
    tree = module_ast()

    # no module-level string constant: the verb has no vocabulary of its own
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert isinstance(target, ast.Name), \
                    f"{ast.dump(target)[:40]} is a module-level name"
                assert not isinstance(node.value, ast.Constant), \
                    f"{getattr(target, 'id', '?')} is a module-level constant"

    # the one decision word it compares against is the gate's own attribute
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert source.count("promotion.PROMOTE") == 1
    compared = [node for node in ast.walk(tree)
                if isinstance(node, ast.Compare)
                and any(isinstance(side, ast.Attribute)
                        and side.attr in {"PROMOTE", "REFUSE", "REVERT"}
                        for side in [node.left] + node.comparators)]
    assert len(compared) == 1

    # and every link it can print comes from a resolution the gate made
    named = {node.attr for node in ast.walk(tree)
             if isinstance(node, ast.Attribute)}
    assert not named & {"LINKS", "WINDOW_LINKS", "PRECONDITION_LINKS", "STAGES"}


def _promote_declaration_block(tree):
    """The statements that build the `promote` subparser, as one list.

    The assignment and the `promote_cmd.add_argument(...)` calls that follow
    it, and nothing else: a later statement about a different verb is where
    this block ends.
    """
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for index, statement in enumerate(body):
            if not (isinstance(statement, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == "promote_cmd"
                            for target in statement.targets)):
                continue
            block = [statement]
            for later in body[index + 1:]:
                call = later.value if isinstance(later, ast.Expr) else None
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "promote_cmd"):
                    break
                block.append(later)
            return block
    return None


def test_the_promote_subparser_declares_no_number_and_no_decision_word():
    """`src/revl/cli/parser.py` is exempt from the 543 sweep because it
    DECLARES the verb and decides nothing.

    The exemption argues that the token the sweep matches is the subcommand's
    own name and that every value the module accepts is handed to a handler.
    That argument is a claim about this block, so the claim is pinned: the
    block that builds `promote` carries no numeric constant -- a threshold, a
    share, a sample size -- and no decision word other than the verb's own
    name. A flag that took a number would be this module deciding, and the
    sweep would then be exempting a policy engine.

    Mutation: `promote_cmd.add_argument("--threshold", type=float,
    default=0.9)` in the block, or a `REFUSE = "refuse"` constant beside it."""
    block = _promote_declaration_block(
        ast.parse(CLI_PARSER_PATH.read_text(encoding="utf-8")))
    assert block is not None, "the promote subparser is not built by name"

    numbers = [node.value for statement in block
               for node in ast.walk(statement)
               if isinstance(node, ast.Constant)
               and isinstance(node.value, (int, float))
               and not isinstance(node.value, bool)]
    assert numbers == [], \
        f"the promote declaration carries a number: {numbers}"

    words = {node.value for statement in block for node in ast.walk(statement)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not words & {"refuse", "revert", "REFUSE", "REVERT", "PROMOTE"}, \
        f"the promote declaration carries a decision word: {sorted(words)}"
    assert "promote" in words, "the verb's own name is not in its declaration"


def test_the_dispatch_of_promote_is_one_return_and_not_a_decision():
    """`src/revl/__main__.py` is exempt from the 543 sweep because it
    DISPATCHES.

    The exemption argues that the token the sweep matches is the comparison
    `args.command == "promote"` and that the verdict is rendered elsewhere, by
    a module that runs the registered path. So the branch is pinned to be
    exactly that: a bare comparison against the verb's name, whose whole body
    is one `return` of the handler's call. A second condition on the branch, a
    status computed in it, or a call to anything else is this module deciding
    -- and it is the place a decision would be cheapest to hide.

    Mutation: `return 0 if _run_promote(args) == 0 else 1`, or
    `if args.command == "promote" and args.force:`."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    branch = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Attribute)
                and test.left.attr == "command"
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == VERB):
            branch = node
            break
    assert branch is not None, f"no `args.command == {VERB!r}` dispatch"

    assert len(branch.body) == 1, \
        f"the dispatch is {len(branch.body)} statements, not one"
    statement = branch.body[0]
    assert isinstance(statement, ast.Return), \
        f"the dispatch does not return: {ast.dump(statement)[:60]}"
    assert isinstance(statement.value, ast.Call), \
        "the dispatch returns something it computed"
    assert isinstance(statement.value.func, ast.Name) \
        and statement.value.func.id == "_run_promote", \
        "the dispatch returns something other than the handler's verdict"

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    assert not any("shadow_register" in name for name in imported), \
        "the dispatch chain imports the arm that LANDS"
