#!/usr/bin/env python3
"""The compositional blast-radius benchmark (issue #1702).

A task is a change to a composition of 20+ components. Its ground truth is
the set of components that must change, and it comes from the compiler's own
composition queries (`revl_query_withdraw`, `revl_query_drift`,
`revl_query_emitters`), so it cannot drift from what the linker resolves.
No model runs here: this file defines tasks, checks them, renders them to
TypeScript, and scores a finished attempt.

Usage:
    python3 bench/blast_radius/blast_radius.py --check
    python3 bench/blast_radius/blast_radius.py --render-ts       # rewrite ts/
    python3 bench/blast_radius/blast_radius.py --score SUBMISSION.json

`--check` recomputes every cascade and answer from the named query, compiles
every expected final state and checks it is the state the cascade implies,
and compares the TypeScript renderings with what the ts emitter produces now.
It exits 1 on any difference.

A submission is a JSON object:

    {"task": "<task id>", "arm": "revl" | "typescript",
     "final": "<path to the final .rvl or .ts, relative to the submission>",
     "answer": "<for a question task>", "turns": <int>, "tokensWritten": <int>,
     "revlState": <the last revl_state result, optional>}

The score reports whether the final state is correct, which components it
touched, which of those lay outside the cascade, which cascade components it
missed, turns and tokens written, and the approval-gate axes read from
`revlState`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from revl import query as Q  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

COMPOSITIONS = HERE / "compositions"
TS_DIR = HERE / "ts"
TASKS = HERE / "tasks.json"
TS_RUNTIME_IMPORT = "../../../backends/typescript/runtime.ts"
QUESTION_KINDS = ("try-and-revert", "no-change")


# ------------------------------------------------------------------ loading

def load_tasks() -> list[dict]:
    return json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]


def task_by_id(task_id: str) -> dict:
    for task in load_tasks():
        if task["id"] == task_id:
            return task
    raise KeyError(f"no task {task_id!r} in {TASKS.name}")


def composition_path(name: str) -> Path:
    return COMPOSITIONS / f"{name}.rvl"


def compile_path(path: Path) -> dict:
    return compile_files([str(path)])


# ------------------------------------------------------------- ground truth

def _queries(task: dict) -> list[dict]:
    spec = task["query"]
    return spec if isinstance(spec, list) else [spec]


def run_query(ir: dict, spec: dict) -> dict:
    verb = spec["verb"]
    if verb == "withdraw":
        return Q.withdrawal(ir, spec["component"])
    if verb == "drift":
        return Q.drift(ir, spec["service"], losses=spec.get("loses") or ())
    if verb == "emitters":
        return Q.emitters(ir, spec["target"])
    raise ValueError(f"unknown query verb {verb!r}")


def _drift_cascade(result: dict) -> set[str]:
    names: set[str] = set()
    for loss in result["losses"]:
        names.update(loss["providersMustDrop"])
        names.update(site["component"] for site in loss["callSites"])
    return names


def _change_cascade(task: dict, ir: dict) -> list[str]:
    """The components a correct change must touch, from the task's queries."""
    names: set[str] = set()
    for spec in _queries(task):
        result = run_query(ir, spec)
        if spec["verb"] == "withdraw":
            names.update(entry["component"] for entry in result["cascade"])
        elif spec["verb"] == "drift":
            names.update(_drift_cascade(result))
    return sorted(names)


def _answer(task: dict, ir: dict) -> str:
    """The answer to a question task, from its `answerRule` over the query."""
    rule = task["groundTruth"]["answerRule"]
    result = run_query(ir, _queries(task)[0])
    if "orphansKey" in rule:
        keys = {o["key"] for o in result["orphanedKeys"]}
        return "yes" if rule["orphansKey"] in keys else "no"
    if "orphansRealm" in rule:
        realms = {o["realm"] for o in result["orphanedKeys"]}
        return "yes" if rule["orphansRealm"] in realms else "no"
    if "absentFromEmitters" in rule:
        hit = rule["absentFromEmitters"] in result["components"]
        return "change needed" if hit else "no change needed"
    if "absentFromCascade" in rule:
        hit = rule["absentFromCascade"] in {e["component"] for e in result["cascade"]}
        return "change needed" if hit else "no change needed"
    raise ValueError(f"unknown answer rule {rule!r}")


def ground_truth(task: dict, ir: dict) -> dict:
    """The task's ground truth recomputed from the compiler."""
    truth = {"cascade": [] if task["kind"] in QUESTION_KINDS
             else _change_cascade(task, ir)}
    if task["kind"] in QUESTION_KINDS:
        truth["answer"] = _answer(task, ir)
    return truth


# ------------------------------------------------------- component identity

def _signature(comp: dict) -> str:
    payload = json.dumps({k: v for k, v in comp.items() if k != "source"},
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revl_signatures(ir: dict) -> dict[str, str]:
    return {comp["name"]: _signature(comp) for comp in ir["components"]}


def touched(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Components added, removed, or changed between two signature maps."""
    names = set(before) | set(after)
    return sorted(n for n in names if before.get(n) != after.get(n))


def _service_methods(ir: dict, service: str) -> set[str]:
    return set((ir["services"].get(service) or {}).get("methods") or {})


def _withdrawn(task: dict, ir: dict) -> set[str]:
    spec = _queries(task)[0]
    return {spec["component"]} | set(_change_cascade(task, ir))


def _lost_methods(task: dict) -> list[tuple[str, str]]:
    return [(spec["service"], method) for spec in _queries(task)
            for method in spec.get("loses") or ()]


# --------------------------------------------------- correctness, revl arm

def revl_final_problems(task: dict, base: dict, final: dict) -> list[str]:
    """Why a compiled final state is not a correct solution ([] when it is)."""
    before, after = revl_signatures(base), revl_signatures(final)
    if task["kind"] == "withdraw":
        return _withdraw_problems(set(before) - _withdrawn(task, base),
                                  before, after)
    if task["kind"] == "interface":
        problems = [f"{svc} still declares `{m}`" for svc, m in _lost_methods(task)
                    if m in _service_methods(final, svc)]
        if set(after) != set(before):
            problems.append("the component set changed")
        return problems
    return [] if after == before else ["the composition was left changed"]


def _withdraw_problems(survivors: set[str], before: dict, after: dict) -> list[str]:
    problems = []
    if set(after) != survivors:
        missing = sorted(survivors - set(after))
        extra = sorted(set(after) - survivors)
        if missing:
            problems.append(f"withdrew components that still link: {missing}")
        if extra:
            problems.append(f"kept components that cannot link: {extra}")
    changed = [n for n in sorted(survivors & set(after)) if after[n] != before[n]]
    if changed:
        problems.append(f"changed surviving components: {changed}")
    return problems


# ---------------------------------------------------- the TypeScript arm

_BLOCK = re.compile(r"^export const (\w+) = \{\n(.*?)^\}\n", re.M | re.S)
_INTERFACE = re.compile(r"^export interface (\w+) \{\n(.*?)^\}\n", re.M | re.S)
_ARRAY = r'^  {field}: \[(.*?)\],$'


def ts_components(text: str) -> dict[str, str]:
    """Each emitted component block (`export const X = {...}` carrying a
    `name:` field), keyed by component name."""
    blocks = {}
    for match in _BLOCK.finditer(text):
        if re.search(r'^  name: "', match.group(2), re.M):
            blocks[match.group(1)] = match.group(2)
    return blocks


def _ts_list(block: str, field: str) -> list[str]:
    match = re.search(_ARRAY.format(field=field), block, re.M)
    return re.findall(r'"(\w+)"', match.group(1)) if match else []


def _ts_realms(block: str) -> dict[str, str]:
    match = re.search(r"^  isolate: \{(.*?)\},$", block, re.M)
    return dict(re.findall(r'"(\w+)": "(\w+)"', match.group(1))) if match else {}


def ts_link_problems(blocks: dict[str, str]) -> list[str]:
    """Every injected (key, realm) must have a provider among the blocks."""
    provided = {(key, _ts_realms(b).get(key, ""))
                for b in blocks.values() for key in _ts_list(b, "provide")}
    problems = []
    for name, block in sorted(blocks.items()):
        realms = _ts_realms(block)
        for key in _ts_list(block, "inject"):
            if (key, realms.get(key, "")) not in provided:
                problems.append(f"{name} injects `{key}`, which nothing provides")
    return problems


def ts_interface_methods(text: str, service: str) -> set[str]:
    for match in _INTERFACE.finditer(text):
        if match.group(1) == service:
            return set(re.findall(r"^  (\w+)\(", match.group(2), re.M))
    return set()


def ts_final_problems(task: dict, base_ir: dict, base_ts: str, final_ts: str) -> list[str]:
    before = {n: _digest(b) for n, b in ts_components(base_ts).items()}
    blocks = ts_components(final_ts)
    after = {n: _digest(b) for n, b in blocks.items()}
    problems = ts_link_problems(blocks)
    if task["kind"] == "withdraw":
        problems += _withdraw_problems(set(before) - _withdrawn(task, base_ir),
                                       before, after)
    elif task["kind"] == "interface":
        problems += [f"interface {svc} still declares `{m}`"
                     for svc, m in _lost_methods(task)
                     if m in ts_interface_methods(final_ts, svc)]
        if set(after) != set(before):
            problems.append("the component set changed")
    elif after != before:
        problems.append("the composition was left changed")
    return problems


def _digest(block: str) -> str:
    return hashlib.sha256(" ".join(block.split()).encode("utf-8")).hexdigest()


def ts_signatures(text: str) -> dict[str, str]:
    return {n: _digest(b) for n, b in ts_components(text).items()}


def render_ts(name: str) -> str:
    from backends.typescript.emit import emit  # noqa: PLC0415
    return emit(compile_path(composition_path(name)),
                runtime_import=TS_RUNTIME_IMPORT)


# -------------------------------------------------------------- the scorer

# The approval-gate axes the scorer reads from a `revl_state` result's
# `loopAxes` block (issue #1738). Each is `{numerator, denominator, value}`
# as the session reported it. An axis the result does not carry is `null` and
# is named in `notInRevlState`, so a score never reads an absence as a zero.
STATE_AXES = (
    "reversibilityRate", "autoApprovedWithProof", "promptsPerSession",
    "preflightCoverage", "violationsCaughtBeforeExecution", "residueAfterAbort",
)


def _axis(reported) -> dict | None:
    if not isinstance(reported, dict):
        return None
    if not {"numerator", "denominator", "value"} <= set(reported):
        return None
    return {k: reported[k] for k in ("numerator", "denominator", "value")}


def gate_axes(state: dict | None) -> dict:
    reported = (state or {}).get("loopAxes") or {}
    axes = {name: _axis(reported.get(name)) for name in STATE_AXES}
    axes["notInRevlState"] = [name for name in STATE_AXES if axes[name] is None]
    return axes


def _final_state(task: dict, arm: str, final_path: Path):
    """(signatures before, signatures after, problems) for one attempt."""
    base_path = composition_path(task["composition"])
    base_ir = compile_path(base_path)
    if arm == "revl":
        try:
            final_ir = compile_path(final_path)
        except Exception as error:  # noqa: BLE001 (a refused state is a score)
            return revl_signatures(base_ir), {}, [f"does not compile: {error}"]
        return (revl_signatures(base_ir), revl_signatures(final_ir),
                revl_final_problems(task, base_ir, final_ir))
    base_ts = (TS_DIR / f"{task['composition']}.ts").read_text(encoding="utf-8")
    final_ts = final_path.read_text(encoding="utf-8")
    return (ts_signatures(base_ts), ts_signatures(final_ts),
            ts_final_problems(task, base_ir, base_ts, final_ts))


def score(submission: dict, base_dir: Path) -> dict:
    task = task_by_id(submission["task"])
    arm = submission.get("arm", "revl")
    before, after, problems = _final_state(task, arm, base_dir / submission["final"])
    truth = ground_truth(task, compile_path(composition_path(task["composition"])))
    if "answer" in truth and submission.get("answer") != truth["answer"]:
        problems.append(f"answered {submission.get('answer')!r}, "
                        f"the answer is {truth['answer']!r}")
    changed = touched(before, after)
    allowed = set(truth["cascade"])
    if task["kind"] == "withdraw":
        allowed.add(_queries(task)[0]["component"])
    return {
        "task": task["id"], "arm": arm, "correct": not problems,
        "problems": problems, "touched": changed,
        "touchedBeyondCascade": sorted(set(changed) - allowed),
        "missedCascade": sorted(set(truth["cascade"]) - set(changed)),
        "turns": submission.get("turns"),
        "tokensWritten": submission.get("tokensWritten"),
        "gate": gate_axes(submission.get("revlState")),
    }


# --------------------------------------------------------------- the check

def check_task(task: dict) -> list[str]:
    base = compile_path(composition_path(task["composition"]))
    problems = _components_floor(task, base)
    truth = ground_truth(task, base)
    recorded = task["groundTruth"]
    if truth["cascade"] != recorded["cascade"]:
        problems.append(f"cascade is {truth['cascade']}, tasks.json records "
                        f"{recorded['cascade']}")
    if truth.get("answer") != recorded.get("answer"):
        problems.append(f"answer is {truth.get('answer')!r}, tasks.json records "
                        f"{recorded.get('answer')!r}")
    return problems + _expected_problems(task, base, truth)


def _components_floor(task: dict, base: dict) -> list[str]:
    count = len(base["components"])
    return [] if count >= 20 else [f"{task['composition']} has {count} components, under 20"]


def _expected_problems(task: dict, base: dict, truth: dict) -> list[str]:
    """The expected final state must compile, be correct, and touch exactly
    the cascade (plus the withdrawn component)."""
    if task["expected"] is None:
        return [] if task["kind"] in QUESTION_KINDS else ["no expected state"]
    final = compile_path(HERE / task["expected"])
    problems = revl_final_problems(task, base, final)
    changed = set(touched(revl_signatures(base), revl_signatures(final)))
    want = set(truth["cascade"])
    if task["kind"] == "withdraw":
        want.add(_queries(task)[0]["component"])
    if changed != want:
        problems.append(f"expected state touches {sorted(changed)}, the cascade "
                        f"is {sorted(want)}")
    return problems


def check_renderings() -> list[str]:
    problems = []
    for path in sorted(COMPOSITIONS.glob("*.rvl")):
        rendering = TS_DIR / f"{path.stem}.ts"
        if not rendering.exists() or rendering.read_text(encoding="utf-8") != render_ts(path.stem):
            problems.append(f"{rendering.relative_to(ROOT)} is stale: run --render-ts")
    return problems


def check() -> list[str]:
    problems = []
    for task in load_tasks():
        problems += [f"{task['id']}: {p}" for p in check_task(task)]
    return problems + check_renderings()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--render-ts", action="store_true")
    group.add_argument("--score", metavar="SUBMISSION")
    args = ap.parse_args(argv)
    if args.render_ts:
        for path in sorted(COMPOSITIONS.glob("*.rvl")):
            (TS_DIR / f"{path.stem}.ts").write_text(render_ts(path.stem), encoding="utf-8")
        return 0
    if args.score:
        path = Path(args.score).resolve()
        print(json.dumps(score(json.loads(path.read_text(encoding="utf-8")),
                               path.parent), indent=2))
        return 0
    problems = check()
    for problem in problems:
        print(problem)
    print(f"blast-radius bench: {len(load_tasks())} tasks, "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
