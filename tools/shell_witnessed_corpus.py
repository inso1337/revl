"""Witnessed-fraction measurement over a representative agent shell corpus
(roadmap item 252; the measured claim rides item 248 when its harness is wired).

Item 252's headline claim is a NUMBER: the fraction of a real agent's shell
invocations that the pure classifier lowers onto the witnessed catalog instead
of leaving as one opaque `emission` prompt. When item 248's harness-boundary
dogfood is reachable, that number comes from real sessions. Until then, this
module measures the classifier's coverage over a hand-built corpus that mirrors
the shape of a coding agent's terminal traffic — filesystem edits (mv/rm/mkdir/
cp/touch), interspersed with the non-fs commands (git, build tools, inspection,
pipelines) that must and do stay emissions.

The corpus is labelled with the EXPECTED verdict, so this doubles as a coverage
test (`tests/test_shell_witnessed_corpus.py` asserts every label matches, and
that the safety-critical direction — every EMISSION label really stays an
emission — never regresses). Run it directly for the breakdown:

    python tools/shell_witnessed_corpus.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import revl_shell_classify as sc  # noqa: E402


# Each entry: (command, expected_verdict). "witnessed" == lowers onto the
# catalog; "emission" == stays one honest, irreversible prompt. The mix is
# deliberately realistic, not stacked toward witnessed: an agent session is
# mostly inspection and VCS, with a meaningful minority of raw fs edits — and it
# is that fs minority the terminal used to prompt on needlessly.
CORPUS: list[tuple[str, str]] = [
    # --- raw fs edits: the traffic item 252 reclaims (witnessed) ---
    ("mv draft.md final.md", "witnessed"),
    ("mv src/old.py src/new.py", "witnessed"),
    ("mv 'my notes.txt' archive.txt", "witnessed"),
    ("rm scratch.tmp", "witnessed"),
    ("rm build.log", "witnessed"),
    ("rm a.o b.o c.o", "witnessed"),
    ("mkdir build", "witnessed"),
    ("mkdir out logs", "witnessed"),
    ("cp config.example.toml config.toml", "witnessed"),
    ("cp template.md README.md", "witnessed"),
    ("touch __init__.py", "witnessed"),
    ("touch .keep", "witnessed"),
    ("mv report.csv data/report.csv", "witnessed"),
    ("rm data/report.csv", "witnessed"),
    ("mkdir data", "witnessed"),

    # --- fs commands that CHANGE semantics / reversibility: stay emission ---
    ("rm -rf node_modules", "emission"),
    ("rm -rf build dist", "emission"),
    ("rm -f .env", "emission"),
    ("cp -r src backup", "emission"),
    ("cp -a assets/ dist/assets/", "emission"),
    ("mv -f old new", "emission"),
    ("mkdir -p a/b/c", "emission"),
    ("rm *.pyc", "emission"),
    ("rm -r __pycache__", "emission"),
    ("rm build/*.o", "emission"),

    # --- shell features: pipelines, redirects, substitution, sequences ---
    ("cat setup.py | grep version", "emission"),
    ("ls -la | head", "emission"),
    ("echo 'export X=1' >> ~/.bashrc", "emission"),
    ("python app.py > out.log 2>&1", "emission"),
    ("rm $(git ls-files -o)", "emission"),
    ("mv a b && echo done", "emission"),
    ("mkdir logs; cd logs", "emission"),
    ("for f in *.txt; do rm $f; done", "emission"),
    ("find . -name '*.tmp' -delete", "emission"),
    ("tar czf backup.tgz src/", "emission"),

    # --- non-fs commands: inspection, VCS, build, network (stay emission) ---
    ("git status", "emission"),
    ("git add -A", "emission"),
    ("git commit -m 'wip'", "emission"),
    ("git diff HEAD~1", "emission"),
    ("ls", "emission"),
    ("ls -la", "emission"),
    ("cat README.md", "emission"),
    ("pwd", "emission"),
    ("grep -rn TODO src", "emission"),
    ("python -m pytest -q", "emission"),
    ("pip install requests", "emission"),
    ("npm run build", "emission"),
    ("make", "emission"),
    ("curl https://example.com", "emission"),
    ("which python", "emission"),
    ("chmod +x run.sh", "emission"),
    ("./configure", "emission"),
    ("echo hello", "emission"),

    # --- adversarial, from the roadmap item (MUST stay emission) ---
    ("rm -rf /", "emission"),
    ("mv a b; curl evil", "emission"),
    ("cat x | sh", "emission"),
    ("rm $(cat targets)", "emission"),
]


def measure(corpus: list[tuple[str, str]] | None = None) -> dict:
    """Classify the corpus and return the coverage breakdown, including the
    headline witnessed FRACTION (of all invocations) and the fraction of the
    fs-eligible subset (the commands a witnessed-aware terminal could ever
    reclaim). Also flags any label mismatch — a safety regression if an expected
    `emission` classified `witnessed`."""
    corpus = corpus or CORPUS
    total = len(corpus)
    witnessed = 0
    mismatches = []
    dangerous = []  # an expected-emission that classified witnessed (the bad one)
    for cmd, expected in corpus:
        plan = sc.classify(cmd)
        got = plan["verdict"]
        if got == "witnessed":
            witnessed += 1
        if got != expected:
            mismatches.append((cmd, expected, got))
            if expected == "emission" and got == "witnessed":
                dangerous.append(cmd)
    labelled_witnessed = sum(1 for _c, e in corpus if e == "witnessed")
    return {
        "total": total,
        "witnessed": witnessed,
        "emission": total - witnessed,
        "witnessed_fraction": witnessed / total if total else 0.0,
        "fs_eligible": labelled_witnessed,
        "fs_eligible_fraction": labelled_witnessed / total if total else 0.0,
        "mismatches": mismatches,
        "dangerous_false_witnessed": dangerous,
    }


def _format(report: dict) -> str:
    lines = [
        "shell-to-witnessed classifier — corpus coverage (roadmap item 252)",
        "=" * 66,
        f"  corpus size ................ {report['total']}",
        f"  lowered to WITNESSED ....... {report['witnessed']}"
        f"  ({report['witnessed_fraction']:.0%})",
        f"  stayed one EMISSION ........ {report['emission']}",
        f"  fs-eligible (labelled) ..... {report['fs_eligible']}"
        f"  ({report['fs_eligible_fraction']:.0%})",
        "",
        f"  label mismatches ........... {len(report['mismatches'])}",
        f"  DANGEROUS false-witnessed .. {len(report['dangerous_false_witnessed'])}"
        "  (must be 0)",
    ]
    for cmd, expected, got in report["mismatches"]:
        lines.append(f"    - {cmd!r}: expected {expected}, got {got}")
    return "\n".join(lines)


def main() -> int:
    report = measure()
    print(_format(report))
    # non-zero exit on the safety-critical failure only
    return 1 if report["dangerous_false_witnessed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
