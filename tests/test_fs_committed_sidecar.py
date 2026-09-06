"""Real-file proofs for the trusted exclusive-metadata cleanup contract.

Sidecar swap probes exercise detectable corruption, not inode-conditional unlink
or safety against arbitrary writers violating the documented exclusivity.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_fs_pinned_root import ROOT, _barrier, _bind, _entries, _session, _snapshot


@pytest.mark.parametrize("case", [
    "success", "unbound", "arguments", "paths", "symlink", "directory", "fifo",
    "hardlink", "permissions", "identity", "digest", "missing", "directory-link",
    "swap-before-open", "swap-during-read", "root-before-open",
    "root-before-unlink", "lifecycle", "descriptor-lifetime",
])
def test_committed_sidecar_process(case, tmp_path):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        (str(ROOT / "src"), str(ROOT / "backends/python"))))
    result = subprocess.run(
        [sys.executable, __file__, case, str(tmp_path)], env=env, cwd=ROOT,
        text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence["case"] == case
    assert evidence["guard"] == str(ROOT / "backends/python/revl_fs_workspace.py")


def _capture(path):
    with path.open("rb") as file:
        st = os.fstat(file.fileno())
        digest = hashlib.sha256(file.read()).hexdigest()
    return dict(path=str(path), expected_sha256=digest,
                expected_dev=st.st_dev, expected_ino=st.st_ino)


def _worker(case, base, api, ws):
    root = base / "root"
    root.mkdir()
    project = root / "project.txt"
    project.write_bytes(b"project")
    directory = root / ws.PREIMAGE_DIRNAME
    directory.mkdir(mode=0o700)
    sidecar = directory / ("pre-" + "a" * 32)
    sidecar.write_bytes(b"preimage")
    captured = _capture(sidecar)
    finalize = api.finalize_committed_sidecar
    assert api.COMMITTED_SIDECAR_API_VERSION == 1
    assert finalize is ws.finalize_committed_sidecar
    assert "finalize_committed_sidecar" not in ws.READ_HELPERS
    assert all("finalize_committed_sidecar" not in entries
               for entries in ws.PATH_FAMILIES.values())
    if case == "unbound":
        os.environ[ws.WORKSPACE_ENV] = str(root)
        with pytest.raises(api.FsOpError, match="EWORKSPACE"):
            finalize(**captured)
        assert sidecar.read_bytes() == b"preimage"
        return
    _bind(root)

    def refused(code, **overrides):
        with pytest.raises(api.FsOpError) as error:
            finalize(**(captured | overrides))
        assert error.value.code == code

    if case == "arguments":
        for value in ("", "x" * 64, "A" * 64, b"a" * 64, None):
            refused("EINVAL", expected_sha256=value)
        for key in ("expected_dev", "expected_ino"):
            for value in (-1, True, "1", 1.5, None):
                refused("EINVAL", **{key: value})
    elif case == "paths":
        for path in (str(project), str(root / ".revl" / sidecar.name),
                     str(root / ws.GARBAGE_DIRNAME / sidecar.name),
                     str(directory / "pre-invalid"), str(sidecar) + "/nested",
                     str(directory / ("pre-" + "A" * 32)),
                     str(directory) + "/./" + sidecar.name,
                     str(base / sidecar.name), sidecar.name):
            refused("EOUTSIDE", path=path)
    elif case in ("symlink", "directory", "fifo", "hardlink"):
        if case == "hardlink":
            os.link(sidecar, directory / "extra-link")
            refused("EIDENTITY")
        else:
            sidecar.unlink()
            if case == "symlink":
                sidecar.symlink_to(project)
            elif case == "directory":
                sidecar.mkdir()
            else:
                os.mkfifo(sidecar)
            refused("EOUTSIDE")
        assert sidecar.lstat()
    elif case == "permissions":
        directory.chmod(0o755)
        refused("EOUTSIDE")
        directory.chmod(0o700)
    elif case == "identity":
        refused("EIDENTITY", expected_ino=captured["expected_ino"] + 1)
        refused("EIDENTITY", expected_dev=captured["expected_dev"] + 1)
    elif case == "digest":
        refused("EIDENTITY", expected_sha256=hashlib.sha256(b"other").hexdigest())
    elif case == "descriptor-lifetime":
        opened = []
        real_open, real_dup = os.open, os.dup

        def tracked_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def tracked_dup(fd):
            duplicate = real_dup(fd)
            opened.append(duplicate)
            return duplicate

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "open", tracked_open)
            patch.setattr(os, "dup", tracked_dup)
            for _ in range(4):
                refused("EIDENTITY", expected_sha256="0" * 64)
            assert finalize(**captured) is None
        assert opened
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
    elif case == "missing":
        sidecar.unlink()
        refused("ENOENT")
        directory.rmdir()
        refused("ENOENT")
    elif case == "directory-link":
        saved = root / "saved-directory"
        directory.rename(saved)
        directory.symlink_to(saved, target_is_directory=True)
        refused("EOUTSIDE")
        assert (saved / sidecar.name).read_bytes() == b"preimage"
    elif case in ("swap-before-open", "swap-during-read"):
        saved = directory / "saved-evidence"

        def swap():
            sidecar.rename(saved)
            sidecar.write_bytes(b"preimage" if case == "swap-before-open" else b"other")

        name = "open" if case == "swap-before-open" else "read"
        with pytest.MonkeyPatch.context() as patch:
            with _barrier(patch, name,
                          lambda p, *a, **k: p == sidecar.name if name == "open" else True,
                          swap):
                refused("EIDENTITY")
        assert saved.read_bytes() == b"preimage"
        assert sidecar.read_bytes() == (b"preimage" if name == "open" else b"other")
    elif case in ("root-before-open", "root-before-unlink"):
        moved = base / "moved"
        replacements = []

        def replace_root():
            root.rename(moved)
            root.mkdir()
            (root / directory.name).mkdir(mode=0o700)
            (root / directory.name / sidecar.name).write_bytes(b"replacement evidence")
            (root / "project.txt").write_bytes(b"replacement project")
            replacements.append(_snapshot(root))

        name = "open" if case == "root-before-open" else "unlink"
        with pytest.MonkeyPatch.context() as patch:
            with _barrier(patch, name, lambda p, *a, **k: p == sidecar.name,
                          replace_root):
                assert finalize(**captured) is None
        assert not (moved / directory.name / sidecar.name).exists()
        assert (moved / "project.txt").read_bytes() == b"project"
        assert _snapshot(root) == replacements[0]
        refused("ENOENT")
        return
    elif case == "lifecycle":
        sidecar.unlink()
        session = _session(base)
        session.call("ops", "op_write", [str(project), "committed"])
        entries = _entries(session)
        assert len(entries) == 1
        ownership = _capture(Path(entries[0].witness["preimage"]))
        # Durable host ledger artifacts, outside the workspace; not runtime receipts.
        prepared = base / "prepared.json"
        with prepared.open("w") as file:
            json.dump(ownership, file)
            file.flush()
            os.fsync(file.fileno())
        ledger_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(ledger_fd)
        finally:
            os.close(ledger_fd)
        report = session.commit_confirm(session.commit()["hash"])
        assert report["committed"], report
        assert all(e.discharged and not e.replayed for e in entries)
        with (base / "confirmed.json").open("w") as file:
            json.dump(report, file)
            file.flush()
            os.fsync(file.fileno())
        ledger_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(ledger_fd)
        finally:
            os.close(ledger_fd)
        assert finalize(**ownership) is None
        assert not list(directory.iterdir())
        session = _session(base)
        assert session.abort()["noResidue"]
        session = _session(base)
        session.call("ops", "op_write", [str(project), "must roll back"])
        assert session.abort()["noResidue"]
        assert project.read_bytes() == b"committed"
        assert not list(directory.iterdir())
        return
    else:
        assert finalize(**captured) is None
        refused("ENOENT")

    assert project.read_bytes() == b"project"
    if case in ("arguments", "paths", "permissions", "identity", "digest"):
        assert sidecar.read_bytes() == b"preimage"


if __name__ == "__main__":
    case, base = sys.argv[1], Path(sys.argv[2]).resolve()
    import revl.fs_workspace as api
    import revl_fs_workspace as ws
    _worker(case, base, api, ws)
    print(json.dumps(dict(case=case, guard=str(Path(ws.__file__).resolve()))))
