#!/usr/bin/env python3
"""A small mutation runner over the refusal arms of one narrow module.

WHAT THIS IS FOR (roadmap item 535, issue #2761 slice 7). A test suite that
scores a candidate evolution is only worth scoring against if it is SENSITIVE:
if deleting a refusal from the code under test leaves every test green, then the
suite cannot tell a candidate that kept the refusal from one that removed it.
Mutation testing measures that. It mutates the SUBJECT, runs the tests, and
reports the mutants nothing noticed. `tools/fuzz_frontend.py` mutates programs
as inputs, to find a checker that faults; that is a different thing and is not
what this does.

WHAT IT MUTATES. One module, named by `TARGET`, and only its REFUSAL ARMS: an
`if <guard>: raise RevlError(...)` statement whose body is exactly the raise. A
refusal arm is the narrowest unit a guarantee lives in, and it has two
unambiguous mutations:

  drop-refusal   the raise is replaced by `pass`: the arm no longer refuses.
  negate-guard   the guard is negated: the arm refuses the opposite.

A mutant is KILLED when at least one test in `TESTS` fails against it, and
SURVIVES when every test passes. A survivor is a named hole: the tests never
pin that arm in that direction. Each survivor is a curriculum task (the
`mutation-survivors` source of `tools/evolve_curriculum.py`).

HOW A MUTANT IS RUN. The mutated source is written to a temporary file and a
fresh interpreter loads it under the module's own name before pytest imports
the tests, so the tests exercise the mutant and nothing is edited in the tree.
The unmutated module is run first and must be green; a red baseline measures the
suite, not the mutant. A mutant that hangs is killed by its timeout, as a
mutation that loops forever is one the tests would also notice.

WHERE THE MUTANTS LIVE. In the tree, and readable by a candidate. That is the
first version on purpose: a mutation test a candidate can read is not a hidden
mutation test, and where hidden mutants should live is the held-out scoring
work's question (issue #1207), not this tool's. The recorded survivors are
`tests/fixtures/mutation_survivors.json`, labelled as such.

Usage:
    python3 tools/mutation_runner.py --arms       # list the refusal arms, no run
    python3 tools/mutation_runner.py              # run every mutant, print the report
    python3 tools/mutation_runner.py --check      # the gate: survivors == the ledger
    python3 tools/mutation_runner.py --write      # regenerate the ledger
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tests" / "fixtures" / "mutation_survivors.json"

# The one narrow module the first version mutates, and the tests that pin it.
# `src/revl/anchor.py` holds the refusals of a chain-neutral signed record (a
# malformed digest set, an unaddressable name, a bad envelope); `tests/
# test_anchor.py` is its own suite. Small enough that every mutant runs in
# seconds, and every arm is a plain `if guard: raise RevlError`.
TARGET = "src/revl/anchor.py"
TESTS = ("tests/test_anchor.py",)

OPERATORS = ("drop-refusal", "negate-guard")

# Seconds one mutant's test run may take before it counts as killed (hung).
TIMEOUT = 300

# What each operator does, in the words a task quotes.
EFFECT = {
    "drop-refusal": "the refusal is removed",
    "negate-guard": "the guard is negated",
}

ABOUT = [
    "The mutants of the refusal arms of one module that NO test noticed, written",
    "by tools/mutation_runner.py --write and gated by --check (issue #2761,",
    "slice 7). Each entry is {arm, operator, line, digest}: the arm is",
    "<function>#<n>, the n-th refusal arm in that function; the digest is the",
    "hash of the arm's source, so a rewritten arm retires its entry.",
    "",
    "This file is in the tree and a candidate can read it: the first version of",
    "mutation testing is NOT hidden. Where hidden mutants should live is issue",
    "#1207's question.",
]

_BOOT = """\
import importlib.util, sys
name, path = sys.argv[1], sys.argv[2]
parent, _, leaf = name.rpartition(".")
if parent:
    __import__(parent)
spec = importlib.util.spec_from_file_location(name, path)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
if parent:
    setattr(sys.modules[parent], leaf, module)
import pytest
sys.exit(pytest.main(sys.argv[3:]))
"""


# ------------------------------------------------------------------ the arms

@dataclass(frozen=True)
class Arm:
    id: str          # "<function>#<n>"
    function: str
    line: int
    digest: str      # of the arm's own source, so a rewrite retires its entry


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def is_refusal_arm(node: ast.AST) -> bool:
    """`if <guard>: raise RevlError(...)`, the body exactly that raise."""
    return (isinstance(node, ast.If) and not node.orelse
            and len(node.body) == 1 and isinstance(node.body[0], ast.Raise)
            and isinstance(node.body[0].exc, ast.Call)
            and _callee(node.body[0].exc).endswith("RevlError"))


class _Walk(ast.NodeTransformer):
    """Visit refusal arms in source order, numbering them per function.

    One traversal serves both the listing and the mutation, so an arm's id
    means the same thing in both: it is never a line number, which a mutation
    elsewhere would shift.
    """

    def __init__(self, replace: tuple[str, str] | None = None):
        self.replace = replace
        self.stack: list[str] = []
        self.count: dict[str, int] = {}
        self.arms: list[Arm] = []

    def _function(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()
        return node

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()
        return node

    def visit_If(self, node):
        if not is_refusal_arm(node):
            return self.generic_visit(node)
        function = ".".join(self.stack) or "<module>"
        n = self.count[function] = self.count.get(function, 0) + 1
        arm_id = f"{function}#{n}"
        digest = hashlib.sha256(ast.unparse(node).encode("utf-8")).hexdigest()[:16]
        self.arms.append(Arm(arm_id, function, node.lineno, digest))
        if self.replace is None or self.replace[0] != arm_id:
            return node
        if self.replace[1] == "drop-refusal":
            node.body = [ast.Pass()]
        elif self.replace[1] == "negate-guard":
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        else:
            raise ValueError(f"unknown operator {self.replace[1]!r}")
        return node


def arms(source: str) -> list[Arm]:
    walk = _Walk()
    walk.visit(ast.parse(source))
    return walk.arms


def mutate(source: str, arm_id: str, operator: str) -> str:
    walk = _Walk((arm_id, operator))
    tree = walk.visit(ast.parse(source))
    if arm_id not in {a.id for a in walk.arms}:
        raise KeyError(f"no refusal arm {arm_id!r}")
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


# ----------------------------------------------------------------- the runner

def module_name(target: str) -> str:
    """`src/revl/anchor.py` -> `revl.anchor`; a top-level file keeps its stem."""
    parts = Path(target).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def run_tests(root: Path, target: str, tests, source: str,
              timeout: float = TIMEOUT) -> tuple[str, str]:
    """`(status, tail)`: `survived`, `killed`, or `timeout` (counted as killed)."""
    with tempfile.TemporaryDirectory() as tmp:
        mutant = Path(tmp) / Path(target).name
        mutant.write_text(source, encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        search = [str(root / "src"), str(root)] if (root / "src").is_dir() \
            else [str(root)]
        env["PYTHONPATH"] = os.pathsep.join(search + [env.get("PYTHONPATH", "")])
        command = [sys.executable, "-c", _BOOT, module_name(target), str(mutant),
                   "-x", "-q", "-p", "no:cacheprovider",
                   *[str(root / t) for t in tests]]
        try:
            done = subprocess.run(command, cwd=root, env=env, text=True,
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout", ""
    tail = (done.stdout + done.stderr)[-600:]
    if done.returncode == 0:
        return "survived", tail
    if done.returncode == 1:
        return "killed", tail
    raise RuntimeError(f"pytest exited {done.returncode} on {target}, which is "
                       f"neither green nor a failing test:\n{tail}")


def survey(root: Path = ROOT, target: str = TARGET, tests=TESTS,
           timeout: float = TIMEOUT, progress=None) -> dict:
    """Run every mutant of every refusal arm and report what survived."""
    source = (root / target).read_text(encoding="utf-8")
    found = arms(source)
    status, tail = run_tests(root, target, tests, source, timeout)
    if status != "survived":
        raise RuntimeError(f"the unmutated {target} does not pass {list(tests)}; "
                           f"a red baseline measures the suite, not a mutant:\n"
                           f"{tail}")
    mutants = []
    for arm in found:
        for operator in OPERATORS:
            status, _ = run_tests(root, target, tests,
                                  mutate(source, arm.id, operator), timeout)
            mutants.append({"arm": arm.id, "operator": operator,
                            "line": arm.line, "digest": arm.digest,
                            "status": "killed" if status == "timeout" else status})
            if progress:
                progress(mutants[-1])
    return {"target": target, "tests": list(tests),
            "arms": [a.id for a in found], "mutants": mutants,
            "survivors": [m for m in mutants if m["status"] == "survived"]}


# ------------------------------------------------------------------- ledger

def ledger_of(report: dict) -> dict:
    return {"_about": ABOUT, "target": report["target"],
            "tests": report["tests"],
            "survivors": sorted(
                ({"arm": m["arm"], "operator": m["operator"], "line": m["line"],
                  "digest": m["digest"]} for m in report["survivors"]),
                key=lambda s: (s["arm"], s["operator"]))}


def load_ledger(path: Path = LEDGER) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check(report: dict, ledger: dict) -> list[str]:
    """Differences between the survivors just measured and the recorded ones.

    Both directions fail, which is what makes the ledger shrink-only: a new
    survivor is a regression (a refusal lost its test, or a refusal arrived
    with none), and a recorded survivor that is now killed is a stale line to
    delete, so closing a hole is free and the count cannot drift back up.
    """
    problems = []
    if (report["target"], report["tests"]) != (ledger["target"], ledger["tests"]):
        problems.append("the ledger records a different target or test set: "
                        f"{ledger['target']} / {ledger['tests']}")
    now = {(s["arm"], s["operator"]) for s in report["survivors"]}
    recorded = {(s["arm"], s["operator"]) for s in ledger["survivors"]}
    for arm, operator in sorted(now - recorded):
        problems.append(f"NEW SURVIVOR {arm} {operator}: no test fails when "
                        f"{EFFECT[operator]}")
    for arm, operator in sorted(recorded - now):
        problems.append(f"no longer survives {arm} {operator}: delete the "
                        "entry from the ledger (--write)")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", action="store_true",
                        help="list the refusal arms of the target; run nothing")
    parser.add_argument("--check", action="store_true",
                        help="fail unless the survivors equal the ledger")
    parser.add_argument("--write", action="store_true",
                        help="regenerate the ledger from a run")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args(argv)

    if args.arms:
        for arm in arms((ROOT / TARGET).read_text(encoding="utf-8")):
            print(f"{arm.id}  line {arm.line}")
        return 0

    report = survey(progress=lambda m: print(
        f"  {m['status']:8s} {m['arm']} {m['operator']}", flush=True))
    killed = sum(m["status"] == "killed" for m in report["mutants"])
    print(f"{len(report['mutants'])} mutants over {len(report['arms'])} refusal "
          f"arms of {report['target']}: {killed} killed, "
          f"{len(report['survivors'])} survived")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    if args.write:
        LEDGER.write_text(json.dumps(ledger_of(report), indent=1) + "\n",
                          encoding="utf-8")
        print(f"wrote {LEDGER.relative_to(ROOT)}")
        return 0
    if args.check:
        problems = check(report, load_ledger())
        for problem in problems:
            print(f"MUTATION-RED {problem}", file=sys.stderr)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
