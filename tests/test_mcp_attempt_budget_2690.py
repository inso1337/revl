"""Issue #2690: refusals carry an attempt count and the remaining budget.

`revl_check` and `revl_admit` are the loop's verdict verbs, and a caller
driving a retry loop could not tell a first refusal from a tenth: nothing on
the refusal said how many attempts this unit of work had already spent, or how
many the operator allowed. Every refusal from those two verbs now carries
`attempt` (1-based, on the loaded composition, reset by a fresh load or an
accepted call) and `attemptsRemaining` (only when the session was served with
`--attempt-limit`, absent otherwise and never a made-up number). The count is
advisory: reaching the limit changes nothing about admission, only what the
caller is told. The aggregate rides on `revl_state`'s `loopAxes` report with
the other session measures.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import repeat  # noqa: E402
from revl.mcp.loop_axes import LoopAxes  # noqa: E402
from revl.mcp.server import SESSION, handle  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv")

GOOD = "component Ok {\n}\n"
BAD = "component Broken {\n"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh():
    """A fresh attempt counter and no configured limit, per test."""
    old_axes = SESSION._loop_axes
    old_limit = getattr(SESSION, "attempt_limit", None)
    old_generation = SESSION._generation
    # absent before issue #2690; restored as absent so a run against a tree
    # without the load epoch fails on an assertion, not in the fixture
    old_load_epoch = getattr(SESSION, "_load_epoch", None)
    SESSION._loop_axes = LoopAxes()
    SESSION.attempt_limit = None
    repeat.forget()
    try:
        yield
    finally:
        if SESSION.loaded:
            SESSION.unload()
        SESSION._loop_axes = old_axes
        SESSION.attempt_limit = old_limit
        SESSION._generation = old_generation
        if old_load_epoch is None:
            if hasattr(SESSION, "_load_epoch"):
                del SESSION._load_epoch
        else:
            SESSION._load_epoch = old_load_epoch
        repeat.forget()


# ------------------------------------------------------------------ the count


def test_three_consecutive_check_refusals_count_up():
    payloads = [_call("revl_check", {"source": BAD}) for _ in range(3)]
    assert [p["attempt"] for p in payloads] == [1, 2, 3]
    assert all(p["ok"] is False for p in payloads)
    # no limit was configured, so no budget is invented
    assert all("attemptsRemaining" not in p for p in payloads)


def test_a_configured_limit_reports_the_remaining_budget():
    SESSION.attempt_limit = 3
    payloads = [_call("revl_check", {"source": BAD}) for _ in range(3)]
    assert [p["attemptsRemaining"] for p in payloads] == [2, 1, 0]


def test_past_the_limit_the_budget_stays_zero_and_admission_is_unchanged():
    SESSION.attempt_limit = 1
    first = _call("revl_check", {"source": BAD})
    second = _call("revl_check", {"source": BAD})
    assert (first["attempt"], first["attemptsRemaining"]) == (1, 0)
    assert (second["attempt"], second["attemptsRemaining"]) == (2, 0)
    # advisory only: the over-budget call is refused for its own reason,
    # with its diagnostics intact, not for having spent the budget
    assert second["ok"] is False and second["diagnostics"]
    assert not any("budget" in d.get("message", "")
                   for d in second["diagnostics"])


def test_an_accepted_call_resets_the_count():
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_check", {"source": BAD})["attempt"] == 2
    assert _call("revl_check", {"source": GOOD})["ok"] is True
    assert _call("revl_check", {"source": BAD})["attempt"] == 1


def test_admit_refusals_carry_the_count_and_budget():
    SESSION.attempt_limit = 2
    first = _call("revl_admit", {"source": BAD, "manifest": {}})
    second = _call("revl_admit", {"source": BAD, "manifest": {}})
    assert (first["attempt"], first["attemptsRemaining"]) == (1, 1)
    assert (second["attempt"], second["attemptsRemaining"]) == (2, 0)
    assert first["admitted"] is False and second["admitted"] is False


def test_check_and_admit_share_one_counter():
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_admit", {"source": BAD, "manifest": {}})["attempt"] == 2
    assert _call("revl_check", {"source": BAD})["attempt"] == 3


def test_a_refusal_through_the_verbs_hatch_carries_the_count_too():
    first = _call("revl_verbs", {"name": "revl_check",
                                 "args": {"source": BAD}})
    second = _call("revl_verbs", {"name": "revl_check",
                                  "args": {"source": BAD}})
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert "attemptsRemaining" not in first


def test_a_new_generation_starts_a_new_unit():
    """A swap, rollback or undo moves the generation, and that move is
    simulated here: those doors boot a composition, so driving one needs the
    cordis runtime and belongs to the swap/rollback/undo suites. This checks
    the counter's contract against the move they make, and the body says so.
    """
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    # simulation of the generation move (see the docstring): a swap, rollback
    # or undo increments the generation without a load
    SESSION._generation += 1
    assert _call("revl_check", {"source": BAD})["attempt"] == 1


def test_a_new_generation_is_reported_as_a_new_unit_immediately():
    """The generation move below is simulated, deliberately: a real one goes
    through swap, rollback or undo, which need cordis to boot a composition.
    What this test covers is the part with no cordis-free public door, the
    report's comparison against the live unit at read time.
    """
    SESSION.attempt_limit = 3
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_check", {"source": BAD})["attempt"] == 2
    # simulation of the generation move: a swap, rollback or undo increments
    # it, and no verdict has run since, so the report must not show the old
    # unit's 2 of 3
    SESSION._generation += 1
    axes = _call("revl_state", {})["loopAxes"]
    assert (axes["attempt"], axes["attemptsRemaining"]) == (0, 3)
    # and the next refusal agrees with what the report just said
    refusal = _call("revl_check", {"source": BAD})
    assert (refusal["attempt"], refusal["attemptsRemaining"]) == (1, 2)


def test_the_report_compares_the_unit_it_is_read_against():
    axes = LoopAxes()
    old = (False, 0, 0)
    axes.record_attempt(old, refused=True)
    axes.record_attempt(old, refused=True)
    assert axes.document(None, 3, unit=old)["attempt"] == 2
    fresh = axes.document(None, 3, unit=(True, 1, 1))
    assert (fresh["attempt"], fresh["attemptsRemaining"]) == (0, 3)
    # reading the report is a read: it does not move the counter, so a report
    # can neither refund a failed load nor spend an attempt
    assert axes.document(None, 3, unit=old)["attempt"] == 2


def test_a_refused_load_does_not_start_a_new_unit():
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    refused = _call("revl_load", {"source": BAD})
    assert refused["ok"] is False
    assert _call("revl_check", {"source": BAD})["attempt"] == 2
    # a failed load moved neither the epoch nor the generation, so the report
    # still shows the count it never reset
    assert _call("revl_state", {})["loopAxes"]["attempt"] == 2


def test_a_failed_load_after_the_epoch_move_does_not_refund_the_budget(
        monkeypatch):
    """A load that fails after the move still leaves the old unit.

    The epoch bump lives late in `Session._boot`, after the candidate is
    installed, so a fault in an activation (or any later step) happens with
    the move already made. `load` captured the whole namespace before it
    called the body and puts it back, which is what makes the failed load
    refund nothing. Booting far enough to fault for real needs cordis, so the
    fault is injected at `_boot`; the checkpoint and the `load` body are the
    real ones, and that restore is what this covers. Without it a failed load
    would move the unit and the next verdict would start at 1, handing back
    the spent budget.
    """
    SESSION.attempt_limit = 2
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_check", {"source": BAD})["attempt"] == 2
    epoch, generation = SESSION._load_epoch, SESSION._generation

    def boot_then_fault(ir, config, record, origin):
        # the moves a boot that got this far would have made, then the fault
        # the checkpoint has to undo
        SESSION._load_epoch += 1
        SESSION._generation = 1
        raise RuntimeError("fault after the epoch move")

    monkeypatch.setattr(SESSION, "_boot", boot_then_fault)
    with pytest.raises(RuntimeError, match="fault after the epoch move"):
        SESSION.load({"components": []})
    monkeypatch.undo()

    assert (SESSION._load_epoch, SESSION._generation) == (epoch, generation)
    # the old unit is still live: the count continues, it does not restart
    refusal = _call("revl_check", {"source": BAD})
    assert (refusal["attempt"], refusal["attemptsRemaining"]) == (3, 0)
    assert _call("revl_state", {})["loopAxes"]["attempt"] == 3


def test_a_limit_below_one_is_a_usage_error_about_the_bound(capsys):
    from revl.cli.parser import build_parser  # noqa: PLC0415

    parser = build_parser()
    # the parser knows the option, so accepting the smallest valid budget is
    # part of the test: on main the flag is unknown and this line exits
    assert parser.parse_args(
        ["mcp", "serve", "--attempt-limit", "1"]).attempt_limit == 1
    for value in ("0", "-2"):
        with pytest.raises(SystemExit):
            parser.parse_args(["mcp", "serve", "--attempt-limit", value])
        err = capsys.readouterr().err
        # refused for the bound, not for being an unrecognised argument
        assert "at least 1" in err
        assert "unrecognized arguments" not in err


def test_a_fresh_load_epoch_restarts_the_report_in_ci():
    """The fresh-load reset, visible without the cordis runtime.

    A successful `Session.load` increments `_load_epoch` in its `_boot` body
    (`src/revl/mcp/session.py`), which is the move that makes a reload a new
    unit even though its generation restarts at 1. Booting needs cordis, so
    this drives that same move on the session and checks what the report does
    with it; the cordis-gated test below drives the real load end to end, and
    `test_session_load_owns_the_epoch_move` keeps this move honest.
    """
    SESSION.attempt_limit = 2
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_check", {"source": BAD})["attempt"] == 2
    SESSION._load_epoch += 1
    axes = _call("revl_state", {})["loopAxes"]
    assert (axes["attempt"], axes["attemptsRemaining"]) == (0, 2)
    assert _call("revl_check", {"source": BAD})["attempt"] == 1


def test_session_load_owns_the_epoch_move():
    """Without this, the simulated move above could stay green while the load
    path stopped bumping the epoch, and no real load would start a new unit.

    The move lives in `Session._boot`, the body `Session.load` calls, so the
    guard names both and fails if the increment moves somewhere else.
    Allowing `load` as well as `_boot` is deliberate: the invariant is that
    the bump happens after `load` captured its checkpoint, not that it sits in
    one named body, and `load` is what owns that checkpoint. The failed-load
    test above keeps the allowance honest, because a bump placed before the
    capture would survive the restore and be seen as a refund. Pinning the
    exact function would fail that refactor without protecting anything the
    failed-load test does not already protect."""
    import ast  # noqa: PLC0415 — test-only

    tree = ast.parse(
        (ROOT / "src/revl/mcp/session.py").read_text(encoding="utf-8"))
    session = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "Session")
    moved = [
        node.name for node in session.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(inner, ast.AugAssign)
            and isinstance(inner.target, ast.Attribute)
            and isinstance(inner.target.value, ast.Name)
            and inner.target.value.id == "self"
            and inner.target.attr == "_load_epoch"
            and isinstance(inner.op, ast.Add)
            for inner in ast.walk(node))]
    assert moved and set(moved) <= {"load", "_boot"}, moved


@needs_cordis
def test_a_fresh_load_starts_a_new_unit_of_work():
    SESSION.attempt_limit = 2
    assert _call("revl_load", {"source": GOOD})["ok"]
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_check", {"source": BAD})["attempt"] == 2
    epoch = SESSION._load_epoch
    assert _call("revl_unload", {})["ok"]
    loaded = _call("revl_load", {"source": GOOD})
    assert loaded["ok"]
    # the load moved the unit: a new epoch, generation 1 again
    assert SESSION._load_epoch == epoch + 1
    # the load response itself carries the new unit's report, not the old 2
    # of 2 it would have shown while the reset stayed lazy
    assert (loaded["loopAxes"]["attempt"],
            loaded["loopAxes"]["attemptsRemaining"]) == (0, 2)
    axes = _call("revl_state", {})["loopAxes"]
    assert (axes["attempt"], axes["attemptsRemaining"]) == (0, 2)
    # generation starts at 1 again, but this is a new unit: the count restarts
    assert _call("revl_check", {"source": BAD})["attempt"] == 1
    assert _call("revl_state", {})["loopAxes"]["attempt"] == 1


# ------------------------------------------- the success paths are untouched


def test_a_successful_check_is_byte_identical_and_carries_no_attempt_fields():
    first = _call("revl_check", {"source": GOOD})
    second = _call("revl_check", {"source": GOOD})
    assert first["ok"] is True and second["ok"] is True
    assert "attempt" not in first and "attemptsRemaining" not in first
    assert first == second


def test_a_successful_admit_carries_no_attempt_fields():
    out = _call("revl_admit", {"source": GOOD, "manifest": {}})
    assert out["ok"] is True and out["admitted"] is True
    assert "attempt" not in out and "attemptsRemaining" not in out


# ------------------------------------------------------ the loop-axes report


def test_the_loop_axes_report_carries_the_attempt_and_budget():
    SESSION.attempt_limit = 3
    _call("revl_check", {"source": BAD})
    _call("revl_check", {"source": BAD})
    axes = _call("revl_state", {})["loopAxes"]
    assert axes["attempt"] == 2
    assert axes["attemptsRemaining"] == 1


def test_the_loop_axes_report_omits_the_budget_without_a_limit():
    _call("revl_check", {"source": BAD})
    axes = _call("revl_state", {})["loopAxes"]
    assert axes["attempt"] == 1
    assert "attemptsRemaining" not in axes


# ------------------------------------------------------- the session setting


def test_the_serve_flag_binds_the_session_limit():
    from revl.cli.interop import _bind_session_authority  # noqa: PLC0415
    from revl.cli.parser import build_parser  # noqa: PLC0415

    args = build_parser().parse_args(["mcp", "serve", "--attempt-limit", "2"])
    assert _bind_session_authority(args) is None
    assert SESSION.attempt_limit == 2
    default = build_parser().parse_args(["mcp", "serve"])
    assert default.attempt_limit is None


def test_the_limit_does_not_change_what_a_call_does():
    SESSION.attempt_limit = 1
    refused = _call("revl_check", {"source": BAD})
    accepted = _call("revl_check", {"source": GOOD})
    assert refused["ok"] is False and refused["diagnostics"]
    assert accepted["ok"] is True and accepted["selfCheck"]
