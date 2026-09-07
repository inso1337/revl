"""`revl deploy --bundle` — the §2 attestation chain, wired onto the PREPARE
path of the actual command (roadmap item 118, Slice 1 piece 1).

`deploy.admit` was built and unit-tested in isolation, but `revl deploy` drove
PREPARE/COMMIT/ABORT over a map WITHOUT ever verifying an attested bundle's
chain. These tests pin the wiring the design's §1.3 step 4 requires: when a
bundle is named, its whole-composition chain is re-hashed against the staged
bytes and verified BEFORE any boundary opens, and a refused chain refuses the
whole deploy with nothing to roll back.

Two layers are pinned: :func:`deploy.admit_bundle_chain` (the operator's own
key(s) admitting the chain, effect-free) and :func:`deploy.deploy_command` (the
command refusing the deploy on a bad chain, and reaching COMMIT only on a good
one).
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import attest, deploy  # noqa: E402


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

SIGNER_KEY = b"item-118-cli-admit-signer"


@pytest.fixture
def bundle(tmp_path):
    """A real `revl bundle` for the python backend with its deploy attestation
    written to `attestation.json`, so `admit` reads it off disk exactly as the
    command does."""
    from revl.bundle import build_bundle

    src = tmp_path / "mailer.rvl"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    att = deploy.make_deploy_attestation(out, SIGNER_KEY, signer="ci")
    (out / deploy.ATTESTATION_NAME).write_text(
        json.dumps(att, sort_keys=True), encoding="utf-8")
    return out


@pytest.fixture
def keyfile(tmp_path):
    path = tmp_path / "signer.key"
    path.write_bytes(SIGNER_KEY)
    return path


@pytest.fixture
def local_map(tmp_path):
    """A minimal, fully-local placement map (no `[deploy]` tables): every
    process is a `via = local` process-boundary child."""
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps({
        "processes": {
            "svc": {"components": ["Smtp", "Notifier"]},
        }
    }), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# admit_bundle_chain: the operator's own key(s), effect-free
# ---------------------------------------------------------------------------


def test_admit_bundle_chain_accepts_under_the_right_key(bundle, keyfile):
    receipt = deploy.admit_bundle_chain(bundle, key_paths=[keyfile],
                                        backend="python")
    assert receipt["verdict"] == deploy.ACCEPT, receipt
    # the receipt binds the RE-HASHED artifact, not anything self-declared
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")


def test_admit_bundle_chain_refuses_with_no_key(bundle):
    """No key -> the chain cannot be verified, so it REFUSES at the signer link
    rather than admitting unverified."""
    receipt = deploy.admit_bundle_chain(bundle, key_paths=[], backend="python")
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNER


def test_admit_bundle_chain_refuses_wrong_key(bundle, tmp_path):
    other = tmp_path / "other.key"
    other.write_bytes(b"a-different-operators-key")
    receipt = deploy.admit_bundle_chain(bundle, key_paths=[other],
                                        backend="python")
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] in (deploy.LINK_SIGNER, deploy.LINK_SIGNATURE)


def test_admit_bundle_chain_refuses_tampered_artifact(bundle, keyfile):
    """The headline binding rule end-to-end: mutate a byte the receiver will
    execute and the chain refuses on receive, key or no key."""
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# tampered\n",
                        encoding="utf-8")
    receipt = deploy.admit_bundle_chain(bundle, key_paths=[keyfile],
                                        backend="python")
    assert receipt["verdict"] == deploy.REFUSE


# ---------------------------------------------------------------------------
# deploy_command: the chain gates the command's PREPARE
# ---------------------------------------------------------------------------


def _args(map_path, **over):
    base = dict(map=str(map_path), dry_run=False, json=False, bundle=None,
                key=None, backend="python", require_gauntlet=False,
                require_conformance=False)
    base.update(over)
    return Namespace(**base)


def test_deploy_dry_run_reports_admitted_chain(bundle, keyfile, local_map, capsys):
    rc = deploy.deploy_command(_args(local_map, dry_run=True, json=True,
                                     bundle=str(bundle), key=[str(keyfile)]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["chain"]["verdict"] == deploy.ACCEPT


def test_deploy_refuses_before_any_boundary_on_bad_chain(bundle, local_map,
                                                         tmp_path, capsys):
    """A refused chain refuses the WHOLE deploy on the real (non-dry-run) path,
    and never reaches COMMIT — no WAL/world files are written for the map's
    processes, because no participant was spawned."""
    other = tmp_path / "wrong.key"
    other.write_bytes(b"not-the-signer")
    rc = deploy.deploy_command(_args(local_map, json=True, bundle=str(bundle),
                                     key=[str(other)]))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["chain"]["verdict"] == deploy.REFUSE
    # PREPARE-time refusal: nothing was committed, so nothing was deployed.
    assert "participants" not in out
    assert "commitLedger" not in out


def test_deploy_commits_on_good_chain(bundle, keyfile, local_map, capsys):
    """A good chain lets the command proceed to the coordinated deploy: the
    process-boundary participant applies and the aggregate verdict is applied."""
    rc = deploy.deploy_command(_args(local_map, json=True, bundle=str(bundle),
                                     key=[str(keyfile)]))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["verdict"] == deploy.DEPLOY_APPLIED
    assert out["participants"]["svc"]["outcome"] == deploy.APPLIED


def test_deploy_without_bundle_is_unchanged(local_map, capsys):
    """No `--bundle`: the command behaves exactly as before — admit the map and
    deploy it, with no chain gate."""
    rc = deploy.deploy_command(_args(local_map, json=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["verdict"] == deploy.DEPLOY_APPLIED
    assert out.get("participants", {}).get("svc", {}).get("outcome") == \
        deploy.APPLIED
