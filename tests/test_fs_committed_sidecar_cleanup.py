"""Real-file proofs for the runtime-owned committed-sidecar cleanup handle
(item 486).

The handle folds the sidecar's ownership and the commit acknowledgment into one
opaque runtime-issued receipt, then reports COMPLETED vs. UNRESOLVED cleanup
instead of leaving the host to reconstruct ownership and classify a raised
refusal. These probes exercise that reporting and the receipt gate; they do NOT
claim inode-conditional unlink or safety against writers violating the
documented exclusivity (that contract is unchanged from the finalizer).
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_fs_committed_sidecar import _capture
from test_fs_pinned_root import ROOT, _bind


@pytest.mark.parametrize("case", [
    "completed", "unresolved-retry", "unresolved-missing", "no-receipt",
    "forged-receipt", "receipt-arguments", "single-use", "unbound",
])
def test_committed_sidecar_cleanup_process(case, tmp_path):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        (str(ROOT / "src"), str(ROOT / "backends/python"))))
    result = subprocess.run(
        [sys.executable, __file__, case, str(tmp_path)], env=env, cwd=ROOT,
        text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence["case"] == case
    assert evidence["guard"] == str(ROOT / "backends/python/revl_fs_workspace.py")


def _fixture(base, ws):
    """A bound-ready root holding one captured preimage sidecar."""
    root = base / "root"
    root.mkdir()
    (root / "project.txt").write_bytes(b"project")
    directory = root / ws.PREIMAGE_DIRNAME
    directory.mkdir(mode=0o700)
    sidecar = directory / ("pre-" + "a" * 32)
    sidecar.write_bytes(b"preimage")
    return root, directory, sidecar, _capture(sidecar)


def _worker(case, base, api, ws):
    root, directory, sidecar, captured = _fixture(base, ws)
    assert api.COMMITTED_SIDECAR_CLEANUP_API_VERSION == 1
    # The public re-exports are the runtime objects, not copies.
    assert api.CommittedSidecarCleanup is ws.CommittedSidecarCleanup
    assert api.issue_committed_sidecar_receipt is ws.issue_committed_sidecar_receipt

    issue = api.issue_committed_sidecar_receipt
    Cleanup = api.CommittedSidecarCleanup

    if case == "no-receipt":
        # The handle refuses to act without a genuine receipt: no filesystem
        # touch is even attempted, and it never reaches an "unresolved" report.
        for bogus in (None, object(), captured, "receipt", 0,
                      dict(captured, _path=str(sidecar))):
            with pytest.raises(api.ConfinementError) as error:
                Cleanup(bogus)
            assert error.value.code == "ERECEIPT"
        assert sidecar.read_bytes() == b"preimage"

    elif case == "forged-receipt":
        # A host cannot mint a receipt: the private grant never leaves the
        # runtime, so a direct construction with any other first argument fails.
        for grant in (None, object(), api.CleanupOutcome, ws._CommitReceiptGrant()):
            with pytest.raises(api.ConfinementError) as error:
                api.CommittedSidecarReceipt(grant, **captured)
            assert error.value.code == "ERECEIPT"
        assert sidecar.read_bytes() == b"preimage"

    elif case == "receipt-arguments":
        # Ownership shape is validated at mint time, before any handle exists.
        for value in ("", "x" * 64, "A" * 64, b"a" * 64, None):
            with pytest.raises(api.FsOpError) as error:
                issue(**(captured | {"expected_sha256": value}))
            assert error.value.code == "EINVAL"
        for key in ("expected_dev", "expected_ino"):
            for value in (-1, True, "1", 1.5, None):
                with pytest.raises(api.FsOpError) as error:
                    issue(**(captured | {key: value}))
                assert error.value.code == "EINVAL"
        assert sidecar.read_bytes() == b"preimage"

    elif case == "single-use":
        _bind(root)
        receipt = issue(**captured)
        handle = Cleanup(receipt)
        # One receipt authorizes exactly one handle for exactly one sidecar.
        with pytest.raises(api.ConfinementError) as error:
            Cleanup(receipt)
        assert error.value.code == "ERECEIPT"
        assert handle.run().completed
        assert not sidecar.exists()

    elif case == "unbound":
        # Minting needs no root; running without a pinned root is UNRESOLVED,
        # reported (not raised) with the finalizer's own EWORKSPACE code.
        handle = Cleanup(issue(**captured))
        outcome = handle.run()
        assert not outcome.completed and outcome.state == "unresolved"
        assert outcome.code == "EWORKSPACE"
        assert not handle.completed
        assert sidecar.read_bytes() == b"preimage"

    elif case == "unresolved-missing":
        _bind(root)
        sidecar.unlink()
        handle = Cleanup(issue(**captured))
        outcome = handle.run()
        # An explicit missing-evidence failure surfaces as unresolved, never as
        # a silent "already clean" completion.
        assert not outcome.completed and outcome.state == "unresolved"
        assert outcome.code == "ENOENT"
        assert not handle.completed
        assert handle.run().code == "ENOENT"  # still live, still unresolved

    elif case == "unresolved-retry":
        _bind(root)
        directory.chmod(0o755)  # not private: the finalizer refuses
        handle = Cleanup(issue(**captured))
        first = handle.run()
        assert not first.completed and first.state == "unresolved"
        assert first.code == "EOUTSIDE"
        assert first.path and first.message
        assert sidecar.exists() and not handle.completed
        # The same handle stays usable: fix exclusivity, retry, and it resolves.
        directory.chmod(0o700)
        second = handle.run()
        assert second.completed and second.state == "completed"
        assert second.code is None and second.message is None
        assert not sidecar.exists() and handle.completed

    else:  # "completed"
        _bind(root)
        receipt = issue(**captured)
        handle = Cleanup(receipt)
        assert not handle.completed
        outcome = handle.run()
        assert outcome.completed and outcome.state == "completed"
        assert outcome.code is None and outcome.message is None and outcome.path is None
        assert not sidecar.exists() and handle.completed
        # Idempotent after success: no second filesystem touch, still completed.
        again = handle.run()
        assert again.completed and again.state == "completed"
        assert not list(directory.iterdir())


if __name__ == "__main__":
    case, base = sys.argv[1], Path(sys.argv[2]).resolve()
    import revl.fs_workspace as api
    import revl_fs_workspace as ws
    _worker(case, base, api, ws)
    print(json.dumps(dict(case=case, guard=str(Path(ws.__file__).resolve()))))
