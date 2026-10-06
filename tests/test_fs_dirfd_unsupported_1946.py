"""Issue #1946: a host whose `os` cannot walk with directory fds (Windows)
refuses a witnessed fs mutation EARLY and LEGIBLY, instead of mislabelling the
platform's own limitation as a lost race.

`stdlib/fs.rvl`'s four witnessed mutations lower to `@py` bodies that reach the
filesystem only through `backends/python/revl_fs_workspace.py`. The guard there
confines every mutation by walking from the workspace root to the target's
parent ONE COMPONENT AT A TIME through directory descriptors, with
`O_DIRECTORY`/`O_NOFOLLOW`. That walk IS the confinement — it is what closes the
window between the membership check and the syscall — so a host that cannot
perform it cannot honestly host a witnessed mutation. Windows is that host:
`os.supports_dir_fd` is empty, `O_DIRECTORY`/`O_NOFOLLOW` do not exist, and a
directory cannot be opened as a descriptor at all.

Before #1946 the unpinned path had no capability check, so the first syscall
failed instead: `os.open(root, O_RDONLY | O_DIRECTORY)` came back `EACCES` and
`open_confined_write`'s generic `OSError` branch reported it as

    Err(EOUTSIDE: the path to the write target changed under the confinement
    check (Permission denied))

— a lost race that never happened. `rm`/`mkdir`/`move` had the same shape (the
sidecar directories are opened through the same root descriptor), and the issue
also reports `[Errno 2] No such file or directory` from the same walk.

The fix is one predicate (`revl_fs_workspace.dirfd_walk_supported()`) consulted
by `_root_dirfd` — the descriptor every mutation needs before its first syscall
— so all four refuse with the `ENOTSUP` refusal the PINNED binder
(`bind_workspace_root`) already raised, before any partial work.

One follow-up defect in that predicate is pinned here too. The walk's capability
is a property of the HOST, so the predicate must read the function objects
CPython registered in `os.supports_follow_symlinks` — never the `os` module
attribute of the moment. `tests/test_fs_pinned_root.py`'s boundary barrier wraps
`os.stat` for the duration of a boundary, so a live read made the guard refuse
`ENOTSUP` on the runtime's own unwind and the barrier's preimage `stat` never
ran. The wrapped-stat tests below FAIL on the commit that shipped the predicate
(`a3c8e4091`, the #2020 merge) and pass once the module binds `os.stat` at
import, exactly as it already did for `_DIRFD_WALK_REQUIRED`.

# How a Windows-only failure is tested on macOS

There is no Windows here, so the host capability is SIMULATED by patching the
exact surface the guard consults, at BOTH levels:

  * the capability declarations the predicate reads: `os.supports_dir_fd`
    emptied, `os.O_DIRECTORY`/`os.O_NOFOLLOW` removed — and the module's cached
    copies zeroed, because the module reads them at import;
  * the syscall behaviour those declarations describe, so the simulation is not
    merely a flag an implementation might ignore: `dir_fd=`/`src_dir_fd=`/
    `dst_dir_fd=` raise `NotImplementedError("dir_fd unavailable on this
    platform")`, and `os.open()` on a directory raises
    `PermissionError(EACCES, "Permission denied")`.

Both are what CPython's Windows `os` actually does. The ops are driven through
the REAL `@py` bodies emitted from the REAL `stdlib/fs.rvl` (the no-cordis
harness `tests/test_fs_stdlib.py` established), so no part of the implementation
is copied into this test.

On the base commit (`origin/main`, 55f2b234d) these tests FAIL, and not with the
new refusal: the write comes back `Err(EOUTSIDE: ... (Permission denied))` and
`rm`/`mkdir`/`move` fail the same way. The evidence is in the PR body.
"""

from __future__ import annotations

import errno
import functools
import os
import stat
import sys
import types
from pathlib import Path

import pytest

from revl.compiler import compile_files

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402  (the guard under test)

# The real module, compiled once — the same IR `tests/test_fs_stdlib.py` drives.
_BASE = compile_files([str(_ROOT / "stdlib" / "fs.rvl")])

#: The four witnessed mutations issue #1946 names.
_OPS = ("write", "rm", "mkdir", "move")


# ---------------------------------------------------------------------------
# The no-cordis harness: the real `@py` bodies, emitted from stdlib/fs.rvl
# ---------------------------------------------------------------------------

def _lit(v: str) -> dict:
    return {"kind": "lit", "value": v}


def _effect(name: str, *args: str) -> dict:
    return {"step": "effect",
            "acquire": {"kind": "fn", "name": name, "args": [_lit(a) for a in args]}}


def _fs_module():
    """The emitted py module for stdlib/fs.rvl, so the REAL `write`/`rm`/
    `mkdir`/`move` `@py` bodies can be called directly. Registered in
    `sys.modules` before exec so the emitted `Ok`/`Err` dataclasses can resolve
    their annotations. The probe component names all four effects so the
    emitter cannot omit one as unreachable."""
    from revl._paths import python_backend_emitter  # noqa: PLC0415

    import copy

    emit = python_backend_emitter()
    ir = copy.deepcopy(_BASE)
    ir["components"] = [{
        "name": "Probe", "source": "fs.rvl", "config": [],
        "requires": {}, "provides": {},
        "body": [_effect("write", "target.txt", "payload"),
                 _effect("rm", "victim.txt"),
                 _effect("mkdir", "newdir"),
                 _effect("move", "src.txt", "dst.txt")],
    }]
    module = types.ModuleType("fs_dirfd_probe_mod")
    sys.modules["fs_dirfd_probe_mod"] = module
    exec(compile(emit.emit(ir), "fs_dirfd_probe_mod.py", "exec"), module.__dict__)
    return module


# ---------------------------------------------------------------------------
# The simulated host: the Windows `os` surface
# ---------------------------------------------------------------------------

@pytest.fixture
def dirfdless_host(monkeypatch):
    """This process's `os` patched to the surface a host without the dir-fd walk
    presents. Every patch is undone by monkeypatch at teardown, so the rest of
    the suite runs against the real host."""
    real_open = os.open
    real_stat = os.stat

    def _no_dirfd(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if any(kwargs.get(k) is not None
                   for k in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
                raise NotImplementedError("dir_fd unavailable on this platform")
            return fn(*args, **kwargs)
        return wrapper

    @functools.wraps(real_open)
    def _open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable on this platform")
        try:
            is_dir = stat.S_ISDIR(real_stat(path).st_mode)
        except OSError:
            is_dir = False
        if is_dir:
            # CPython's `os.open` on Windows cannot open a directory at all.
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _open)
    for name in ("stat", "mkdir", "unlink", "rmdir", "rename", "replace"):
        monkeypatch.setattr(os, name, _no_dirfd(getattr(os, name)))

    # ...and the declarations those behaviours match.
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    # the module reads the two flags at import, so its cached copies are what
    # the walk actually uses — zero them rather than only the `os` attributes.
    monkeypatch.setattr(ws, "_O_DIRECTORY", 0)
    monkeypatch.setattr(ws, "_O_NOFOLLOW", 0)

    # On the base commit the predicate does not exist yet. Guarding the
    # self-check is what makes the base failure BEHAVIOURAL rather than a
    # collection-time AttributeError: the ops still run, reach the filesystem,
    # and fail on the OBSERVED `Err(EOUTSIDE)` / `Err(EACCES)` — the symptom
    # #1946 reports. On the fixed tree the self-check always runs.
    if hasattr(ws, "dirfd_walk_supported"):
        assert ws.dirfd_walk_supported() is False, (
            "the simulation did not convince the guard's own predicate; the "
            "test would be proving nothing")
    return monkeypatch


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


def _seed(root: Path, op: str) -> None:
    """The pre-existing state each op needs to be able to do real work."""
    if op == "rm":
        (root / "victim.txt").write_text("keep me", encoding="utf-8")
    if op == "move":
        (root / "src.txt").write_text("keep me", encoding="utf-8")


def _invoke(mod, op: str):
    if op == "write":
        return mod.write("target.txt", "payload")
    if op == "rm":
        return mod.rm("victim.txt")
    if op == "mkdir":
        return mod.mkdir("newdir")
    return mod.move("src.txt", "dst.txt")


def _assert_no_partial_work(root: Path) -> None:
    """Refuse EARLY means refuse before anything on disk moved: no target, no
    directory, no renamed source, and no sidecar directory created for a
    reversal that will never be registered."""
    assert not (root / "target.txt").exists()
    assert not (root / "newdir").exists()
    assert not (root / "dst.txt").exists()
    assert not (root / ws.GARBAGE_DIRNAME).exists(), \
        "a garbage sidecar directory was created before the refusal"
    assert not (root / ws.PREIMAGE_DIRNAME).exists(), \
        "a preimage sidecar directory was created before the refusal"
    for name in ("victim.txt", "src.txt"):
        if (root / name).exists():
            assert (root / name).read_text(encoding="utf-8") == "keep me"


# ---------------------------------------------------------------------------
# The regression: every witnessed mutation refuses, with a named refusal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", _OPS)
def test_a_dirfdless_host_refuses_each_witnessed_mutation_with_enotsup(
        workspace, dirfdless_host, op):
    _seed(workspace, op)
    mod = _fs_module()
    assert hasattr(mod, op), f"the emitter did not emit `{op}`"

    result = _invoke(mod, op)

    assert isinstance(result, mod.Err), (
        f"`{op}` returned {result!r}: a host that cannot walk with directory "
        f"fds cannot confine this mutation, so it must refuse, never succeed "
        f"unconfined")
    err = result.value
    assert err["code"] == "ENOTSUP", err
    assert "not supported on this platform" in err["message"], err
    assert err["path"] == os.path.realpath(str(workspace)), err
    # the misleading pre-#1946 message named a lost race that never happened
    assert "changed under the confinement check" not in err["message"], err
    # ...and the refusal says WHY, so a caller on such a host can act on it
    assert "dir_fd" in err["message"] or "directory descriptors" in err["message"], err
    _assert_no_partial_work(workspace)


def test_the_refusal_does_not_mention_an_errno_the_caller_cannot_use(
        workspace, dirfdless_host):
    """The issue's ask is that the message name the PLATFORM limitation rather
    than surfacing a bare errno the caller has to decode (`[Errno 2]`, or the
    `Permission denied` a bare `EACCES` stringifies to)."""
    mod = _fs_module()
    err = mod.write("target.txt", "payload").value
    assert err["code"] == "ENOTSUP"
    assert "Permission denied" not in err["message"]
    assert "[Errno" not in err["message"]


# ---------------------------------------------------------------------------
# The boundary of the refusal: reads keep working
# ---------------------------------------------------------------------------

def test_a_dirfdless_host_still_answers_the_read_only_checks(
        workspace, dirfdless_host):
    """The refusal is on the mutations, not on the module: `resolve_within`,
    `lexists_confined` and `is_dir_confined` are name-based on the unpinned path
    and must keep answering, so a caller can still observe the workspace and
    find out what is there."""
    (workspace / "artifact.txt").write_text("v1", encoding="utf-8")
    real = os.path.realpath(str(workspace))

    assert ws.resolve_within("artifact.txt") == os.path.join(real, "artifact.txt")
    assert ws.lexists_confined(os.path.join(real, "artifact.txt")) is True
    assert ws.lexists_confined(os.path.join(real, "absent.txt")) is False
    assert ws.is_dir_confined(real) is True
    assert ws.is_dir_confined(os.path.join(real, "artifact.txt")) is False


# ---------------------------------------------------------------------------
# The predicate itself, clause by clause
# ---------------------------------------------------------------------------

def test_each_capability_the_walk_needs_can_alone_make_the_predicate_false(
        monkeypatch):
    """`dirfd_walk_supported()` is the honest host probe, not a flag: every
    capability the walk actually uses is consulted, and any one of them missing
    is enough. The clauses are the syscalls the walk performs with `dir_fd=`,
    the two flags that refuse a swapped component, and the no-follow `stat` the
    walk's leaf checks take."""
    if os.supports_dir_fd and getattr(os, "O_DIRECTORY", 0) \
            and getattr(os, "O_NOFOLLOW", 0):
        assert ws.dirfd_walk_supported() is True, \
            "this host has the walk, so the predicate must admit it"

    with monkeypatch.context() as m:
        m.setattr(os, "supports_dir_fd", set())
        assert ws.dirfd_walk_supported() is False

    with monkeypatch.context() as m:
        m.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) - {os.open})
        assert ws.dirfd_walk_supported() is False

    with monkeypatch.context() as m:
        m.setattr(ws, "_O_DIRECTORY", 0)
        assert ws.dirfd_walk_supported() is False

    with monkeypatch.context() as m:
        m.setattr(ws, "_O_NOFOLLOW", 0)
        assert ws.dirfd_walk_supported() is False

    with monkeypatch.context() as m:
        m.setattr(os, "supports_follow_symlinks", set())
        assert ws.dirfd_walk_supported() is False


# ---------------------------------------------------------------------------
# The predicate asks the HOST, not the module attribute of the moment
# ---------------------------------------------------------------------------

def _wrap_os_stat(m) -> list:
    """`os.stat` replaced by a DELEGATING wrapper — what a tracer, an audit
    shim or a test barrier does to the module attribute — while the capability
    sets stay exactly as CPython declared them. Returns the list the wrapper
    records its calls in."""
    real = os.stat
    seen: list = []

    @functools.wraps(real)
    def wrapper(*args, **kwargs):
        seen.append(args)
        return real(*args, **kwargs)

    m.setattr(os, "stat", wrapper)
    assert os.stat is wrapper
    return seen


def test_a_wrapped_os_stat_does_not_make_a_capable_host_look_incapable(
        monkeypatch):
    """The walk's capability is a property of the HOST, so the probe must read
    the function objects CPython registered in `os.supports_follow_symlinks` —
    never the `os` module attribute of the moment. Wrapping `os.stat` changes
    the attribute and leaves the set intact, so a host WITH the walk must still
    be admitted, and the probe must not CALL `os.stat` either: it asks the set
    about the capability, it does not exercise it.

    This is the CLASS, not an instance. `tests/test_fs_pinned_root.py`'s
    `_barrier` wraps `os.stat` for the duration of a boundary, so a live read
    made the guard refuse `ENOTSUP` on the runtime's own unwind
    (`session.abort` -> `runtime.drain` -> `_replay` -> `restore` ->
    `_root_dirfd`), and the preimage `stat` the barrier was waiting for never
    ran: `stat boundary was not exercised`."""
    if not ws.dirfd_walk_supported():
        pytest.skip("this host has no directory-fd walk for the probe to admit")
    real_stat = os.stat

    with monkeypatch.context() as m:
        seen = _wrap_os_stat(m)

        assert real_stat in os.supports_follow_symlinks, \
            "the capability set still names the real stat: it is untouched"
        assert ws.dirfd_walk_supported() is True
        assert seen == [], "the probe asks the set; it does not call os.stat"


def test_a_wrapped_os_stat_does_not_refuse_a_mutation(
        workspace, monkeypatch):
    """The reach, not just the predicate: `_root_dirfd()` is the descriptor
    every mutation needs before its first syscall, and the real `write` body
    must still complete while `os.stat` is wrapped. No cordis needed — this
    calls the guard and the emitted `@py` body directly."""
    monkeypatch.setattr(ws, "_pinned_root", None)
    if not ws.dirfd_walk_supported():
        pytest.skip("this host has no directory-fd walk to hand out")
    mod = _fs_module()

    with monkeypatch.context() as m:
        _wrap_os_stat(m)

        fd = ws._root_dirfd()
        try:
            assert fd >= 0
            assert stat.S_ISDIR(os.fstat(fd).st_mode)
        finally:
            os.close(fd)

        result = mod.write("target.txt", "payload")
        assert isinstance(result, mod.Ok), result
        assert (workspace / "target.txt").read_text(encoding="utf-8") == "payload"


# ---------------------------------------------------------------------------
# The pinned binder: #1946 asks for the SAME refusal
# ---------------------------------------------------------------------------

def test_the_pinned_binder_refuses_the_same_host_with_the_same_code(
        workspace, dirfdless_host, monkeypatch):
    """The issue asks for "the same `ENOTSUP` refusal the pinned binder raises",
    so the shared predicate must make the binder refuse too, with the same code
    — a caller cannot tell the two apart except by the message. The binder's
    refusal is raised before it dups anything, so nothing is retained."""
    monkeypatch.setattr(ws, "_pinned_root", None)
    monkeypatch.setattr(ws, "_workspace_used", False)

    with pytest.raises(ws.ConfinementError) as ei:
        ws.bind_workspace_root(-1, 0, 0, root_label=str(workspace))

    assert ei.value.code == "ENOTSUP"
    assert "directory-fd" in ei.value.message
