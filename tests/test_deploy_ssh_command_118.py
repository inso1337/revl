"""`revl deploy MAP --bundle` over a cross-machine (`via = ssh`) target — the
deploy CLI wired onto the live ssh runner transport (roadmap item 118, toward
issue #79, design §1.3 the cross-machine leg).

The ssh runner transport, the stage step and the coordinated PREPARE/COMMIT
protocol were each built and tested; this file drives the WHOLE `deploy_command`
ssh path end to end. No live remote is needed: the ssh channel is a fake `ssh`
shim that IS the runner (it reads one JSON request off stdin and serves
`deploy-admit`), and staging is a fake `scp` shim that copies the bundle into a
"remote" directory on this same disk — exactly the shim technique
`test_deploy_ssh_transport_118.py` uses, one layer up at the command.

What is pinned:

  * a clean cross-machine PREPARE→COMMIT succeeds through the shims, and the
    deploy applies;
  * an ssh target with no pinned `known_hosts` refuses at ADMISSION, before any
    boundary is opened;
  * a byte that changes in transit (a tampering stage shim) is caught by the
    RUNNER's re-hash at PREPARE — the conductor verified the local bytes, the far
    side refuses the staged ones;
  * a COMMIT failure on the far side aborts through the EXISTING coordinated
    protocol, with nothing falsely rolled back;
  * `--dry-run` runs the fallible admission/chain half and opens NO boundary — it
    neither stages nor sshes.
"""

from __future__ import annotations

import json
import stat
import sys
from argparse import Namespace
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

# In this single-trust-domain slice one key plays the signer (verifies the
# bundle's attestation chain) and the host's receipt key (signs the
# admission/COMMIT receipts the conductor verifies). That is exactly the
# landed model — the operator who signs is the operator who deploys.
HOST_KEY = b"item-118-79-ssh-command-host-key"
HOST = "deploy@testhost"
HOSTNAME = "testhost"


@pytest.fixture
def bundle(tmp_path):
    """A real attested bundle on the conductor's disk (its `attestation.json`
    written), the `--bundle` the command stages to the far host."""
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
    path = tmp_path / "known_hosts"
    path.write_text(
        f"{hostname} ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIExamplePinnedHostKeyBytesForTest\n",
        encoding="utf-8")
    return path


def _write_shim(tmp_path, name: str, body: str) -> Path:
    shim = tmp_path / name
    shim.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _ssh_runner_shim(tmp_path, keyfile, *, commit_unsigned=False) -> Path:
    """A fake `ssh <host> revl deploy-admit`: it ignores its ssh argv, reads one
    JSON request off stdin and answers as the runner, signed by HOST_KEY. It re-
    hashes the STAGED bytes named in the request (`wire['bundle']`), which is the
    whole point — the far side trusts nothing it did not measure itself.

    With `commit_unsigned`, the COMMIT leg answers with NO signing key, so the
    conductor's hard R2 gate refuses the commit (a load-time measurement it
    cannot pin to the host's key) — a deterministic COMMIT failure.
    """
    commit_host_key = "None" if commit_unsigned else f"{HOST_KEY!r}"
    return _write_shim(tmp_path, "fake_ssh.py", (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from revl import deploy\n"
        "wire = json.loads(sys.stdin.readline())\n"
        f"kf = {str(keyfile)!r}\n"
        f"hk = {HOST_KEY!r}\n"
        "if wire.get('kind') == deploy.COMMIT_REQUEST_KIND:\n"
        f"    out = deploy.serve_commit_request(wire, host_key={commit_host_key},\n"
        "        runtime_versions={'python': '3.11'})\n"
        "else:\n"
        "    out = deploy.serve_admit_request(wire, key_paths=[kf], host_key=hk,\n"
        "        runtime_versions={'python': '3.11'})\n"
        "sys.stdout.write(json.dumps(out) + '\\n')\n"))


def _scp_shim(tmp_path, *, tamper=False) -> Path:
    """A fake `scp -r <opts> SRC host:DEST`: SRC is argv[-2] and `host:DEST` is
    argv[-1] (the stage step puts them last for exactly this reason). It copies
    the bundle tree into the "remote" DEST on this same disk.

    With `tamper`, it flips a byte AFTER the copy — a corruption in transit that
    the conductor's earlier verify (over the LOCAL bytes) cannot see, so the far
    side's re-hash is the only thing that can catch it.
    """
    body = (
        "import shutil, sys\n"
        "src = sys.argv[-2]\n"
        "dest = sys.argv[-1].split(':', 1)[1]\n"
        "shutil.copytree(src, dest)\n")
    if tamper:
        body += (
            "art = dest + '/emitted/python/components.py'\n"
            "with open(art, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('\\n# tampered in transit\\n')\n")
    return _write_shim(tmp_path, "fake_scp.py", body)


def _ssh_map(tmp_path, keyfile, kh, remote_bundle, *, with_known_hosts=True):
    """A deploy map placing one process on a `via = ssh` machine boundary."""
    deploy_tbl = {
        "via": "ssh",
        "host": HOST,
        "host_key": str(keyfile),
        "remote_bundle": str(remote_bundle),
        "extra_args": ["--key", str(keyfile), "--host-key", str(keyfile),
                       "--runtime-version", "python=3.11"],
    }
    if with_known_hosts:
        deploy_tbl["known_hosts"] = str(kh)
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps({
        "processes": {"svc": {"components": ["Smtp", "Notifier"],
                              "deploy": deploy_tbl}}}), encoding="utf-8")
    return path


def _args(map_path, **over):
    base = dict(map=str(map_path), dry_run=False, json=True, bundle=None,
                key=None, backend="python", require_gauntlet=False,
                require_conformance=False, ssh_exe=None, scp_exe=None)
    base.update(over)
    return Namespace(**base)


# ---------------------------------------------------------------------------
# a clean cross-machine PREPARE -> COMMIT applies through the shims
# ---------------------------------------------------------------------------


def test_ssh_deploy_applies_end_to_end(tmp_path, bundle, keyfile, capsys):
    kh = _pinned_known_hosts(tmp_path)
    ssh = _ssh_runner_shim(tmp_path, keyfile)
    scp = _scp_shim(tmp_path)
    remote = tmp_path / "staged"
    map_path = _ssh_map(tmp_path, keyfile, kh, remote)

    rc = deploy.deploy_command(_args(
        map_path, bundle=str(bundle), key=[str(keyfile)],
        ssh_exe=str(ssh), scp_exe=str(scp)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["verdict"] == deploy.DEPLOY_APPLIED, out
    assert out["participants"]["svc"]["outcome"] == deploy.APPLIED
    # the bundle really was staged onto the "remote" disk and re-hashed there
    assert (remote / deploy.ATTESTATION_NAME).is_file()
    # the applied receipt is the host's signed load-time measurement
    assert out["participants"]["svc"]["receipt"]["ok"] is True


# ---------------------------------------------------------------------------
# a missing pinned known_hosts refuses at ADMISSION, before any boundary
# ---------------------------------------------------------------------------


def test_ssh_deploy_without_a_pinned_known_hosts_refuses_at_admission(
        tmp_path, bundle, keyfile, capsys):
    ssh = _ssh_runner_shim(tmp_path, keyfile)
    scp = _scp_shim(tmp_path)
    remote = tmp_path / "staged"
    map_path = _ssh_map(tmp_path, keyfile, None, remote, with_known_hosts=False)

    rc = deploy.deploy_command(_args(
        map_path, bundle=str(bundle), key=[str(keyfile)],
        ssh_exe=str(ssh), scp_exe=str(scp)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False
    assert out["refusals"][0]["rule"] == "machine-boundary"
    assert "known_hosts" in out["refusals"][0]["reason"]
    # fail-closed: nothing was staged, no boundary opened
    assert not remote.exists()
    assert "participants" not in out


# ---------------------------------------------------------------------------
# a byte that changes in transit is caught by the RUNNER's re-hash
# ---------------------------------------------------------------------------


def test_a_tampered_staged_byte_refuses_on_the_runner(tmp_path, bundle, keyfile,
                                                      capsys):
    """The conductor verifies the LOCAL bundle (intact) and proceeds; the stage
    shim corrupts a byte in transit; the far side re-hashes the staged bytes and
    REFUSES at PREPARE. Nothing committed, nothing to roll back."""
    kh = _pinned_known_hosts(tmp_path)
    ssh = _ssh_runner_shim(tmp_path, keyfile)
    scp = _scp_shim(tmp_path, tamper=True)
    remote = tmp_path / "staged"
    map_path = _ssh_map(tmp_path, keyfile, kh, remote)

    rc = deploy.deploy_command(_args(
        map_path, bundle=str(bundle), key=[str(keyfile)],
        ssh_exe=str(ssh), scp_exe=str(scp)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1, out
    assert out["verdict"] == deploy.DEPLOY_REFUSED
    assert out["phase"] == "prepare"
    # a PREPARE refusal is not a rollback: nothing was activated
    assert out["residue"]["clean"] is True


# ---------------------------------------------------------------------------
# a COMMIT failure aborts through the existing coordinated protocol
# ---------------------------------------------------------------------------


def test_a_commit_failure_aborts(tmp_path, bundle, keyfile, capsys):
    """PREPARE succeeds but the far side answers COMMIT with an unsigned
    measurement, so the conductor's hard R2 gate refuses it. `run_deploy` drives
    the abort: the one participant never counted as committed, so the deploy
    aborts CLEAN with nothing falsely rolled back."""
    kh = _pinned_known_hosts(tmp_path)
    ssh = _ssh_runner_shim(tmp_path, keyfile, commit_unsigned=True)
    scp = _scp_shim(tmp_path)
    remote = tmp_path / "staged"
    map_path = _ssh_map(tmp_path, keyfile, kh, remote)

    rc = deploy.deploy_command(_args(
        map_path, bundle=str(bundle), key=[str(keyfile)],
        ssh_exe=str(ssh), scp_exe=str(scp)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1, out
    assert out["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, out
    assert out["failedAt"] == "svc"
    assert out["participants"]["svc"]["outcome"] == deploy.NEVER_COMMITTED
    assert out["residue"]["clean"] is True


# ---------------------------------------------------------------------------
# --dry-run runs the fallible half and opens NO boundary
# ---------------------------------------------------------------------------


def test_dry_run_admits_but_opens_no_boundary(tmp_path, bundle, keyfile, capsys):
    kh = _pinned_known_hosts(tmp_path)
    ssh = _ssh_runner_shim(tmp_path, keyfile)
    scp = _scp_shim(tmp_path)
    remote = tmp_path / "staged"
    map_path = _ssh_map(tmp_path, keyfile, kh, remote)

    rc = deploy.deploy_command(_args(
        map_path, dry_run=True, bundle=str(bundle), key=[str(keyfile)],
        ssh_exe=str(ssh), scp_exe=str(scp)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["ok"] is True
    assert out["targets"]["svc"]["boundary"] == deploy.BOUNDARY_MACHINE
    assert out["chain"]["verdict"] == deploy.ACCEPT
    # opened NO boundary: nothing was staged and the runner was never spawned
    assert not remote.exists()
