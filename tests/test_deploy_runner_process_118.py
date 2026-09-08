"""The `revl deploy-admit` runner PROCESS and its stdio transport (roadmap
item 118, S2 — the far-side runner made real).

The admit/commit protocol was landed and tested over :class:`deploy.InProcessTransport`,
a stub that runs the runner INSIDE the conductor. The design (§1.3 steps 3–5 /
step 7) has the conductor STAGE the bundle on the far host and spawn
`revl deploy-admit` THERE — a separate process reached over a real byte channel,
holding its OWN local trust store. This slice builds that process and a
:class:`deploy.ProcessRunnerTransport` that speaks to it over stdin/stdout, so the
whole PREPARE→COMMIT handshake runs across a real process boundary on one
machine. The cross-machine leg (scp/rsync staging, a pinned SSH host key) is the
same command behind `ssh <host>` and is a following slice.

These tests re-pin the runner-protocol properties the in-process test pins, now
over a real child process driven by real newline-JSON bytes:

  * an ACCEPT verdict round-trips over the pipe, still signed by the host and
    binding the RE-HASHED artifact bytes, and reports the host's OWN runtime
    versions;
  * the headline binding rule survives the process seam: tampered staged bytes
    REFUSE on the runner, not on the conductor;
  * the request carries no trust — no key in the host's store REFUSES at the
    signer link, and the host's evidence floor cannot be loosened by the request
    (S2.4);
  * one long-lived runner process serves BOTH phases, and the COMMIT leg's
    load-time measurement passes the conductor's hard R2 comparison;
  * a runner that dies rather than answering fails CLOSED to a transport refusal.
"""

from __future__ import annotations

import json
import os
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

# The HOST's own receipt-signing / trust key (design R5). It lives on the
# runner's disk; the conductor only checks the reply against the matching key.
HOST_KEY = b"item-118-s2-runner-process-host-key"
RUNTIME = {"python": "runner-process-test"}


@pytest.fixture
def bundle(tmp_path):
    """A real `revl bundle` with its deploy attestation on disk, as if the
    conductor had staged it onto the runner's filesystem (§1.3 step 2)."""
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
    """The signer key as the runner holds it in its local trust store."""
    path = tmp_path / "host.key"
    path.write_bytes(HOST_KEY)
    return path


@pytest.fixture
def hostkeyfile(tmp_path):
    """The host's own signing key, on the runner's disk (design R5)."""
    path = tmp_path / "host-signing.key"
    path.write_bytes(HOST_KEY)
    return path


def _child_env():
    """The child must import `revl` from THIS tree, not any installed copy."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    return env


def _transport(keyfile=None, hostkeyfile=None, **flags):
    return deploy.ProcessRunnerTransport.local(
        key_paths=[keyfile] if keyfile is not None else [],
        host_key_path=hostkeyfile,
        runtime_versions=RUNTIME,
        env=_child_env(),
        **flags)


def _request(bundle, challenge="conductor-challenge-0", **kw):
    return deploy.AdmitRequest(bundle=str(bundle), backend="python",
                               challenge=challenge, **kw)


# ---------------------------------------------------------------------------
# ACCEPT round-trips over a real process, still signed by the host
# ---------------------------------------------------------------------------


def test_accept_verdict_round_trips_over_a_real_process(bundle, keyfile,
                                                        hostkeyfile):
    with _transport(keyfile, hostkeyfile) as transport:
        receipt = deploy.request_admission(transport, _request(bundle))
    assert receipt["verdict"] == deploy.ACCEPT, receipt
    ok, why = deploy.verify_receipt(receipt, HOST_KEY)
    assert ok, why
    # it binds the RE-HASHED artifact bytes, not any self-declared value
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")
    # the host's OWN runtime versions, passed on the runner's command line
    assert receipt["runtime_versions"] == RUNTIME


def test_tampered_staged_bytes_refuse_on_the_runner_process(bundle, keyfile,
                                                            hostkeyfile):
    """The headline binding rule survives a real process seam."""
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# tampered\n",
                        encoding="utf-8")
    with _transport(keyfile, hostkeyfile) as transport:
        receipt = deploy.request_admission(transport, _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT


# ---------------------------------------------------------------------------
# S2.4: the host verifies with its OWN trust, and the request cannot loosen it
# ---------------------------------------------------------------------------


def test_runner_with_no_key_refuses_at_the_signer_link(bundle, hostkeyfile):
    with _transport(keyfile=None, hostkeyfile=hostkeyfile) as transport:
        receipt = deploy.request_admission(transport, _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNER


def test_host_evidence_floor_cannot_be_loosened_by_the_request(bundle, keyfile,
                                                               hostkeyfile):
    """The conductor asks for no conformance evidence, but the runner REQUIRES
    it on its command line. The effective requirement is the host's floor OR the
    ask, so the runner stays stricter and REFUSES (S2.4)."""
    request = _request(bundle)
    assert request.require_conformance is False
    with _transport(keyfile, hostkeyfile, require_conformance=True) as transport:
        receipt = deploy.request_admission(transport, request)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    # with the floor down, the very same request/bundle is admitted — so the
    # refusal above is the floor biting and nothing else
    with _transport(keyfile, hostkeyfile) as transport:
        assert deploy.request_admission(transport, request)[
            "verdict"] == deploy.ACCEPT


def test_request_can_ask_for_stricter_checking(bundle, keyfile, hostkeyfile):
    """The other half of the OR: even with the host floor down, a request that
    ASKS for conformance evidence is honoured over the seam."""
    request = _request(bundle, challenge="c1", require_conformance=True)
    with _transport(keyfile, hostkeyfile) as transport:
        receipt = deploy.request_admission(transport, request)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE


# ---------------------------------------------------------------------------
# one long-lived process serves BOTH phases; COMMIT passes the R2 gate
# ---------------------------------------------------------------------------


def test_one_process_serves_prepare_then_commit(bundle, keyfile, hostkeyfile):
    """One channel carries BOTH phases, exactly as a real orchestration channel
    does: PREPARE admits, then COMMIT's load-time measurement over the SAME
    process passes the conductor's hard R2 comparison."""
    with _transport(keyfile, hostkeyfile) as transport:
        admission = deploy.request_admission(transport, _request(bundle))
        assert admission["verdict"] == deploy.ACCEPT, admission

        commit = deploy.CommitRequest(bundle=str(bundle), backend="python",
                                      challenge="commit-challenge-0")
        ok, reason, receipt = deploy.request_commit(
            transport, commit, admission_receipt=admission, host_key=HOST_KEY)
    assert ok, reason
    # the COMMIT receipt is the host's signed load-time measurement of the bytes
    # it was admitted to load — it binds the same re-hashed artifact digest
    assert receipt["artifact_hash"] == admission["artifact_hash"]


def test_commit_without_a_host_key_refuses(bundle, keyfile):
    """A runner with no signing key cannot mint an attributable measurement, so
    COMMIT refuses rather than returning an unsigned one (R2/R5)."""
    with _transport(keyfile, hostkeyfile=None) as transport:
        commit = deploy.CommitRequest(bundle=str(bundle), backend="python",
                                      challenge="commit-challenge-1")
        ok, reason, receipt = deploy.request_commit(
            transport, commit, admission_receipt={"verdict": deploy.ACCEPT,
                                                   "artifact_hash": "x"},
            host_key=HOST_KEY)
    assert not ok
    assert receipt["verdict"] == deploy.REFUSE


# ---------------------------------------------------------------------------
# a runner that dies rather than answering fails CLOSED
# ---------------------------------------------------------------------------


def test_a_dead_runner_fails_closed(bundle):
    """A transport whose child exits immediately (it imports nothing and returns)
    returns no verdict line; `request_admission` must refuse at the transport
    link, never mistake the silence for an answer."""
    transport = deploy.ProcessRunnerTransport(
        [sys.executable, "-c", "raise SystemExit(0)"])
    try:
        receipt = deploy.request_admission(transport, _request(bundle))
    finally:
        transport.close()
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT


# ---------------------------------------------------------------------------
# the CLI is wired: `revl deploy-admit` is a real subcommand
# ---------------------------------------------------------------------------


def test_deploy_admit_is_a_wired_subcommand():
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["deploy-admit", "--key", "/k", "--host-key", "/h",
         "--require-gauntlet", "--runtime-version", "python=3.14"])
    assert args.command == "deploy-admit"
    assert args.key == ["/k"] and args.host_key == "/h"
    assert args.require_gauntlet is True
    assert args.runtime_version == ["python=3.14"]
