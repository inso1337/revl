"""A provider's record of a served cross-process call is counted with the
caller's crossing (issue #1889).

In a placement run, `agent` emits `tickets.file("T1") compensate
tickets.withdraw("T1")` and `desk`, in the other process, answers it with
`return emit file_host(t)`. One physical crossing, recorded twice: agent's
WAL holds `tickets.file`, desk's holds `file_host`. Inside one process
`_split_nested` folds the provider's record into the crossing it was made
inside (issue #1609); across the seam nothing linked the two, so recover
reported desk's `file_host` as residue.

Now the bridge carries the caller's crossing, named by its process, with the
call (`bridge._enclosing_crossing`); the provider records every step of its
answer with it (`replay.Step.within`); and placement recover counts such a
record with the caller's crossing when the caller's WAL holds it. The
end-to-end runs are `tests/test_recover_placement_1477.py` and
`tests/test_recover_no_reactivate_1477.py`; these are the rules, unit by unit.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import bridge  # noqa: E402
from revl.recovery import _split_nested  # noqa: E402


def _emission(seq: int, within: dict | None = None) -> dict:
    record = {"record": "effect", "seq": seq, "label": "file_host",
              "boundary": {"class": "emission"}}
    if within is not None:
        record["within"] = within
    return record


SERVED = {"seq": 4, "component": "Agent", "label": "tickets.file",
          "process": "agent"}


def test_a_served_record_folds_when_the_caller_wal_holds_the_crossing():
    record = _emission(6, SERVED)
    own, nested = _split_nested([record], [record], {"agent": {3, 4, 5}})
    assert (own, nested) == ([], [record])


def test_it_stays_residue_when_the_caller_wal_lacks_the_crossing():
    record = _emission(6, SERVED)
    assert _split_nested([record], [record], {"agent": {3, 5}}) == ([record], [])
    # and when there is no caller WAL at all: a single-process recover
    assert _split_nested([record], [record]) == ([record], [])


def test_a_served_record_is_never_matched_against_this_wal_s_own_seqs():
    # desk's own WAL happens to hold a seq 4 too: the two seq spaces are
    # unrelated, so it must not fold against it
    local = {"record": "effect", "seq": 4, "label": "note",
             "boundary": {"class": "emission"}}
    record = _emission(6, SERVED)
    own, nested = _split_nested([local, record], [local, record], {})
    assert nested == [] and own == [local, record]


def test_an_in_process_record_folds_as_before():
    caller = {"record": "effect", "seq": 2, "label": "tickets.file",
              "boundary": {"class": "emission"}}
    inner = _emission(3, {"seq": 2, "component": "Agent", "label": "tickets.file"})
    assert _split_nested([caller, inner], [caller, inner]) == ([caller], [inner])


def test_the_bridge_sends_the_caller_crossing_only_from_a_placement_process(
        monkeypatch):
    import replay

    token = replay._ENCLOSING.set({"seq": 4, "component": "Agent",
                                   "label": "tickets.file"})
    try:
        monkeypatch.setattr(bridge, "CALLER_PROCESS", None)
        assert bridge._enclosing_crossing() is None
        monkeypatch.setattr(bridge, "CALLER_PROCESS", "agent")
        assert bridge._enclosing_crossing() == SERVED
    finally:
        replay._ENCLOSING.reset(token)
    assert bridge._enclosing_crossing() is None   # no enclosing crossing


def test_a_served_crossing_is_taken_only_in_its_exact_shape():
    assert bridge._served_within({"within": SERVED}) == SERVED
    for bad in (None, "x", {"seq": "4", "process": "agent"},
                {"seq": True, "process": "agent"}, {"seq": 4},
                {"seq": 4, "process": 7}):
        assert bridge._served_within({"within": bad}) is None, bad
    # and only the four named fields are kept
    extra = {**SERVED, "secret": "s"}
    assert bridge._served_within({"within": extra}) == SERVED


def test_a_served_method_runs_with_the_caller_crossing_as_its_enclosing_one():
    import replay

    seen = []

    class Service:
        def file(self, t):
            seen.append(replay._ENCLOSING.get())
            return t

    assert bridge._call_served(Service(), "file", ["T1"], SERVED) == "T1"
    assert seen == [SERVED]
    assert replay._ENCLOSING.get() is None
