"""The swap refusal distinguishes a FAILED successor from a PENDING one.

Issue #2455. Item 372 makes both states non-raising, so item 334's health gate
is what refuses them, and it refused them with ONE fixed sentence:
`the successor fiber is {state} after the swap load`.

The PENDING half was already carrying something better by the time this shipped
(PR #2434 named the requirement that kept the fiber there, through
`pending_reason`). The FAILED half dropped the exception the activation had
raised, the only thing that says WHY, so a caller had to read the runtime log
to find out. It now carries that error the way `state()` already does, through
the same `_fiber_activation_error` reading rather than a second spelling.

Assertions below are made on `ActivationError.reason`, the per-state part. The
`SessionError` the MCP surface finally raises appends one fixed sentence
naming both states ("failed to activate (FAILED) or has an unmet requirement
(PENDING)"), so a substring test on `str(err)` cannot tell the two apart.
"""

import pytest

from revl.mcp.session import Session
from revl.run import ActivationError

_COMPONENT = {"name": "Toolkit", "requires": {},
              "provides": {"gadget": "Gadget"}}
_IR_FAILED = {"components": [dict(_COMPONENT)]}
# one requirement that no component of the composition provides: the #2386 case
_IR_PENDING = {"components": [
    {"name": "Toolkit", "requires": {"Audit": "Audit"},
     "provides": {"gadget": "Gadget"}}]}


class _Fiber:
    """A cordis fiber reduced to what the health gate reads: its state, and the
    error its activation left recorded on it (`fiber._error`, issue #1895)."""

    def __init__(self, state: int, error: BaseException | None = None):
        self.state = state
        self._error = error


class _FakeDriver:
    """The sliver of `Session.swap`'s driver that `_assert_successor_activated`
    touches, so these tests need no cordis runtime: the fibers, their state enum
    and an empty resolved-key set (nothing came up)."""

    class FiberState:
        _NAMES = {0: "PENDING", 1: "FAILED"}

        def __init__(self, value: int):
            self.value = value

        @property
        def name(self) -> str:
            return self._NAMES.get(self.value, "ACTIVE")

    def __init__(self, fibers: dict):
        self.fibers = fibers
        self.ir = None

    def resolved_keys(self) -> set:
        return set()


def _refusal(fibers: dict, ir: dict = _IR_FAILED):
    """The `ActivationError` the health gate raises for a successor that did not
    truly come up, caught out of `_assert_successor_activated` directly.

    The Session is built with `Session.__new__` rather than constructed (the
    pattern in tests/test_1938_declared_resource_dimensions.py): the gate reads
    only the driver, and building a real one needs a cordis runtime these tests
    do not need and must not pay for."""
    session = Session.__new__(Session)  # no `__init__`: no runtime to build
    session._driver = _FakeDriver(fibers)
    with pytest.raises(ActivationError) as excinfo:
        session._assert_successor_activated(ir)
    return excinfo.value


def _why(fibers: dict, ir: dict = _IR_FAILED) -> str:
    """The per-state part alone, without the sentence the gate appends.

    The swap that catches this adds one fixed policy sentence naming BOTH
    states: "...component failed to activate (FAILED) or has an unmet
    requirement (PENDING)...". So the full string always contains both words
    whatever the state actually is, and a negative assertion made against it
    cannot tell the two apart. Those read this trimmed prefix instead."""
    reason = _refusal(fibers, ir).reason
    return reason.split(". A swap must not install")[0]


def test_a_failed_successor_carries_the_error_its_activation_raised():
    """The fix. On base `reason` stopped at "the successor fiber is FAILED
    after the swap load" and named neither the exception nor its text, so an
    agent had no way to see why without reading the runtime log."""
    raised = ModuleNotFoundError("No module named 'no_such_module_xyz'")
    reason = _refusal({"Toolkit": _Fiber(1, raised)}).reason
    assert reason.startswith("the successor fiber is FAILED after the swap "
                             "load"), reason
    assert ("its activation raised ModuleNotFoundError: "
           "No module named 'no_such_module_xyz'") in reason, reason
    # the detail rides in the message a caller actually receives, too
    assert "ModuleNotFoundError" in str(_refusal({"Toolkit": _Fiber(1, raised)}))


def test_a_failed_successor_with_no_recorded_error_says_so():
    """A cancelled `_LazyTask` lands a fiber FAILED with no `_error` (item 372).
    The refusal must say that rather than imply an error existed and was read."""
    reason = _refusal({"Toolkit": _Fiber(1)}).reason
    assert reason.startswith("the successor fiber is FAILED after the swap "
                             "load"), reason
    assert ("its activation did not complete and recorded no error"
            in reason), reason
    # "raised" belongs only to the other arm, and the appended policy sentence
    # does not contain it, so the whole reason can be read here
    assert "raised" not in reason, reason


def test_a_pending_successor_still_names_the_unmet_requirement():
    """The control: PR #2434's half still reads exactly as it did. This test
    passes on base too, which pins that this change did not disturb it."""
    reason = _refusal({"Toolkit": _Fiber(0)}, ir=_IR_PENDING).reason
    assert reason.startswith("the successor fiber is PENDING after the swap "
                             "load"), reason
    assert "`Toolkit` requires `Audit: Audit`, which nothing in the " \
           "composition provides" in reason, reason
    assert "unmet requirement" in reason, reason
    # and it does not borrow the FAILED wording. Both negatives read the
    # per-state part: the appended policy sentence names FAILED either way.
    why = _why({"Toolkit": _Fiber(0)}, ir=_IR_PENDING)
    assert "FAILED" not in why, why
    assert "its activation raised" not in why, why


def test_the_two_states_are_told_apart_by_their_detail():
    """A caller that must branch on the answer can: the refusal differs in its
    why, not only in the state word both halves already carried."""
    failed = _why({"Toolkit": _Fiber(1, RuntimeError("boom"))})
    pending = _why({"Toolkit": _Fiber(0)}, ir=_IR_PENDING)
    assert "RuntimeError: boom" in failed
    assert "unmet requirement" not in failed
    assert "unmet requirement" in pending
    assert "RuntimeError" not in pending
