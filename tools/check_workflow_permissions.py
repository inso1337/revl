#!/usr/bin/env python3
"""Static gate: every job's EFFECTIVE GITHUB_TOKEN permissions must support the
actions that job invokes.

WHY THIS EXISTS (issue #191). `publish.yml`'s `publish` job declared

    permissions:
      id-token: write

and nothing else. A job-level `permissions` block REPLACES the workflow-level
one rather than merging into it, so `contents` was `none` for that job and
`actions/checkout` could not have read the tag it publishes. The release would
have failed at checkout. Nobody would have found that until a release was
attempted, because the publish job never runs on a PR or on main: a green CI
certifies the jobs that RAN, and that one does not run. So it would have been
discovered under time pressure, with the tag already pushed.

The replace-not-merge rule is the trap, and it is a trap anyone can fall into
again: the block reads like an addition. This gate is the cheap half of the
answer -- it costs no runner time, it runs in the `lint` job that already gates
every merge, and it reaches jobs that no amount of running CI reaches, because
it reads the workflow rather than executing it. The other half, exercising the
build-and-check half of the release for real, is
`.github/workflows/release-dryrun.yml`.

WHAT IT CHECKS

  1. For each job, resolve the effective permission set: the job's own block if
     it has one (REPLACING, not merging), else the workflow-level block, else
     unknown (the repository default, which this tool cannot see).
  2. For each `uses:` step in that job, look up the scopes that action needs in
     the small explicit table below, and report any the job does not grant at a
     sufficient level.
  3. Follow local reusable-workflow calls (`uses: ./.github/workflows/x.yml`)
     and check the called workflow's jobs under the caller's set as a CEILING,
     which is the real rule: a called workflow can narrow what the caller
     granted, never widen it.

WHAT IT DOES NOT KNOW, and will not pretend to know.

  * The repository's DEFAULT permissions, which apply to any job with no block
    at any level. Such a job is reported as a note, never an error, because the
    tool has no way to see the setting. `--strict` promotes those notes to
    errors, which is the right setting once every workflow declares a floor.
  * What an action not in the table needs. An unknown action is SKIPPED with a
    note. Guessing would produce either false reds (which get muted) or false
    greens (which are worse). Add the entry instead -- the table is meant to
    stay small and hand-checked, not exhaustive.
  * What a `run:` step needs. A shell step that calls `gh` or curls the API
    needs scopes this tool cannot infer from the script text. Not attempted.
  * Whether a granted scope is UNNECESSARY. This gate is about under-grant,
    which fails closed at runtime in a way nobody sees until the job runs.
    Over-grant is a separate review question with a much higher false-positive
    rate, so it is not litigated here.

USAGE

    python3 tools/check_workflow_permissions.py            # gate
    python3 tools/check_workflow_permissions.py --strict   # + undeclared jobs
    python3 tools/check_workflow_permissions.py --self-test

`--self-test` reintroduces the issue-#191 bug into a synthetic workflow and
asserts this gate reports it, plus the surrounding cases (inheritance,
`{}`, `read-all`, the ceiling rule, unknown actions). It runs in `lint`
alongside the gate itself: a checker whose own logic is never exercised is the
same shape of gap as the one it was written to close.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass, field

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a finding
    sys.stderr.write(
        "check_workflow_permissions: PyYAML is required.\n"
        "  CI installs it in the `lint` job; locally use\n"
        "    uv run --with pyyaml==6.0.2 python3 tools/check_workflow_permissions.py\n"
        "  or `pip install pyyaml`.\n"
        "Refusing to run: a workflow gate that silently skips is the defect it exists to prevent.\n"
    )
    raise SystemExit(2) from None

# The GITHUB_TOKEN permission scopes, in the order GitHub documents them. Used
# to expand the `read-all` / `write-all` shorthands and to reject a typo'd
# scope name (a misspelled scope grants nothing and is silently ignored by the
# runner, which is its own quiet failure).
ALL_SCOPES = (
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "repository-projects",
    "security-events",
    "statuses",
)

LEVELS = {"none": 0, "read": 1, "write": 2}

# ---------------------------------------------------------------------------
# The action -> required-scopes table.
#
# Deliberately SMALL and hand-checked. Every entry is an action this repository
# actually uses, with the scopes its documentation requires. An action that is
# not listed here is skipped with a note rather than guessed at; add it (with
# the reason, in a comment) when the repository starts using it.
# ---------------------------------------------------------------------------

# Actions that need no GITHUB_TOKEN scope at all. Listed explicitly so they
# count as KNOWN: an empty requirement and an unknown action are different
# facts and must not be reported the same way.
NEEDS_NO_SCOPE = {
    # Toolchain installers. They download from vendor CDNs or the tool-cache and
    # touch no repository API. Their optional dependency caching goes through the
    # Actions cache service, which is authorised by ACTIONS_RUNTIME_TOKEN rather
    # than by GITHUB_TOKEN scopes.
    "actions/setup-python": "toolchain installer; no repository API",
    "actions/setup-node": "toolchain installer; no repository API",
    "actions/setup-go": "toolchain installer; no repository API",
    "actions/setup-java": "toolchain installer; no repository API",
    "astral-sh/setup-uv": "toolchain installer; no repository API",
    "dtolnay/rust-toolchain": "toolchain installer; no repository API",
    "actions/cache": "Actions cache service, authorised by ACTIONS_RUNTIME_TOKEN",
    "actions/upload-artifact": "artifact service, authorised by ACTIONS_RUNTIME_TOKEN",
    "actions/download-artifact": "artifact service, authorised by ACTIONS_RUNTIME_TOKEN",
    "actions/upload-pages-artifact": "wraps upload-artifact; same runtime token",
    # lean-action runs `lake build` in the checked-out tree; the checkout step
    # that precedes it is what needs `contents: read`.
    "leanprover/lean-action": "builds the already-checked-out tree",
}

# Actions with real scope requirements.
ACTION_SCOPES = {
    # THE ONE THAT BIT. checkout authenticates its fetch with GITHUB_TOKEN, so
    # `contents: none` leaves it unable to read the ref it was asked for.
    "actions/checkout": {"contents": "read"},
    # Pages. configure-pages reads/creates the Pages deployment configuration
    # and deploy-pages creates the deployment and presents an OIDC token to the
    # Pages service.
    "actions/configure-pages": {"pages": "write"},
    "actions/deploy-pages": {"pages": "write", "id-token": "write"},
    # CodeQL. init resolves query packs (`packages: read` for private packs);
    # analyze uploads the SARIF as a code-scanning result and reads workflow
    # metadata to attribute it.
    "github/codeql-action/init": {"contents": "read", "packages": "read"},
    "github/codeql-action/analyze": {
        "contents": "read",
        "actions": "read",
        "security-events": "write",
    },
    # PyPI upload. Its requirement is CONDITIONAL, so it is resolved in
    # required_scopes() below rather than stated flatly here: Trusted Publishing
    # mints an OIDC token and needs `id-token: write`; passing an API token in
    # `with: password:` needs no scope at all.
    "pypa/gh-action-pypi-publish": {"id-token": "write"},
}


def required_scopes(action: str, step: dict) -> dict[str, str] | None:
    """Scopes `action` needs, or None when the action is not in the table.

    `step` is the whole step mapping, because a couple of actions' requirements
    depend on their inputs.
    """
    if action in NEEDS_NO_SCOPE:
        return {}
    if action == "pypa/gh-action-pypi-publish":
        with_ = step.get("with") or {}
        # An explicit password is a long-lived API token: no OIDC, no id-token.
        if "password" in with_:
            return {}
        return {"id-token": "write"}
    return ACTION_SCOPES.get(action)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str  # "error" or "note"
    where: str  # "publish.yml:publish" or "publish.yml:publish -> ci.yml:lint"
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def error(self, where: str, message: str) -> None:
        self.findings.append(Finding("error", where, message))

    def note(self, where: str, message: str) -> None:
        self.findings.append(Finding("note", where, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def notes(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "note"]


# ---------------------------------------------------------------------------
# Permission-set algebra
# ---------------------------------------------------------------------------


def normalize_permissions(raw, where: str, report: Report) -> dict[str, str] | None:
    """A `permissions:` value as a {scope: level} map, or None if absent.

    Returns `{}` for `permissions: {}` -- an empty block grants nothing, which
    is a DECLARED set, not an unknown one.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw == "read-all":
            return dict.fromkeys(ALL_SCOPES, "read")
        if raw == "write-all":
            return dict.fromkeys(ALL_SCOPES, "write")
        report.error(where, f"unrecognised permissions shorthand {raw!r}")
        return {}
    if not isinstance(raw, dict):
        report.error(where, f"permissions must be a mapping or a shorthand, got {type(raw).__name__}")
        return {}
    out: dict[str, str] = {}
    for scope, level in raw.items():
        scope = str(scope)
        level = str(level)
        if scope not in ALL_SCOPES:
            report.error(where, f"unknown permission scope {scope!r}; the runner ignores it silently")
            continue
        if level not in LEVELS:
            report.error(where, f"permission {scope}: {level!r} is not one of read/write/none")
            continue
        out[scope] = level
    return out


def grants(effective: dict[str, str], scope: str, level: str) -> bool:
    return LEVELS.get(effective.get(scope, "none"), 0) >= LEVELS[level]


def intersect(ceiling: dict[str, str] | None, declared: dict[str, str] | None) -> dict[str, str] | None:
    """The set a called workflow actually gets: it can narrow, never widen."""
    if ceiling is None:
        return declared
    if declared is None:
        return ceiling
    return {
        scope: min(ceiling.get(scope, "none"), declared.get(scope, "none"), key=lambda lv: LEVELS[lv])
        for scope in set(ceiling) | set(declared)
    }


def narrowed_scopes(workflow_level: dict[str, str] | None, job_level: dict[str, str] | None) -> list[str]:
    """Scopes the workflow granted that the job's own block silently drops.

    This is the replace-not-merge trap made visible. Reported as a note even
    when no known action needs the dropped scope, because the next step added
    to that job is where it turns into an outage.
    """
    if workflow_level is None or job_level is None:
        return []
    return sorted(
        scope
        for scope, level in workflow_level.items()
        if LEVELS[level] > LEVELS.get(job_level.get(scope, "none"), 0)
    )


# ---------------------------------------------------------------------------
# Walking the workflows
# ---------------------------------------------------------------------------


def step_actions(job: dict) -> list[tuple[str, dict]]:
    """(action-name-without-ref, step) for every `uses:` step in the job."""
    out = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str):
            out.append((uses.split("@", 1)[0].strip(), step))
    return out


def check_step_scopes(action: str, step: dict, effective: dict[str, str], where: str, report: Report) -> None:
    needs = required_scopes(action, step)
    if needs is None:
        report.note(where, f"`{action}` is not in the action table; its scopes were not checked")
        return
    for scope, level in sorted(needs.items()):
        if not grants(effective, scope, level):
            have = effective.get(scope, "none")
            report.error(
                where,
                f"`{action}` needs `{scope}: {level}` but this job's effective "
                f"permissions give `{scope}: {have}`",
            )


def check_job(
    name: str,
    job: dict,
    workflow_perms: dict[str, str] | None,
    ceiling: dict[str, str] | None,
    label: str,
    workflows: dict[str, dict],
    report: Report,
    strict: bool,
    stack: tuple[str, ...],
) -> None:
    where = f"{label}:{name}"
    job_perms = normalize_permissions(job.get("permissions"), where, report)
    # The rule this whole gate exists for: a job block REPLACES the workflow
    # block, so the fallback is the workflow set, never a merge of the two.
    declared = job_perms if job_perms is not None else workflow_perms
    effective = intersect(ceiling, declared)

    for scope in narrowed_scopes(workflow_perms, job_perms):
        report.note(
            where,
            f"the job's own `permissions` block drops `{scope}` that the workflow granted "
            f"(a job block REPLACES the workflow block, it does not merge into it)",
        )

    if effective is None:
        message = (
            "no `permissions` block on the job or the workflow, so the effective set is the "
            "repository default and cannot be checked here"
        )
        (report.error if strict else report.note)(where, message)
        return

    called = job.get("uses")
    if isinstance(called, str):
        check_called_workflow(called, effective, where, workflows, report, strict, stack)
        return

    for action, step in step_actions(job):
        check_step_scopes(action, step, effective, where, report)


def check_called_workflow(
    ref: str,
    ceiling: dict[str, str],
    where: str,
    workflows: dict[str, dict],
    report: Report,
    strict: bool,
    stack: tuple[str, ...],
) -> None:
    key = pathlib.PurePosixPath(ref).name if ref.startswith("./") else ref
    if key not in workflows:
        report.note(where, f"reusable workflow `{ref}` is not local to this repository; not followed")
        return
    if key in stack:
        report.note(where, f"reusable workflow `{ref}` is already on the call stack; not followed again")
        return
    check_workflow(key, workflows[key], workflows, report, strict, ceiling, where, stack + (key,))


def check_workflow(
    label: str,
    doc: dict,
    workflows: dict[str, dict],
    report: Report,
    strict: bool,
    ceiling: dict[str, str] | None = None,
    prefix: str = "",
    stack: tuple[str, ...] = (),
) -> None:
    display = f"{prefix} -> {label}" if prefix else label
    workflow_perms = normalize_permissions(doc.get("permissions"), display, report)
    for name, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            check_job(
                str(name), job, workflow_perms, ceiling, display, workflows, report, strict, stack
            )


def load_workflows(directory: pathlib.Path) -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for path in sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise SystemExit(f"{path}: does not parse as a YAML mapping")
        docs[path.name] = doc
    return docs


def run(workflows: dict[str, dict], strict: bool, entry: list[str] | None = None) -> Report:
    """Check every workflow. Reusable workflows are ALSO checked standalone,
    because they run on their own triggers too; findings are deduplicated."""
    report = Report()
    for label in entry if entry is not None else sorted(workflows):
        check_workflow(label, workflows[label], workflows, report, strict, stack=(label,))
    seen = set()
    deduped = []
    for finding in report.findings:
        key = (finding.severity, finding.where, finding.message)
        if key not in seen:
            seen.add(key)
            deduped.append(finding)
    report.findings = deduped
    return report


# ---------------------------------------------------------------------------
# Self-test: the gate, run against the bug it was written for.
# ---------------------------------------------------------------------------

# Issue #191, verbatim in shape: the publish job names `id-token: write` and
# nothing else, so `contents` is `none` for that job and the checkout above it
# cannot read the tag.
BUGGY_PUBLISH = """
name: publish
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - uses: pypa/gh-action-pypi-publish@release/v1
"""

FIXED_PUBLISH = BUGGY_PUBLISH.replace(
    "      id-token: write", "      contents: read\n      id-token: write"
)

SELF_TEST_CASES = [
    (
        "issue #191, as it stood: job block replaces the workflow block",
        {"publish.yml": BUGGY_PUBLISH},
        ["`actions/checkout` needs `contents: read`"],
    ),
    (
        "issue #191, with the fix PR #190 applied",
        {"publish.yml": FIXED_PUBLISH},
        [],
    ),
    (
        "a job with no block of its own inherits the workflow's",
        {
            "w.yml": """
permissions:
  contents: read
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
"""
        },
        [],
    ),
    (
        "`permissions: {}` grants nothing, and that is a declared set",
        {
            "w.yml": """
permissions:
  contents: read
jobs:
  build:
    permissions: {}
    steps:
      - uses: actions/checkout@v4
"""
        },
        ["`actions/checkout` needs `contents: read`"],
    ),
    (
        "`read-all` covers checkout but cannot cover a write scope",
        {
            "w.yml": """
jobs:
  build:
    permissions: read-all
    steps:
      - uses: actions/checkout@v4
      - uses: pypa/gh-action-pypi-publish@release/v1
"""
        },
        ["`pypa/gh-action-pypi-publish` needs `id-token: write`"],
    ),
    (
        "an API token instead of Trusted Publishing needs no id-token",
        {
            "w.yml": """
permissions:
  contents: read
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: x
"""
        },
        [],
    ),
    (
        "an action outside the table is skipped with a note, never guessed",
        {
            "w.yml": """
permissions:
  contents: read
jobs:
  build:
    steps:
      - uses: some-org/some-action@v1
"""
        },
        [],
    ),
    (
        "a called workflow cannot widen what the caller granted",
        {
            "caller.yml": """
permissions: {}
jobs:
  call:
    uses: ./.github/workflows/called.yml
""",
            "called.yml": """
permissions:
  contents: read
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
""",
        },
        ["`actions/checkout` needs `contents: read`"],
    ),
    (
        "a scope silently dropped by a job block is noted even when nothing needs it yet",
        {
            "w.yml": """
permissions:
  contents: read
  packages: read
jobs:
  build:
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
"""
        },
        [],
    ),
]


def self_test() -> int:
    failures = 0
    for title, sources, expected in SELF_TEST_CASES:
        docs = {name: yaml.safe_load(text) for name, text in sources.items()}
        entry = ["caller.yml"] if "caller.yml" in docs else None
        report = run(docs, strict=False, entry=entry)
        got = [f.message for f in report.errors]
        ok = len(got) == len(expected) and all(
            any(want in message for message in got) for want in expected
        )
        print(f"  {'PASS' if ok else 'FAIL'}  {title}")
        if not ok:
            failures += 1
            print(f"        expected {len(expected)} error(s) matching {expected}")
            for message in got:
                print(f"        got: {message}")
    # The dropped-scope note is the replace-not-merge trap made visible; assert
    # it fires on the last case above, which grants everything its steps need.
    docs = {"w.yml": yaml.safe_load(SELF_TEST_CASES[-1][1]["w.yml"])}
    notes = [f.message for f in run(docs, strict=False).notes]
    ok = any("drops `packages`" in message for message in notes)
    print(f"  {'PASS' if ok else 'FAIL'}  the dropped scope is reported as a note")
    if not ok:
        failures += 1
        for message in notes:
            print(f"        got note: {message}")
    print()
    if failures:
        print(f"self-test FAILED: {failures} case(s)")
        return 1
    print(f"self-test passed: {len(SELF_TEST_CASES) + 1} cases")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--workflows",
        default=str(pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"),
        help="directory of workflow files (default: .github/workflows)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail a job whose effective set is the repository default (no block anywhere)",
    )
    parser.add_argument("--self-test", action="store_true", help="run this gate against the bug it was written for")
    parser.add_argument("--quiet-notes", action="store_true", help="print errors only")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    directory = pathlib.Path(args.workflows)
    if not directory.is_dir():
        print(f"error: no such directory: {directory}")
        return 2
    report = run(load_workflows(directory), strict=args.strict)

    if report.notes and not args.quiet_notes:
        print("notes (not failures):")
        for finding in report.notes:
            print(f"  {finding.where}: {finding.message}")
        print()
    for finding in report.errors:
        print(f"::error::{finding.where}: {finding.message}")
        print(f"  {finding.where}: {finding.message}")
    if report.errors:
        print()
        print(f"workflow permission gate FAILED: {len(report.errors)} finding(s).")
        print("A job-level `permissions` block REPLACES the workflow-level one; name every scope the job needs.")
        return 1
    print("workflow permission gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
