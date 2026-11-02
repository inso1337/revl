"""The update journal (docs/updates.md).

One JSONL line per event, appended and flushed, under the update home. The
journal is the record a host reads after a restart: which release was checked,
staged, planned, applied or rolled back, and what the running generation was
on each side of an apply. It is append only; a corrupted last line never takes
the earlier lines with it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .errors import UpdateError
from .fetch import update_home

JOURNAL = "journal.jsonl"


class Journal:
    """Append only event log for one update home."""

    def __init__(self, home_dir: str | os.PathLike | None = None) -> None:
        self.home = update_home(home_dir)
        self.path = self.home / JOURNAL

    def append(self, event: str, **fields: object) -> dict:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **fields,
        }
        self.home.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def read(self, limit: int | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        records: list[dict] = []
        with open(self.path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records[-limit:] if limit else records

    def tail(self, limit: int = 20) -> list[dict]:
        return self.read(limit=limit)

    def last(self, event: str) -> dict | None:
        for record in reversed(self.read()):
            if record.get("event") == event:
                return record
        return None


def journal_for(home_dir: str | os.PathLike | None = None) -> Journal:
    return Journal(home_dir)


def require_readable(home_dir: str | os.PathLike | None = None) -> Journal:
    """A journal whose home exists and is writable, or a named refusal."""
    journal = Journal(home_dir)
    try:
        journal.home.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UpdateError(
            "UPDATE_HOME",
            f"the update home {journal.home} cannot be created: {error}",
            fix="set REVL_UPDATE_HOME or --home to a writable directory"
        ) from None
    return journal


def staged_directories(home_dir: str | os.PathLike | None = None) -> list[Path]:
    root = update_home(home_dir) / "staged"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())
