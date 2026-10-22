#!/usr/bin/env python
"""The escape demo: refused, explained, repaired (issue #2691).

One short story for a reader who asks "what stops an untrusted component from
getting out?". Three components try to exceed what they declared, each is
refused with a diagnostic that says why, and each takes the smallest edit that
makes it admissible. The repaired source is then audited, so the crossing the
compiler now permits is the one the reader can see.

    1  a component that may only read, whose body writes      G4
    2  untrusted text turned into a shell command             G9
    3  a Secret handed to a log                     G-SECRET-FLOW

It needs the compiler and nothing else: no runtime, no network, no host
process. Every program is written into a throwaway temp dir, the checkout is
untouched, and a second run passes as cleanly as the first. It exits nonzero
on the first failed check.

What it does NOT show, because the compiler does not do it today: it never
runs the repaired component, never asks an endorsement policy to approve a
declassification, and never proves that the shell command the repaired
component runs is a good one. The repairs are admissible programs, not safe
ones; safety is the remaining work the README lists.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

sys.path.insert(0, str(HERE))
import programs  # noqa: E402

VERBOSE = False
_CHECKS = 0


# --------------------------------------------------------------- the plumbing

def _revl(args: list[str], cwd: Path, timeout: int = 180):
    """Drive the real CLI a reader would type (the same shape the flagship
    demo uses: `-P`, `src/` on PYTHONPATH, no installed package needed)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-P", "-m", "revl", *args],
        cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=timeout)


def _step(number: str, title: str, guarantee: str) -> None:
    print(f"\n[{number}] {title}")
    print(f"     guarantee: {guarantee}")


def _artifact(label: str, text: str, keep: int = 8) -> None:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    shown = lines if VERBOSE else lines[:keep]
    print(f"     --- {label} ---")
    for line in shown:
        print(f"     | {line if VERBOSE else line[:160]}")
    if len(shown) < len(lines):
        print(f"     | ... {len(lines) - len(shown)} more line(s); --verbose prints them")


def _check(condition: bool, what: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        print(f"\nFAILED CHECK: {what}")
        raise SystemExit(1)
    print(f"     ok: {what}")


def _boundary_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("boundary:"):
            return line.strip()
    return ""


# ------------------------------------------------------------------ the story

def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true",
                    help="print every artifact in full rather than its head")
    VERBOSE = ap.parse_args().verbose

    work = Path(tempfile.mkdtemp(prefix="revl-escape-"))

    print("THE ESCAPE DEMO - refused, explained, repaired")
    print("issue #2691, docs/guarantees.md for the G-codes")
    print(f"work dir: {work}")

    for scenario in programs.SCENARIOS:
        number = scenario["number"]
        refused = work / f"{number}_refused.rvl"
        repaired = work / f"{number}_repaired.rvl"
        refused.write_text(scenario["refused"])
        repaired.write_text(scenario["fixed"])

        _step(number, scenario["title"], scenario["guarantee"])

        # 1. the refusal, and the code that names the guarantee
        proc = _revl(["compile", refused.name], work)
        out = proc.stdout
        _check(proc.returncode != 0, f"`revl compile` refuses {refused.name}")
        _check(f"({scenario['code']})" in out,
               f"the diagnostic carries the refusal code {scenario['code']}")
        _check(scenario["fix_hint"] in out,
               "the diagnostic explains the fix the compiler itself suggests")
        _artifact(f"revl compile {refused.name}", out)

        # 2. the explanation, in one sentence, and the edit it names
        print(f"     why: {scenario['guarantee']}")
        print(f"     smallest repair: {scenario['repair']}")

        # 3. the repair is admitted, and the audit shows what it now permits
        proc = _revl(["compile", repaired.name], work)
        _check(proc.returncode == 0,
               f"`revl compile` ADMITS the repaired {repaired.name}")
        _check('"ir_version"' in proc.stdout, "the admitted result is IR")
        proc = _revl(["audit", repaired.name], work)
        _check(proc.returncode == 0, f"`revl audit` reads {repaired.name}")
        for crossing in scenario["audit_crossings"]:
            _check(crossing in proc.stdout,
                   f"the audit names the permitted crossing `{crossing}`")
        boundary = _boundary_line(proc.stdout)
        if boundary:
            print(f"     --- revl audit {repaired.name} ---")
            print(f"     | {boundary[:200]}")

    print(f"\nescape demo OK - {_CHECKS} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
