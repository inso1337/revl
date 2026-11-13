"""Guard for the MCP answer-shape A/B (issue #2847).

`tools/shape_ab.py` replays the committed corpus through the current shape of
an MCP exchange (A) and the token-shaped one (B) and commits the delta in
`bench/results/shape-ab.md`. These tests keep that measurement honest and make
a regression in answer size visible where changes land:

  1. a small fixture component is measured, so the tool does not depend on the
     corpus to do its job;
  2. two runs produce the identical table;
  3. over the whole corpus, B never spends more than A in total on turns,
     emitted tokens or received tokens, nor on any brief that runs to the end,
     and both arms reach the same outcome on every brief, so a saving cannot be
     bought by changing the answer;
  4. `compare` is green against a fresh render and red on a wrong row;
  5. the committed table reproduces, so a stale or grown result is a test
     failure rather than a review miss.

All of it runs in process, with no model, network or cordis.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import shape_ab  # noqa: E402

#: A provider that admits against `shape_ab.RUNNING`, so the compound arms run
#: all three stages.
FIXTURE = """service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 7
  }
}
"""


def _by_name(results):
    return {r.name: r for r in results}


@pytest.fixture(scope="module")
def corpus_briefs():
    return shape_ab.corpus()


@pytest.fixture(scope="module")
def corpus_results(corpus_briefs):
    return shape_ab.measure(corpus_briefs)


def test_a_fixture_brief_is_measured_in_both_arms():
    results = _by_name(shape_ab.measure([shape_ab.Brief("fixture", FIXTURE)]))

    compound = results["compound"]
    assert (compound.a[0].turns, compound.b[0].turns) == (3, 1)
    assert compound.a[0].verdict == compound.b[0].verdict == "planned"
    assert compound.b[0].emitted < compound.a[0].emitted

    handle = results["handle"]
    assert (handle.a[0].turns, handle.b[0].turns) == (2, 2)
    assert handle.a[0].verdict == handle.b[0].verdict == "checked:ok"
    # the handle arm neither receives nor re-sends the formatted text
    assert handle.b[0].emitted < handle.a[0].emitted
    assert handle.b[0].received < handle.a[0].received


def test_the_table_is_stable_across_two_runs():
    briefs = [shape_ab.Brief("fixture", FIXTURE)]

    first = shape_ab.measure(briefs)
    second = shape_ab.measure(briefs)

    assert first == second
    assert shape_ab.render(first) == shape_ab.render(second)


def test_the_shaped_arm_never_costs_more_in_total_nor_on_a_finished_brief(
        corpus_results, corpus_briefs):
    for result in corpus_results:
        assert shape_ab.verdict_mismatches(result) == [], result.name
        for key in ("turns", "emitted", "received"):
            a, b = shape_ab._sum(result.a, key), shape_ab._sum(result.b, key)
            assert b <= a, (result.name, key, a, b)
        # on a brief that runs to the end, B never spends more than A on any
        # metric (a refused brief is different: see the next test)
        # (a handle has a fixed cost, so the handle experiment starts at
        # MIN_HANDLE_SOURCE_CHARS; below that the table reports the loss)
        for i, (arm_a, arm_b) in enumerate(zip(result.a, result.b)):
            if not shape_ab.through(arm_b):
                continue
            if (result.name == "handle"
                    and len(corpus_briefs[i].source)
                    < shape_ab.MIN_HANDLE_SOURCE_CHARS):
                continue
            assert arm_b.turns <= arm_a.turns, (result.name, i)
            assert arm_b.emitted <= arm_a.emitted, (result.name, i)
            assert arm_b.received <= arm_a.received, (result.name, i)


def test_a_refused_brief_costs_b_the_manifest_but_not_a_turn(corpus_results):
    # `revl_ship` carries the running manifest into a call that `revl_check`
    # alone would have refused without it: the table says so, and this pins
    # that the extra cost is tokens and never a turn.
    refused = shape_ab.split(_by_name(corpus_results)["compound"], False)

    assert refused.a
    for arm_a, arm_b in zip(refused.a, refused.b):
        assert arm_b.turns == arm_a.turns == 1


def test_parity_rejects_a_different_diagnostic_or_a_different_stage():
    a = shape_ab.Arm(verdict="stopped:check", extra={"code": "G8"})
    gate = shape_ab.Arm(verdict="stopped:gate", extra={"code": "G8"})
    other_code = shape_ab.Arm(verdict="stopped:gate", extra={"code": "USAGE"})
    other_stage = shape_ab.Arm(verdict="stopped:admit", extra={"code": "G8"})

    def mismatches(b):
        return shape_ab.verdict_mismatches(shape_ab.Result("x", (a,), (b,)))

    assert mismatches(gate) == []
    assert mismatches(other_code) == [0]
    assert mismatches(other_stage) == [0]


def test_the_replay_pins_and_restores_the_authoring_profile():
    from revl.mcp import server

    leaked = server.AuthoringTrust(host_code=True)
    before = server.AUTHORING
    server.AUTHORING = leaked
    try:
        shape_ab.measure([shape_ab.Brief("fixture", FIXTURE)])
        assert server.AUTHORING is leaked
    finally:
        server.AUTHORING = before


def test_compound_is_one_turn_per_brief(corpus_results):
    compound = _by_name(corpus_results)["compound"]

    assert [arm.turns for arm in compound.b] == [1] * len(compound.b)


def test_compare_is_green_then_red_on_a_wrong_row(corpus_results, tmp_path,
                                                  capsys):
    committed = tmp_path / "committed.md"
    committed.write_text(shape_ab.render(corpus_results), encoding="utf-8")

    assert shape_ab.compare(corpus_results, committed) == 0

    text = committed.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines()
               if line.startswith("| turns |"))
    committed.write_text(text.replace(row, row.replace("| turns |",
                                                       "| turn |", 1)),
                         encoding="utf-8")

    assert shape_ab.compare(corpus_results, committed) == 1
    assert "does not match a fresh run" in capsys.readouterr().err


def test_the_committed_table_reproduces(corpus_results, capsys):
    assert shape_ab.compare(corpus_results, shape_ab.DEFAULT_DOC) == 0
    assert "reproduced" in capsys.readouterr().out
