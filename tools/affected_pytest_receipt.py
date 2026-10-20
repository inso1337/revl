#!/usr/bin/env python3
"""Read the affected pytest run back, so a green step means tests executed.

WHY THIS EXISTS (issue #2642). `root-suite-affected` runs the selection
`tools/affected_tests.py` computes. A selected test file that guards on the
cordis-py runtime skips every one of its tests on the job's interpreter, which
has no runtime. pytest exits 0 when tests are skipped, so the job concluded
success, the merge box stayed green, and the one test the change added never
executed anywhere before the merge. The whole selection can be that one file.

The fix is a receipt, not a heavier job. After pytest, this script reads the
summary it printed and fails unless something actually ran:

  * a selection requested but zero tests collected (pytest exit 5) is red,
    except for a shard that was deliberately handed no file (the four-shard
    split runs in `root-suite-affected`, issue #1774);
  * a selection whose every collected test was skipped is red, with the skip
    count and the reason;
  * a partial skip (a real pass beside the skips) is green, because the
    selected tests did execute;
  * a real failure stays red and keeps its exit status.

Nothing here weakens a test: it refuses to call a run successful when the run
did not execute the selection. The receipt line names the selected files, the
passed, failed, errors, skipped and not-run counts, and the reason, so a green
board can be read without opening the log.

Not-run counts the collected tests that never executed in this run, which in a
pytest summary is exactly the skipped ones. It is stated separately because a
skip is a passing line in pytest and a non-run in this receipt.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A count in the terminal summary line, e.g. "3 passed", "1 error".
_COUNT = re.compile(
    r"(?<![\w.])(\d+) "
    r"(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b"
)
# pytest -q prints one final line: "<counts> in 0.42s" and, when it collected
# nothing, "no tests ran in 0.01s". With colours on there is a trailing reset;
# the ANSI strip above removes it. A duration suffix in parentheses is allowed.
_SUMMARY = re.compile(
    r"^(?P<body>.+?)\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^)]*\))?$"
)
_NO_TESTS = re.compile(r"\bno tests ran\b")
# tests/conftest.py prints this when the shard filter leaves nothing to run.
# It is the only evidence that an empty pytest run is the shard split's
# deliberate outcome rather than a selection that collected nothing.
_EMPTY_SHARD = re.compile(r"REVL_TEST_SHARD \d+/\d+: 0 file\(s\)")

# pytest summary word -> field name. "1 error" is singular in the summary.
_KINDS = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "deselected": "deselected",
}


@dataclass(frozen=True)
class Counts:
    """What the pytest summary says happened in the selected run."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    deselected: int = 0
    summary_found: bool = False
    no_tests: bool = False

    @property
    def collected(self) -> int:
        """Tests the run collected, excluding the ones a shard deselected."""
        return (self.passed + self.failed + self.errors + self.skipped
                + self.xfailed + self.xpassed)

    @property
    def executed(self) -> int:
        """Tests that produced a result, including expected failures."""
        return self.passed + self.failed + self.errors + self.xfailed + self.xpassed

    @property
    def not_run(self) -> int:
        """Collected tests that never executed: the skipped ones."""
        return self.skipped


@dataclass(frozen=True)
class Verdict:
    ok: bool
    exit_code: int
    reason: str
    counts: Counts


def parse_summary(output: str) -> Counts:
    """Read pytest -q's final summary line, or report that there was none.

    The LAST matching line wins, so a test that prints something that looks
    like a summary cannot be mistaken for pytest's own. Anything unreadable
    returns `summary_found=False` rather than an all-zero pass.
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
            return Counts(summary_found=True, no_tests=True)
        found: dict[str, int] = {}
        for number, kind in _COUNT.findall(body):
            key = _KINDS[kind]
            found[key] = found.get(key, 0) + int(number)
        if found:
            return Counts(summary_found=True, **found)
    return Counts()


def judge(output: str, *, pytest_exit: int = 0,
          allow_empty_shard: bool = False) -> Verdict:
    """Decide whether a selected run counted, from its summary and exit code.

    `allow_empty_shard` is for `root-suite-affected`'s four-shard split: a
    shard handed no test file is not a silently skipped selection, because the
    other shards run that file. It is never true for a whole selection.
    """
    counts = parse_summary(output)
    if pytest_exit not in (0, 5):
        return Verdict(
            False, pytest_exit,
            f"pytest exited {pytest_exit}; the selected run did not complete",
            counts,
        )
    if pytest_exit == 5 or counts.no_tests or (
            counts.summary_found and counts.collected == 0):
        if allow_empty_shard:
            if _EMPTY_SHARD.search(_ANSI.sub("", output or "")):
                return Verdict(True, 0, "shard assigned no test file", counts)
            return Verdict(
                False, 5,
                "pytest collected no tests and no shard recorded itself empty",
                counts,
            )
        return Verdict(
            False, 5,
            "a selection was requested but pytest collected no tests",
            counts,
        )
    if not counts.summary_found:
        return Verdict(
            False, 1,
            "could not read a pytest summary; refusing to report success on an "
            "unreadable run",
            counts,
        )
    if counts.failed or counts.errors:
        return Verdict(
            False, 1,
            f"{counts.failed} failed and {counts.errors} error(s) in the "
            "selected tests",
            counts,
        )
    if counts.executed == 0:
        return Verdict(
            False, 3,
            f"every selected test was skipped ({counts.skipped} skipped, "
            "0 executed)",
            counts,
        )
    return Verdict(
        True, 0,
        f"{counts.executed} selected test(s) executed"
        + (f", {counts.skipped} skipped" if counts.skipped else ""),
        counts,
    )


def receipt_line(verdict: Verdict, selection: str) -> str:
    """One line a reviewer can read from a green or red board."""
    chosen = " ".join(selection.split()) or "(none)"
    counts = verdict.counts
    return (
        f"affected receipt: selection=[{chosen}] "
        f"passed={counts.passed} failed={counts.failed} "
        f"errors={counts.errors} skipped={counts.skipped} "
        f"not-run={counts.not_run} deselected={counts.deselected} "
        f"verdict={'PASS' if verdict.ok else 'FAIL'} reason={verdict.reason}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail the affected run when its selection did not execute.")
    parser.add_argument("--log", required=True,
                        help="file holding the pytest output to read back")
    parser.add_argument("--pytest-exit", type=int, default=0,
                        help="pytest's own exit status for that run")
    parser.add_argument("--selection", default="",
                        help="the selected test files, space separated")
    parser.add_argument("--allow-empty-shard", action="store_true",
                        help="a shard may legitimately be assigned no test file")
    args = parser.parse_args(argv)
    try:
        output = Path(args.log).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print("affected receipt: FAIL reason=could not read the pytest log: "
              f"{exc}")
        return 1
    verdict = judge(output, pytest_exit=args.pytest_exit,
                    allow_empty_shard=args.allow_empty_shard)
    print(receipt_line(verdict, args.selection))
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
