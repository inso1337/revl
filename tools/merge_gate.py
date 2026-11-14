#!/usr/bin/env python3
"""The merge-time gate: a pull request does not land while any check is red.

WHY THIS EXISTS (issue #2861). Branch protection names SEVEN required contexts
(`lint` and the six `backend-*` jobs). The four `root-suite-affected` shards,
the largest test surface on a pull request, are not among them, so a green
required rollup says nothing about them. #2620 merged with
`root-suite-affected (4)` red (a real guard-order regression that #2840 fixed
afterwards), and the read_text and open-encoding guards were earlier found red
on merge refs while every required context was green. In both cases the red was
discovered after landing.

THE DECISION. Option 2 of the issue: the four shards stay advisory, which keeps
the required rollup fast, and the rule moves to the merge boundary. This tool
IS that boundary. `merge_gate.py merge <pr>` reads every check on the pull
request, refuses while any is not green, and only then calls `gh pr merge`.
It applies to every non-required job, not to the shards by name, so a job added
to ci.yml tomorrow is covered without anyone editing a list.

THE RULE. A merge is refused while any check on the pull request is red and
not superseded. For each check context (workflow plus job name) only the
newest run counts, because a re-run supersedes the run it repeats. That newest
run is then judged:

  * SUCCESS and NEUTRAL pass. SKIPPED is ignored: a job the `changes` job
    skipped is not a check that ran.
  * FAILURE, TIMED_OUT, STARTUP_FAILURE, ACTION_REQUIRED and ERROR refuse.
  * CANCELLED refuses. A cancelled run is "not green", not "absent": reading it
    as absent would let a run cancelled by a concurrency group, or by a
    runner being reclaimed, stand in for a pass it never gave.
  * A run that has not finished refuses. The merge waits for the slowest job,
    exactly as a required context would make it.
  * A run whose base commit is not the CURRENT base tip refuses, whatever it
    concluded. A green on an old base certifies a tree that no longer lands.
    GitHub records the base sha on the workflow run
    (`pull_requests[].base.sha`); a run that records none is refused too,
    because its base cannot be shown to be the tip.
  * No check at all refuses. An empty rollup (the window right after a push,
    before the workflows register) or one where everything was skipped
    certifies nothing.

The refusal names every offending context and the reason, so the seat knows
which job to read or re-run.

WHAT THIS DOES NOT DO. It is a tool, not branch protection: `gh pr merge`
typed by hand still works, and so does the web button. The only server-side
enforcement is promoting the contexts to required checks (option 1), which
slows every merge by the 20 to 45 minutes of the longest job and which, done
by name, leaves the next advisory job in the same gap. The rule therefore
lives where the merge procedure lives (docs/process.md, "Merging") and the
merge goes through this tool.

AUTO-MERGE. `--auto` is refused. GitHub's auto-merge fires the moment the
REQUIRED contexts are green, which is exactly before the advisory shards have
finished, so it cannot honour this rule. Wait for the checks to finish, then
merge through this tool.

WAIVERS. `--waive CONTEXT=REASON` accepts one named offending context (written
as the verdict prints it, workflow then job: `ci / census-artifact`) that is red for a
reason the pull request did not cause (docs/process.md, "A red check is not
always yours"). The reason is mandatory and is printed with the verdict. A
waiver is for one invocation; nothing persists.

USAGE.
    tools/merge_gate.py check 123
    tools/merge_gate.py merge 123 --merge
    tools/merge_gate.py merge 123 --squash --waive 'ci / census-artifact=stale ref, re-run is green on main'

`check` exits 0 when the merge is allowed and 1 when it is refused; both
print the verdict. `merge` runs the gate and then
`gh pr merge <pr> --match-head-commit <the head it judged> <extra args>`, so
the merge cannot land a head the gate did not read. Only the merge-method and
message flags of `gh pr merge` pass through (ALLOWED_MERGE_FLAGS); any other
flag, including `--repo`, is refused. Exit 2 is a usage or
`gh` failure. `REVL_MERGE_GH` names the `gh` binary to use (default `gh`),
which is how the merge account's wrapper is selected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Sequence

DEFAULT_REPO = "inso1337/revl"

# A conclusion the gate reads as "this check did not pass".
RED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
)
PASS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL"})
SKIPPED_CONCLUSIONS = frozenset({"SKIPPED"})
# Legacy commit statuses (StatusContext nodes) use `state`, not `conclusion`;
# only SUCCESS passes.
STATUS_PENDING = frozenset({"PENDING", "EXPECTED"})

# Flags `merge` refuses with a reason: each defeats the gate.
REFUSED_MERGE_FLAGS = {
    "--auto": (
        "auto-merge fires when the REQUIRED contexts are green, before the "
        "advisory shards finish, so it cannot honour this gate"
    ),
    "--admin": "an admin merge bypasses the checks this gate exists to read",
    "--match-head-commit": (
        "the gate passes the head sha it judged; supplying another would "
        "merge a head it did not read"
    ),
    "--repo": "the gate judged --repo of this invocation; merge that one",
    "-R": "the gate judged --repo of this invocation; merge that one",
}
# What passes through to `gh pr merge`. A flag that takes a value is True.
ALLOWED_MERGE_FLAGS = {
    "-m": False, "--merge": False,
    "-s": False, "--squash": False,
    "-r": False, "--rebase": False,
    "-d": False, "--delete-branch": False,
    "-t": True, "--subject": True,
    "-b": True, "--body": True,
    "-F": True, "--body-file": True,
}

Gh = Callable[[Sequence[str]], str]


class GateError(RuntimeError):
    """The gate could not read what it needs. Never read as a pass."""


@dataclass(frozen=True)
class Check:
    """One check as GitHub reports it, reduced to what the rule reads."""

    context: str
    kind: str  # "run" or "status"
    state: str  # "pass" | "red" | "cancelled" | "pending" | "skipped"
    detail: str
    run_id: int | None = None
    order: str = ""  # sortable recency: newest run of a context wins


@dataclass
class Snapshot:
    number: int
    head: str
    base_ref: str
    base_tip: str
    checks: list[Check]
    # run id -> base sha GitHub recorded for that run ("" when it recorded none)
    run_bases: dict[int, str] = field(default_factory=dict)
    state: str = "OPEN"


@dataclass(frozen=True)
class Refusal:
    context: str
    reason: str


@dataclass
class Verdict:
    number: int
    head: str
    refusals: list[Refusal]
    waived: list[tuple[str, str]]
    judged: int  # contexts the rule looked at (after superseding and skips)

    @property
    def allowed(self) -> bool:
        return not self.refusals

    def render(self) -> str:
        sha = self.head[:12]
        lines = []
        if self.allowed:
            lines.append(
                f"merge gate: PR #{self.number} @ {sha} ALLOWED "
                f"({self.judged} contexts judged, none red, cancelled, "
                f"pending or stale)"
            )
        else:
            lines.append(
                f"merge gate: PR #{self.number} @ {sha} REFUSED "
                f"({len(self.refusals)} of {self.judged} contexts)"
            )
            for r in self.refusals:
                lines.append(f"  - {r.context}: {r.reason}")
        for ctx, why in self.waived:
            lines.append(f"  waived: {ctx} ({why})")
        return "\n".join(lines)


def _stamp(value) -> str:
    """A timestamp, or "" for none. `gh` encodes a queued run's start as the
    zero time `0001-01-01T00:00:00Z`, which would sort as the OLDEST run and
    let an older green beat a queued re-run."""
    text = str(value or "")
    return "" if text.startswith("0001-") else text


def _run_id(url: str) -> int | None:
    m = re.search(r"/actions/runs/(\d+)", url or "")
    return int(m.group(1)) if m else None


def parse_rollup(rollup: Sequence[dict]) -> list[Check]:
    """`gh pr view --json statusCheckRollup` nodes -> Checks."""
    out: list[Check] = []
    for node in rollup:
        kind = node.get("__typename") or (
            "StatusContext" if "context" in node else "CheckRun"
        )
        if kind == "StatusContext":
            name = str(node.get("context") or "")
            raw = str(node.get("state") or "").upper()
            if raw == "SUCCESS":
                state = "pass"
            elif raw in STATUS_PENDING:
                state = "pending"
            else:
                # FAILURE, ERROR and any state this tool does not know: only
                # SUCCESS is a pass.
                state = "red"
            out.append(
                Check(name, "status", state, f"status {raw or 'UNKNOWN'}",
                      None, _stamp(node.get("startedAt")))
            )
            continue
        workflow = str(node.get("workflowName") or "")
        name = str(node.get("name") or "")
        context = f"{workflow} / {name}" if workflow else name
        status = str(node.get("status") or "").upper()
        conclusion = str(node.get("conclusion") or "").upper()
        if status != "COMPLETED":
            state, detail = "pending", f"not finished ({status or 'UNKNOWN'})"
        elif conclusion in PASS_CONCLUSIONS:
            state, detail = "pass", conclusion
        elif conclusion in SKIPPED_CONCLUSIONS:
            state, detail = "skipped", conclusion
        elif conclusion == "CANCELLED":
            state, detail = "cancelled", "CANCELLED"
        elif conclusion in RED_CONCLUSIONS:
            state, detail = "red", conclusion
        else:
            # Unknown or empty conclusion on a finished run: not a pass.
            state, detail = "red", f"unrecognised conclusion {conclusion or 'none'!r}"
        order = _stamp(node.get("startedAt")) or _stamp(node.get("completedAt"))
        out.append(
            Check(context, "run", state, detail, _run_id(node.get("detailsUrl", "")), order)
        )
    return out


def newest_per_context(checks: Sequence[Check]) -> list[Check]:
    """A re-run supersedes the run it repeats: keep the newest per context.

    An unfinished run sorts as newest, since it was created after every run
    that has finished.
    """
    best: dict[str, Check] = {}
    for c in checks:
        cur = best.get(c.context)
        if cur is None or _recency(c) >= _recency(cur):
            best[c.context] = c
    return list(best.values())


def _recency(c: Check) -> tuple[int, str, int]:
    # An unfinished run is the newest: it is a re-run queued after every run
    # that has finished. Then by start time, then by run id.
    return (1 if c.state == "pending" else 0, c.order, c.run_id or 0)


def evaluate(snap: Snapshot, waivers: dict[str, str] | None = None) -> Verdict:
    waivers = dict(waivers or {})
    refusals: list[Refusal] = []
    waived: list[tuple[str, str]] = []
    judged = 0
    for c in sorted(newest_per_context(snap.checks), key=lambda c: c.context):
        if c.state == "skipped":
            continue
        judged += 1
        problem: str | None = None
        if c.state == "red":
            problem = f"red ({c.detail})"
        elif c.state == "cancelled":
            problem = "cancelled; a cancelled run is not a pass, re-run it"
        elif c.state == "pending":
            problem = f"not finished ({c.detail}); wait for it"
        elif c.state == "pass" and c.kind == "run":
            recorded = snap.run_bases.get(c.run_id or 0, "")
            if not recorded:
                problem = (
                    "green, but GitHub recorded no base commit for the run, "
                    "so it cannot be shown to be on the current "
                    f"{snap.base_ref} tip; re-run it"
                )
            elif recorded != snap.base_tip:
                problem = (
                    f"green, but stale: it ran on base {recorded[:12]}, "
                    f"{snap.base_ref} is now {snap.base_tip[:12]}; re-run it"
                )
        if problem is None:
            continue
        if c.context in waivers:
            waived.append((c.context, waivers.pop(c.context)))
            continue
        refusals.append(Refusal(c.context, problem))
    for ctx in waivers:
        # A waiver that matches nothing is a typo, and a typo must not read as
        # a waived red.
        refusals.append(Refusal(
            ctx, "--waive names no offending context (the name is written "
            "as the verdict prints it, workflow then job)"))
    if judged == 0:
        refusals.append(Refusal(
            "(no checks)",
            "no check ran on this head, so nothing certifies it; wait for "
            "the workflows to register and finish"))
    return Verdict(snap.number, snap.head, refusals, waived, judged)


def make_gh(binary: str | None = None) -> Gh:
    exe = binary or os.environ.get("REVL_MERGE_GH") or "gh"

    def run(args: Sequence[str]) -> str:
        try:
            proc = subprocess.run(
                [exe, *args], capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise GateError(f"cannot run {exe}: {exc}") from exc
        if proc.returncode != 0:
            raise GateError(
                f"{exe} {' '.join(args[:3])} failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    return run


def read_base_tip(repo: str, base_ref: str, gh: Gh) -> str:
    out = gh(["api", f"repos/{repo}/branches/{base_ref}", "--jq", ".commit.sha"])
    tip = out.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tip):
        raise GateError(f"could not read the {base_ref} tip, got {tip!r}")
    return tip


def gather(number: int, repo: str, gh: Gh) -> Snapshot:
    raw = gh([
        "pr", "view", str(number), "--repo", repo,
        "--json", "headRefOid,baseRefName,state,statusCheckRollup",
    ])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateError(f"gh pr view returned unparseable output: {exc}") from exc
    head = data.get("headRefOid") or ""
    base_ref = data.get("baseRefName") or ""
    if not head or not base_ref:
        raise GateError("gh pr view named no head commit or base branch")
    checks = parse_rollup(data.get("statusCheckRollup") or [])
    tip = read_base_tip(repo, base_ref, gh)
    bases: dict[int, str] = {}
    for c in newest_per_context(checks):
        if c.kind != "run" or c.state != "pass" or c.run_id is None:
            continue
        if c.run_id in bases:
            continue
        try:
            run = json.loads(gh(["api", f"repos/{repo}/actions/runs/{c.run_id}"]))
        except json.JSONDecodeError as exc:
            raise GateError(f"run {c.run_id} record is unparseable: {exc}") from exc
        recorded = ""
        for pr in run.get("pull_requests") or []:
            if pr.get("number") == number:
                recorded = str((pr.get("base") or {}).get("sha") or "")
        bases[c.run_id] = recorded
    return Snapshot(number, head, base_ref, tip, checks, bases,
                    str(data.get("state") or "OPEN"))


def _parse_waivers(items: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        ctx, sep, why = item.partition("=")
        if not sep or not ctx.strip() or not why.strip():
            raise GateError(f"--waive wants CONTEXT=REASON with a reason, got {item!r}")
        if ctx.strip() in out:
            raise GateError(f"--waive names {ctx.strip()!r} twice")
        out[ctx.strip()] = why.strip()
    return out


def _check_merge_flags(extra: Sequence[str]) -> None:
    """Allowlist the flags passed on to `gh pr merge`."""
    it = iter(extra)
    for tok in it:
        bare, eq, _ = tok.partition("=")
        if bare in REFUSED_MERGE_FLAGS:
            raise GateError(f"{bare} is refused: {REFUSED_MERGE_FLAGS[bare]}")
        if bare not in ALLOWED_MERGE_FLAGS:
            raise GateError(
                f"{tok!r} is not a flag this gate passes to gh pr merge "
                f"(allowed: {', '.join(sorted(ALLOWED_MERGE_FLAGS))})")
        if ALLOWED_MERGE_FLAGS[bare] and not eq:
            if next(it, None) is None:
                raise GateError(f"{bare} needs a value")
        elif eq and not ALLOWED_MERGE_FLAGS[bare]:
            raise GateError(f"{bare} takes no value")


def run_gate(number: int, repo: str, gh: Gh, waivers: dict[str, str]) -> tuple[Snapshot, Verdict]:
    snap = gather(number, repo, gh)
    if snap.state != "OPEN":
        raise GateError(f"PR #{number} is {snap.state}, not open")
    return snap, evaluate(snap, waivers)


def main(argv: Sequence[str] | None = None, gh: Gh | None = None,
         out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    ap = argparse.ArgumentParser(
        prog="merge_gate.py",
        description="Refuse a merge while any check on the pull request is red, "
        "cancelled, unfinished or stale. See the module docstring.",
        allow_abbrev=False,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "merge"):
        p = sub.add_parser(name, allow_abbrev=False)
        p.add_argument("pr", type=int)
        p.add_argument("--repo", default=DEFAULT_REPO)
        p.add_argument("--waive", action="append", default=[],
                       metavar="CONTEXT=REASON")
    args, extra = ap.parse_known_args(argv)
    if args.cmd == "check" and extra:
        err.write(f"merge_gate.py: unexpected arguments: {' '.join(extra)}\n")
        return 2
    gh = gh or make_gh()
    try:
        _check_merge_flags(extra)
        waivers = _parse_waivers(args.waive)
        snap, verdict = run_gate(args.pr, args.repo, gh, waivers)
        out.write(verdict.render() + "\n")
        if not verdict.allowed:
            return 1
        if args.cmd == "check":
            return 0
        # The base may have moved while the checks were read; the verdict is
        # only true against the tip it was judged on.
        if read_base_tip(args.repo, snap.base_ref, gh) != snap.base_tip:
            err.write(
                f"merge gate: {snap.base_ref} moved while the checks were "
                f"read; nothing merged, run the gate again\n"
            )
            return 1
        gh(["pr", "merge", str(args.pr), "--repo", args.repo,
            "--match-head-commit", snap.head, *extra])
        out.write(f"merge gate: PR #{args.pr} merge requested at {snap.head[:12]}\n")
        return 0
    except GateError as exc:
        err.write(f"merge_gate.py: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
