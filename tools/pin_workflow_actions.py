#!/usr/bin/env python3
"""Resolve and check the commit pins in .github/workflows/*.yml.

`tests/test_workflow_action_pins.py` is the hermetic half of this: it asserts,
with no network, that every external `uses:` is `owner/repo(/path)@<40 hex>` and
carries a comment. What it cannot see is whether the sha is the right sha, and
there are exactly two ways to get that wrong:

  * **The annotated-tag trap.** `refs/tags/v4` for github/codeql-action is a TAG
    object, not a commit, so the sha the API returns for the ref is not the sha
    the workflow needs. Pinning it breaks the run at checkout time. The commit is
    one dereference further.
  * **Staleness.** A pin records the release that was current when it was
    written, and a moving `@v4` would have kept following the line. A pin is
    supposed to be a decision, so this tool only *reports* a newer release on the
    pinned major line; `--strict` turns that report into a failure.

Usage:

    python tools/pin_workflow_actions.py            # report every pin
    python tools/pin_workflow_actions.py --check    # exit 1 on a bad pin
    python tools/pin_workflow_actions.py --write    # rewrite pins in place
    python tools/pin_workflow_actions.py --strict   # a newer release is a failure

Reads GITHUB_TOKEN / GH_TOKEN when present (the unauthenticated limit is 60
requests/hour, and this walks every action in the matrix). Stdlib only, so it
runs without the dev extra.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

USES = re.compile(
    r"^(?P<indent>\s*(?:-\s*)?uses:\s*)(?P<target>\S+)(?P<gap>\s*)(?:#\s*(?P<comment>\S.*?))?\s*$",
    re.MULTILINE,
)
SHA = re.compile(r"^[0-9a-f]{40}$")
SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
API = "https://api.github.com"


def _get(path: str) -> object | None:
    request = urllib.request.Request(f"{API}{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "revl-pin-workflow-actions")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _is_commit(repo: str, sha: str) -> bool:
    """True when `sha` is a commit in `repo`.

    `/commits/{sha}` answers 422, not 404, for a sha that is a valid object but
    not a commit -- which is precisely what a tag object is. Treating 422 as an
    error here would crash the one check that exists to catch the annotated-tag
    trap, so both codes mean "not a commit".
    """
    try:
        return _get(f"/repos/{repo}/commits/{sha}") is not None
    except urllib.error.HTTPError as error:
        if error.code == 422:
            return False
        raise


def _releases(repo: str) -> dict[str, str]:
    """{tag name: commit sha} for the repo's tags, newest API page first."""
    payload = _get(f"/repos/{repo}/tags?per_page=100")
    if not isinstance(payload, list):
        return {}
    return {entry["name"]: entry["commit"]["sha"] for entry in payload}


def _newest_on_line(releases: dict[str, str], major: int) -> tuple[str, str] | None:
    candidates = []
    for name, sha in releases.items():
        match = SEMVER.match(name)
        if match and int(match.group(1)) == major:
            candidates.append((tuple(int(g) for g in match.groups()), name, sha))
    if not candidates:
        return None
    _, name, sha = max(candidates)
    return name, sha


def _external_uses() -> list[tuple[Path, re.Match[str]]]:
    found = []
    for path in sorted(p for p in WORKFLOWS.rglob("*.y*ml") if p.is_file()):
        for match in USES.finditer(path.read_text(encoding="utf-8")):
            if not match.group("target").startswith("./"):
                found.append((path, match))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 on a bad pin")
    parser.add_argument("--write", action="store_true", help="rewrite pins in place")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="a newer release on the pinned major line is a failure, not a report",
    )
    args = parser.parse_args(argv)

    problems: list[str] = []
    stale: list[str] = []
    rewrites: dict[Path, list[tuple[str, str]]] = {}

    for path, match in _external_uses():
        target, comment = match.group("target"), match.group("comment")
        if "@" not in target:
            problems.append(f"{path.name}: `{target}` carries no ref")
            continue
        repo, ref = target.rsplit("@", 1)
        # GitHub's grammar is `{owner}/{repo}/{path}@{ref}` -- the repo is always
        # the first two segments, so `github/codeql-action/init` is the repo
        # `github/codeql-action` with the path `init`.
        parts = repo.split("/")
        if len(parts) < 2:
            problems.append(f"{path.name}: `{target}` is not owner/repo")
            continue
        repo = "/".join(parts[:2])

        if not SHA.fullmatch(ref):
            resolved = _resolve(repo, ref)
            if resolved is None:
                problems.append(f"{path.name}: `{target}` does not resolve")
                continue
            sha, label = resolved
            problems.append(
                f"{path.name}: `{target}` is a moving ref; pin `{sha}`  # {label}"
            )
            rewrites.setdefault(path, []).append((target, f"{repo}@{sha}  # {label}"))
            continue

        if not _is_commit(repo, ref):
            problems.append(
                f"{path.name}: `{repo}@{ref}` is not a commit in {repo} -- an "
                "annotated tag's object sha resolves to nothing at checkout; use "
                "the PEELED commit"
            )
            continue

        line = _pinned_major(comment)
        if line is None:
            continue
        newest = _newest_on_line(_releases(repo), line)
        if newest and newest[1] != ref:
            note = f"{path.name}: {repo}@{ref} ({comment}) -- {newest[0]} exists"
            (stale if not args.strict else problems).append(note)

    for note in sorted(set(stale)):
        print(f"stale:     {note}")
    for problem in sorted(set(problems)):
        print(f"problem:   {problem}", file=sys.stderr)

    if args.write and rewrites:
        for path, pairs in rewrites.items():
            text = path.read_text(encoding="utf-8")
            for old, new in pairs:
                text = text.replace(f"uses: {old}", f"uses: {new}")
            path.write_text(text, encoding="utf-8")
            print(f"rewrote {path.relative_to(ROOT)}")

    if problems:
        print(f"\n{len(problems)} pin(s) need attention", file=sys.stderr)
        return 1
    print("every pin is a commit on its recorded release")
    return 0


def _resolve(repo: str, ref: str) -> tuple[str, str] | None:
    """(peeled commit sha, label) for a tag or branch ref, or None."""
    for kind in ("tags", "heads"):
        payload = _get(f"/repos/{repo}/git/ref/{kind}/{ref}")
        if not isinstance(payload, dict):
            continue
        obj = payload["object"]
        sha = obj["sha"]
        # An annotated tag's ref points at a TAG object; the commit a workflow
        # needs is one dereference further. Pinning the object sha breaks the run.
        if obj["type"] == "tag":
            tag = _get(f"/repos/{repo}/git/tags/{sha}")
            if not isinstance(tag, dict):
                return None
            sha = tag["object"]["sha"]
        return sha, ref
    return None


def _pinned_major(comment: str | None) -> int | None:
    """The major line a pin's comment names, so staleness can be judged."""
    if not comment:
        return None
    for token in re.findall(r"v(\d+)(?:\.\d+){0,2}", comment):
        return int(token)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
