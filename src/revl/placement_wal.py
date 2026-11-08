"""The write-ahead logs of a placement run (issue #1477).

`revl run --placement P --wal FILE` writes one WAL per process: each py process
records its own crossings through the same `replay.Recorder` a single-process
run uses. FILE itself is the run's INDEX, the one path the operator names to
`revl recover`. It is JSON Lines:

* ``placement-index`` (first line): ``{placementVersion, processes: [{name,
  wal, components, backend}]}``. ``wal`` is the process WAL's file name,
  relative to the index's own directory, so the set moves as one.
* ``opened`` per run that armed this index: ``{run}``.
* ``committed`` once every process of that run was UP: ``{run}``. It is
  written BEFORE the conductor tells each process to stamp its own
  `activation-complete`, so it is the placement's commit decision. A crash
  between the two leaves processes whose WALs lack the marker under a run
  that committed; recover completes that commit instead of rolling them back.

A placement's activation is the whole composition's, so no process stamps its
marker on its own. A crash before the commit leaves every process WAL
uncommitted and recover rolls all of them back, as it would the same
composition run in one process.
"""

from __future__ import annotations

import json
import os
from typing import Optional

INDEX_KIND = "placement-index"
INDEX_VERSION = 1

#: The tiers whose placement runner writes a WAL. Every other tier is refused
#: by name when `--wal` is given, rather than run with a process that records
#: nothing and recovers as if it had crossed nothing.
WAL_TIERS = frozenset({"py"})


class PlacementIndexError(RuntimeError):
    """An index that cannot be armed or read."""


def process_wal_name(index_path: str, process: str) -> str:
    return f"{os.path.basename(index_path)}.{process}"


def entry_problem(index_path: str, entry: object) -> Optional[str]:
    """Why an index entry does not name its own process WAL, or ``None``.

    An entry's ``wal`` is exactly the name `arm` wrote, `process_wal_name`: a
    plain file name next to the index. Anything else (a path with a separator,
    ``..``, an absolute path, another process's name) would make recover read,
    replay and stamp a WAL the run never wrote (issue #3193)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) \
            or not isinstance(entry.get("wal"), str):
        return f"an index entry is not a {{name, wal}} pair: {entry!r}"
    name, wal = entry["name"], entry["wal"]
    expected = process_wal_name(index_path, name)
    separators = {os.sep} | ({os.altsep} if os.altsep else set())
    plain = (os.path.basename(wal) == wal and wal not in ("", ".", "..")
             and not any(sep in wal for sep in separators))
    if not plain or wal != expected:
        return (f"process {name!r}: its WAL is named {wal!r}, but a placement "
                f"index names each process WAL {expected!r}, next to the index; "
                f"refusing to read or stamp any other file")
    return None


def process_wal_path(index_path: str, entry: dict) -> str:
    """The absolute path of one process's WAL, from its index entry. Raises
    :class:`PlacementIndexError` for an entry that does not name its own
    process WAL (:func:`entry_problem`), and for a process WAL that is a
    symbolic link or does not resolve to a file in the index's own directory:
    the run never writes one, and following it would read and stamp a file
    outside that directory (issue #3193)."""
    problem = entry_problem(index_path, entry)
    if problem is not None:
        raise PlacementIndexError(f"{index_path}: {problem}")
    directory = os.path.dirname(os.path.abspath(index_path))
    path = os.path.join(directory, entry["wal"])
    if os.path.islink(path) or os.path.dirname(os.path.realpath(path)) \
            != os.path.realpath(directory):
        raise PlacementIndexError(
            f"{index_path}: process {entry['name']!r}: its WAL {path} is a "
            f"symbolic link, or resolves outside the index's directory; a "
            f"placement run writes each process WAL as a plain file next to "
            f"the index. Refusing to read or stamp it.")
    return path


def wal_problem(processes: dict, backends: dict, sandboxes: dict) -> Optional[str]:
    """Why this placement cannot write a WAL per process, or ``None``."""
    for pname in processes:
        if pname == "estop":
            return ("--wal with --placement: a process named 'estop' would write "
                    "its WAL at the path `revl estop --wal` derives for the "
                    "run's E-Stop latch (FILE.estop). Rename the process.")
        if backends.get(pname) not in WAL_TIERS:
            return (f"--wal with --placement: process {pname!r} runs on the "
                    f"{backends.get(pname)} tier, whose placement runner writes "
                    f"no WAL. A crash would leave that process's crossings "
                    f"unrecorded and `revl recover` would read them as nothing. "
                    f"Place it on the py tier, or run without --wal.")
        if pname in sandboxes:
            return (f"--wal with --placement: process {pname!r} is sandboxed, "
                    f"and its WAL would have to be written outside the boundary "
                    f"the sandbox establishes. Run it unsandboxed, or run "
                    f"without --wal.")
    return None


def _fsync_append(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _entries(index_path: str, processes: dict, backends: dict) -> list:
    return [{"name": pname, "wal": process_wal_name(index_path, pname),
             "components": list(pconf.get("components") or []),
             "backend": backends[pname]}
            for pname, pconf in processes.items()]


def arm(index_path: str, processes: dict, backends: dict, specs: dict) -> int:
    """Write (or reopen) the index, create each process WAL, and hand each
    process its WAL path through its spec. Returns this run's number.

    An index that already exists is reused only for the same processes with
    the same components: a later run appends to the same process WALs, which
    each record the new opening (#641/#642). A different process set is
    refused, because the old run's process WALs would drop out of the index
    and nothing would recover them."""
    entries = _entries(index_path, processes, backends)
    existing = read_index(index_path) if os.path.exists(index_path) else None
    if os.path.exists(index_path) and existing is None:
        raise PlacementIndexError(
            f"{index_path} exists and is not a placement index; a placement "
            f"run writes its index there. Name a new --wal path.")
    if existing is not None:
        if existing["processes"] != entries:
            raise PlacementIndexError(
                f"{index_path} indexes a placement with different processes "
                f"({_roster(existing['processes'])}); this placement has "
                f"{_roster(entries)}. Recover that run first, or name a new "
                f"--wal path.")
        run = existing["run"] + 1
    else:
        directory = os.path.dirname(os.path.abspath(index_path))
        os.makedirs(directory, exist_ok=True)
        _fsync_append(index_path, {"record": INDEX_KIND,
                                   "placementVersion": INDEX_VERSION,
                                   "processes": entries})
        run = 1
    for entry in entries:
        path = process_wal_path(index_path, entry)
        open(path, "a", encoding="utf-8").close()
        specs[entry["name"]]["wal"] = path
    _fsync_append(index_path, {"record": "opened", "run": run})
    return run


def mark_committed(index_path: str, run: int) -> None:
    _fsync_append(index_path, {"record": "committed", "run": run})


def _roster(entries: list) -> str:
    return ", ".join(f"{e['name']}=[{', '.join(e['components'])}]"
                     for e in entries)


def read_index(path: str) -> Optional[dict]:
    """``{processes, run, committed}`` for a placement index, ``None`` for any
    other file (a process WAL, or a single-process run's WAL). ``run`` is the
    latest run that armed the index; ``committed`` is whether that run
    committed."""
    try:
        with open(path, encoding="utf-8") as handle:
            first = handle.readline()
            rest = handle.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        header = json.loads(first)
    except json.JSONDecodeError:
        return None
    if not isinstance(header, dict) or header.get("record") != INDEX_KIND:
        return None
    if header.get("placementVersion") != INDEX_VERSION:
        raise PlacementIndexError(
            f"{path} is a placement index of version "
            f"{header.get('placementVersion')!r}; this revl reads version "
            f"{INDEX_VERSION}")
    processes = header.get("processes") or []
    for entry in processes:
        problem = entry_problem(path, entry)
        if problem is not None:
            raise PlacementIndexError(f"{path}: {problem}")
    run, committed = 0, False
    for line in rest:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue   # a torn final line: the crash itself
        if record.get("record") == "opened":
            run, committed = record.get("run") or 0, False
        elif record.get("record") == "committed" and record.get("run") == run:
            committed = True
    return {"processes": processes, "run": run, "committed": committed}
