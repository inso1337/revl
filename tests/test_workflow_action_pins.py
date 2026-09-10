"""Every external `uses:` in .github/workflows/ is pinned to a commit (CI-4).

A workflow that names an action by a MOVING ref (`@v4`, `@main`) executes
whatever that ref resolves to on the day it runs. On this repository that is not
a theoretical exposure: `pages.yml` carries `pages: write` and `id-token: write`,
so a moved `@v4` there runs inside the `github-pages` environment holding an OIDC
token minted for it, and `codeql.yml` carries `security-events: write`. Neither
workflow triggers on `pull_request` (nor does either smoke lane), so the
reachable path is an upstream compromise or a force-pushed tag rather than an
attacker-authored PR -- which is exactly the case a commit pin closes and a tag
pin does not.

The convention is `owner/repo(/path)@<40 hex>` plus a trailing comment naming
what that sha is. ci.yml already followed it; pages.yml, codeql.yml,
site-wheel.yml, arm64-smoke.yml and x64-smoke.yml did not, and nothing in-tree
noticed. This file is the pin.

Two things this test deliberately CANNOT check, because both need the network:

  * whether the sha is the PEELED commit. `github/codeql-action@v4` is an
    ANNOTATED tag, so the `refs/tags/v4` object sha is a TAG object and pinning
    it breaks the run; the commit is one dereference further.
  * whether the pinned release is still the newest on its major line.

Both live in tools/pin_workflow_actions.py, whose `--check` mode re-resolves
every pin against the API and reports a tag object, a moved major line, or a
release that is no longer the latest. So the split is the same one the gate
census uses: the tool talks to the world, this test is hermetic.

The comment grammar is intentionally NOT prescribed. The tree legitimately holds
`# v4.4.0`, `# v7 (v7.6.0)`, `# release/v1 (v1.14.2)` and dtolnay's
`# stable branch @ 2026-08-05; ...`, because the ref really is a moving branch
there; what is asserted is that a pin carries a comment at all, so a reader can
tell what a bump would be without resolving it.

It reads the workflows as text (no PyYAML, which is not a declared dependency)
and rides the frontend job's plain `pytest tests/ -q`, like
tests/test_required_checks_pinned.py and tests/test_site_wheel_gate_runs_in_ci.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# `- uses: owner/repo@ref` / `uses: owner/repo/path@ref`, optional trailing
# comment. Anchored on the line start so a commented-out step (`# - uses: ...`)
# and a `uses:` inside a `run: |` body are not scanned.
USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<target>\S+)\s*(?:#\s*(?P<comment>\S.*?))?\s*$",
    re.MULTILINE,
)

# The 14 workflow files that constitute the CI surface. Named so that a workflow
# moved out of the scanned directory cannot silently leave the check's scope.
EXPECTED_WORKFLOWS = frozenset({
    "arm64-smoke.yml",
    "ci.yml",
    "codeql.yml",
    "pages.yml",
    "publish.yml",
    "release-dryrun.yml",
    "site-wheel.yml",
    "x64-smoke.yml",
})

# Every external action reference in the tree today, so the scan cannot pass by
# matching nothing. Grow this when a workflow adds an action.
MINIMUM_EXTERNAL_USES = 15


def _workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS.rglob("*.y*ml") if p.is_file())


def _external_uses() -> list[tuple[str, str, str | None]]:
    """(file name, `uses:` target, trailing comment) for every non-local uses."""
    found: list[tuple[str, str, str | None]] = []
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for match in USES.finditer(text):
            target = match.group("target")
            if target.startswith("./"):
                continue
            found.append((path.name, target, match.group("comment")))
    return found


def test_the_scan_covers_every_workflow_file() -> None:
    names = {p.name for p in _workflow_files()}
    assert names == set(EXPECTED_WORKFLOWS), (
        "the set of workflow files changed; add the new file here so it is "
        f"inside the pin check: {sorted(names ^ set(EXPECTED_WORKFLOWS))}"
    )


def test_the_scan_is_not_vacuous() -> None:
    found = _external_uses()
    assert len(found) >= MINIMUM_EXTERNAL_USES, (
        f"only {len(found)} external `uses:` found; the scan stopped matching, "
        "so the pin assertions below would pass without checking anything"
    )


def test_every_external_action_is_pinned_to_a_commit() -> None:
    unpinned: list[str] = []
    for name, target, _ in _external_uses():
        if "@" not in target:
            unpinned.append(f"{name}: {target} (no ref at all)")
            continue
        repo, ref = target.rsplit("@", 1)
        if len(repo.split("/")) < 2:
            unpinned.append(f"{name}: {target} (not owner/repo)")
        elif not re.fullmatch(r"[0-9a-f]{40}", ref):
            unpinned.append(f"{name}: {target} (ref is a moving tag/branch)")
    assert not unpinned, (
        "these actions are referenced by a moving ref, so what runs is whatever "
        "that ref points at on the day the job runs; pin the 40-char commit sha "
        "of the intended release (resolve annotated tags with the peeled commit, "
        "`git rev-parse v4^{}` / tools/pin_workflow_actions.py):\n  "
        + "\n  ".join(unpinned)
    )


def test_every_pin_says_which_release_it_is() -> None:
    uncommented: list[str] = []
    for name, target, comment in _external_uses():
        if not comment:
            uncommented.append(f"{name}: {target}")
    assert not uncommented, (
        "a commit sha on its own does not tell a reader what it is or what a "
        "bump would be; add a trailing comment naming the ref it was resolved "
        "from (`# v4.4.0`):\n  " + "\n  ".join(uncommented)
    )


def test_no_pin_carries_a_mutable_ref_alongside_the_commit() -> None:
    """`@<sha> # v4` is the convention; `@<sha>@v4` and `@v4` are not."""
    suspicious: list[str] = []
    for name, target, _ in _external_uses():
        if target.count("@") != 1:
            suspicious.append(f"{name}: {target}")
    assert not suspicious, (
        "a `uses:` target must carry exactly one `@`, the one before the "
        "commit sha:\n  " + "\n  ".join(suspicious)
    )
