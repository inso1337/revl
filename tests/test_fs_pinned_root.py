"""Process-isolated pinning proofs using real emitted FsOpsC and host syscalls.

The barrier wrappers only pause a syscall; they always invoke the real syscall
after a second thread renames the admitted root and plants a replacement.
No binding state is reset between tests: each actor is a fresh Python process.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSUMER = """
use "stdlib/fs.rvl" { write, rm, move, mkdir }
service FsOps {
  emission fn op_write(path: Str, content: Str)
  emission fn op_rm(path: Str)
  emission fn op_move(src: Str, dst: Str)
  emission fn op_mkdir(path: Str)
}
component FsOpsC provides ops: FsOps {
  provide ops {
    fn op_write(path, content) { effect write(path, content) }
    fn op_rm(path) { effect rm(path) }
    fn op_move(src, dst) { effect move(src, dst) }
    fn op_mkdir(path) { effect mkdir(path) }
  }
}
"""


def _run(case, base):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        (str(ROOT / "src"), str(ROOT / "backends/python"))))
    result = subprocess.run(
        [sys.executable, __file__, case, str(base)], env=env, cwd=ROOT,
        text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("case", [
    "binding", "late", "unsupported", "legacy", "symlinks", "lifetime",
    "pre-effect", "leaf-open", "preimage-mkdir", "preimage-copy",
    "write-syscall", "pre-inverse", "inverse-stat", "inverse-replace",
    "rm-syscall", "move-syscall", "mkdir-syscall", "unlink-inverse",
    "rmdir-inverse",
])
def test_pinned_root_process(case, tmp_path):
    evidence = _run(case, tmp_path)
    assert evidence["case"] == case
    assert evidence["guard"] == str(ROOT / "backends/python/revl_fs_workspace.py")


def test_two_roots_have_independent_record_revert_cycles(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    # Separate actor processes, even when the relative filenames are identical.
    for base in (left, right):
        assert _run("pre-inverse", base)["cycles"] == 3
    assert (left / "moved/same.txt").read_bytes() == b"original\r\n"
    assert (right / "moved/same.txt").read_bytes() == b"original\r\n"


def _bind(root):
    from revl.fs_workspace import PINNED_ROOT_API_VERSION, bind_workspace_root
    assert PINNED_ROOT_API_VERSION == 1
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        st = os.fstat(fd)
        bind_workspace_root(fd, st.st_dev, st.st_ino, root_label=str(root))
    finally:
        os.close(fd)


def _session(base):
    from revl import compile_files
    from revl.mcp.session import Session
    source = base / "consumer.rvl"
    source.write_text(CONSUMER)
    session = Session()
    session.load(compile_files([str(source)]), record=True)
    import emit
    import runtime
    assert Path(emit.__file__).resolve() == ROOT / "backends/python/emit.py"
    assert Path(runtime.__file__).resolve() == ROOT / "backends/python/runtime.py"
    return session


def _entries(session):
    return [entry for frame in session._owner._registry
            for entry in frame._transactional
            if not entry.discharged and not entry.replayed]


@contextmanager
def _barrier(monkeypatch, name, predicate, replace_root):
    """Suspend immediately before a real syscall while the adversary swaps."""
    original = getattr(os, name)
    reached = threading.Event()
    released = threading.Event()
    failures = []

    def swap():
        try:
            assert reached.wait(10), f"{name} boundary was not reached"
            replace_root()
        except BaseException as exc:
            failures.append(exc)
        finally:
            released.set()

    def paused(*args, **kwargs):
        if not reached.is_set() and predicate(*args, **kwargs):
            reached.set()
            assert released.wait(10), f"{name} barrier timed out"
        return original(*args, **kwargs)

    thread = threading.Thread(target=swap)
    with monkeypatch.context() as patch:
        patch.setattr(os, name, paused)
        thread.start()
        try:
            yield
        finally:
            thread.join(12)
            assert not thread.is_alive()
    assert reached.is_set(), f"{name} boundary was not exercised"
    assert not failures, failures


def _snapshot(root):
    return {str(p.relative_to(root)): (
        "dir" if p.is_dir() else p.read_bytes(),
        p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns)
        for p in root.rglob("*")}


def _race(case, base, ws):
    root = base / "root"
    root.mkdir()
    (root / "same.txt").write_bytes(b"original\r\n")
    (root / "only-original").mkdir()
    moved = base / "moved"
    _bind(root)
    replacements = []

    def replace_root():
        root.rename(moved)
        root.mkdir()
        (root / "same.txt").write_bytes(b"replacement")
        (root / "only-original").write_bytes(b"not a directory")
        (root / "only-replacement").mkdir()
        for name in ws.SIDECAR_KINDS.values():
            (root / name).mkdir()
            (root / name / "do-not-touch").write_bytes(b"private replacement")
        replacements.append(_snapshot(root))

    monkeypatch = pytest.MonkeyPatch()
    forward = {
        "leaf-open": ("open", lambda p, flags, *a, **k:
                      p == "same.txt" and flags & os.O_RDWR),
        "preimage-mkdir": ("mkdir", lambda p, *a, **k:
                           p == ws.PREIMAGE_DIRNAME),
        "preimage-copy": ("open", lambda p, flags, *a, **k:
                          str(p).startswith("pre-") and flags & os.O_CREAT),
        "write-syscall": ("ftruncate", lambda *a, **k: True),
        "rm-syscall": ("replace", lambda *a, **k: True),
        "move-syscall": ("replace", lambda *a, **k: True),
        "mkdir-syscall": ("mkdir", lambda p, *a, **k: p == "new-dir"),
    }
    inverse = {
        "inverse-stat": ("stat", lambda p, *a, **k: str(p).startswith("pre-")),
        "inverse-replace": ("replace", lambda *a, **k: True),
        "unlink-inverse": ("unlink", lambda p, *a, **k: p == "created"),
        "rmdir-inverse": ("rmdir", lambda p, *a, **k: p == "new-dir"),
    }
    try:
        if case == "preimage-copy":
            # Exercise the real pread/write fallback even on APFS.
            monkeypatch.setattr(ws, "_clone_from_fd", lambda *a: False)
        for cycle in range(3):
            session = _session(base)
            if cycle == 0 and case == "pre-effect":
                replace_root()

            def call():
                if case == "rm-syscall":
                    session.call("ops", "op_rm", [str(root / "same.txt")])
                elif case == "move-syscall":
                    session.call("ops", "op_move",
                                 [str(root / "same.txt"), str(root / "dest.txt")])
                elif case in ("mkdir-syscall", "rmdir-inverse"):
                    session.call("ops", "op_mkdir", [str(root / "new-dir")])
                elif case == "unlink-inverse":
                    session.call("ops", "op_write", [str(root / "created"), "new"])
                else:
                    session.call("ops", "op_write",
                                 [str(root / "same.txt"), f"changed-{cycle}"])

            if cycle == 0 and case in forward:
                name, predicate = forward[case]
                with _barrier(monkeypatch, name, predicate, replace_root):
                    call()
            else:
                call()
            entries = _entries(session)
            assert len(entries) == 1
            assert entries[0].frame.name == "FsOpsC"
            physical = moved if moved.exists() else root
            if case not in ("rm-syscall", "move-syscall", "mkdir-syscall",
                            "rmdir-inverse", "unlink-inverse"):
                session.call("ops", "op_write",
                             [str(root / "same.txt"), f"newest-{cycle}"])
                entries = _entries(session)
                assert len(entries) == 2
                snapshots = [
                    (physical / Path(e.witness["preimage"]).relative_to(root)).read_bytes()
                    for e in entries]
                assert snapshots == [b"original\r\n", f"changed-{cycle}".encode()]
                assert (physical / "same.txt").read_bytes() == f"newest-{cycle}".encode()

            if cycle == 0 and case == "pre-inverse":
                replace_root()
            if cycle == 0 and case in inverse:
                name, predicate = inverse[case]
                with _barrier(monkeypatch, name, predicate, replace_root):
                    report = session.abort()
            else:
                report = session.abort()
            assert report["aborted"] and report["noResidue"], report
            assert all(e.replayed and not e.discharged for e in entries)
            assert replacements, "the adversary never replaced the root"
            assert (moved / "same.txt").read_bytes() == b"original\r\n"
            assert not (moved / "created").exists()
            assert not (moved / "dest.txt").exists()
            assert not (moved / "new-dir").exists()
            for name in ws.SIDECAR_KINDS.values():
                side = moved / name
                assert not side.exists() or not list(side.iterdir())
            assert ws.is_dir_confined(str(root / "only-original"))
            assert ws.lexists_confined(str(root / "only-original"))
            assert not ws.lexists_confined(str(root / "only-replacement"))
            assert not ws.is_dir_confined(str(root / "only-replacement"))
            assert _snapshot(root) == replacements[0]
            os.environ[ws.WORKSPACE_ENV] = str(base / "unrelated-environment")
        return {"cycles": 3}
    finally:
        monkeypatch.undo()


def _binding_cases(case, base, ws):
    from revl.fs_workspace import bind_workspace_root, ConfinementError
    root = base / "root"
    root.mkdir()
    other = base / "other"
    other.mkdir()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    st = os.fstat(fd)

    def bind(**overrides):
        args = dict(root_fd=fd, expected_dev=st.st_dev,
                    expected_ino=st.st_ino, root_label=str(root))
        bind_workspace_root(**(args | overrides))

    try:
        if case == "binding":
            invalid = [
                ({"root_fd": -1}, "EINVAL"),
                ({"root_fd": 10**8}, "EBADF"),
                ({"root_fd": 10**100}, "EINVAL"),
                ({"root_fd": "3"}, "EINVAL"),
                ({"expected_dev": st.st_dev + 1}, "EIDENTITY"),
                ({"expected_ino": st.st_ino + 1}, "EIDENTITY"),
                ({"root_label": str(other)}, "EIDENTITY"),
                ({"root_label": "relative"}, "EINVAL"),
                ({"root_label": str(root) + "/.."}, "EINVAL"),
                ({"root_label": str(root) + "\x00"}, "EINVAL"),
            ]
            file = root / "file"
            file.write_bytes(b"untouched")
            with file.open() as handle:
                fst = os.fstat(handle.fileno())
                invalid.append((dict(root_fd=handle.fileno(),
                                     expected_dev=fst.st_dev, expected_ino=fst.st_ino),
                                "EIDENTITY"))
                for args, code in invalid:
                    with pytest.raises(ConfinementError) as exc:
                        bind(**args)
                    assert exc.value.code == code
            with pytest.MonkeyPatch.context() as patch:
                duplicate = os.dup
                copies = []
                def track(fd):
                    result = duplicate(fd)
                    copies.append(result)
                    return result
                patch.setattr(os, "dup", track)
                with pytest.raises(ConfinementError, match="EIDENTITY"):
                    bind(expected_ino=st.st_ino + 1)
                with pytest.raises(OSError):
                    os.fstat(copies[-1])
                bind()
                assert not os.get_inheritable(copies[-1])
            with pytest.raises(ConfinementError, match="EBOUND"):
                bind()
            with pytest.raises(ConfinementError, match="EBOUND"):
                _bind(other)
            original_fd = fd
            os.close(fd)
            fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
            # Deliberately reuse the caller's old descriptor number.
            assert fd == original_fd
            assert ws.workspace_root() == str(root)
            ws.mkdir_confined(str(root / "still-original"))
            assert (root / "still-original").is_dir()
            assert not (other / "still-original").exists()
            assert bind_workspace_root is ws.bind_workspace_root
        elif case == "late":
            os.environ[ws.WORKSPACE_ENV] = str(root)
            ws.resolve_within("first-use")
            with pytest.raises(ConfinementError, match="EBOUND"):
                bind()
        elif case == "unsupported":
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(ws, "_O_NOFOLLOW", 0)
                with pytest.raises(ConfinementError, match="ENOTSUP"):
                    bind()
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(os, "supports_dir_fd", set())
                with pytest.raises(ConfinementError, match="ENOTSUP"):
                    bind()
            bind()  # Refused attempts must not leave partial binding state.
        elif case == "legacy":
            os.environ[ws.WORKSPACE_ENV] = str(root)
            assert ws.resolve_within("file") == str(root / "file")
            os.environ[ws.WORKSPACE_ENV] = str(other)
            assert ws.resolve_within("file") == str(other / "file")
        elif case == "symlinks":
            bind()
            (root / "link").symlink_to(other, target_is_directory=True)
            (root / "inside").symlink_to(root, target_is_directory=True)
            for path in ("relative", str(other / "escape"),
                         str(root / "../other/escape"), str(root / "link/escape"),
                         str(root / "inside/file")):
                with pytest.raises(ws.FsOpError):
                    ws.open_confined_write(path)
            (root / ws.PREIMAGE_DIRNAME).symlink_to(other, target_is_directory=True)
            with pytest.raises(ws.FsOpError):
                ws.preimage_dir()
            for kind in ws.SIDECAR_KINDS:
                with pytest.raises(ws.FsOpError):
                    ws.resolve_sidecar(str(other / "donor"), kind)
            directory = root / "directory"
            directory.mkdir()
            target = ws.resolve_within(str(directory / "file"))
            def swap_directory():
                directory.rename(root / "old-directory")
                directory.symlink_to(other, target_is_directory=True)
            with pytest.MonkeyPatch.context() as patch:
                with _barrier(patch, "open",
                              lambda p, *a, **k: p == "directory",
                              swap_directory):
                    with pytest.raises(ws.FsOpError):
                        ws.open_confined_write(target)
            assert not list(other.iterdir())
        elif case == "lifetime":
            bind()
            file = root / "same.txt"
            file.write_bytes(b"before")
            session = _session(base)
            session.call("ops", "op_write", [str(file), "stranded"])
            entries = _entries(session)
            assert len(entries) == 1 and not entries[0].discharged
            # Normal interpreter exit, deliberately no abort/unload/commit.
            return {"stranded": entries[0].witness["preimage"]}
        return {}
    finally:
        os.close(fd)


def test_process_exit_does_not_discharge_or_recover(tmp_path):
    result = _run("lifetime", tmp_path)
    assert (tmp_path / "root/same.txt").read_bytes() == b"stranded"
    assert Path(result["stranded"]).read_bytes() == b"before"


if __name__ == "__main__":
    case, base = sys.argv[1], Path(sys.argv[2]).resolve()
    # The public API must bootstrap the exact backend without a prior Session.
    from revl.fs_workspace import bind_workspace_root
    import revl_fs_workspace as ws
    assert bind_workspace_root is ws.bind_workspace_root
    if case in ("binding", "late", "unsupported", "legacy", "symlinks", "lifetime"):
        result = _binding_cases(case, base, ws)
    else:
        result = _race(case, base, ws)
    print(json.dumps(dict(result, case=case, guard=str(Path(ws.__file__).resolve()))))
