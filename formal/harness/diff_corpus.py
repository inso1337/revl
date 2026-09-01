"""Differential harness: formal-model verdicts vs the Python checker.

Wire-up plan (formal/STATUS.md, "integration"): once RevL.Typing exposes
a decision procedure, run it over the same .rvl corpus the Python checker
already accepts/rejects (examples/, tck/, tests/) and diff verdicts.
Any mismatch is definitional drift between the calculus and the checker —
the mechanism that keeps parallel edits to the formal model honest,
because drift surfaces as a corpus failure instead of a silent fork.

Until the decision procedure exists this prints the corpus census and
exits 0; it exists so the wiring point is pinned and the census is cheap
to check from `make formal`.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS_DIRS = ("examples", "tck", "tests")


def census() -> int:
    """Count the .rvl files the formal oracle will eventually be run over."""
    total = 0
    for name in CORPUS_DIRS:
        root = REPO / name
        if root.is_dir():
            total += sum(1 for p in root.rglob("*.rvl"))
    return total


def main() -> int:
    n = census()
    print(f"corpus census: {n} .rvl files across {CORPUS_DIRS}")
    print("formal oracle: not wired yet (see formal/STATUS.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
