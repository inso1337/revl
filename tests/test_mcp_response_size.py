"""Pin the text each MCP verb returns (issue #2939).

`bench/mcp_response_size.py` drives a fixed scripted session through the real
MCP dispatcher and measures the size of the text payload the agent receives,
in the same proxy tokens as `bench/tokens.py`. A response is reinstalled into
the agent's context on every later turn, so a refusal or a plan that quietly
grows costs input tokens for the rest of the session.

This file is the pin. Each scripted call has one recorded ceiling, set at the
size measured on the revision that recorded it:

  * `test_each_response_stays_under_its_recorded_ceiling` fails when a response
    grows past its ceiling. Raising a ceiling is a reviewed change, in the same
    diff as the growth that needs it.
  * `test_each_ceiling_is_no_looser_than_todays_measurement` keeps the pin
    tight: a response that shrinks must lower its ceiling in the same diff, so
    the gained headroom cannot hide a later regression.

The scripted session is one passing `revl_ship`, one refused `revl_ship`, and
one `revl_plan`; if it changes, update `SCRIPTED_STEPS` and `CEILINGS` together.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import mcp_response_size as responses  # noqa: E402

# Imported so tools/affected_tests.py selects this pin for every module on the
# MCP response path: the ceilings below pin the text that path returns, so a
# change to server.py (or remedy.py/brief.py behind it) must run this file.
from revl.mcp import server  # noqa: F401,E402

# One ceiling per scripted call, in the proxy tokens of `bench/tokens.py`
# (`count_tokens` over the text payload the agent receives).
SCRIPTED_STEPS = ["ship-pass", "ship-refused", "plan"]
CEILINGS = {
    "ship-pass": 1488,
    "ship-refused": 1041,
    "plan": 1100,
}


@pytest.fixture(scope="module")
def rows():
    return responses.measure()


def _over_ceiling(rows, ceilings):
    """The scripted calls whose measured size is past their recorded ceiling.

    This is the single comparison the real pin and the guard test share. A
    missing ceiling is a failure, not a KeyError: when the scripted session
    grows, the pin must say which call has no recorded ceiling, even when this
    file is run with a subset of its tests.
    """
    out = []
    for row in rows:
        ceiling = ceilings.get(row["step"])
        assert ceiling is not None, (
            f"no recorded ceiling for scripted call `{row['step']}`; add one "
            f"to CEILINGS when the scripted session gains a call")
        if row["est_tokens"] > ceiling:
            out.append(row)
    return out


def test_scripted_session_covers_the_three_measured_cases(rows):
    assert [row["step"] for row in rows] == SCRIPTED_STEPS
    assert set(CEILINGS) == {row["step"] for row in rows}, (
        "the scripted session changed: record a ceiling for every call it makes")
    assert [row["verb"] for row in rows] == ["revl_ship", "revl_ship", "revl_plan"]
    assert [row["outcome"] for row in rows] == ["ok", "refused", "ok"]


def test_each_response_stays_under_its_recorded_ceiling(rows):
    over = _over_ceiling(rows, CEILINGS)
    assert not over, "; ".join(
        f"{row['step']} ({row['verb']}, {row['outcome']}) returned "
        f"{row['est_tokens']} est. tokens, past its ceiling of "
        f"{CEILINGS[row['step']]}. Every later turn of the session pays this "
        f"text again; raise the ceiling in this file only when the growth is "
        f"intended"
        for row in over)


def test_each_ceiling_is_no_looser_than_todays_measurement(rows):
    for row in rows:
        ceiling = CEILINGS.get(row["step"])
        assert ceiling is not None, f"no recorded ceiling for `{row['step']}`"
        assert row["est_tokens"] == ceiling, (
            f"{row['step']} ({row['verb']}, {row['outcome']}) now returns "
            f"{row['est_tokens']} est. tokens against a ceiling of {ceiling}. "
            f"Re-record the ceiling to today's size so the pin stays tight.")


def test_a_response_that_grows_past_its_ceiling_is_rejected(rows):
    """Lowering one ceiling below the measured size reports exactly that call.

    This exercises the same comparison as the real pin, without editing the
    file: a response one token past a ceiling cannot pass.
    """
    row = rows[0]
    lowered = {**CEILINGS, row["step"]: row["est_tokens"] - 1}
    over = _over_ceiling(rows, lowered)
    assert [(r["step"], r["est_tokens"]) for r in over] == [
        (row["step"], row["est_tokens"])]


def test_measurement_is_deterministic_across_runs():
    assert responses.measure() == responses.measure()


def test_report_is_deterministic_and_aggregates_per_verb(rows):
    first = responses.render(rows, sha="test")
    assert first == responses.render(responses.measure(), sha="test")
    text = "\n".join(first)
    assert "| scripted call | verb | outcome | est. tokens | chars |" in text
    assert "### per verb" in text
    # revl_ship is the one verb the session calls twice: median 1264.5, max 1488.
    assert "| `revl_ship` | 2 | 1264.5 | 1488 | 2529 |" in text
    assert "| `revl_plan` | 1 | 1100 | 1100 | 1100 |" in text
