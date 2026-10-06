"""Issue #1946: a host with no directory-fd support refuses, it does not lie.

`backends/python/revl_fs_workspace.py` confines every witnessed mutation by
walking the path from the workspace root one component at a time with
`O_DIRECTORY`/`O_NOFOLLOW` and `dir_fd`. On Windows none of that exists: a
directory cannot be held open as a descriptor, `O_DIRECTORY` and `O_NOFOLLOW`
have no value, and `os.supports_dir_fd` is empty. The walk's very first call,
`_root_dirfd`'s `os.open(root, os.O_RDONLY | _O_DIRECTORY)`, therefore failed
opening a directory that plainly EXISTS, and the caller translated the
`FileNotFoundError` into `ENOENT: parent directory does not exist` naming a
directory it had just created. The harness reported the witnessed write path as
red on Windows with `[Errno 2] No such file or directory` and nothing about the
real cause (`composition:witnessed_fs`, `composition:product`).

The pinned binder already refused such a host up front with `ENOTSUP`
(`bind_workspace_root`); the unpinned witnessed path did not. Both now read one
definition, `ws._dirfd_walk_supported`, and the walk refuses with `ENOTSUP`
before its first syscall.

What this suite holds:

  * `test_unwitnessable_host_refuses_every_witnessed_op` - write, rm, mkdir,
    move and rmdir each refuse `ENOTSUP` on a host that cannot walk;
  * `test_refusal_precedes_the_first_syscall` - the refusal is raised without
    opening anything, which is what makes the platform the first thing a caller
    hears rather than an errno about a path that exists;
  * `test_base_symptom_was_an_enoent_about_a_directory_that_exists` - the same
    simulated host with the capability check disabled (i.e. the pre-#1946
    control flow) answers `ENOENT` "parent directory does not exist" for a
    write into a directory that exists. That is the fail-on-base evidence: the
    base symptom was a different, misleading code;
  * `test_binder_and_walk_agree_on_what_this_host_supports` - every leg of the
    platform report refuses BOTH surfaces, so the binder's inline copy of the
    check cannot come back;
  * `test_a_supporting_host_is_unaffected` - on the real host all five ops
    still run.

The unsupported host is simulated the way `tests/test_fs_pinned_root.py` already
does it - by patching the PRODUCTION predicates (`_O_DIRECTORY`, `_O_NOFOLLOW`,
`os.supports_dir_fd`), never by a test-only switch - plus a faithful `os.open`
for the one behaviour those predicates describe: Windows' `_wopen` cannot open
a directory at all.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An unpinned workspace root, configured the way a run configures it.

    `_pinned_root` is forced to None so the unpinned path is what runs whatever
    order the suite collects this file in: the pinned path is a different walk
    with a different first syscall, and #1946's symptom is the unpinned one.
    """
    root = tmp_path / "root"
    (root / "nested").mkdir(parents=True)
    monkeypatch.setattr(ws, "_pinned_root", None)
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


def _unwitnessable_host(monkeypatch):
    """Pretend this process runs on Windows, at the level the walk asks about.

    `os.supports_dir_fd` empty and the two `O_` flags valueless is what the
    platform reports; the `os.open` below is what it does. CPython's `_wopen`
    cannot open a directory, and `dir_fd` is unavailable there - both are the
    errno and the message the issue recorded.
    """
    real_open = os.open
    monkeypatch.setattr(ws, "_O_DIRECTORY", 0)
    monkeypatch.setattr(ws, "_O_NOFOLLOW", 0)
    monkeypatch.setattr(os, "supports_dir_fd", set())

    def windows_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable on this platform")
        if os.path.isdir(path):
            raise FileNotFoundError(
                errno.ENOENT, "No such file or directory", str(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", windows_open)


#: One entry per witnessed mutation `stdlib/fs.rvl` offers, each aimed at a
#: path whose parent EXISTS - so nothing below can be explained by a missing
#: directory.
_WITNESSED_OPS = {
    "write": lambda root: ws.open_confined_write(str(root / "nested" / "file")),
    "rm": lambda root: ws.remove_confined(str(root / "nested" / "missing")),
    "mkdir": lambda root: ws.mkdir_confined(str(root / "nested" / "made")),
    "move": lambda root: ws.replace_confined(
        str(root / "nested" / "from"), str(root / "nested" / "to")),
    "rmdir": lambda root: ws.rmdir_confined(str(root / "nested" / "made")),
}


@pytest.mark.parametrize("op", sorted(_WITNESSED_OPS))
def test_unwitnessable_host_refuses_every_witnessed_op(
        workspace, monkeypatch, op):
    _unwitnessable_host(monkeypatch)
    with pytest.raises(ws.ConfinementError) as refused:
        _WITNESSED_OPS[op](workspace)
    assert refused.value.code == "ENOTSUP", refused.value
    assert "directory-fd" in refused.value.message
    # An ordinary FsOpError too, so an `@py` body turns it into an `Err` on the
    # forward path (which registers no inverse) rather than failing the call.
    assert isinstance(refused.value, ws.FsOpError)


def test_refusal_precedes_the_first_syscall(workspace, monkeypatch):
    """`ENOTSUP` is decided from the platform's own capability report, before
    the walk opens the root - so it is not a translation of an errno the walk
    produced against a path that exists."""
    _unwitnessable_host(monkeypatch)
    opened = []
    windows_open = os.open
    monkeypatch.setattr(
        os, "open",
        lambda *a, **k: (opened.append(a), windows_open(*a, **k))[1])
    with pytest.raises(ws.ConfinementError) as refused:
        ws.open_confined_write(str(workspace / "nested" / "file"))
    assert refused.value.code == "ENOTSUP"
    assert opened == []
    # The READ half of the surface mutates nothing and keeps answering on such
    # a host, so the refusal is scoped to the ops that need the walk.
    assert not ws.lexists_confined(str(workspace / "nested" / "file"))
    assert ws.is_dir_confined(str(workspace / "nested"))


@pytest.mark.parametrize("op", ["write", "mkdir"])
def test_base_symptom_was_an_enoent_about_a_directory_that_exists(
        workspace, monkeypatch, op):
    """Fail-on-base, in one process: the pre-#1946 control flow on this host.

    Disabling the capability check is what the base code did unconditionally
    (`_dirfd_walk_supported`, and the call in `_open_dirfd`, are the whole
    change), so the op below walks straight into the host's refusal to open a
    directory and reports `ENOENT: parent directory does not exist` for
    `nested/`, which exists. The same call, with the check in place, is
    `ENOTSUP`.
    """
    _unwitnessable_host(monkeypatch)
    with pytest.MonkeyPatch.context() as base:
        base.setattr(ws, "_dirfd_walk_supported", lambda: True)
        with pytest.raises(ws.FsOpError) as misleading:
            _WITNESSED_OPS[op](workspace)
    assert misleading.value.code == "ENOENT", misleading.value
    # The host's own `[Errno 2] No such file or directory`, translated by the
    # caller into a claim about a directory that exists.
    assert misleading.value.message == "parent directory does not exist"
    assert (workspace / "nested").is_dir()
    with pytest.raises(ws.ConfinementError) as refused:
        _WITNESSED_OPS[op](workspace)
    assert refused.value.code == "ENOTSUP"
    assert refused.value.code != misleading.value.code
    assert not (workspace / "nested" / "file").exists()
    assert not (workspace / "nested" / "made").exists()


#: One entry per leg of the platform report `_dirfd_walk_supported` reads, so a
#: host that fails any single one is refused by both surfaces.
_CAPABILITY_LEGS = (
    ("no_follow_flag", lambda: {"_O_NOFOLLOW": 0}),
    ("no_directory_flag", lambda: {"_O_DIRECTORY": 0}),
    ("no_dir_fd_open",
     lambda: {"supports_dir_fd": os.supports_dir_fd - {os.open}}),
    ("no_dir_fd_stat",
     lambda: {"supports_dir_fd": os.supports_dir_fd - {os.stat}}),
    ("no_dir_fd_mkdir",
     lambda: {"supports_dir_fd": os.supports_dir_fd - {os.mkdir}}),
    ("no_follow_symlinks",
     lambda: {"supports_follow_symlinks":
              os.supports_follow_symlinks - {os.stat}}),
    ("no_fd_utime", lambda: {"supports_fd": os.supports_fd - {os.utime}}),
    ("no_pread", lambda: {"has_pread": False}),
)


def _drop_leg(monkeypatch, leg):
    """Remove one capability from the report, on whichever host this runs.

    `hasattr(os, "pread")` is asked of the module, so the leg that names it is
    dropped by hiding the attribute rather than by an equivalent we would have
    to invent for a host that lacks it.
    """
    if "has_pread" in leg:
        monkeypatch.delattr(os, "pread", raising=False)
        return
    if ("supports_dir_fd" in leg or "supports_fd" in leg
            or "supports_follow_symlinks" in leg):
        for name, value in leg.items():
            monkeypatch.setattr(os, name, value)
        return
    for name, value in leg.items():
        monkeypatch.setattr(ws, name, value)


@pytest.mark.parametrize("name,leg", _CAPABILITY_LEGS,
                         ids=[n for n, _ in _CAPABILITY_LEGS])
def test_binder_and_walk_agree_on_what_this_host_supports(
        workspace, monkeypatch, name, leg):
    """Every leg of the platform report refuses BOTH surfaces.

    This list used to live only in `bind_workspace_root`. It is one definition
    now, and neither surface may accept a host the other refuses.
    """
    root = workspace
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    st = os.fstat(fd)
    try:
        with pytest.MonkeyPatch.context() as binding:
            _drop_leg(binding, leg())
            assert not ws._dirfd_walk_supported(), name
            # Binding is once-per-process; this asserts the capability verdict
            # alone, so the recorded use is put back for the assertion.
            binding.setattr(ws, "_workspace_used", False)
            with pytest.raises(ws.ConfinementError) as refused:
                ws.bind_workspace_root(fd, st.st_dev, st.st_ino,
                                       root_label=str(root))
            assert refused.value.code == "ENOTSUP", (name, refused.value)
        with pytest.MonkeyPatch.context() as walk:
            _drop_leg(walk, leg())
            with pytest.raises(ws.ConfinementError) as refused:
                ws.open_confined_write(str(root / "nested" / "file"))
            assert refused.value.code == "ENOTSUP", (name, refused.value)
    finally:
        os.close(fd)


def test_a_supporting_host_is_unaffected(workspace):
    """On a host that can walk, the guard is invisible: all five ops run."""
    assert ws._dirfd_walk_supported()
    root = workspace
    handle = ws.open_confined_write(str(root / "nested" / "file"))
    try:
        os.write(handle.fd, b"witnessed")
    finally:
        ws.close_handle(handle)
    assert (root / "nested" / "file").read_bytes() == b"witnessed"
    ws.mkdir_confined(str(root / "nested" / "made"))
    assert (root / "nested" / "made").is_dir()
    ws.replace_confined(str(root / "nested" / "file"),
                        str(root / "nested" / "moved"))
    assert (root / "nested" / "moved").read_bytes() == b"witnessed"
    assert not (root / "nested" / "file").exists()
    ws.remove_confined(str(root / "nested" / "moved"))
    assert not (root / "nested" / "moved").exists()
    ws.remove_confined(str(root / "nested" / "missing"))
    ws.rmdir_confined(str(root / "nested" / "made"))
    assert not (root / "nested" / "made").exists()
