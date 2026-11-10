#!/usr/bin/env python3
"""Red-before / green-after evidence receipts for a changed test.

WHY THIS EXISTS (issue #2849). A lane proved a change by hand-typing two
numbers into a PR body: the test "fails on main" and passes here. Two failures
in one batch showed how weak that is. One "red" was a mutation (the author
broke the code on purpose) rather than a base failure, and one pair of counts
was stale (78 written, 115 measured). A reviewer cannot tell either from the
text, so the reviewer asks for the counts again, in most reviews.

This tool makes the claim checkable. It exports the base ref with `git
archive`, overlays the head's changed test files onto that export, runs the
requested pytest node there, then exports the head commit the same way and
runs the node in that export, and prints one paste-ready block naming the
exact commands, the base failure line, the head pass line and the diff range.
It refuses to call anything evidence when the node passes on base (no
red-before), when an export cannot be built, when the head side does not
pass, when either run outlives ``--timeout``, or when the working tree is not
the commit the receipt would name (a tracked edit or an untracked file).

    python3 tools/evidence_receipt.py --base origin/main \
        --node tests/test_foo.py::test_bar

The base side is the HEAD test files against BASE everything else: the only
arrangement in which a genuinely new test can be red before the implementation
exists. The receipt names every overlaid path, so a reviewer can see the
method instead of trusting the prose. A receipt is evidence only for the
commit it names: the tool refuses to run from a dirty tree, because a green
side measured on uncommitted edits is the stale-count failure again.

The base side runs the head test files; the implementation stays at the base
ref unless ``--overlay`` names a file explicitly.

Both sides run in a `git archive` export, never in the live worktree. What
binds the head run to the head sha is that sha itself: it is read once, before
either run, and the head export is `git archive <head sha>`, so the head side
sees only committed files. An ignored or untracked file in the worktree (a
generated module, a local build) cannot turn a red-before into a green-after,
and a commit or checkout made while a run is in flight does not change what
either run measured.

Exit status: 0 receipt printed, 1 the claim is rejected (no red-before, or no
green-after), 2 the receipt cannot be produced (bad ref, uncommitted or
untracked changes, unreadable run, or a run stopped by ``--timeout``, which
prints ``TIMED OUT``).
"""

from __future__ import annotations

import argparse
import io
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A count in the terminal summary line, e.g. "3 passed", "1 error" (same rule
# as tools/affected_pytest_receipt.py; that tool's `_KINDS` map is not copied
# here, see `parse_summary`, because a redeclared closed vocabulary is what the
# mirror gate (issue #1285) reports).
_COUNT = re.compile(
    r"(?<![\w.])(\d+) "
    r"(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b"
)
_SUMMARY = re.compile(
    r"^(?P<body>.+?)\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^)]*\))?$"
)
_NO_TESTS = re.compile(r"\bno tests ran\b")

# The pytest flags a receipt run always adds: `-q` for the one-line summary the
# receipt quotes, and no cache so the base export and the head tree are left
# untouched.
_PYTEST_FLAGS = ("-q", "-p", "no:cacheprovider")

# pytest cuts its short summary ("FAILED node - message") at the terminal
# width, which is 80 columns for a child with no terminal. A wide COLUMNS keeps
# the cause whole in the line the receipt quotes.
_CHILD_COLUMNS = "4096"

# Seconds each pytest run may take before the tool stops it and refuses.
DEFAULT_TIMEOUT = 600.0


class Refused(Exception):
    """The run cannot produce evidence: an environment or usage problem."""


class NoRedBefore(Refused):
    """The node passed (or skipped) on base, so there is nothing to prove."""


class NoGreenAfter(Refused):
    """The node did not pass on head, so the receipt's green side is missing."""


class TimedOut(Refused):
    """A pytest run outlived ``--timeout``; a run that never ended is no side."""


@dataclass(frozen=True)
class Counts:
    """What pytest's summary line says happened."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    deselected: int = 0
    found: bool = False
    no_tests: bool = False
    summary_line: str = ""

    @property
    def executed(self) -> int:
        return (self.passed + self.failed + self.errors
                + self.xfailed + self.xpassed)


@dataclass(frozen=True)
class Run:
    """One pytest invocation, its command, its exit status and its output."""

    command: tuple[str, ...]
    cwd: Path
    exit_code: int
    output: str
    counts: Counts


def parse_summary(output: str) -> Counts:
    """Read pytest -q's final summary line, or report that there was none.

    The LAST matching line wins, so a test that prints something that looks
    like a summary cannot be mistaken for pytest's own. Anything unreadable
    returns ``found=False`` rather than an all-zero pass.
    """
    text = _ANSI.sub("", output or "")
    for raw in reversed(text.splitlines()):
        line = raw.strip().strip("=").strip()
        if not line:
            continue
        match = _SUMMARY.match(line)
        if not match:
            continue
        body = match.group("body")
        if _NO_TESTS.search(body):
            return Counts(found=True, no_tests=True, summary_line=line)
        found: dict[str, int] = {}
        for number, kind in _COUNT.findall(body):
            # Every summary word is already a Counts field name except the
            # singular "error"; no map is needed to rename the rest.
            key = "errors" if kind == "error" else kind
            found[key] = found.get(key, 0) + int(number)
        if found:
            return Counts(found=True, summary_line=line, **found)
    return Counts()


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout, or refuse with git's message."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise Refused(
            f"git {' '.join(args)} failed in {repo}: "
            + (detail[-1] if detail else f"exit {proc.returncode}"))
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    """True when git accepts the query. A presence probe, not a read."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def _git_archive(repo: Path, sha: str, side: str = "base") -> bytes:
    """A commit's tree as a tar archive, read without touching the repository."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Refused(
            f"the {side} export cannot be built: git archive {sha} failed: "
            + (detail[-1] if detail else f"exit {proc.returncode}"))
    return proc.stdout


def _commit(repo: Path, ref: str) -> str:
    try:
        return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except Refused:
        raise Refused(f"cannot resolve base {ref!r} to a commit") from None


def _head_info(repo: Path) -> tuple[str, str]:
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    # A detached worktree is legitimate; a branch name is only nicer to read.
    proc = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True, text=True)
    branch = proc.stdout.strip() if proc.returncode == 0 else ""
    return head, branch or "HEAD"


def _require_committed(repo: Path) -> None:
    """A receipt pins a commit; the working tree has to be that commit.

    The measurement no longer depends on this check: the head side runs in an
    export of the head sha, so a tracked edit or an untracked module is never
    run. The check stays for the claim. A receipt is pasted as the evidence for
    the author's change, and a tree with edits git does not have at HEAD is not
    that change: a green on the commit says nothing about the edits about to be
    committed on top of it, which is the stale-count failure of issue #2849.
    Ignored build output is not part of the change and stays allowed (it is
    not in either export).
    """
    state = _git(repo, "status", "--porcelain",
                 "--untracked-files=all").strip()
    if state:
        shown = ", ".join(line[3:] for line in state.splitlines()[:5])
        raise Refused(
            "the working tree has uncommitted or untracked changes ("
            + shown + "); the receipt must name the commit it measures. "
            "Commit or remove them first.")


def _is_test_path(path: str) -> bool:
    """Test support the base side needs from head, by path shape."""
    name = Path(path).name
    return (path == "conftest.py" or path.startswith("tests/")
            or name == "conftest.py" or name.startswith("test_"))


def _overlay_paths(repo: Path, base: str, head: str,
                   nodes: list[str], extra: list[str]) -> list[str]:
    """Head-side files copied onto the base export.

    Every changed test path, the node's own file, and any explicit
    ``--overlay``. Implementation files are deliberately NOT included: the
    red side must run the implementation the base ref has.
    """
    changed = _git(repo, "diff", "--name-only", f"{base}...{head}").splitlines()
    changed_tests = {
        path for path in changed if path.strip() and _is_test_path(path)}
    explicit = set(extra)
    for node in nodes:
        explicit.add(node.split("::", 1)[0])
    present = []
    for path in sorted(changed_tests | explicit):
        if not _git_ok(repo, "cat-file", "-e", f"{head}:{path}"):
            if path in explicit:
                raise Refused(
                    f"{path} is not in {head[:12]}; commit it before asking "
                    "for a receipt")
            # A test deleted by the change has no head content to overlay.
            continue
        present.append(path)
    return present


def _export(repo: Path, sha: str, dest: Path, side: str) -> None:
    """Unpack ``sha``'s tree under ``dest``; refuse when it cannot be built."""
    data = _git_archive(repo, sha, side)
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(dest, filter="data")
            else:
                archive.extractall(dest)
    except (tarfile.TarError, OSError) as exc:
        raise Refused(f"the {side} export cannot be unpacked: {exc}") from None


def _overlay(repo: Path, head: str, dest: Path, paths: list[str]) -> None:
    for path in paths:
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{head}:{path}"],
            capture_output=True)
        if blob.returncode != 0:
            raise Refused(f"cannot read {path} at {head[:12]} from git")
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)


def _stop(proc: subprocess.Popen) -> None:
    """Kill the run and everything it started, then reap it."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
    proc.communicate()


def _run_pytest(python: str, cwd: Path, nodes: list[str],
                pythonpath: str | None, *, timeout: float,
                side: str, label: str) -> Run:
    command = [python, "-m", "pytest", *nodes, *_PYTEST_FLAGS]
    env = os.environ.copy()
    # Ambient pytest config would make a receipt depend on the machine rather
    # than on the two commits it names.
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = _CHILD_COLUMNS
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    # Its own process group, so a timeout stops the tests' own children too
    # rather than leaving them holding the output pipes.
    proc = subprocess.Popen(
        command, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace",
        start_new_session=(os.name == "posix"))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(proc)
        raise TimedOut(
            f"the {side} run on {label} did not finish within {timeout:g}s "
            "and was stopped; a run that never ended is neither red nor "
            "green. Raise --timeout if the node is slow.") from None
    except BaseException:
        # An interrupt must not leave the run alive in its own group.
        _stop(proc)
        raise
    output = stdout + stderr
    return Run(tuple(command), cwd, proc.returncode, output,
               parse_summary(output))


def _pythonpath_for(export: Path) -> str:
    parts = []
    src = export / "src"
    if src.is_dir():
        parts.append(str(src))
    parts.append(str(export))
    existing = os.environ.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _result_phrase(run: Run) -> str:
    counts = run.counts
    if not counts.found:
        return f"unreadable (pytest exited {run.exit_code})"
    parts = []
    for number, label in ((counts.passed, "passed"), (counts.failed, "failed"),
                          (counts.errors, "error" if counts.errors == 1
                           else "errors"),
                          (counts.skipped, "skipped"),
                          (counts.xfailed, "xfailed"),
                          (counts.xpassed, "xpassed")):
        if number:
            parts.append(f"{number} {label}")
    if counts.no_tests:
        return "no tests ran"
    return ", ".join(parts) if parts else "no counts"


def _failure_lines(run: Run) -> list[str]:
    """The lines a reviewer checks: the exact failing or erroring node ids.

    pytest's short summary ("FAILED node - message", "ERROR node - message")
    comes first. The traceback's last ``E`` line is always added for the
    cause: a collection error's summary line carries only the path, and a
    summary line can still be cut short by pytest. When no summary line was
    printed at all, the detail lines are the whole evidence.
    """
    text = _ANSI.sub("", run.output or "")
    lines = [raw.strip() for raw in text.splitlines()]
    summary = [line for line in lines if line.startswith(("FAILED ", "ERROR "))]
    detail = [line for line in lines if line.startswith("E   ")]
    if summary:
        chosen = summary[:3]
        if detail and detail[-1] not in chosen:
            chosen.append(detail[-1])
        return chosen
    return detail[-4:]


def _base_is_red(run: Run) -> bool:
    """Red is read from the summary, not the exit code.

    pytest 9 exits 4 (usage) when a ``node::test`` selection hits a collection
    ImportError, and 2 for the same error on a file selection. A readable
    failed/error count is the evidence in both cases.
    """
    counts = run.counts
    return bool(counts.failed or counts.errors)


def _check_base(run: Run, base_ref: str, base_sha: str) -> None:
    counts = run.counts
    label = f"{base_ref} ({base_sha[:12]})"
    if _base_is_red(run):
        return
    if run.exit_code == 0 and counts.executed == 0 and counts.skipped:
        raise NoRedBefore(
            f"the node skipped on base {label}; a skip is not a red-before. "
            "Nothing here proves the test fails without the change.")
    if run.exit_code == 0 and counts.executed == 0:
        raise Refused(
            f"the node collected no executable test on base {label}; "
            "there is nothing to be red or green")
    if run.exit_code == 0 and counts.xfailed and not counts.passed:
        raise NoRedBefore(
            f"the node was expected to fail on base {label}; an xfail is "
            "not a red-before")
    if run.exit_code == 0:
        raise NoRedBefore(
            f"the node passed on base {label}; there is no red-before. "
            "Nothing here proves the test fails without the change.")
    if run.exit_code == 5 or counts.no_tests:
        raise Refused(
            f"pytest collected no tests for the node on base {label}; "
            "the node does not exist there and was not overlaid")
    raise Refused(
        f"the base run did not complete: pytest exited {run.exit_code} on "
        f"{label} without a readable failure (interpreter "
        f"{run.command[0]}; it needs pytest and the repo's test dependencies)")


def _check_head(run: Run, head_sha: str) -> None:
    counts = run.counts
    label = head_sha[:12]
    if run.exit_code == 0 and counts.passed and not (
            counts.failed or counts.errors):
        return
    if run.exit_code == 0 and counts.executed == 0 and counts.skipped:
        raise NoGreenAfter(
            f"the node skipped on head {label}; a skip is not a green-after")
    if run.exit_code == 0 and not counts.found:
        raise Refused(
            f"could not read pytest's summary on head {label}; refusing to "
            "call an unreadable run green")
    raise NoGreenAfter(
        f"the node did not pass on head {label}: pytest exited "
        f"{run.exit_code} ({_result_phrase(run)})")


def _command_line(run: Run, nodes: list[str]) -> str:
    """The pytest invocation, with the interpreter's name not its full path."""
    interpreter = Path(run.command[0]).name
    return " ".join([interpreter, *run.command[1:3], *nodes])


def _shortstat(repo: Path, base: str, head: str) -> str:
    text = _git(repo, "diff", "--shortstat", f"{base}...{head}").strip()
    return text or "no files changed"


def build_receipt(*, base_ref: str, base_sha: str, head_sha: str,
                  branch: str, nodes: list[str],
                  overlayed: list[str], explicit_overlay: list[str],
                  base_run: Run, head_run: Run, stat: str) -> str:
    base_cmd = _command_line(base_run, nodes)
    head_cmd = _command_line(head_run, nodes)
    flags = " ".join(_PYTEST_FLAGS)
    overlay_args = "".join(f" --overlay {path}" for path in explicit_overlay)
    lines = [
        "### Red-before and green-after receipt",
        "",
        f"- reproduce: `python3 tools/evidence_receipt.py --base "
        f"{base_sha} --node {nodes[0]}{overlay_args}`",
        f"- base: `{base_ref}` at `{base_sha}`",
        f"- head: `{branch}` at `{head_sha}`",
        f"- diff: `git diff {base_sha[:12]}...{head_sha[:12]}`: {stat}",
        "- base export: `git archive " + base_sha + "` into a temporary "
        "directory; files from head overlaid: "
        + (", ".join(f"`{path}`" for path in overlayed) or "(none)"),
        f"- base command: `{base_cmd} {flags}` (in the base export, "
        "`PYTHONPATH` pointing at its `src`)",
        "- base result: "
        + ("FAILED" if base_run.counts.failed else "ERROR")
        + f" (`{base_run.counts.summary_line}`)",
    ]
    for line in _failure_lines(base_run):
        lines.append(f"  - `{line}`")
    lines += [
        "- head export: `git archive " + head_sha + "` into a temporary "
        "directory, nothing overlaid",
        f"- head command: `{head_cmd} {flags}` (in the head export, "
        "`PYTHONPATH` pointing at its `src`)",
        f"- head result: PASSED (`{head_run.counts.summary_line}`)",
        "",
        "The automatic overlay is the change's test files; the implementation "
        "stays at the base ref unless an `--overlay` path is named above.",
    ]
    return "\n".join(lines)


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a number of seconds: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError("the timeout must be above zero")
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/evidence_receipt.py",
        description="Produce a red-before / green-after receipt for a pytest "
                    "node, or refuse when the base run is not red.")
    parser.add_argument("--base", required=True,
                        help="ref the test must fail on, e.g. origin/main")
    parser.add_argument("--node", action="append", required=True,
                        metavar="TEST",
                        help="pytest node id or file; one receipt names one "
                             "node")
    parser.add_argument("--repo", default=".",
                        help="repository to measure (default: current)")
    parser.add_argument("--out", metavar="PATH",
                        help="also write the paste-ready receipt to PATH")
    parser.add_argument("--overlay", action="append", default=[],
                        metavar="PATH",
                        help="extra head file to overlay on the base export "
                             "(repeatable)")
    parser.add_argument("--timeout", type=_positive_seconds,
                        default=DEFAULT_TIMEOUT, metavar="SECONDS",
                        help="stop each pytest run after SECONDS and refuse "
                             f"(default: {DEFAULT_TIMEOUT:g})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        repo = Path(
            _git(Path(args.repo), "rev-parse", "--show-toplevel").strip())
        head_sha, branch = _head_info(repo)
        base_sha = _commit(repo, args.base)
        _require_committed(repo)
        nodes = list(args.node)
        if len(nodes) != 1:
            raise Refused(
                "one receipt names one node; pass a test file for several "
                "tests or run the tool once per node")
        for node in nodes:
            if node.startswith("-"):
                raise Refused(f"node {node!r} looks like a pytest flag")
            part = node.split("::", 1)[0]
            if (not part or Path(part).is_absolute()
                    or ".." in Path(part).parts):
                raise Refused(
                    f"node {node!r} must name a file inside the repository")
            if not (repo / part).is_file():
                raise Refused(f"the node's file does not exist: {part}")
        for path in args.overlay:
            if (not path or Path(path).is_absolute()
                    or ".." in Path(path).parts):
                raise Refused(
                    f"--overlay {path!r} must name a file inside the "
                    "repository")
        overlayed = _overlay_paths(repo, base_sha, head_sha, nodes,
                                   args.overlay)
        stat = _shortstat(repo, base_sha, head_sha)
        python = sys.executable or "python3"
        with tempfile.TemporaryDirectory(prefix="evidence-receipt-") as tmp:
            export = Path(tmp) / "base"
            export.mkdir()
            _export(repo, base_sha, export, "base")
            _overlay(repo, head_sha, export, overlayed)
            for node in nodes:
                part = node.split("::", 1)[0]
                if not (export / part).is_file():
                    raise Refused(
                        f"the node's file {part} is neither at the base ref "
                        "nor overlaid from head")
            base_run = _run_pytest(
                python, export, nodes, _pythonpath_for(export),
                timeout=args.timeout, side="base",
                label=f"{args.base} ({base_sha[:12]})")
            _check_base(base_run, args.base, base_sha)
            if not _failure_lines(base_run):
                raise Refused(
                    "the base run was red but no FAILED or ERROR line could "
                    "be read from its output; the receipt needs that line")
            # The head side runs in an export of the sha read above, exactly
            # as the base side does: only committed files, whatever the
            # worktree holds or becomes while the runs are in flight.
            head_export = Path(tmp) / "head"
            head_export.mkdir()
            _export(repo, head_sha, head_export, "head")
            head_run = _run_pytest(
                python, head_export, nodes, _pythonpath_for(head_export),
                timeout=args.timeout, side="head", label=head_sha[:12])
            _check_head(head_run, head_sha)
            receipt = build_receipt(
                base_ref=args.base, base_sha=base_sha, head_sha=head_sha,
                branch=branch, nodes=nodes, overlayed=overlayed,
                explicit_overlay=args.overlay, base_run=base_run,
                head_run=head_run, stat=stat)
    except NoRedBefore as exc:
        print(f"NO RED-BEFORE: {exc}", file=sys.stderr)
        return 1
    except NoGreenAfter as exc:
        print(f"NOT GREEN: {exc}", file=sys.stderr)
        return 1
    except TimedOut as exc:
        print(f"TIMED OUT: {exc}", file=sys.stderr)
        return 2
    except Refused as exc:
        print(f"NO RECEIPT: {exc}", file=sys.stderr)
        return 2
    print(receipt)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(receipt + "\n", encoding="utf-8")
        print(f"receipt: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
