"""The load-measured COMMIT receipt, carried over the runner protocol
(roadmap item 118, S2 slice 2).

S2 slice 1 put :func:`deploy.admit` on the PREPARE path and carried it over the
orchestration channel (`test_deploy_admit_runner_118.py`). The COMMIT twin was
landed only as LOCAL primitives — :func:`deploy.commit_receipt` (the host's
fresh load-time measurement) and :func:`deploy.compare_commit_receipt` (the
conductor's hard gate over it) — usable only when conductor and host shared a
process. The design's Slice 2 names the remaining wiring exactly: *carrying it
over the runner protocol*.

These tests pin that COMMIT-phase handshake, driven over the same
:class:`deploy.InProcessTransport` stub the admit protocol uses (one channel,
dispatching on the request kind). What is pinned:

  * a COMMITTED load-time measurement round-trips through the wire form, stays
    signed by the host, and passes the conductor's gate against the admission
    receipt the host signed in PREPARE;
  * the headline R2 property survives the seam: bytes mutated BETWEEN admit and
    commit are DETECTED at the conductor's COMMIT gate, not merely at PREPARE;
  * a host that cannot measure what it loads reports a local COMMIT failure, not
    a COMMITTED receipt;
  * the request carries no admitted hash — it asks the host to MEASURE, never
    tells it what to measure;
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

# The HOST's own receipt-signing key (design R5: the receipt key is the host's
# mTLS identity). The conductor never holds it to sign; it only checks the reply.
HOST_KEY = b"item-118-s2-commit-runner-host-key"


@pytest.fixture
def bundle(tmp_path):
    """A real `revl bundle` for the python backend with its deploy attestation
    on disk, staged as if the conductor had copied it to the runner."""
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
    store (needed to obtain an ACCEPT admission receipt to commit against)."""
    path = tmp_path / "host.key"
    path.write_bytes(HOST_KEY)
    return path


def _runner(keyfile=None, **kw):
    """An in-process transport whose runner holds `keyfile` in its trust store
    and signs both admission and commit receipts with HOST_KEY."""
    return deploy.InProcessTransport(
        key_paths=[keyfile] if keyfile is not None else [],
        host_key=HOST_KEY, runtime_versions={"python": "3.14"}, **kw)


def _admission(runner, bundle):
    """Run PREPARE over the runner and return the ACCEPT admission receipt the
    COMMIT phase is gated against."""
    receipt = deploy.request_admission(
        runner, deploy.AdmitRequest(bundle=str(bundle), backend="python",
                                    challenge="prepare-challenge-0"))
    assert receipt["verdict"] == deploy.ACCEPT, receipt
    return receipt


def _commit_request(bundle, challenge="commit-challenge-0"):
    return deploy.CommitRequest(bundle=str(bundle), backend="python",
                                challenge=challenge)


# ---------------------------------------------------------------------------
# happy path: a measurement round-trips, stays signed, and passes the gate
# ---------------------------------------------------------------------------


def test_commit_measurement_round_trips_and_passes_the_gate(bundle, keyfile):
    runner = _runner(keyfile)
    admission = _admission(runner, bundle)
    ok, reason, receipt = deploy.request_commit(
        runner, _commit_request(bundle), admission_receipt=admission,
        host_key=HOST_KEY)
    assert ok, reason
    # it is a COMMIT-phase load-time measurement, not an admission
    assert receipt["verdict"] == deploy.COMMITTED
    assert receipt["phase"] == deploy.COMMIT_PHASE
    # the host signed it: an audit can attribute the claim to the host's key
    signed_ok, why = deploy.verify_receipt(receipt, HOST_KEY)
    assert signed_ok, why
    # and it measured the same bytes the admission bound (a fresh measurement)
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")
    assert receipt["artifact_hash"] == admission["artifact_hash"]
    assert receipt["composition_hash"] == admission["composition_hash"]
    assert receipt["runtime_versions"] == {"python": "3.14"}


# ---------------------------------------------------------------------------
# R2: bytes changed BETWEEN admit and commit are detected at the COMMIT gate
# ---------------------------------------------------------------------------


def test_bytes_mutated_between_admit_and_commit_are_detected(bundle, keyfile):
    """The headline R2 property over the seam: PREPARE proved bytes, then the
    staged tree changed, and the conductor's COMMIT gate catches it — the load-
    time measurement is the authority, not PREPARE's earlier verify."""
    runner = _runner(keyfile)
    admission = _admission(runner, bundle)
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# swapped\n",
                        encoding="utf-8")
    ok, reason, receipt = deploy.request_commit(
        runner, _commit_request(bundle), admission_receipt=admission,
        host_key=HOST_KEY)
    assert not ok
    assert "COMMIT-load time" in reason
    # the host honestly measured and signed the DIFFERENT bytes — the lie (if it
    # were one) is attributable; here it is an honest-but-changed detection
    assert receipt["verdict"] == deploy.COMMITTED
    assert receipt["artifact_hash"] != admission["artifact_hash"]


# ---------------------------------------------------------------------------
# a host that cannot measure has NOT committed
# ---------------------------------------------------------------------------


def test_host_that_cannot_measure_reports_a_local_commit_failure(bundle, keyfile):
    """The emitted artifact is gone at COMMIT time, so the host cannot measure
    the bytes it was to load: it reports a local COMMIT failure (a REFUSE), never
    a COMMITTED receipt, and the gate refuses."""
    runner = _runner(keyfile)
    admission = _admission(runner, bundle)
    import shutil
    shutil.rmtree(bundle / "emitted" / "python")
    ok, reason, receipt = deploy.request_commit(
        runner, _commit_request(bundle), admission_receipt=admission,
        host_key=HOST_KEY)
    assert not ok
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT


def test_runner_with_no_signing_key_refuses_to_commit(bundle):
    """With no host key the runner cannot mint an attributable measurement, so it
    REFUSES rather than returning an unsigned COMMIT."""
    runner = deploy.InProcessTransport(host_key=None,
                                       runtime_versions={"python": "3.14"})
    # a synthetic ACCEPT admission is enough to reach the commit exchange
    admission = {"verdict": deploy.ACCEPT, "backend": "python",
                 "composition_hash": "x", "artifact_hash": "y"}
    ok, reason, receipt = deploy.request_commit(
        runner, _commit_request(bundle), admission_receipt=admission,
        host_key=HOST_KEY)
    assert not ok
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT


# ---------------------------------------------------------------------------
# S2.4 shape: the request tells the host to measure, never what to measure
# ---------------------------------------------------------------------------


def test_commit_request_carries_no_admitted_hash(bundle):
    """An echo of the admitted hash would defeat the load-time measurement, so
    the request has no place to put one."""
    wire = _commit_request(bundle).to_wire()
    assert "artifact_hash" not in wire and "composition_hash" not in wire


# ---------------------------------------------------------------------------
# the challenge binds the reply; malformed exchanges fail closed
# ---------------------------------------------------------------------------


class _ReplaysAWrongChallenge:
    def __init__(self, inner):
        self._inner = inner

    def send(self, request_wire, *, now=None):
        response = self._inner.send(request_wire, now=now)
        response["challenge"] = "some-other-exchange"
        return response


def test_commit_reply_that_does_not_echo_the_challenge_is_refused(bundle, keyfile):
    runner = _runner(keyfile)
    admission = _admission(runner, bundle)
    ok, reason, receipt = deploy.request_commit(
        _ReplaysAWrongChallenge(runner), _commit_request(bundle),
        admission_receipt=admission, host_key=HOST_KEY)
    assert not ok
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT
    assert "challenge" in receipt["reason"]


class _RepliesGarbage:
    def send(self, request_wire, *, now=None):
        return {"kind": "not-a-response", "version": "1.0"}


def test_unparseable_commit_reply_fails_closed(bundle):
    admission = {"verdict": deploy.ACCEPT, "backend": "python",
                 "composition_hash": "x", "artifact_hash": "y"}
    ok, reason, receipt = deploy.request_commit(
        _RepliesGarbage(), _commit_request(bundle),
        admission_receipt=admission, host_key=HOST_KEY)
    assert not ok
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT


def test_malformed_commit_request_refuses_on_the_runner():
    """A request the runner cannot parse must refuse, never commit on a guess."""
    response = deploy.serve_commit_request(
        {"kind": deploy.COMMIT_REQUEST_KIND, "version": "1.0",
         "backend": "python", "challenge": "c"},  # no `bundle`
        host_key=HOST_KEY)
    parsed = deploy.CommitResponse.from_wire(response)
    assert parsed.receipt["verdict"] == deploy.REFUSE
    assert parsed.receipt["link"] == deploy.LINK_TRANSPORT


# ---------------------------------------------------------------------------
# the message shapes: serialize, and validate fail-closed
# ---------------------------------------------------------------------------


def test_commit_request_wire_round_trip_preserves_every_field():
    request = deploy.CommitRequest(bundle="/staged/app", backend="rust",
                                   challenge="nonce-9")
    back = deploy.CommitRequest.from_wire(
        json.loads(json.dumps(request.to_wire())))
    assert back == request


@pytest.mark.parametrize("mutate", [
    {"kind": "wrong"},
    {"version": "9.9"},
    {"bundle": ""},
    {"backend": 3},
])
def test_commit_request_from_wire_is_fail_closed(mutate):
    wire = deploy.CommitRequest(bundle="/b", backend="python",
                               challenge="c").to_wire()
    wire.update(mutate)
    with pytest.raises(ValueError):
        deploy.CommitRequest.from_wire(wire)


@pytest.mark.parametrize("mutate", [
    {"kind": "wrong"},
    {"version": "9.9"},
    {"receipt": "not-a-mapping"},
    {"challenge": ""},
])
def test_commit_response_from_wire_is_fail_closed(mutate):
    wire = deploy.CommitResponse(receipt={"verdict": deploy.COMMITTED},
                                 challenge="c").to_wire()
    wire.update(mutate)
    with pytest.raises(ValueError):
        deploy.CommitResponse.from_wire(wire)


def test_commit_and_admit_kinds_are_distinct():
    """A commit request can never be read as an admit request (which would run a
    chain verify instead of a measurement), or vice versa."""
    assert deploy.COMMIT_REQUEST_KIND != deploy.ADMIT_REQUEST_KIND
    assert deploy.COMMIT_RESPONSE_KIND != deploy.ADMIT_RESPONSE_KIND
    with pytest.raises(ValueError):
        deploy.AdmitRequest.from_wire(
            deploy.CommitRequest(bundle="/b", backend="python",
                                 challenge="c").to_wire())
    with pytest.raises(ValueError):
        deploy.CommitRequest.from_wire(
            deploy.AdmitRequest(bundle="/b", backend="python",
                                challenge="c").to_wire())
