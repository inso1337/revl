"""Typed delegation, the in-process runtime slice: mint from a lease over a
wiring key, use through the delegatee, attribution and revocation
(roadmap item 442, issue #2693; docs/design/442-typed-delegation.md §5, §6).

The composition is the design's flagship shape: `A` requires `fs` and `worker`,
leases `fs.write(path="/tmp")` for three uses with a TTL, and hands the typed
handle to `W` inside a call it makes. `W` holds no `requires` key for `fs`; its
only way to the provider is the delegated reference.

What this file pins, one acceptance bullet at a time:

  * three delegated uses succeed and the fourth is refused, and the ledger row
    reads `subject W`, `lender A`, `consumed` with zero uses left;
  * a lapsed lender's lease revokes the delegated handle: the next use is
    refused and the row reads `revoked`;
  * the delegation record carries the one chain link this depth admits, with the
    parameter the lease narrowed;
  * a method the delegated surface does not declare is refused at compile time
    (the checker tests live in tests/test_delegate_442_mint.py).

A swap of the lender is covered at the ledger level
(tests/test_delegate_442_ledger.py) because the runtime path for it is the same
one the expiry test drives: `_lender_generation_live` reads the class map.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "backends" / "python") not in sys.path:
    sys.path.insert(0, str(_ROOT / "backends" / "python"))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the delegated crossing runs through the live cordis-py composition",
)

# `A` leases a key it REQUIRES (a wiring key, not a bare capability), so the
# lease mints a delegatable handle; `W` receives it as a `Delegate[Fs]` and uses
# it without holding `fs` itself.
_SOURCE = (
    'extern emission[fs.write(path="/tmp")] fn wr(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('w:' + msg + '\\n')\n"
    "    return\n}\n"
    "service Fs { emission fn write(sink: Str, msg: Str) }\n"
    "service Worker { emission fn run(job: Str, d: Delegate[Fs]) }\n"
    "service Job { emission fn go(sink: Str) }\n"
    "component FsProvider provides fs: Fs {\n"
    "  provide fs { fn write(sink, msg) { emit wr(sink, msg) } }\n"
    "}\n"
    "component W provides worker: Worker {\n"
    '  provide worker { fn run(job, d) { emit d.write(job, "delegated") } }\n'
    "}\n"
    "component A requires fs: Fs, worker: Worker provides job: Job {\n"
    '  let l = effect lease fs.write(path="/tmp") ttl 10m uses 3 '
    "undo l.revoke()\n"
    "  provide job { fn go(sink) { emit worker.run(sink, l) } }\n"
    "}\n"
)


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


def _lines(sink):
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


def _boot(session: Session) -> None:
    """Load the composition, minting the ONE lease ticket it raises, then mint a
    bounded standing grant for the `job.go` crossing the test drives. The
    delegated crossing itself is authorized by the lease's grant, never by a
    per-call prompt."""
    from revl.mcp.approval import ApprovalRequired  # noqa: PLC0415

    ir = compile_source(_SOURCE, "delegation.rvl")
    try:
        session.load(copy.deepcopy(ir), record=True)
    except ApprovalRequired as exc:
        assert exc.ticket["kind"] == "lease"
        session.mint_standing_grant(ticket_hash=exc.ticket["hash"])
        session.load(copy.deepcopy(ir), record=True)
    session.mint_standing_grant(capability="worker", uses=10)


def _session() -> Session:
    session = Session()
    session.approval_policy = "auto"
    return session


def _lease_row(session: Session) -> dict:
    rows = [row for row in session.approval_metrics()["standingGrants"]
            if row["capability"].startswith("fs.write")]
    assert len(rows) == 1, f"expected one lease row, got {rows}"
    return rows[0]


@needs_cordis
def test_three_delegated_uses_succeed_and_the_fourth_is_refused(sink):
    session = _session()
    _boot(session)

    for _ in range(3):
        session.call("job", "go", [sink])
    assert _lines(sink) == ["w:delegated"] * 3

    with pytest.raises(Exception, match="no uses left"):
        session.call("job", "go", [sink])
    assert _lines(sink) == ["w:delegated"] * 3, "the fourth use fired nothing"

    row = _lease_row(session)
    assert row["component"] == "FsProvider", "the grant stays bound to its crossing"
    assert row["lender"] == "A", "the delegator is the party that lent it"
    assert row["subject"] == "W", "the delegatee is the subject"
    assert row["remainingUses"] == 0 and row["consumed"] is True
    assert row["chain"] == [{"from": "A", "to": "W", "narrowed": ["path"]}]
    assert row["revoked"] is False, "exhaustion is not revocation"


@needs_cordis
def test_an_expired_lease_revokes_the_delegated_handle(sink):
    box = {"now": 1_000}
    session = _session()
    session._clock_ms = lambda: box["now"]
    _boot(session)

    session.call("job", "go", [sink])
    assert _lines(sink) == ["w:delegated"]

    box["now"] = 1_000 + 600_000 + 1        # one ms past the lease's 10m window
    with pytest.raises(Exception, match="lapsed"):
        session.call("job", "go", [sink])
    assert _lines(sink) == ["w:delegated"], "a lapsed delegation fires nothing"

    row = _lease_row(session)
    assert row["revoked"] is True, "the delegated handle reads revoked"
    assert row["remainingUses"] == 2, "a refused use is not spent"
