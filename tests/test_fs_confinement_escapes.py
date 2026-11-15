"""Confinement-escape regressions for the witnessed `stdlib/fs.rvl` slice
(backends/python/revl_fs_workspace.py).

Every test here is an executed escape from an adversarial audit of the
workspace jail. `tests/test_fs_stdlib.py` proves the three confinement musts on
the paths that DO route through `resolve_within`; this suite proves the paths
that did not, and pins the single-choke-point claim so a fourth path family
cannot be added silently.

The families the audit found reaching a syscall around the guard:

  * the INVERSE SOURCE endpoints — `restore`'s `preimage`, `unrm`'s `garbage`.
    `os.replace` is a RENAME, so an unchecked source is a *steal-and-destroy*
    primitive: the named file is removed from where it lived and appears inside
    the workspace. Guarded now by `resolve_sidecar` (family `inverse-source`),
    which admits only a sidecar this workspace itself produced.
  * the SIDECAR DIRECTORIES — `garbage_dir()`/`preimage_dir()` used
    `os.makedirs(..., exist_ok=True)`, whose existence check FOLLOWS symlinks,
    then returned the unresolved path. One pre-existing symlink named
    `.revl-fs-garbage` / `.revl-fs-preimage` inside the workspace redirected
    every preimage snapshot and every parked `rm` outside the root.
  * HARDLINK aliases — `realpath` cannot see through a hardlink, so an inside
    name linked to an outside inode let `open(target, "w")` write THROUGH the
    boundary, and the undo's rename then replaced the NAME (breaking the link)
    so the inside file looked reverted while the outside file kept the bytes.
  * the CHECK-TO-SYSCALL window — the guard resolved a path and then re-walked
    it by name at the syscall, so a concurrent writer in the workspace could
    swap a component to a symlink in between.

Every escape is demonstrated against a canary file in a directory that is
explicitly OUTSIDE the declared workspace root (both under pytest's `tmp_path`),
never against a real path.
"""

from __future__ import annotations

import copy
import errno
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from revl.compiler import compile_files

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402

_BASE = compile_files([str(_ROOT / "stdlib" / "fs.rvl")])

_CANARY = "TOP SECRET canary OUTSIDE the workspace root\n"


def _component() -> dict:
    return {"name": "Probe", "source": "fs.rvl", "config": [],
            "requires": {}, "provides": {}, "body": []}


def _fs_module():
    """The emitted py module for stdlib/fs.rvl, so the real `write`/`rm`/
    `restore`/`unrm` `@py` bodies can be driven directly (the shape
    tests/test_fs_stdlib.py uses)."""
    from revl._paths import python_backend_emitter  # noqa: PLC0415
    emit = python_backend_emitter()
    ir = copy.deepcopy(_BASE)
    ir["components"] = [_component()]
    module = types.ModuleType("fs_escape_mod")
    sys.modules["fs_escape_mod"] = module
    exec(compile(emit.emit(ir), "fs_escape_mod.py", "exec"), module.__dict__)
    return module


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "artifact.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


@pytest.fixture
def outside(tmp_path):
    """A directory that is explicitly NOT under the workspace root, holding one
    canary file. Everything here is under pytest's tmp_path."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "canary.txt").write_text(_CANARY, encoding="utf-8")
    return d


# ===========================================================================
# F1 (CRITICAL) — the inverse SOURCE endpoint
# ===========================================================================

def test_restore_cannot_steal_a_file_from_outside_the_root(workspace, outside):
    """`restore` confined only its TARGET. Its source (`preimage`) went to
    `os.replace` unchecked, and `os.replace` is a rename: the outside canary was
    REMOVED from its directory and reappeared as an inside file."""
    mod = _fs_module()
    canary = outside / "canary.txt"
    loot = workspace / "loot.txt"

    witness = {"path": str(loot), "preimage": str(canary), "created": False}
    with pytest.raises(ws.ConfinementError) as ei:
        mod.restore(witness)
    assert ei.value.code == "EOUTSIDE"

    assert canary.exists(), "restore STOLE a file from outside the workspace root"
    assert canary.read_text(encoding="utf-8") == _CANARY
    assert not loot.exists(), "restore planted an outside file inside the root"
    assert sorted(p.name for p in outside.iterdir()) == ["canary.txt"]


def test_unrm_cannot_steal_a_file_from_outside_the_root(workspace, outside):
    """The same hole in `unrm`, whose source endpoint is `garbage`."""
    mod = _fs_module()
    canary = outside / "canary.txt"
    loot = workspace / "loot.txt"

    witness = {"path": str(loot), "garbage": str(canary)}
    with pytest.raises(ws.ConfinementError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EOUTSIDE"

    assert canary.exists(), "unrm STOLE a file from outside the workspace root"
    assert canary.read_text(encoding="utf-8") == _CANARY
    assert not loot.exists()


def test_an_inverse_source_must_be_a_sidecar_this_workspace_produced(workspace):
    """Confinement to the root is not enough on its own: an inverse's source is
    a sidecar the workspace machinery created, so an ordinary inside file named
    as a `preimage` is refused too. That keeps the capability-free inverses from
    being a general rename primitive even within the boundary."""
    mod = _fs_module()
    (workspace / "notes.txt").write_text("inside, but not a sidecar", encoding="utf-8")

    with pytest.raises(ws.ConfinementError) as ei:
        mod.restore({"path": str(workspace / "victim.txt"),
                     "preimage": str(workspace / "notes.txt"),
                     "created": False})
    assert ei.value.code == "EOUTSIDE"
    assert (workspace / "notes.txt").exists()
    assert not (workspace / "victim.txt").exists()


# ===========================================================================
# F2 (HIGH) — the sidecar DIRECTORIES followed a symlink out
# ===========================================================================

def test_sidecar_symlink_cannot_exfiltrate_a_workspace_file(workspace, outside):
    """`preimage_dir()` did `makedirs(exist_ok=True)` (whose check follows
    links) and returned the path UNRESOLVED, so a pre-existing
    `.revl-fs-preimage` symlink sent every preimage snapshot outside the root:
    the contents of an inside file land in an attacker-chosen directory."""
    (workspace / ws.PREIMAGE_DIRNAME).symlink_to(outside)
    mod = _fs_module()

    result = mod.write("artifact.txt", "v2")
    assert isinstance(result, mod.Err), "a redirected sidecar must refuse the write"
    assert result.value["code"] == "EOUTSIDE"

    leaked = [p.name for p in outside.iterdir() if p.name != "canary.txt"]
    assert leaked == [], f"workspace contents exfiltrated outside the root: {leaked}"
    assert (workspace / "artifact.txt").read_text(encoding="utf-8") == "v1", \
        "a refused write must not mutate the target"


def test_sidecar_symlink_cannot_plant_attacker_bytes_outside(workspace, outside):
    """The write-then-rm variant: fully attacker-controlled bytes are written to
    an inside name, then `rm` parks that file in the garbage dir — which the
    `.revl-fs-garbage` symlink pointed outside the root."""
    (workspace / ws.GARBAGE_DIRNAME).symlink_to(outside)
    mod = _fs_module()

    written = mod.write("payload.txt", "ATTACKER CONTROLLED BYTES")
    assert isinstance(written, mod.Ok)
    removed = mod.rm("payload.txt")
    assert isinstance(removed, mod.Err), "a redirected garbage dir must refuse the rm"
    assert removed.value["code"] == "EOUTSIDE"

    planted = [p.name for p in outside.iterdir() if p.name != "canary.txt"]
    assert planted == [], f"attacker bytes planted outside the root: {planted}"


# ===========================================================================
# F3 (HIGH) — hardlink write-through, and the undo that does not revert it
# ===========================================================================

def test_write_refuses_a_hardlink_alias_out_of_the_root(workspace, outside):
    """`realpath` cannot see through a hardlink, so an inside name linked to an
    outside inode let the forward write mutate the OUTSIDE file. The honest
    control is refusal: writing through the fd does not help — the fd names the
    same shared inode."""
    canary = outside / "canary.txt"
    alias = workspace / "alias.txt"
    os.link(canary, alias)
    assert os.stat(alias).st_nlink == 2

    mod = _fs_module()
    result = mod.write("alias.txt", "pwned")

    assert isinstance(result, mod.Err), "a hardlinked target must be refused"
    assert result.value["code"] == "EMULTILINK"
    assert canary.read_text(encoding="utf-8") == _CANARY, \
        "the write went THROUGH the hardlink and mutated the outside file"
    assert alias.read_text(encoding="utf-8") == _CANARY


def test_hardlink_undo_would_leave_unenumerated_outside_residue(workspace, outside):
    """The second half of F3: even if the forward write were allowed, the undo's
    `os.replace(preimage, target)` replaces the NAME and breaks the link, so the
    inside file reads as reverted while the outside file keeps the attacker's
    bytes — silent residue the WAL discharge descriptors report as clean. The
    refusal above is what makes this unreachable: nothing is registered, so
    there is no undo to mislead."""
    canary = outside / "canary.txt"
    alias = workspace / "alias.txt"
    os.link(canary, alias)

    mod = _fs_module()
    result = mod.write("alias.txt", "pwned")
    assert isinstance(result, mod.Err)

    # Ok-conditional registration: a refused mutation registers no inverse, so
    # there is no witness at all, and both names still hold the original bytes.
    assert canary.read_text(encoding="utf-8") == _CANARY
    assert alias.read_text(encoding="utf-8") == _CANARY
    assert os.stat(canary).st_ino == os.stat(alias).st_ino


# ===========================================================================
# F4 (MEDIUM) — the check-to-syscall window
# ===========================================================================

def test_a_concurrent_writer_cannot_divert_a_write_out_of_the_root(workspace, outside):
    """The guard resolved a path and then re-walked it BY NAME at the syscall.
    A competing writer in the workspace that swaps the leaf for a symlink in
    that window diverted the write outside the root. Closed by opening the file
    through directory fds walked down from the root with `O_NOFOLLOW`, so the
    bytes can only ever reach the inode the check admitted."""
    target = workspace / "racy.txt"
    target.write_text("v1", encoding="utf-8")
    victim = outside / "raced.txt"
    stop = threading.Event()

    def swapper():
        while not stop.is_set():
            try:
                os.unlink(target)
            except OSError:
                pass
            try:
                os.symlink(victim, target)
            except OSError:
                pass
            try:
                os.unlink(target)
            except OSError:
                pass
            try:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write("v1")
            except OSError:
                pass

    mod = _fs_module()
    thread = threading.Thread(target=swapper, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            # The swapper UNLINKS the target on purpose, so a write regularly
            # lands in the window where the leaf is gone. The call carries no
            # `except` on purpose (item 431(b)): the guard's entry points are
            # total (item 422 F6) and a parted name is a typed `Err(ERACE)`, so
            # a raw exception escaping here is a real regression rather than a
            # transient. It briefly needed `except OSError`, because in CI a
            # `FileNotFoundError` escaped the undo-snapshot read; that path
            # refuses with `ESNAPSHOT` now instead of raising.
            result = mod.write("racy.txt", "RACED PAYLOAD")
            assert isinstance(result, (mod.Ok, mod.Err))
            if victim.exists():
                break
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not victim.exists(), \
        "a concurrent writer diverted a witnessed write outside the workspace root"
    leaked = [p.name for p in outside.iterdir() if p.name != "canary.txt"]
    assert leaked == [], f"the race leaked outside the root: {leaked}"


# ===========================================================================
# item 422 F6: the guard's entry points are TOTAL
# ===========================================================================

def test_a_nul_byte_in_a_path_is_refused_not_raised(workspace):
    """`os.path.realpath` calls `lstat`, which raises `ValueError`, not an
    `OSError`, on an embedded NUL. The `@py` bodies catch `FsOpError` only, so
    that escaped all four witnessed ops as a raw exception and broke fs.rvl's
    declared `-> Result[_, FsError]` contract: a caller handling the `Err` arm
    still crashed. `hostfile.py:189-196` already handled exactly this for the
    item-396 jail; the workspace guard had not had the same treatment."""
    mod = _fs_module()
    for result in (mod.write("a\x00b.txt", "x"), mod.rm("a\x00b.txt"),
                   mod.move("a\x00b.txt", "c.txt"), mod.mkdir("a\x00b")):
        assert isinstance(result, mod.Err)
        assert result.value["code"] == "EINVAL"
        # item 274: the refusal names the nearest allowed space, and the NUL
        # never survives verbatim into the message or a WAL witness.
        assert "with the NUL removed" in result.value["message"]
        assert "\x00" not in result.value["path"]


def test_the_nul_refusal_reaches_the_guard_before_any_syscall(workspace):
    """Refused at family 1, where every caller-supplied path enters, so one
    check covers the forward ops AND every inverse endpoint."""
    with pytest.raises(ws.FsOpError) as ei:
        ws.resolve_within("a\x00b")
    assert ei.value.code == "EINVAL"
    with pytest.raises(ws.FsOpError) as ei:
        ws.resolve_sidecar("a\x00b", "garbage")
    assert ei.value.code == "EINVAL"


def test_every_enumerated_guard_entry_point_is_total(workspace):
    """The property, stated over the same enumeration that states the choke
    point: an entry point a `@py` body can call raises `FsOpError` or nothing.
    Driven by the table so a fifth entry point cannot be added without it."""
    listed = [e for entries in ws.PATH_FAMILIES.values() for e in entries]
    listed += list(ws.READ_HELPERS)
    for name in listed:
        assert getattr(getattr(ws, name), "is_total_guard", False), \
            f"guard entry point `{name}` is not wrapped total (item 422 F6)"


# ===========================================================================
# item 431(b): a witnessed write never lies about where the bytes went
# ===========================================================================

def _unlink_during(monkeypatch, hook: str, target: Path):
    """Drive the exact window item 431(b) names: unlink the leaf just before
    `hook` runs, i.e. after `open_confined_write` already holds the fd. A hook
    rather than a thread, because the question is what the RESULT says, and a
    deterministic window answers that without a 200-trial race."""
    original = getattr(ws, hook)

    def hooked(*args, **kwargs):
        try:
            os.unlink(target)
        except OSError:
            pass
        return original(*args, **kwargs)

    monkeypatch.setattr(ws, hook, hooked)


@pytest.mark.parametrize("hook", ["snapshot_preimage", "write_through"])
def test_a_write_racing_an_unlink_refuses_rather_than_claiming_ok(
        workspace, monkeypatch, hook):
    """Before item 431(b) this returned **Ok** with a witness naming a path that
    did not hold the bytes. Confinement held the whole way, the fd reached the
    inode the check admitted, but the unlink had made that inode an ORPHAN, so
    the discharge descriptor enumerated a successful witnessed write of a file
    that was gone, and the registered undo would have restored a preimage over a
    forward mutation that never became visible.

    The answer is not to recreate the leaf (see the guard module's "a write
    never lies": atomicity is a liveness promise a jail whose writer is
    untrusted by premise cannot honour). It is that the RESULT must be true."""
    target = workspace / "racy.txt"
    target.write_text("v1", encoding="utf-8")
    _unlink_during(monkeypatch, hook, target)

    mod = _fs_module()
    result = mod.write("racy.txt", "PAYLOAD")

    assert isinstance(result, mod.Err), \
        "a write whose target vanished mid-call reported Ok with a false witness"
    assert result.value["code"] == "ERACE"
    assert "Retry the write" in result.value["message"]   # item 274


def test_a_lost_race_leaves_no_preimage_residue(workspace, monkeypatch):
    """An `Err` registers no inverse, so anything the attempt left behind is
    residue nothing enumerates. The snapshot is taken before the write, so the
    refusal has to clear it, which the body cannot do itself (the family scan
    admits only paths bound from a family 1-3 guard), hence the sidecar is
    recorded on the handle and `discard_write` removes it."""
    target = workspace / "racy.txt"
    target.write_text("v1", encoding="utf-8")
    _unlink_during(monkeypatch, "write_through", target)

    mod = _fs_module()
    assert isinstance(mod.write("racy.txt", "PAYLOAD"), mod.Err)

    preimage_dir = workspace / ws.PREIMAGE_DIRNAME
    left = sorted(p.name for p in preimage_dir.iterdir()) if preimage_dir.is_dir() else []
    assert left == [], f"a refused write left preimage residue: {left}"


def test_a_failed_snapshot_capture_leaves_no_preimage_residue(
        workspace, monkeypatch):
    """The sidecar is created before its capture is taken, so an `ESNAPSHOT`
    refusal after that point must still leave nothing behind. On main the
    handle learned the sidecar's path only after the capture succeeded, so a
    capture failure (EIO, or an `EACCES` reopen after `fchmod` to a mode whose
    owner read bit is denied) leaked a sidecar no inverse enumerates. The
    portable copy path now captures through the fd it still holds, and the
    handle records the name before the capture either way."""
    monkeypatch.setattr(ws, "_clone_from_fd", lambda *a, **k: False)

    def boom(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(ws, "_capture_sidecar", boom)
    # The fixed copy path captures through the still-open fd instead of
    # reopening by name; patched with raising=False so this test can also run
    # against the tree before that helper exists.
    monkeypatch.setattr(ws, "_capture_open_sidecar", boom, raising=False)

    mod = _fs_module()
    result = mod.write("artifact.txt", "PAYLOAD")

    assert isinstance(result, mod.Err)
    assert result.value["code"] == "ESNAPSHOT"
    preimage_dir = workspace / ws.PREIMAGE_DIRNAME
    left = sorted(p.name for p in preimage_dir.iterdir()) if preimage_dir.is_dir() else []
    assert left == [], f"a failed snapshot left preimage residue: {left}"
    assert (workspace / "artifact.txt").read_text(encoding="utf-8") == "v1"


def test_a_lost_race_does_not_delete_the_competing_writers_file(
        workspace, monkeypatch):
    """`discard_write` removed the created leaf BY NAME. After a lost race that
    name is the competitor's file, not ours, ours is an unlinked orphan that
    goes away with the fd, so removing by name deleted somebody else's data
    while cleaning up our own. It is inode-checked now."""
    target = workspace / "fresh.txt"          # does not exist: the open creates it
    original = ws.write_through

    def hooked(handle, contents):
        os.unlink(target)                     # our created leaf becomes an orphan
        target.write_text("THE OTHER WRITER", encoding="utf-8")
        return original(handle, contents)

    monkeypatch.setattr(ws, "write_through", hooked)

    mod = _fs_module()
    assert isinstance(mod.write("fresh.txt", "PAYLOAD"), mod.Err)

    assert target.exists(), "the refusal deleted the competing writer's file"
    assert target.read_text(encoding="utf-8") == "THE OTHER WRITER"


def test_an_unraced_write_still_returns_ok_and_is_reversible(workspace):
    """The truthfulness check must not cost the ordinary path: an uncontended
    write is `Ok`, the bytes are visible under the name, and the witness
    restores. Both the overwrite and the create branch, because `created`
    decides whether a preimage exists at all."""
    mod = _fs_module()

    overwrite = mod.write("artifact.txt", "v2")
    assert isinstance(overwrite, mod.Ok)
    assert (workspace / "artifact.txt").read_text(encoding="utf-8") == "v2"

    created = mod.write("brand-new.txt", "hello")
    assert isinstance(created, mod.Ok)
    assert created.value["created"] is True
    assert (workspace / "brand-new.txt").read_text(encoding="utf-8") == "hello"

    mod.restore(overwrite.value)
    mod.restore(created.value)
    assert (workspace / "artifact.txt").read_text(encoding="utf-8") == "v1"
    assert not (workspace / "brand-new.txt").exists()


# ===========================================================================
# #3184: the EOUTSIDE refusal carries the caller's own path, never a fact about
# what lives outside the root (existence by case folding, symlink targets)
# ===========================================================================

def test_outside_refusal_path_is_the_callers_spelling_3184(workspace, outside, tmp_path):
    """The boundary refusal used to carry `os.path.realpath(target)`, so a path
    through a symlink came back as its resolved target. A refusal for an outside
    path that exists and one for a path that does not must be the same form: the
    caller's own path, nothing resolved."""
    link = tmp_path / "alias"
    link.symlink_to(outside, target_is_directory=True)
    existing = str(link / "canary.txt")       # exists, behind a symlink
    missing = str(link / "no_such_file.txt")  # does not exist
    for given in (existing, missing):
        with pytest.raises(ws.ConfinementError) as ei:
            ws.resolve_within(given)
        assert ei.value.code == "EOUTSIDE"
        assert ei.value.message == "path escapes the session workspace root"
        assert ei.value.path == given
        assert str(outside) not in ei.value.path


def test_outside_refusal_through_the_stdlib_body_3184(workspace, outside, tmp_path):
    mod = _fs_module()
    link = tmp_path / "alias2"
    link.symlink_to(outside, target_is_directory=True)
    seen = []
    for name in ("canary.txt", "no_such_file.txt"):
        given = str(link / name)
        r = mod.lexists(given)
        # the py tier's Result carries its case in the class, not a `kind`
        # field: assert the failure case directly instead of the vacuous
        # `hasattr` fallback that could never fail (#3235)
        assert isinstance(r, mod.Err)
        err = r.value
        code = err["code"] if isinstance(err, dict) else err.code
        path = err["path"] if isinstance(err, dict) else err.path
        assert code == "EOUTSIDE"
        assert path == given
        seen.append(path.replace(name, "N"))
    assert seen[0] == seen[1]


def test_inside_resolution_is_unchanged_3184(workspace):
    got = ws.resolve_within("artifact.txt")
    assert got == os.path.join(os.path.realpath(str(workspace)), "artifact.txt")


# ===========================================================================
# #3235: a `..` AFTER a symlink must not move the boundary. The ts tier
# collapses `..` lexically (path.resolve); this tier applied it to the link's
# target, so the same path was Ok or EOUTSIDE depending on whether an outside
# name was a symlink and where it pointed.
# ===========================================================================

def test_dotdot_after_a_symlink_is_not_a_symlink_oracle_3235(workspace, outside):
    """`O/dlink/../../../ws/in.txt` was EOUTSIDE for a plain directory `dlink`
    and Ok for a symlink one. Both tiers must collapse the `..` before
    resolving any link, so both spellings are refused the same way."""
    (workspace / "a" / "b" / "c").mkdir(parents=True)
    (workspace / "in.txt").write_text("in\n", encoding="utf-8")
    plain = outside / "dlink_plain"
    plain.mkdir()
    link = outside / "dlink_link"
    # deep enough that three `..` land back at the workspace root
    link.symlink_to(workspace / "a" / "b" / "c", target_is_directory=True)
    traversal = os.path.join("..", "..", "..", "ws", "in.txt")
    for dlink in (plain, link):
        given = os.path.join(str(dlink), traversal)
        with pytest.raises(ws.ConfinementError) as ei:
            ws.resolve_within(given)
        assert ei.value.code == "EOUTSIDE"


# ===========================================================================
# #3284: peers of the ts cases of the same number, so both tiers answer these
# setups identically. A `..` in link text applies to the RESOLVED component
# before it, and a walk that cannot finish is EOUTSIDE with the caller's path.
# ===========================================================================

def _assert_outside_refusal(mod, given):
    with pytest.raises(ws.ConfinementError) as ei:
        ws.resolve_within(given)
    assert ei.value.code == "EOUTSIDE"
    assert ei.value.path == given
    r = mod.lexists(given)
    assert isinstance(r, mod.Err)
    err = r.value
    code = err["code"] if isinstance(err, dict) else err.code
    path = err["path"] if isinstance(err, dict) else err.path
    assert code == "EOUTSIDE"
    assert path == given


def test_dotdot_in_link_text_is_no_existence_oracle_3284(workspace, outside):
    """`ws/D -> <ws>/inlink/../X` with `inlink` leaving the root is `<outside>/X`:
    refused the same whether `<outside>/X` is absent, a directory or a file."""
    mod = _fs_module()
    (outside / "deep").mkdir()
    (workspace / "inlink").symlink_to(outside / "deep", target_is_directory=True)
    (workspace / "D").symlink_to(f"{workspace}/inlink/../X")
    given = str(workspace / "D" / "x")
    _assert_outside_refusal(mod, given)
    (outside / "X").mkdir()
    _assert_outside_refusal(mod, given)
    (outside / "X").rmdir()
    (outside / "X").write_text("x\n", encoding="utf-8")
    _assert_outside_refusal(mod, given)


def test_dotdot_in_link_text_back_inside_resolves_inside_3284(workspace):
    (workspace / "sub").mkdir()
    (workspace / "sublink").symlink_to(workspace / "sub", target_is_directory=True)
    (workspace / "back").symlink_to(f"{workspace}/sublink/../missing")
    got = ws.resolve_within(str(workspace / "back" / "leaf"))
    assert got == os.path.join(os.path.realpath(str(workspace)), "missing", "leaf")


def test_inside_link_into_an_outside_cycle_is_eoutside_3284(workspace, outside):
    mod = _fs_module()
    (outside / "c1").symlink_to(outside / "c2")
    (outside / "c2").symlink_to(outside / "c1")
    (workspace / "cyc").symlink_to(outside / "c1")
    for given in (str(workspace / "cyc"), str(workspace / "cyc" / "x")):
        _assert_outside_refusal(mod, given)


def test_link_chain_past_the_budget_is_eoutside_3284(workspace, outside):
    """l0 -> l1 -> ... -> l44 -> <outside>/end, every link outside the root."""
    mod = _fs_module()
    for i in range(45):
        target = outside / ("end" if i == 44 else f"l{i + 1}")
        (outside / f"l{i}").symlink_to(target)
    (workspace / "chain").symlink_to(outside / "l0")
    for given in (str(workspace / "chain"), str(workspace / "chain" / "x")):
        _assert_outside_refusal(mod, given)
