"""A session abort replays escrowed inverses continue-and-record (issue #1473).

WHAT WAS WRONG. A swap withdraws the previous generation mid-session, and its
witnessed entries wait in the session owner's escrow for the verdict.
`SessionOwner.finalize_abort` replayed that escrow newest first with a bare
`entry()`, so the first inverse that raised escaped `Session.abort`. Measured
on main with three stashed files whose middle undo raises: `c` was restored,
the raise escaped, `a` was never restored, and the session was left loaded
with its owner half settled.

The teardown contract's Phase-1 rule is continue-and-record
(docs/design/teardown-contract.md, "Phase-1 failure"): a failed witnessed
restore is `restore-residue` and every older entry still runs. A live frame's
`drain` already did this; the escrow now does too, and a restore that raised
is not named in the WAL's `aborted` record as replayed, on either path.

Every test here drives a live cordis-py composition. Without the pinned
`cordis` fork they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the abort runs against a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`",
)

#: `stash` renames p to p.bak; its undo renames it back, and RAISES for a
#: path ending in `b`.
SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if w['path'].endswith('b'):\n"
    "        raise RuntimeError('restore failed for ' + os.path.basename(w['path']))\n"
    "    os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    os.replace(p, p + '.bak')\n"
    "    return Ok({'path': p, 'bak': p + '.bak'})\n"
    "}\n"
    "service Ops { emission fn stash(p: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn stash(p) { effect stash_path(p) } }\n"
    "}\n"
)


def _recorded_session(wal_path: str, name: str):
    """A session whose own WAL is open before the first call, the way a
    session under an approval policy opens it."""
    from revl.mcp.session import Session
    session = Session()
    session._wal_path = wal_path
    session.load(compile_source(SOURCE, name), record=True)
    session._ensure_wal_open()
    return session


@pytest.fixture
def files(tmp_path):
    paths = [tmp_path / name for name in ("a", "b", "c")]
    for path in paths:
        path.write_text(path.name, encoding="utf-8")
    return paths


def _restored(path: Path) -> bool:
    return path.exists() and not Path(f"{path}.bak").exists()


def _records(wal_path: str, kind: str) -> list:
    import replay
    return [r for r in replay.WriteAheadLog.read(wal_path)["records"]
            if r.get("record") == kind]


def _stash_all(session, files) -> None:
    for path in files:
        session.call("ops", "stash", [str(path)])


@needs_cordis
def test_a_failing_escrowed_undo_is_residue_and_the_others_still_run(
        files, tmp_path):
    wal_path = str(tmp_path / "escrow.wal")
    session = _recorded_session(wal_path, "escrow_1473.rvl")
    _stash_all(session, files)
    session.swap(compile_source(SOURCE, "escrow_1473.rvl"))
    assert len(session._owner._escrow) == 3

    verdict = session.abort()          # on main this raised

    a, b, c = files
    assert _restored(a) and _restored(c)
    assert not _restored(b)
    [residue] = verdict["compensationResidue"]
    assert residue["kind"] == "restore-residue"
    assert residue["method"] == "unstash"
    assert residue["error"]["message"] == "restore failed for b"
    # the session ends in a defined state: torn down, as any abort leaves it
    assert not session.loaded
    # the `aborted` record names the two restores that ran and not the one
    # that raised
    [aborted] = _records(wal_path, "aborted")
    descriptors = {d["seq"]: d["witness"]["path"]
                   for d in _records(wal_path, "discharge-descriptor")}
    assert sorted(os.path.basename(descriptors[s]) for s in aborted["replayed"]) \
        == ["a", "c"]


@needs_cordis
def test_the_live_frame_path_names_only_what_ran_too(files, tmp_path):
    """No swap: the live frame's `drain` already continued past the raise and
    recorded it. What it still did was name the raised seq in the `aborted`
    record, so recover read a restore that did not happen as done."""
    wal_path = str(tmp_path / "live.wal")
    session = _recorded_session(wal_path, "live_1473.rvl")
    _stash_all(session, files)
    verdict = session.abort()
    a, b, c = files
    assert _restored(a) and _restored(c) and not _restored(b)
    [residue] = verdict["compensationResidue"]
    assert residue["kind"] == "restore-residue"
    [aborted] = _records(wal_path, "aborted")
    descriptors = {d["seq"]: d["witness"]["path"]
                   for d in _records(wal_path, "discharge-descriptor")}
    assert sorted(os.path.basename(descriptors[s]) for s in aborted["replayed"]) \
        == ["a", "c"]
