"""Item 250 Slice 3b (the offline half): replay-mode readiness over a durable
WAL (docs/design/250-slice3b-replay-modes.md).

Slice 3a made ONE model decision durable per completion crossing (a
`model-decision` record: which model, at what token/cost, in how many attempts,
keyed on the completion's own effect record). Slice 3b is the four replay modes
that record enables. The LIVE executor that re-runs a branch is the other half
and needs a live component; this is the offline readiness surface, mirroring
`revl branch` / `revl compare`: it reads the record and says, per mode, whether
the WAL carries enough to inform that mode and what the live executor would
still need. It runs nothing.

What these pin:

* the four modes are reported, each with its requirements, what the WAL meets,
  what it is missing, and the two verdicts (`plannable`, `executable`);
* a WAL carrying model decisions makes model-substitute / counterfactual
  `plannable` (the decisions are the substitution surface) but never
  `executable` offline; exact / tool-only are never plannable because the
  response text they need is never on the WAL;
* a WAL with NO model decision informs no mode (nothing on the record), which
  the plan states rather than guesses at, and which the CLI turns into a
  nonzero exit — an honest gate on "is this WAL model-aware";
* the reader is tier-agnostic (it reads model-decision records by kind, so a
  WAL written before the reader's constant landed still reads), and the CLI
  wiring dispatches and honors --mode / --json.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import replay_modes as rm  # noqa: E402


# ---------------------------------------------------------------------------
# WAL fixtures: JSON Lines a Slice-3a runtime would write, built directly so
# these tests do not depend on the writer's Slice-3a methods being present.
# ---------------------------------------------------------------------------


def _write_wal(path: Path, records: list) -> str:
    header = {"record": "header", "walVersion": 1, "generation": 1,
              "guarantee": "test"}
    lines = [json.dumps(header)] + [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _effect(seq, component, step_index, kind="emission", label="complete"):
    return {"record": "effect", "seq": seq, "component": component,
            "stepIndex": step_index, "kind": kind, "label": label,
            "site": None, "source": None, "origin": {},
            "boundary": {}, "inverse": {}}


def _decision(component, step_index, model="openai:gpt-4o", outcome="validated",
              attempts=1, ceiling=3):
    return {"record": "model-decision", "component": component,
            "stepIndex": step_index, "outcome": outcome,
            "llm": {"model": model, "modelProvenance": "host-reported",
                    "tokensIn": 1204, "tokensOut": 88,
                    "usageProvenance": "host-reported",
                    "cost": {"amount": 0.0121, "currency": "USD",
                             "provenance": "host-reported"},
                    "latencySeconds": 1.84,
                    "latencyProvenance": "revl-measured-bracket",
                    "attempts": attempts, "attemptCeiling": ceiling,
                    "attemptsProvenance": "revl-controlled", "verifiedBy": []}}


def _model_aware_wal(tmp_path) -> str:
    """A WAL with two model completions and a plain fs effect between them."""
    return _write_wal(tmp_path / "run.wal", [
        _effect(0, "AgentLoop", 0),
        _decision("AgentLoop", 0, model="openai:gpt-4o"),
        _effect(1, "AgentLoop", 1, kind="effect", label="write"),
        _effect(2, "AgentLoop", 2),
        _decision("AgentLoop", 2, model="anthropic:claude", attempts=2),
    ])


def _pre_3a_wal(tmp_path) -> str:
    """A WAL from before Slice 3a (or a run that made no completion): effects
    only, no model-decision record anywhere."""
    return _write_wal(tmp_path / "old.wal", [
        _effect(0, "AgentLoop", 0, kind="effect", label="write"),
        _effect(1, "AgentLoop", 1, kind="emission", label="send"),
    ])


# ---------------------------------------------------------------------------
# 1. the four modes, their requirements and verdicts
# ---------------------------------------------------------------------------


def test_all_four_modes_are_reported_with_their_requirements(tmp_path):
    doc = rm.plan(_model_aware_wal(tmp_path))
    assert doc["kind"] == "revl.replay-plan"
    assert [m["mode"] for m in doc["modes"]] == list(rm.MODES)
    for entry in doc["modes"]:
        assert entry["requires"], "every mode names its requirements"
        assert entry["intent"]
        # the requirement vocabulary the plan reports on is documented
        for req in entry["requires"]:
            assert req in doc["requirements"]


def test_no_mode_is_ever_executable_offline(tmp_path):
    doc = rm.plan(_model_aware_wal(tmp_path))
    assert all(m["executable"] is False for m in doc["modes"])
    assert "offline readiness plan" in doc["offlineNote"]


def test_substitute_modes_are_plannable_when_decisions_are_recorded(tmp_path):
    doc = rm.plan(_model_aware_wal(tmp_path))
    by_mode = {m["mode"]: m for m in doc["modes"]}
    # the decisions are the substitution surface: model-substitute and
    # counterfactual are plannable, blocked only on the live executor.
    assert by_mode["model-substitute"]["plannable"] is True
    assert by_mode["counterfactual"]["plannable"] is True
    assert by_mode["model-substitute"]["blocker"] == "liveComponent"


def test_exact_and_tool_only_are_never_plannable_without_response_text(tmp_path):
    doc = rm.plan(_model_aware_wal(tmp_path))
    by_mode = {m["mode"]: m for m in doc["modes"]}
    assert by_mode["exact"]["plannable"] is False
    assert by_mode["tool-only"]["plannable"] is False
    # the headline blocker is the response text, not the live component
    assert by_mode["exact"]["blocker"] == "responseText"
    assert "responseText" in by_mode["tool-only"]["missing"]


def test_the_substitution_surface_lists_the_recorded_decisions(tmp_path):
    doc = rm.plan(_model_aware_wal(tmp_path))
    decisions = doc["modelDecisions"]
    assert [(d["component"], d["stepIndex"]) for d in decisions] == \
        [("AgentLoop", 0), ("AgentLoop", 2)]
    assert decisions[0]["model"] == "openai:gpt-4o"
    assert decisions[1]["model"] == "anthropic:claude"
    assert decisions[1]["attempts"] == 2


# ---------------------------------------------------------------------------
# 2. a WAL with no model decision informs no mode
# ---------------------------------------------------------------------------


def test_a_pre_3a_wal_makes_no_mode_plannable(tmp_path):
    doc = rm.plan(_pre_3a_wal(tmp_path))
    assert doc["modelDecisions"] == []
    assert all(m["plannable"] is False for m in doc["modes"])
    for entry in doc["modes"]:
        assert "modelDecisions" in entry["missing"]
    assert "made no model completion" in doc["pre3aNote"]


# ---------------------------------------------------------------------------
# 3. --mode selects one, unknown mode refused, bad WAL refused
# ---------------------------------------------------------------------------


def test_mode_argument_reports_one_mode(tmp_path):
    doc = rm.plan(_model_aware_wal(tmp_path), mode="model-substitute")
    assert [m["mode"] for m in doc["modes"]] == ["model-substitute"]
    expected = set(rm.MODE_RECORD_REQUIRES["model-substitute"]) | \
        set(rm.MODE_EXECUTOR_REQUIRES["model-substitute"])
    assert set(doc["requirements"]) == expected


def test_an_unknown_mode_is_refused():
    with pytest.raises(rm.ReplayPlanError):
        rm.plan("unused.wal", mode="teleport")


def test_an_unreadable_wal_is_refused(tmp_path):
    with pytest.raises(rm.ReplayPlanError):
        rm.plan(str(tmp_path / "does-not-exist.wal"))


def test_render_is_a_string_naming_every_mode(tmp_path):
    text = rm.render(rm.plan(_model_aware_wal(tmp_path)))
    assert "replay plan" in text
    for mode in rm.MODES:
        assert mode in text


# ---------------------------------------------------------------------------
# 4. CLI wiring: dispatch, exit status, --mode, --json
# ---------------------------------------------------------------------------


def _run_cli(argv):
    from revl.__main__ import main  # noqa: PLC0415
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def test_cli_exit_zero_and_json_for_a_model_aware_wal(tmp_path):
    path = _model_aware_wal(tmp_path)
    code, out = _run_cli(["replay", path, "--json"])
    assert code == 0
    doc = json.loads(out)
    assert doc["kind"] == "revl.replay-plan"
    assert len(doc["modelDecisions"]) == 2


def test_cli_exit_nonzero_for_a_wal_with_no_model_decision(tmp_path):
    path = _pre_3a_wal(tmp_path)
    code, _ = _run_cli(["replay", path])
    assert code == 1


def test_cli_mode_flag_is_honored(tmp_path):
    path = _model_aware_wal(tmp_path)
    code, out = _run_cli(["replay", path, "--mode", "counterfactual", "--json"])
    assert code == 0
    doc = json.loads(out)
    assert [m["mode"] for m in doc["modes"]] == ["counterfactual"]
