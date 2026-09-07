"""The remote deploy-admit runner protocol (roadmap item 118, S2 slice 1).

Slice 1 put :func:`deploy.admit` on the PREPARE path, but the conductor ran it
in its OWN process over its OWN disk (`via = local`). The design's cross-machine
PREPARE (§1.3 steps 3–5) has the HOST run the check, against the HOST's own
local trust store (S2.4), and return a SIGNED admission verdict over a transport
the conductor never verifies the artifact through.

These tests pin that request/response protocol and its message shapes, driven
over the :class:`deploy.InProcessTransport` stub that stands in for the
SSH/subprocess orchestration channel — so the whole handshake is exercised on
one host, with no live second machine. What is pinned:

  * the ACCEPT / REFUSE verdict round-trips through the wire form and stays
    signed by the host, verifiable with the host's own key;
  * the headline binding rule survives the seam: tampered staged bytes REFUSE
    on the runner, not on the conductor;
  * the request carries no key — the host verifies with its OWN trust, and the
    host's evidence floor cannot be loosened by the request (S2.4);
  * a reply that does not answer THIS request (challenge mismatch) or does not
    parse fails CLOSED to a transport refusal;
  * the message shapes serialize and validate fail-closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import deploy  # noqa: E402


SOURCE = """\
service Mail { emission[smtp] fn send(to: Str) }

extern emission fn smtp(line: Str) = @py { pass }

component Smtp provides mail: Mail {
  provide mail {
    fn send(to) = emit smtp(to)
  }
}

component Notifier requires mail: Mail {
  emit mail.send("a@b")
}
"""

# The HOST's own receipt-signing / trust key (design R5: the receipt key is the
# host's mTLS identity). The conductor never holds it; it only checks the reply.
HOST_KEY = b"item-118-s2-remote-runner-host-key"


@pytest.fixture
def bundle(tmp_path):
    """A real `revl bundle` for the python backend with its deploy attestation
    on disk, staged as if the conductor had copied it to the runner (§1.3 step
    2)."""
    from revl.bundle import build_bundle

    src = tmp_path / "mailer.rvl"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    att = deploy.make_deploy_attestation(out, HOST_KEY, signer="ci")
    (out / deploy.ATTESTATION_NAME).write_text(
        json.dumps(att, sort_keys=True), encoding="utf-8")
    return out


@pytest.fixture
def keyfile(tmp_path):
    """The signer key as the runner holds it on disk, in its local trust
    store."""
    path = tmp_path / "host.key"
    path.write_bytes(HOST_KEY)
    return path


def _runner(keyfile=None, **kw):
    """An in-process transport whose runner holds `keyfile` in its trust store
    and signs receipts with HOST_KEY."""
    return deploy.InProcessTransport(
        key_paths=[keyfile] if keyfile is not None else [],
        host_key=HOST_KEY, runtime_versions={"python": "3.14"}, **kw)


def _request(bundle):
    return deploy.AdmitRequest(bundle=str(bundle), backend="python",
                               challenge="conductor-challenge-0")


# ---------------------------------------------------------------------------
# the verdict round-trips over the transport, still signed by the host
# ---------------------------------------------------------------------------


def test_accept_verdict_round_trips_and_stays_signed(bundle, keyfile):
    receipt = deploy.request_admission(_runner(keyfile), _request(bundle))
    assert receipt["verdict"] == deploy.ACCEPT, receipt
    # the host measured and signed it: the conductor verifies with the host key
    ok, why = deploy.verify_receipt(receipt, HOST_KEY)
    assert ok, why
    # and the receipt binds the RE-HASHED artifact bytes, not anything the
    # request or the attestation self-declared
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")
    # the host's OWN runtime versions came back on the verdict (§1.3 step 5)
    assert receipt["runtime_versions"] == {"python": "3.14"}


def test_refuses_when_the_runner_has_no_key(bundle):
    """No key in the host's trust store -> the chain cannot be verified, so the
    runner REFUSES at the signer link rather than admitting unverified — the
    same fail-closed shape a local admit uses, now over the seam."""
    receipt = deploy.request_admission(_runner(keyfile=None), _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNER


def test_tampered_staged_bytes_refuse_on_the_runner(bundle, keyfile):
    """The headline binding rule survives the seam: a byte the runner will
    execute is mutated on the STAGED tree and the runner refuses on receive."""
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# tampered\n",
                        encoding="utf-8")
    receipt = deploy.request_admission(_runner(keyfile), _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT


# ---------------------------------------------------------------------------
# S2.4: the host verifies with its OWN trust, and the request cannot loosen it
# ---------------------------------------------------------------------------


def test_request_carries_no_key(bundle):
    """The trust inversion: the request has no place to put a key, so a request
    can ASK for admission but never supply the trust that grants it."""
    wire = _request(bundle).to_wire()
    assert "key" not in wire and "keys" not in wire and "trust" not in wire


def test_host_evidence_floor_cannot_be_loosened_by_the_request(bundle, keyfile):
    """The conductor asks for no conformance evidence, but the HOST requires it.
    The effective requirement is the host's floor OR the ask, so the host stays
    stricter and REFUSES — a conductor cannot turn a host's evidence
    requirement off by omitting it (S2.4)."""
    request = _request(bundle)
    assert request.require_conformance is False
    runner = _runner(keyfile, require_conformance=True)
    receipt = deploy.request_admission(runner, request)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    # and with the floor down, the very same request/bundle is admitted, so the
    # refusal above is the floor biting and nothing else
    assert deploy.request_admission(_runner(keyfile), request)[
        "verdict"] == deploy.ACCEPT


def test_request_can_ask_for_stricter_checking(bundle, keyfile):
    """The other half of the OR: even with the host floor down, a request that
    ASKS for conformance evidence is honoured — the ask flows through to the
    trust store the runner builds."""
    request = deploy.AdmitRequest(bundle=str(bundle), backend="python",
                                  challenge="c1", require_conformance=True)
    receipt = deploy.request_admission(_runner(keyfile), request)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE


# ---------------------------------------------------------------------------
# the challenge binds the reply to the request; malformed exchanges fail closed
# ---------------------------------------------------------------------------


class _ReplaysAWrongChallenge:
    """A transport whose runner answers with a verdict that does not echo the
    request's challenge — a stale/replayed reply from an earlier exchange."""

    def __init__(self, inner):
        self._inner = inner

    def send(self, request_wire, *, now=None):
        response = self._inner.send(request_wire, now=now)
        response["challenge"] = "some-other-exchange"
        return response


def test_reply_that_does_not_echo_the_challenge_is_refused(bundle, keyfile):
    receipt = deploy.request_admission(
        _ReplaysAWrongChallenge(_runner(keyfile)), _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT
    assert "challenge" in receipt["reason"]


class _RepliesGarbage:
    def send(self, request_wire, *, now=None):
        return {"kind": "not-a-response", "version": "1.0"}


def test_unparseable_reply_fails_closed(bundle):
    receipt = deploy.request_admission(_RepliesGarbage(), _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT


def test_malformed_request_refuses_on_the_runner(keyfile):
    """A request the runner cannot parse must refuse, never admit on a guess."""
    response = deploy.serve_admit_request(
        {"kind": "revl.deploy.admit-request", "version": "1.0",
         "backend": "python", "challenge": "c"},  # no `bundle`
        key_paths=[keyfile], host_key=HOST_KEY)
    parsed = deploy.AdmitResponse.from_wire(response)
    assert parsed.receipt["verdict"] == deploy.REFUSE
    assert parsed.receipt["link"] == deploy.LINK_TRANSPORT


# ---------------------------------------------------------------------------
# the message shapes: serialize, and validate fail-closed
# ---------------------------------------------------------------------------


def test_request_wire_round_trip_preserves_every_field():
    request = deploy.AdmitRequest(bundle="/staged/app", backend="rust",
                                  challenge="nonce-9", require_gauntlet=True,
                                  require_conformance=True)
    back = deploy.AdmitRequest.from_wire(json.loads(json.dumps(request.to_wire())))
    assert back == request


@pytest.mark.parametrize("mutate", [
    {"kind": "wrong"},
    {"version": "9.9"},
    {"bundle": ""},
    {"backend": 3},
])
def test_request_from_wire_is_fail_closed(mutate):
    wire = deploy.AdmitRequest(bundle="/b", backend="python",
                               challenge="c").to_wire()
    wire.update(mutate)
    with pytest.raises(ValueError):
        deploy.AdmitRequest.from_wire(wire)


@pytest.mark.parametrize("mutate", [
    {"kind": "wrong"},
    {"version": "9.9"},
    {"receipt": "not-a-mapping"},
    {"challenge": ""},
])
def test_response_from_wire_is_fail_closed(mutate):
    wire = deploy.AdmitResponse(receipt={"verdict": deploy.ACCEPT},
                                challenge="c").to_wire()
    wire.update(mutate)
    with pytest.raises(ValueError):
        deploy.AdmitResponse.from_wire(wire)
