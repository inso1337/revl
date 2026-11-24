"""The refusals on the delegated-call path (roadmap item 442, issue #2693).

`Frame.delegate_call` (backends/python/runtime.py) and
`Session._delegation_grant` (src/revl/mcp/session.py) each refuse a handoff that
is not a live, well-formed delegation. Every one of those refusals is pinned here
at the unit level, with no cordis runtime, so the suite runs everywhere the
ledger suite does. The two refusals a wrong handoff actually hits (a lease over a
bare capability handed to a `Delegate[S]` parameter, and a handle whose service
is not the parameter's S) are also driven end to end in
tests/test_delegate_442_runtime.py.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "backends" / "python") not in sys.path:
    sys.path.insert(0, str(_ROOT / "backends" / "python"))

import runtime  # noqa: E402

from revl.mcp.session import Session, SessionError  # noqa: E402

_CAP = 'fs.write(path="/tmp")'


def _frame(owner) -> SimpleNamespace:
    """The two attributes `Frame.delegate_call` reads off `self`. It is called as
    the plain function so no activation, context or runtime is needed."""
    return SimpleNamespace(name="W", _owner=owner)


def _owner(*, lookup=None, begin=None, fired=None) -> SimpleNamespace:
    return SimpleNamespace(
        lease_provider=lookup or (lambda request_id: None),
        delegate_begin=begin or (lambda *a, **k: []),
        delegate_fired=fired or (lambda spends: None))


def _handle(**fields) -> SimpleNamespace:
    base = {"request_id": "grant:1", "key": "fs", "holder": "A"}
    base.update(fields)
    return SimpleNamespace(**base)


def _call(frame, handle, service="Fs", method="write"):
    return runtime.Frame.delegate_call(frame, handle, service, method, ["x"])


def test_no_session_owner_refuses():
    with pytest.raises(runtime.LeaseRefused, match="no session grant ledger"):
        _call(_frame(None), _handle())


def test_an_owner_without_the_delegation_seam_refuses():
    owner = SimpleNamespace()       # a runtime the session never wired
    with pytest.raises(runtime.LeaseRefused, match="no session grant ledger"):
        _call(_frame(owner), _handle())


@pytest.mark.parametrize("fields", [{"request_id": None}, {"key": None}])
def test_a_value_with_no_request_id_or_key_refuses(fields):
    # what a lease over a BARE capability hands over: it names no wiring key
    with pytest.raises(runtime.LeaseRefused, match="not a delegatable lease"):
        _call(_frame(_owner()), _handle(**fields))


def test_a_lease_that_registered_no_provider_refuses():
    with pytest.raises(runtime.LeaseRefused, match="registered no provider"):
        _call(_frame(_owner(lookup=lambda request_id: None)), _handle())


def test_a_provider_service_that_disagrees_with_the_parameter_refuses():
    owner = _owner(lookup=lambda request_id: (object(), "Fs"))
    with pytest.raises(runtime.LeaseRefused, match="the handle and"):
        _call(_frame(owner), _handle(), service="Other")


def test_a_provider_with_no_such_method_refuses_before_any_spend():
    spent = []
    owner = _owner(lookup=lambda request_id: (object(), "Fs"),
                   begin=lambda *a, **k: spent.append(a) or [])
    with pytest.raises(runtime.LeaseRefused, match="has no `write`"):
        _call(_frame(owner), _handle())
    assert spent == [], "the method is resolved before anything is spent"


def test_a_well_formed_delegation_fires_the_provider_after_the_spend():
    order = []

    class Provider:
        def write(self, text):
            order.append(("fire", text))
            return "done"

    owner = _owner(
        lookup=lambda request_id: (Provider(), "Fs"),
        begin=lambda *a, **k: order.append(("spend", a[1])) or ["spend-1"],
        fired=lambda spends: order.append(("pair", spends)))

    assert _call(_frame(owner), _handle()) == "done"
    assert order == [("spend", "W"), ("fire", "x"), ("pair", ["spend-1"])]


def test_a_grant_that_is_not_a_lease_cannot_be_delegated():
    session = Session()
    session._session_id = "sess-1"
    session._grants.append({
        "requestId": "grant:1", "capability": _CAP, "component": "A",
        "session": "sess-1", "revoked": False, "consumed": False,
        "expiresAt": None, "remainingUses": 3, "lease": False,
        "candidateHash": "h"})

    with pytest.raises(SessionError, match="is not a lease"):
        session._delegation_grant("grant:1", "fs.write")
