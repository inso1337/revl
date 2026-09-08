"""The live cross-machine runner transport — SSH, slice 1 (roadmap item 118,
issue #79, the design's §1.2 / [REMAINS] "live cross-machine transport").

The in-process and stdio runner protocols were built and tested on one host.
This is the FIRST slice of the real cross-machine leg: a
:class:`deploy.StdioRunnerTransport` that spawns `revl deploy-admit` behind a
stdio pipe and speaks the SAME `send` contract, in a `.local(...)` (child
process) and a `.ssh(host, ...)` (remote) shape, the latter with a PINNED
host-key verification that fails CLOSED (design R4).

No live remote host is required: the ssh leg is exercised with a fake `ssh`
shim (an executable that stands in for `ssh <host> revl deploy-admit`), and the
`run_deploy` integration runs over an injected in-process transport. What is
pinned:

  * `.ssh(...)` builds the `ssh <opts> <host> revl deploy-admit` argv with
    `StrictHostKeyChecking=yes`, `BatchMode=yes` and the pinned
    `UserKnownHostsFile`; it REQUIRES a `known_hosts` and REFUSES any option
    that would re-set (loosen) the pin;
  * `verify_host_key()` fails closed on a missing / empty / wrong-host pin and
    passes on a matching (or hashed) one, BEFORE anything is spawned;
  * a host-key failure reported by ssh on the wire fails closed to a transport
    REFUSE marked `host_key_failure`, never mistaken for a verdict;
  * the ACCEPT verdict round-trips over a real subprocess pipe, still signed by
    the host;
  * `run_deploy` drives a :class:`deploy.RemoteParticipant` over the same
    contract — applied on a good chain, refused clean on a bad one — and the
    cross-machine ABORT leg is honestly `unresolved`, not a false rollback.
"""

from __future__ import annotations

import json
import stat
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

HOST_KEY = b"item-118-79-ssh-transport-host-key"
HOST = "deploy@testhost"
HOSTNAME = "testhost"


@pytest.fixture
def bundle(tmp_path):
    """A real bundle for the python backend with its deploy attestation on disk,
    staged as if the conductor had copied it to the runner (§1.3 step 2)."""
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
    path = tmp_path / "host.key"
    path.write_bytes(HOST_KEY)
    return path


def _pinned_known_hosts(tmp_path, hostname=HOSTNAME):
    """A plaintext known_hosts pinning one host key for `hostname` — enough for
    the pre-flight to match by name (the key bytes themselves are ssh's to
    check at connect)."""
    path = tmp_path / "known_hosts"
    path.write_text(
        f"{hostname} ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIExamplePinnedHostKeyBytesForTest\n",
        encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# argv construction and the pin the caller cannot loosen
# ---------------------------------------------------------------------------


def test_ssh_argv_pins_host_key_checking(tmp_path):
    kh = _pinned_known_hosts(tmp_path)
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh)
    argv = t.argv
    assert argv[0] == "ssh"
    # the two fail-closed options are forced on
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    # the pin points at OUR known_hosts, never /dev/null
    assert f"UserKnownHostsFile={kh}" in argv
    assert "UserKnownHostsFile=/dev/null" not in argv
    # host, then the remote runner command
    assert argv[-3:] == [HOST, "revl", deploy.DEPLOY_ADMIT_SUBCOMMAND]


def test_ssh_requires_a_pinned_known_hosts():
    with pytest.raises(ValueError, match="pinned known_hosts"):
        deploy.StdioRunnerTransport.ssh(HOST, known_hosts=None)


def test_ssh_refuses_an_option_that_would_loosen_the_pin(tmp_path):
    kh = _pinned_known_hosts(tmp_path)
    with pytest.raises(ValueError, match="loosen the pin"):
        deploy.StdioRunnerTransport.ssh(
            HOST, known_hosts=kh, options=["StrictHostKeyChecking=no"])
    with pytest.raises(ValueError, match="loosen the pin"):
        deploy.StdioRunnerTransport.ssh(
            HOST, known_hosts=kh, options=["UserKnownHostsFile=/dev/null"])


def test_ssh_port_and_extra_options_are_added(tmp_path):
    kh = _pinned_known_hosts(tmp_path)
    t = deploy.StdioRunnerTransport.ssh(
        HOST, known_hosts=kh, port=2222, options=["ConnectTimeout=5"])
    argv = t.argv
    assert "-p" in argv and "2222" in argv
    assert "ConnectTimeout=5" in argv


def test_local_argv_runs_the_module_runner():
    t = deploy.StdioRunnerTransport.local()
    assert t.argv[1:] == ["-m", "revl", deploy.DEPLOY_ADMIT_SUBCOMMAND]


# ---------------------------------------------------------------------------
# verify_host_key: fail closed on an absent / empty / wrong-host pin
# ---------------------------------------------------------------------------


def test_verify_host_key_refuses_a_missing_pin(tmp_path):
    t = deploy.StdioRunnerTransport.ssh(
        HOST, known_hosts=tmp_path / "nope")
    with pytest.raises(deploy.SshRunnerRefused) as exc:
        t.verify_host_key()
    assert exc.value.host_key_failure is True


def test_verify_host_key_refuses_an_empty_pin(tmp_path):
    kh = tmp_path / "empty_known_hosts"
    kh.write_text("# only a comment\n", encoding="utf-8")
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh)
    with pytest.raises(deploy.SshRunnerRefused, match="no host-key entries"):
        t.verify_host_key()


def test_verify_host_key_refuses_a_pin_for_another_host(tmp_path):
    kh = _pinned_known_hosts(tmp_path, hostname="someone-else")
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh)
    with pytest.raises(deploy.SshRunnerRefused, match="no entry for"):
        t.verify_host_key()


def test_verify_host_key_accepts_a_matching_pin(tmp_path):
    kh = _pinned_known_hosts(tmp_path)
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh)
    # user@ is stripped for the host-key identity match; no raise == pinned
    t.verify_host_key()


def test_verify_host_key_defers_to_ssh_for_a_hashed_pin(tmp_path):
    kh = tmp_path / "hashed_known_hosts"
    kh.write_text("|1|abc=|def= ssh-ed25519 AAAAHashedEntry\n", encoding="utf-8")
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh)
    # a hashed pin cannot be matched by name; we do not wrongly refuse it
    t.verify_host_key()


def test_local_transport_never_checks_a_host_key():
    deploy.StdioRunnerTransport.local().verify_host_key()  # no raise


# ---------------------------------------------------------------------------
# a host-key failure on the wire fails closed to a transport REFUSE
# ---------------------------------------------------------------------------


def _write_shim(tmp_path, body: str) -> Path:
    shim = tmp_path / "fake_ssh.py"
    shim.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def test_host_key_verification_failure_fails_closed(tmp_path, bundle):
    """ssh reports the host key changed -> the transport refuses fail-closed,
    marks it a host-key failure, and the conductor-side driver reads a REFUSE."""
    kh = _pinned_known_hosts(tmp_path)  # pre-flight matches, so ssh is reached
    shim = _write_shim(tmp_path, (
        "import sys\n"
        "sys.stderr.write('@@@@@@\\nWARNING: REMOTE HOST IDENTIFICATION HAS "
        "CHANGED!\\nHost key verification failed.\\n')\n"
        "sys.exit(255)\n"))
    t = deploy.StdioRunnerTransport.ssh(
        HOST, known_hosts=kh, ssh_exe=str(shim))
    request = deploy.AdmitRequest(bundle=str(bundle), backend="python",
                                  challenge="c-hostkey")
    receipt = deploy.request_admission(t, request)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT
    assert receipt.get("host_key_failure") is True


def test_a_dead_runner_fails_closed_without_a_host_key_mark(tmp_path, bundle):
    kh = _pinned_known_hosts(tmp_path)
    shim = _write_shim(tmp_path, (
        "import sys\n"
        "sys.stderr.write('some unrelated failure\\n')\n"
        "sys.exit(2)\n"))
    t = deploy.StdioRunnerTransport.ssh(
        HOST, known_hosts=kh, ssh_exe=str(shim))
    receipt = deploy.request_admission(
        t, deploy.AdmitRequest(bundle=str(bundle), backend="python",
                               challenge="c-dead"))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_TRANSPORT
    assert "host_key_failure" not in receipt


# ---------------------------------------------------------------------------
# the ACCEPT verdict round-trips over a REAL subprocess pipe (fake-ssh runner)
# ---------------------------------------------------------------------------


def _runner_shim(tmp_path, keyfile) -> Path:
    """A fake `ssh <host> revl deploy-admit`: ignores its ssh argv, reads one
    JSON request off stdin and answers with a real runner response, signed by
    HOST_KEY. It IS the runner, reached over a real pipe."""
    return _write_shim(tmp_path, (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from revl import deploy\n"
        "wire = json.loads(sys.stdin.readline())\n"
        f"kf = {str(keyfile)!r}\n"
        f"hk = {HOST_KEY!r}\n"
        "if wire.get('kind') == deploy.COMMIT_REQUEST_KIND:\n"
        "    out = deploy.serve_commit_request(wire, host_key=hk,\n"
        "        runtime_versions={'python': '3.11'})\n"
        "else:\n"
        "    out = deploy.serve_admit_request(wire, key_paths=[kf], host_key=hk,\n"
        "        runtime_versions={'python': '3.11'})\n"
        "sys.stdout.write(json.dumps(out) + '\\n')\n"))


def test_accept_round_trips_over_ssh_and_stays_signed(tmp_path, bundle, keyfile):
    kh = _pinned_known_hosts(tmp_path)
    shim = _runner_shim(tmp_path, keyfile)
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh, ssh_exe=str(shim))
    request = deploy.AdmitRequest(bundle=str(bundle), backend="python",
                                  challenge="c-accept")
    receipt = deploy.request_admission(t, request)
    assert receipt["verdict"] == deploy.ACCEPT, receipt
    ok, why = deploy.verify_receipt(receipt, HOST_KEY)
    assert ok, why
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")


def test_tampered_bytes_refuse_over_ssh(tmp_path, bundle, keyfile):
    """The headline binding rule survives the real seam too."""
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# tampered\n",
                        encoding="utf-8")
    kh = _pinned_known_hosts(tmp_path)
    shim = _runner_shim(tmp_path, keyfile)
    t = deploy.StdioRunnerTransport.ssh(HOST, known_hosts=kh, ssh_exe=str(shim))
    receipt = deploy.request_admission(
        t, deploy.AdmitRequest(bundle=str(bundle), backend="python",
                               challenge="c-tamper"))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT


# ---------------------------------------------------------------------------
# run_deploy drives a RemoteParticipant over the same send() contract
# ---------------------------------------------------------------------------


def _transport(keyfile=None, **kw):
    return deploy.InProcessTransport(
        key_paths=[keyfile] if keyfile is not None else [],
        host_key=HOST_KEY, runtime_versions={"python": "3.11"}, **kw)


def _participant(bundle, transport, identity="proc/mailer"):
    return deploy.RemoteParticipant(
        identity, transport,
        admit_request=deploy.AdmitRequest(bundle=str(bundle), backend="python",
                                          challenge="c-prepare"),
        commit_request=deploy.CommitRequest(bundle=str(bundle), backend="python",
                                            challenge="c-commit"),
        host_key=HOST_KEY)


def test_run_deploy_applies_over_a_remote_participant(bundle, keyfile):
    result = deploy.run_deploy([_participant(bundle, _transport(keyfile))])
    assert result["verdict"] == deploy.DEPLOY_APPLIED, result
    outcome = result["participants"]["proc/mailer"]
    assert outcome["outcome"] == deploy.APPLIED
    # run_deploy stores the participant's commit REPORT; its own receipt is the
    # host's signed COMMITTED load-time measurement.
    assert outcome["receipt"]["ok"] is True
    assert outcome["receipt"]["receipt"]["verdict"] == deploy.COMMITTED


def test_run_deploy_refuses_clean_on_a_bad_chain(bundle, keyfile):
    """A tampered staged tree refuses at PREPARE, so run_deploy refuses the whole
    deploy with nothing committed and nothing to roll back."""
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# tampered\n",
                        encoding="utf-8")
    result = deploy.run_deploy([_participant(bundle, _transport(keyfile))])
    assert result["verdict"] == deploy.DEPLOY_REFUSED
    assert result["phase"] == "prepare"
    assert result["residue"]["clean"] is True


def test_remote_abort_is_unresolved_not_a_false_rollback(bundle, keyfile):
    """Slice 1's honest boundary: no ABORT leg over the runner protocol yet, so
    the coordinator reports `unresolved`, never a rollback it did not obtain."""
    p = _participant(bundle, _transport(keyfile))
    with pytest.raises(deploy.Unreachable):
        p.abort()
