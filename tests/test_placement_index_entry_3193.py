"""A placement index entry names only its own process WAL (issue #3193).

`revl run --placement P --wal INDEX` writes each process WAL next to the index
as `process_wal_name(INDEX, process)`. Recover used to join whatever an entry's
`wal` said onto the index directory, so `../elsewhere/x.wal` or an absolute
path made it read, replay and stamp `activation-complete` onto a WAL the run
never wrote. Every reader of the index now refuses such an entry by name, and
no file is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement_wal  # noqa: E402
from revl.placement_wal import PlacementIndexError  # noqa: E402

HEADER = {"record": "header", "walVersion": 1, "generation": 1, "guarantee": "g"}


def _write(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _index(tmp_path: Path, name: str, wal: str) -> Path:
    index = tmp_path / "run" / "run.wal"
    _write(index, [{"record": "placement-index", "placementVersion": 1,
                    "processes": [{"name": name, "wal": wal,
                                   "components": ["Agent"], "backend": "py"}]},
                   {"record": "opened", "run": 1},
                   {"record": "committed", "run": 1}])
    return index


def _cases(tmp_path: Path) -> list:
    """``(name, wal, target)``: an entry and the WAL-shaped file it points at."""
    other = tmp_path / "elsewhere" / "other-run.wal"
    sibling = tmp_path / "run" / "run.wal.desk"
    return [
        ("agent", "../elsewhere/other-run.wal", other),
        ("agent", str(other), other),
        # a plain name, but another process's WAL
        ("agent", "run.wal.desk", sibling),
        # a process name that is itself a path
        ("../elsewhere/other-run.wal", "run.wal.../elsewhere/other-run.wal", None),
    ]


def test_an_entry_that_does_not_name_its_own_wal_is_refused_by_every_reader(tmp_path):
    from revl.recover_placement import recover_index  # noqa: PLC0415
    for name, wal, target in _cases(tmp_path):
        if target is not None:
            _write(target, [HEADER])
        before = target.read_text(encoding="utf-8") if target else None
        index = _index(tmp_path, name, wal)

        with pytest.raises(PlacementIndexError, match="refusing to read or stamp"):
            placement_wal.read_index(str(index))
        with pytest.raises(PlacementIndexError, match="refusing to read or stamp"):
            placement_wal.process_wal_path(str(index), {"name": name, "wal": wal})
        # a caller holding an already-parsed index cannot reach the file either
        with pytest.raises(PlacementIndexError):
            recover_index(str(index), {"processes": [
                {"name": name, "wal": wal, "components": ["Agent"],
                 "backend": "py"}], "run": 1, "committed": True})
        if target is not None:
            assert target.read_text(encoding="utf-8") == before, wal


def test_the_entry_arm_writes_is_accepted(tmp_path):
    index = _index(tmp_path, "agent", "run.wal.agent")
    assert placement_wal.read_index(str(index))["processes"][0]["wal"] == "run.wal.agent"
    assert placement_wal.process_wal_path(
        str(index), {"name": "agent", "wal": "run.wal.agent"}) == os.path.join(
            str(index.parent), "run.wal.agent")


def _revl(args: list, cwd: Path) -> subprocess.CompletedProcess:
    code = ("import sys\nfrom revl.__main__ import main\n"
            f"sys.exit(main({args!r}))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd),
                          env=dict(os.environ, PYTHONPATH=path),
                          capture_output=True, text=True, timeout=600)


def test_revl_recover_refuses_an_index_entry_outside_the_index_directory(tmp_path):
    other = tmp_path / "elsewhere" / "other-run.wal"
    _write(other, [HEADER])
    before = other.read_text(encoding="utf-8")
    index = _index(tmp_path, "agent", "../elsewhere/other-run.wal")

    proc = _revl(["recover", "--wal", str(index), "--json"], tmp_path)

    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "refusing to read or stamp" in proc.stderr
    assert other.read_text(encoding="utf-8") == before


def test_no_process_wal_is_stamped_when_any_entry_is_refused(tmp_path):
    """The entries are checked before the first stamp: a good entry recovered
    ahead of a bad one is not stamped either."""
    from revl.recover_placement import recover_index  # noqa: PLC0415
    index = tmp_path / "run" / "run.wal"
    good = tmp_path / "run" / "run.wal.desk"
    _write(good, [HEADER])
    before = good.read_text(encoding="utf-8")
    entries = [{"name": "agent", "wal": "../agent.wal", "components": ["Agent"],
                "backend": "py"},
               {"name": "desk", "wal": "run.wal.desk", "components": ["Desk"],
                "backend": "py"}]
    with pytest.raises(PlacementIndexError):
        recover_index(str(index), {"processes": entries, "run": 1, "committed": True})
    assert good.read_text(encoding="utf-8") == before


def _linked(tmp_path: Path, how: str) -> tuple:
    """``(index, outside)``: an index whose entry is the name the run writes,
    `run.wal.agent`, but on disk that name is a symbolic link out of the
    index's directory, directly or through a symlinked directory."""
    outside = tmp_path / "elsewhere" / "o.wal"
    _write(outside, [HEADER])
    index = _index(tmp_path, "agent", "run.wal.agent")
    link = index.parent / "run.wal.agent"
    if how == "file":
        link.symlink_to(Path("..") / "elsewhere" / "o.wal")
    else:
        (index.parent / "sub").symlink_to(Path("..") / "elsewhere",
                                          target_is_directory=True)
        link.symlink_to(Path("sub") / "o.wal")
    return index, outside


@pytest.mark.parametrize("how", ["file", "directory"])
def test_a_process_wal_that_is_a_symlink_out_of_the_directory_is_never_stamped(tmp_path, how):
    from revl.cli.change import _estop_outstanding  # noqa: PLC0415
    index, outside = _linked(tmp_path, how)
    before = outside.read_text(encoding="utf-8")

    with pytest.raises(PlacementIndexError, match="symbolic link"):
        placement_wal.process_wal_path(str(index),
                                       {"name": "agent", "wal": "run.wal.agent"})
    proc = _revl(["recover", "--wal", str(index), "--json"], tmp_path)
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "symbolic link" in proc.stderr
    assert outside.read_text(encoding="utf-8") == before
    estop = _estop_outstanding(str(index))
    assert estop["known"] is False and "symbolic link" in estop["note"]


def test_an_index_reached_through_a_symlinked_directory_is_still_read(tmp_path):
    """The index and its process WALs move as one: a symlink to the directory
    that holds both is not an escape."""
    index = _index(tmp_path, "agent", "run.wal.agent")
    _write(index.parent / "run.wal.agent", [HEADER])
    (tmp_path / "alias").symlink_to(index.parent, target_is_directory=True)
    aliased = tmp_path / "alias" / "run.wal"
    path = placement_wal.process_wal_path(str(aliased),
                                          {"name": "agent", "wal": "run.wal.agent"})
    assert os.path.samefile(path, index.parent / "run.wal.agent")
