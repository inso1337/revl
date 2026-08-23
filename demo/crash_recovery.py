#!/usr/bin/env python
"""revl crash-recovery demo — the accumulator as a write-ahead log (item 47).

Simulates a `kill -9` mid-activation *deterministically*: it writes a
write-ahead log the way `revl run --wal` does — a durable file acquire (whose
inverse is reconstructible from its description) and a bare emission (which
already crossed the boundary) — then, WITHOUT stamping the `activation-complete`
marker, throws away every in-memory object and recovers from the log file alone.

    backends/python/.venv/bin/python demo/crash_recovery.py            # narrated
    backends/python/.venv/bin/python demo/crash_recovery.py --script   # CI, exits 1 on a wrong verdict

Needs no runtime: recovery works from the durable log, because the process that
produced it is (in the story) dead. See docs/crash-recovery.md.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import replay  # noqa: E402
from revl.recovery import recover, render  # noqa: E402


def _write_crashed_wal(path: str) -> None:
    """Write a WAL as activation runs, then 'crash' — no completion marker."""
    wal = replay.WriteAheadLog(path, ir={}, generation=7).open()

    # a durable acquire: a scratch file on disk. Its undo is a NAMED call with
    # captured args — reconstructible from its description in a fresh process.
    wal.record_boundary(
        "Store", "create scratch file", resource="File",
        inverse_op={"receiver": "fs", "method": "unlink",
                    "args": ["/var/db/PeerWall/gen7.scratch"]})

    # a bare emission: it already left the process. No inverse (G4). After a
    # crash it is simply still out in the world — recovery must say so.
    tl = replay.Timeline("Store")
    tl.record_emission("bus", "send", ("order#42 placed",), "Bus", ("<gen7>", 18))
    wal.append_timeline(tl)

    wal.close()  # <-- kill -9 here: commit_activation() never runs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", action="store_true",
                        help="CI mode: same output, exit 1 on a wrong verdict")
    parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "gen7.wal")
        _write_crashed_wal(path)

        print("== a process died mid-activation; only this WAL survived ==")
        loaded = replay.WriteAheadLog.read(path)
        print(f"  {len([r for r in loaded['records'] if r['record'] == 'effect'])} "
              f"committed effect(s), activation-complete marker: {loaded['complete']}\n")

        print("== recover from the log alone (the runtime is gone) ==")
        report = recover(path)
        print(render(report))

    # the demo's own exit test: the verdict must be roll-back, must have RUN the
    # reconstructible file inverse, and must honestly flag the bare emission.
    ok = (report["verdict"] == "rolled-back"
          and [e["op"]["method"] for e in report["ran"]] == ["unlink"]
          and any(e["kind"] == "emission" for e in report["unreconstructible"])
          and report["residue"]["clean"] is False)
    if not ok:
        print("\nDEMO FAILED: recovery did not produce the expected verdict",
              file=sys.stderr)
        return 1
    print("\nOK: rolled back — reconstructed the file inverse, flagged the "
          "emission as honest residue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
