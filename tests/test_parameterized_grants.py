"""Cone-aware standing-grant lookups and ceiling erasure — item 294 Slice 2.

Slice 1 shipped the parameterized-capability grammar and the `cap_order.covers`
partial order (static admission only). Slice 2 makes the session GRANT ledger
cone-aware: a grant/lookup resolves a capability that `covers` a declared
crossing instead of matching it by string, so

  * a narrow mint spelling (`fs.write(path="/tmp")`) resolves against a wider
    declared crossing (bare `fs.write`, or `fs.write(path="/tmp")`) that covers
    it — mint-narrow works instead of being refused dead on arrival;
  * a grant auto-approves every class-(c) crossing WITHIN its cone
    (`fs.write(path="/tmp/job-42")`) and no crossing outside it (a sibling path,
    or a different token — the F1 no-over-coverage-across-tokens fix preserved);
  * a `calls=N` ceiling parameter is erased from the grant's valuation at mint
    and translated into the shipped `remainingUses` counter.

Bare-token grants (every existing standing-grant test) behave bit-for-bit as
before: `covers` on empty valuations is string identity. The end-to-end flow
runs through the live cordis-py composition, exactly as test_standing_approval.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cone-aware grants are proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh`",
)

# A composition whose crossings carry parameterized capabilities: three `fs.write`
# cones (a wide `/tmp`, a narrower `/tmp/job-42` inside it, a sibling `/etc`), a
# distinct `db.read` token (the F1 witness), and a bare-ceiling `model.complete`.
_SOURCE = (
    'extern emission[fs.write(path="/tmp")] fn wr_tmp(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('tmp:' + msg + '\\n')\n"
    "    return\n}\n"
    'extern emission[fs.write(path="/tmp/job-42")] fn wr_job(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('job:' + msg + '\\n')\n"
    "    return\n}\n"
    'extern emission[fs.write(path="/etc")] fn wr_etc(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('etc:' + msg + '\\n')\n"
    "    return\n}\n"
    "extern emission[db.read] fn rd_db(sink: Str, msg: Str)"
    " = @py {\n    with open(sink, 'a') as f: f.write('db:' + msg + '\\n')\n"
    "    return\n}\n"
    "extern emission[model.complete] fn complete(sink: Str, msg: Str)"
    " = @py {\n    with open(sink, 'a') as f: f.write('ml:' + msg + '\\n')\n"
    "    return\n}\n"
    "service Ops {\n"
    "  emission fn a_tmp(sink: Str, msg: Str)\n"
    "  emission fn a_job(sink: Str, msg: Str)\n"
    "  emission fn a_etc(sink: Str, msg: Str)\n"
    "  emission fn a_db(sink: Str, msg: Str)\n"
    "  emission fn a_model(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn a_tmp(sink, msg) { emit wr_tmp(sink, msg) }\n"
    "    fn a_job(sink, msg) { emit wr_job(sink, msg) }\n"
    "    fn a_etc(sink, msg) { emit wr_etc(sink, msg) }\n"
    "    fn a_db(sink, msg) { emit rd_db(sink, msg) }\n"
    "    fn a_model(sink, msg) { emit complete(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "parameterized_grants.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _session():
    s = Session()
    s.approval_policy = "auto"
    return s


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# Cone-aware mint: a narrow spelling resolves against a covering crossing
# ---------------------------------------------------------------------------

@needs_cordis
def test_narrow_mint_resolves_against_covering_crossing():
    """`fs.write(path="/tmp/job-42")` is not the STRING any crossing declares
    (`/tmp`, `/tmp/job-42`, `/etc`), yet it mints: it is at or below the declared
    `fs.write(path="/tmp")` cone (and equal to `/tmp/job-42`). A string-keyed
    lookup refused this dead on arrival; the cone-aware lookup resolves it."""
    session = _session()
    session.load(_ir(), record=True)
    grant = session.mint_standing_grant(
        capability='fs.write(path="/tmp/job-42")', uses=2)
    assert grant["granted"]
    assert grant["capability"] == 'fs.write(path="/tmp/job-42")'


@needs_cordis
def test_mint_covering_no_declared_crossing_is_refused():
    """A sibling path outside every declared cone (`/other`) and the bare token
    (wider than every declared crossing) both resolve to nothing — a grant may
    only NARROW a declaration, never widen past it."""
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(SessionError, match="not a live class-\\(c\\) crossing"):
        session.mint_standing_grant(capability='fs.write(path="/other")', uses=1)
    with pytest.raises(SessionError, match="not a live class-\\(c\\) crossing"):
        session.mint_standing_grant(capability="fs.write", uses=1)


# ---------------------------------------------------------------------------
# Cone-aware consume: a grant auto-approves its whole sub-cone, nothing outside
# ---------------------------------------------------------------------------

@needs_cordis
def test_grant_consumes_covered_crossing_not_siblings(sink):
    """A `fs.write(path="/tmp")` grant auto-approves the `/tmp/job-42` crossing
    (inside the cone), and prompts for the `/etc` sibling and the `db.read`
    token (F1: a grant for one token never covers another)."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability='fs.write(path="/tmp")', uses=5)

    # inside the cone: /tmp/job-42 <= /tmp -> auto-approved, no prompt
    out = session.call("ops", "a_job", [sink, "x"])
    assert out["result"] is None
    assert _lines(sink) == ["job:x"]
    assert session._owner.prompts["perCall"] == 0

    # sibling path outside the cone: /etc not<= /tmp -> prompt
    with pytest.raises(ApprovalRequired):
        session.call("ops", "a_etc", [sink, "y"])
    # different token (F1): fs.write grant never covers db.read -> prompt
    with pytest.raises(ApprovalRequired):
        session.call("ops", "a_db", [sink, "z"])
    assert _lines(sink) == ["job:x"]           # neither sibling fired


@needs_cordis
def test_grant_for_a_different_token_does_not_cover(sink):
    """F1 preserved verbatim: a grant minted for `db.read` covers only `db.read`
    crossings, never any `fs.write` cone."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="db.read", uses=2)

    out = session.call("ops", "a_db", [sink, "q"])
    assert out["result"] is None and _lines(sink) == ["db:q"]
    with pytest.raises(ApprovalRequired):      # db.read grant does not cover fs.write
        session.call("ops", "a_tmp", [sink, "w"])


# ---------------------------------------------------------------------------
# Ceiling erasure: calls=N -> remainingUses=N, erased from the grant valuation
# ---------------------------------------------------------------------------

@needs_cordis
def test_ceiling_calls_erases_to_remaining_uses(sink):
    """`model.complete(calls=3)` mints a grant whose valuation is BARE
    `model.complete` (the ceiling erased) and whose `remainingUses` is 3. A
    crossing binds no `calls`, so the erased-bare grant covers the crossing; the
    third fires, the fourth prompts."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    grant = session.mint_standing_grant(capability="model.complete(calls=3)")
    assert grant["remainingUses"] == 3
    # the ceiling is erased from the stored valuation (a crossing binds no calls)
    assert session._grants[-1]["capability"] == "model.complete"

    for msg in ("a", "b", "c"):
        session.call("ops", "a_model", [sink, msg])
    assert _lines(sink) == ["ml:a", "ml:b", "ml:c"]
    assert session._grants_consumed == 3
    with pytest.raises(ApprovalRequired):      # the fourth: exhausted -> prompt
        session.call("ops", "a_model", [sink, "d"])


# ---------------------------------------------------------------------------
# Revoke by a parameterized capability retires exactly the sub-cone
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_by_cone_retires_the_subcone_only():
    """Revoking `fs.write(path="/tmp")` retires a `/tmp/job-42` grant (below it)
    but not an `/etc` grant (a sibling) — the revoke spelling covers only its own
    sub-cone."""
    session = _session()
    session.load(_ir(), record=True)
    g_job = session.mint_standing_grant(
        capability='fs.write(path="/tmp/job-42")', uses=1)
    g_etc = session.mint_standing_grant(capability='fs.write(path="/etc")', uses=1)

    out = session.revoke_standing_grant(capability='fs.write(path="/tmp")')
    assert out["count"] == 1
    assert out["requestIds"] == [g_job["requestId"]]
    # the sibling grant is untouched and still live
    etc = next(x for x in session._grants
               if x["requestId"] == g_etc["requestId"])
    assert not etc["revoked"]
