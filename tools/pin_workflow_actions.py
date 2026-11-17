#!/usr/bin/env python3
"""Resolve and check the commit pins in .github/workflows/*.yml.

`tests/test_workflow_action_pins.py` is the hermetic half of this: it asserts,
with no network, that every external `uses:` is `owner/repo(/path)@<40 hex>` and
carries a comment. What it cannot see is whether the sha is the right sha, and
there are exactly three ways to get that wrong:

  * **The annotated-tag trap.** `refs/tags/v4` for github/codeql-action is a TAG
    object, not a commit, so the sha the API returns for the ref is not the sha
    the workflow needs. Pinning it breaks the run at checkout time. The commit is
    one dereference further.
  * **The impostor commit.** `/repos/{repo}/commits/{sha}` answers for any commit
    in the repository's fork network, so a sha that is a real commit but not the
    commit of the release the comment names passes a bare "is it a commit" test.
    `--check` therefore resolves the tag the comment names (`# v5.1.0`) to its
    peeled commit and fails when the pin is a different sha.
  * **Staleness.** A pin records the release that was current when it was
    written, and a moving `@v4` would have kept following the line. A pin is
    supposed to be a decision, so this tool only *reports* a newer release on the
    pinned major line; `--strict` turns that report into a failure.

Usage:

    python tools/pin_workflow_actions.py            # report every pin
    python tools/pin_workflow_actions.py --check    # exit 1 on a bad pin or a pin
                                                    # that is not its tag's commit,
                                                    # 2 if the API cannot be read;
                                                    # no staleness report (cheap)
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
import time
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


class ApiUnavailable(RuntimeError):
    """The GitHub API could not be read (network failure, rate limit, 5xx)."""


ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def _get(path: str) -> object | None:
    """GET `path`; None on 404. A transient failure is retried, then raised as
    `ApiUnavailable` so `main` can say so instead of printing a traceback."""
    request = urllib.request.Request(f"{API}{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "revl-pin-workflow-actions")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            # 403 is how an exhausted rate limit answers; 422 is a real answer
            # `_is_commit` handles, so it is not retried.
            if error.code not in (403, 429) and error.code < 500:
                raise
            failure = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            failure = str(getattr(error, "reason", None) or error)
        if attempt + 1 < ATTEMPTS:
            time.sleep(BACKOFF_SECONDS * (2**attempt))
    raise ApiUnavailable(f"{path}: {failure}")


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


def _is_commit_cached(repo: str, sha: str, cache: dict[tuple[str, str], bool]) -> bool:
    if (repo, sha) not in cache:
        cache[(repo, sha)] = _is_commit(repo, sha)
    return cache[(repo, sha)]


def _tag_commit(repo: str, tag: str) -> str | None:
    """The commit `tag` points at in `repo`, peeling annotated tags, or None."""
    payload = _get(f"/repos/{repo}/git/ref/tags/{tag}")
    if not isinstance(payload, dict):
        return None
    obj = payload["object"]
    # An annotated tag may in principle point at another tag object; bound the walk.
    for _ in range(5):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            return None
        tag_object = _get(f"/repos/{repo}/git/tags/{obj['sha']}")
        if not isinstance(tag_object, dict):
            return None
        obj = tag_object["object"]
    return None


def _recorded_tag(comment: str | None) -> str | None:
    """The exact release tag (`vX.Y.Z`) a pin's comment names, if it names one.

    `# v4.4.0`, `# v7 (v7.6.0)` and `# release/v1 (v1.14.2)` name one; a bare
    major (`# v4`) is a moving tag and a branch note (`# stable branch @ ...`)
    names none, so those are not checked against a tag.
    """
    if not comment:
        return None
    match = re.search(r"(?<![\w./-])v\d+\.\d+\.\d+(?![\w.-])", comment)
    return match.group(0) if match else None


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
    try:
        return _main(argv)
    except ApiUnavailable as error:
        # A pin that could not be verified is not a verified pin, so this fails
        # the run (exit 2, distinct from a bad pin's 1) rather than passing.
        print(
            f"could not reach the GitHub API after {ATTEMPTS} attempts ({error}); "
            "no pin was reported good or bad",
            file=sys.stderr,
        )
        return 2


def _main(argv: list[str] | None = None) -> int:
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
    unchecked: list[str] = []
    verified = 0
    tag_commits: dict[tuple[str, str], str | None] = {}
    release_tags: dict[str, dict[str, str]] = {}
    commit_cache: dict[tuple[str, str], bool] = {}
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

        tag = _recorded_tag(comment)
        tagged = None
        if tag is not None:
            if (repo, tag) not in tag_commits:
                tag_commits[(repo, tag)] = _tag_commit(repo, tag)
            tagged = tag_commits[(repo, tag)]
        if tagged is not None and tagged == ref:
            # The tag's own peeled commit is a commit in `repo` by construction,
            # so the `/commits/{sha}` lookup would only repeat the answer.
            verified += 1
        elif not _is_commit_cached(repo, ref, commit_cache):
            problems.append(
                f"{path.name}: `{repo}@{ref}` is not a commit in {repo} -- an "
                "annotated tag's object sha resolves to nothing at checkout; use "
                "the PEELED commit"
            )
            continue
        elif tag is None:
            unchecked.append(f"{path.name}: {repo}@{ref} ({comment or 'no comment'})")
        elif tagged is None:
            problems.append(
                f"{path.name}: {repo}@{ref} names {tag} in its comment, but "
                f"{repo} has no tag {tag}"
            )
        else:
            problems.append(
                f"{path.name}: {repo}@{ref} is not the commit of {tag}; "
                f"{tag} is {tagged}"
            )

        # Staleness needs one `/tags` listing per repository, the largest share of
        # the calls, and `--check` is about whether a pin is right, not whether it
        # is the newest. So it runs for the plain report and for `--strict` only.
        if args.check and not args.strict:
            continue
        line = _pinned_major(comment)
        if line is None:
            continue
        if repo not in release_tags:
            release_tags[repo] = _releases(repo)
        newest = _newest_on_line(release_tags[repo], line)
        if newest and newest[1] != ref:
            note = f"{path.name}: {repo}@{ref} ({comment}) -- {newest[0]} exists"
            (stale if not args.strict else problems).append(note)

    for note in sorted(set(stale)):
        print(f"stale:     {note}")
    for note in sorted(set(unchecked)):
        print(f"unchecked: {note} -- no exact release tag in the comment")
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
    print(
        f"every pin is a commit; {verified} pin(s) match the commit of the tag "
        f"their comment names, {len(set(unchecked))} unchecked"
    )
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
