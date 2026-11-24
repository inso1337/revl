#!/usr/bin/env python3
"""An offline snapshot of the open issue tracker, for the evolution curriculum.

WHY IT IS A SNAPSHOT (roadmap item 535, issue #2761 slice 6). The curriculum
(`tools/evolve_curriculum.py`) must stay computable in a checkout with no
network and no credentials, so it never calls GitHub. This tool is the only
place that does: `--write` reads the open issues once and commits the part of
them a curriculum task needs, and everything downstream reads the file.

WHAT IS KEPT. Per open issue: its number, title and labels, and `verifiers`,
the in-tree paths under `tests/` and `tools/` its body names, in the order they
first appear. The body itself is not kept: a task derives from the artifact a
verifier names, not from prose, and a snapshot that quoted bodies would be a
second copy of the tracker that rots against it. Nothing in the file is a
timestamp, so a `--write` that finds the same issues is a no-op diff.

WHAT IT CANNOT KNOW. An issue closed after the snapshot was taken is still in
it until the next `--write`. That is the price of being offline, stated here and
not hidden: the curriculum reads a task from the snapshot, and a person
regenerating it is what retires the task.

Usage:
    python3 tools/tracker_snapshot.py --write     # needs `gh`; rewrites the file
    python3 tools/tracker_snapshot.py --check     # offline; the file is well formed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "tracker_snapshot.json"
REPO = "inso1337/revl"

# A path an issue body names as its verifier. Narrow on purpose: a file under
# `tests/` or `tools/`, so a path in a stack trace or a URL is not one.
_VERIFIER = re.compile(
    r"(?<![\w/.-])((?:tests|tools)/[A-Za-z0-9_./-]*?\.(?:py|json|rvl|sh))")

ABOUT = [
    "An offline snapshot of the open issues of inso1337/revl, written by",
    "tools/tracker_snapshot.py --write and read by tools/evolve_curriculum.py",
    "(slice 6 of issue #2761). It keeps each issue's number, title and labels and",
    "the tests/ and tools/ paths its body names as `verifiers`; never the body.",
    "An issue closed since the snapshot was taken stays here until the next",
    "--write. The file is in the tree, so a candidate can read it.",
]


def verifiers_of(body: str) -> list[str]:
    """The distinct verifier paths in `body`, in order of first appearance."""
    return list(dict.fromkeys(_VERIFIER.findall(body or "")))


def entry(issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": " ".join(issue["title"].split()),
        "labels": sorted(label["name"] for label in issue.get("labels", [])),
        "verifiers": verifiers_of(issue.get("body", "")),
    }


def snapshot_of(issues: list[dict]) -> dict:
    return {"_about": ABOUT, "repo": REPO,
            "issues": sorted((entry(i) for i in issues),
                             key=lambda e: e["number"])}


def fetch(repo: str = REPO) -> list[dict]:
    out = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--limit", "5000", "--json", "number,title,labels,body"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def problems(data) -> list[str]:
    """Reasons the snapshot is not well formed. Empty means green. Offline."""
    if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
        return ["the snapshot has no `issues` list"]
    found: list[str] = []
    seen: set[int] = set()
    last = 0
    for position, issue in enumerate(data["issues"]):
        where = f"issues[{position}]"
        if not isinstance(issue, dict):
            found.append(f"{where} is not an object")
            continue
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            found.append(f"{where} has no integer number")
            continue
        if number in seen:
            found.append(f"{where}: issue #{number} appears twice")
        if number < last:
            found.append(f"{where}: issue #{number} is out of order")
        seen.add(number)
        last = max(last, number)
        if not isinstance(issue.get("title"), str) or not issue["title"]:
            found.append(f"{where}: issue #{number} has no title")
        for field in ("labels", "verifiers"):
            value = issue.get(field)
            if not (isinstance(value, list)
                    and all(isinstance(v, str) for v in value)):
                found.append(f"{where}: issue #{number} {field} is not a "
                             "list of strings")
        for path in issue.get("verifiers", ()):
            if isinstance(path, str) and not _VERIFIER.fullmatch(path):
                found.append(f"{where}: issue #{number} verifier {path!r} is "
                             "not a tests/ or tools/ path")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true",
                       help="read the open issues with gh and rewrite the file")
    group.add_argument("--check", action="store_true",
                       help="offline: the committed file is well formed")
    parser.add_argument("--file", type=Path, default=SNAPSHOT)
    args = parser.parse_args(argv)
    if args.write:
        data = snapshot_of(fetch())
        args.file.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        print(f"wrote {len(data['issues'])} open issues to {args.file}")
        return 0
    found = problems(json.loads(args.file.read_text(encoding="utf-8")))
    for line in found:
        print(f"TRACKER-SNAPSHOT {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
