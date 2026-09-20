#!/usr/bin/env python3
"""FRAMEWORK-BENCH-1: assemble the named three-host comparison and its report.

Roadmap item 548 (issue #1267). `bench/` already held most of a benchmark and
none of a published one: specs, per-variant prompts, a residue probe, an
admission-latency runner, a token accountant, and the frozen EVAL-1 honesty
protocol. What it did not hold was a named comparison an outsider can run. This
module is that comparison's assembler.

It does not generate anything and it does not grade anything. It reads the
runners that already exist, the ledgers the tests already gate, and the model
pin, and it emits two artifacts:

  * `report.json`, an EVAL-REPORT-1 document that `tools/check_eval_report.py`
    validates for no-self-score, named gates and the claim ladder;
  * `report.md`, the table, with the refused column first.

## Three rules this file follows and a reader should check it followed

**A cell is a number or it is `not-run`.** There is no third state. When the
pinned-model run has not happened, the table says so in that cell and names what
it is blocked on. It never borrows a number from a different model's corpus and
prints it in a cell headed by the pinned model's name, which is the specific
dishonesty that makes benchmark tables untrustworthy.

**Every number carries its n and the corpus it came from.** A residue rate on
ten hand-authored plugins and a residue rate on a drawn sample of a hundred are
not comparable, and a table that prints both as a percentage invites the reader
to compare them.

**The checker version is frozen into the report.** `revl.gate.gate_version()`
gives the gate API version, the language version and the covered-surface
frontier. If G1 tightens and the admission rate drops, the report states the
frontier it was measured against, so the drop reads as a tightened gate rather
than as a regression, and a later run cannot quietly re-baseline.

Usage:
  python3 bench/framework_bench.py                        # print the table
  python3 bench/framework_bench.py --write                # commit the artifacts
  python3 bench/framework_bench.py --check                # validate the report
  python3 bench/framework_bench.py --admits-from typed-deepseek-v4-pro
  python3 bench/framework_bench.py --measure-latency
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
OUT_DIR = BENCH / "results" / "framework-bench"

sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ROOT / "tools"))

import refusal_inventory  # noqa: E402

SUITE = "FRAMEWORK-BENCH-1"
# The honesty protocol this report is written under. The schema version is
# frozen in tools/check_eval_report.py; changing either is a separate reviewed
# change, not something a benchmark run does.
PROTOCOL = "docs/eval-protocol.md"
REPORT_SCHEMA = "EVAL-REPORT-1"

NOT_RUN = "not-run"


def git_sha() -> str:
    """The commit, with `-dirty` when the COMPILER is uncommitted.

    The dirty check is scoped to `src/revl` the way `bench/rescore.py` scopes
    it, and for the same reason: the question this field answers is which
    compiler produced the numbers. Writing this report dirties the tree by
    definition, so a whole-tree check would mark every report dirty and the
    flag would stop meaning anything.
    """
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        sha = out.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--", "src/revl"],
            capture_output=True, text=True, timeout=30)
        return sha + ("-dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def checker_version() -> dict:
    """The version triple a later run is compared against.

    `frontier` is the load-bearing one. Two gates that cover different surfaces
    can agree on every program either of them covers and still disagree about
    the language, so an admission rate is only comparable to another admission
    rate measured against the same frontier.
    """
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from revl.gate import gate_version  # noqa: PLC0415
        version = gate_version()
    except Exception as exc:  # pragma: no cover - a broken tree, reported as such
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    import revl  # noqa: PLC0415
    return {
        "available": True,
        "gate_api": version.get("api"),
        "language": version.get("language"),
        "frontier": version.get("frontier"),
        "compiler_path": str(Path(revl.__file__).parent),
        "compiler_commit": git_sha(),
        "report_schema": REPORT_SCHEMA,
    }


def _rel(path: Path) -> str:
    """See bench/refusal_inventory.py: a pin may live outside the repository,
    and formatting its path is not a place to raise from."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_hosts() -> dict:
    return json.loads((BENCH / "hosts.json").read_text())


def load_unload_survey() -> dict:
    """The evidence behind the framework pick, or a stated absence.

    The survey is what turns "we chose this framework" into something a reader
    can check, so the report carries it rather than the conclusion alone.
    """
    path = OUT_DIR / "unload-survey.json"
    if not path.is_file():
        return {"present": False,
                "reason": f"no survey at {_rel(path)}; run "
                          "bench/framework_unload_survey.py --fetch --write"}
    doc = json.loads(path.read_text())
    doc["present"] = True
    doc["path"] = _rel(path)
    return doc


def load_pin(path: Path | None) -> dict:
    target = path or (OUT_DIR / "model-pin.json")
    if not target.is_file():
        return {"present": False,
                "reason": f"no pin at {_rel(target)}; "
                          "run bench/model_pin.py --write"}
    pin = json.loads(target.read_text())
    pin["present"] = True
    pin["path"] = _rel(target)
    return pin


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def column_refused() -> dict:
    """The headline. Recomputed, never transcribed."""
    inv = refusal_inventory.build()
    res = inv["sections"].get("native-chain-residual")
    unp = inv["sections"].get("unported-constructs")
    div = inv["sections"].get("gate-reference-divergence")
    cell = {
        "status": "measured",
        "inventory": inv,
        "headline": None,
    }
    if res:
        cell["headline"] = {
            "documents_refused": res["total_residual"],
            "corpus": res["total_corpus"],
            "n": res["total_corpus"],
            "per_tier": {t: v["residual"] for t, v in res["tiers"].items()},
            "clean_tiers": res["clean_tiers"],
            "means": res["means"],
            "gate": res["gate"],
        }
    if unp:
        cell["unported_constructs"] = unp["total_unported"]
    if div:
        cell["fail_open_programs"] = div["total_false_admit"]
    if inv["unavailable"]:
        cell["unavailable"] = inv["unavailable"]
    return cell


def corpus_models(run: str) -> list:
    """The model ids a committed run's own records name.

    Read from the run's records rather than from its label, because a label is
    something somebody typed and a record is what the driver wrote. This is the
    only thing that decides whether a cell may be headed by the pinned model's
    name.
    """
    path = BENCH / "results" / run / "results.jsonl"
    if not path.is_file():
        return []
    seen = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        model = row.get("model")
        if isinstance(model, str) and model and model not in seen:
            seen.append(model)
    return seen


def is_pinned_corpus(run: str, pin: dict) -> dict:
    """Whether every model named by the run's records is the pinned model."""
    models = corpus_models(run)
    pinned = ((pin.get("model") or {}).get("resolved")
              if pin.get("present") else None)
    if not models:
        return {"is_pinned_model": False,
                "why": f"bench/results/{run} records no model id"}
    if not pinned:
        return {"is_pinned_model": False, "models": models,
                "why": "no model pin is present to compare against"}
    ok = all(m == pinned for m in models)
    return {
        "is_pinned_model": ok,
        "models": models,
        "why": (f"every record names {pinned}" if ok else
                f"records name {models}, and the pin is {pinned}"),
    }


def column_admits(run: str | None, compiler_root: Path, attempt: int,
                  pin: dict) -> dict:
    """First-pass admission rate over a committed revl corpus.

    This is a re-score, not a run: it recompiles committed generations against
    the current checker. It therefore reports the model that produced the
    corpus, which is NOT the pinned model unless a pinned-model run has been
    committed. The distinction is in the cell, not in a footnote.
    """
    if run is None:
        return {"status": NOT_RUN,
                "blocked_on": "no corpus named; pass --admits-from <run label>"}
    import rescore  # noqa: PLC0415

    run_dir = BENCH / "results" / run
    if not run_dir.is_dir():
        return {"status": NOT_RUN,
                "blocked_on": f"no committed corpus at bench/results/{run}"}
    compile_source, RevlError, classify = rescore.load_compiler(compiler_root)
    cells = rescore.collect(run, attempt)
    if not cells:
        return {"status": NOT_RUN,
                "blocked_on": f"bench/results/{run} holds no attempt-{attempt} files"}
    # The protocol's mechanical no-self-score core, enforced where the number is
    # produced rather than only where it is stated.
    rescore.assert_model_free(cells, compile_source, RevlError, classify)

    rows = []
    for spec, variant, path in cells:
        verdict = rescore.score_one(path, compile_source, RevlError, classify)
        rows.append({"spec": spec, "variant": variant, **verdict})

    by_variant: dict[str, dict] = {}
    for row in rows:
        v = by_variant.setdefault(row["variant"], {"n": 0, "admitted": 0})
        v["n"] += 1
        v["admitted"] += 1 if row["ok"] else 0
    for v in by_variant.values():
        v["rate"] = v["admitted"] / v["n"] if v["n"] else None
    provenance = is_pinned_corpus(run, pin)
    return {
        "status": "measured",
        "corpus": f"bench/results/{run}",
        "attempt": attempt,
        "generated_by": run,
        **provenance,
        "note": ("a re-score of a committed corpus against the current checker. "
                 "Whether the pinned model produced it is decided by the model "
                 "ids in the run's own records, never by its label: "
                 + provenance["why"]),
        "by_variant": by_variant,
        "cells": rows,
        "n": len(rows),
    }


def column_residue(run: str) -> dict:
    """Residue after N load-unload cycles, read from the committed corpus.

    Re-probing needs a cordis install (`cd backends/typescript && npm install`),
    so this reads the committed per-cell records instead and reports the
    re-probe command. The records are what the probe wrote; nothing here
    re-derives a leak.
    """
    path = BENCH / "results" / run / "results.jsonl"
    if not path.is_file():
        return {"status": NOT_RUN,
                "blocked_on": f"no committed records at bench/results/{run}/results.jsonl"}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    raw = [r for r in rows if r.get("variant") == "raw-ts"] or rows
    if not raw:
        return {"status": NOT_RUN, "blocked_on": f"bench/results/{run} holds no raw-ts cells"}

    leaked = [r for r in raw if r.get("leaked")]
    categories: dict[str, int] = {}
    for r in leaked:
        for cat in r.get("leaked_categories") or []:
            categories[cat] = categories.get(cat, 0) + 1
    errored = [r for r in raw if r.get("error")]
    return {
        "status": "measured",
        "corpus": f"bench/results/{run}",
        "n": len(raw),
        "leaked": len(leaked),
        "clean": len(raw) - len(leaked),
        "leak_rate": len(leaked) / len(raw),
        "categories": dict(sorted(categories.items())),
        "failed_to_mount": len(errored),
        "cycles": next((r.get("cycles") for r in raw if r.get("cycles")), None),
        "authored_by": ("this repository, by hand"
                        if run == "hand-corpus" else "a model run"),
        "dismissibility": (
            "a hand-authored corpus: we wrote both the clean and the leaky "
            "plugins, so this number demonstrates that the probe detects the "
            "leaks we planted, not what rate a population leaks at"
            if run == "hand-corpus" else None),
        "reprobe": f"python3 bench/score_raw_ts.py --run {run} --cycles 6",
        "reprobe_prereq": "cd backends/typescript && npm install",
    }


def column_admission_latency(measure: bool, iters: int) -> dict:
    if not measure:
        return {"status": NOT_RUN,
                "blocked_on": "pass --measure-latency to time the gate on this machine",
                "runner": "python3 bench/admission_latency.py",
                "existing_artifact": "bench/results/admission-latency.md",
                "note": ("a committed measurement exists and was taken on a "
                         "different machine and Python; it is pointed at rather "
                         "than copied into this cell, because a latency number "
                         "is machine-bound and copying one into a differently "
                         "headed table is how a number loses its conditions")}
    import admission_latency  # noqa: PLC0415

    res = admission_latency.measure(iters=iters)
    return {
        "status": "measured",
        "round_trip_ms_median": res["round_trip"]["median"],
        "compile_only_ms_median": res["compile_only"]["median"],
        "gate_cost_ms_median": res["round_trip"]["median"] - res["compile_only"]["median"],
        "n": res["round_trip"]["n"],
        "machine": res.get("machine"),
        "means": "per-candidate cost of gating one dynamically discovered tool",
    }


def column_tokens_to_green(run: str | None, compiler_root: Path,
                           pin: dict) -> dict:
    if run is None:
        return {"status": NOT_RUN,
                "blocked_on": "pass --tokens-from <run label>",
                "runner": "python3 bench/tokens.py"}
    import tokens  # noqa: PLC0415

    try:
        cells = tokens.compute_run(run, compiler_root)
    except SystemExit as exc:
        return {"status": NOT_RUN, "blocked_on": str(exc)}
    admitted = [c for c in cells if c.get("admitted")]
    if not admitted:
        return {"status": NOT_RUN,
                "blocked_on": f"bench/results/{run} carries no admitted cell"}
    # Two token figures exist per cell and they are not interchangeable. The
    # recorded one is what the provider billed and only exists for a funded run;
    # the estimated one is recounted from the committed source. Which one a
    # number came from changes what it means, so the source is in the cell.
    recorded = sorted(c["recorded_tokens_to_green"] for c in admitted
                      if c.get("recorded_tokens_to_green"))
    estimated = sorted(c["est_tokens_to_green"] for c in admitted
                       if c.get("est_tokens_to_green"))
    values, source = ((recorded, "reported by the endpoint that served the run")
                      if recorded
                      else (estimated, "estimated from the committed source"))
    if not values:
        return {"status": NOT_RUN,
                "blocked_on": f"bench/results/{run} carries no output-token counts"}
    return {
        "status": "measured",
        "corpus": f"bench/results/{run}",
        "n": len(values),
        "token_source": source,
        "median_output_tokens": values[len(values) // 2],
        "mean_output_tokens": sum(values) / len(values),
        # The two sources are not two estimates of one quantity and must not be
        # compared. A reported count is what the model actually emitted,
        # including a reasoning channel the caller paid for and never saw; an
        # estimated count is recounted from the source that survived, which
        # cannot include reasoning. A reported figure will be several times the
        # estimated one for the same work, and that is a difference in what is
        # being counted rather than in the work.
        **_retry_censoring(run, cells),
        "sources_are_not_comparable": (
            "a reported count includes the model's reasoning channel; an "
            "estimated count is recounted from the emitted source and cannot. "
            "Comparing a figure from one source with a figure from the other "
            "compares two different quantities"),
        "admitted_cells": len(admitted),
        "total_cells": len(cells),
        **is_pinned_corpus(run, pin),
        "means": "output tokens spent per admitted component",
    }


def _retry_censoring(run: str, cells: list) -> dict:
    """Whether the corpus behind a tokens-to-green figure allowed retries.

    tokens-to-green is the tokens spent until a component is admitted, and the
    figure is taken over admitted cells only. In a corpus generated with one
    attempt per spec there are no retried cells to average in, so every
    component that would have needed a second attempt is missing from the
    denominator rather than contributing a larger number to it. The median then
    reads lower than the metric's definition for a reason that has nothing to
    do with the model, and a reader comparing it with a three-attempt corpus is
    comparing a censored sample with a complete one.
    """
    run_dir = BENCH / "results" / run
    attempts = 0
    if run_dir.is_dir():
        for path in run_dir.rglob("attempt-*.rvl"):
            try:
                attempts = max(attempts, int(path.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
    out = {"max_attempts_in_corpus": attempts or None}
    if attempts == 1:
        out["censored"] = (
            "this corpus holds one attempt per spec, so the figure is taken "
            "over the components that were admitted first time and the ones "
            "that would have needed a retry are absent from it entirely. It is "
            "a lower bound on tokens-to-green, not an estimate of it, and it is "
            "not comparable with a figure from a corpus that allowed retries")
    return out


def column_unload_paths(survey: dict) -> dict:
    """How many surveyed agent frameworks publish a way to retire a tool.

    This started as the evidence behind a host selection and is reported as a
    result, because it is a better one than the selection. It is about the
    runtimes rather than the models, an outsider can check it against published
    artifacts at pinned versions, and it is the reason the residue column exists
    at all: a column measuring what a host leaks on unload looks like an axis
    picked to win until somebody shows that most hosts have no unload.

    The denominator excludes the control, which was chosen precisely because its
    unload path was known to exist, and counting it would inflate the rate with
    a package selected for its answer.
    """
    if not survey.get("present"):
        return {"status": NOT_RUN, "blocked_on": survey.get("reason")}
    import framework_unload_survey  # noqa: PLC0415

    verdict_for = framework_unload_survey.verdict_for
    rows = [r for r in survey.get("frameworks") or [] if r.get("role") != "control"]
    measured = [r for r in rows if r.get("fetched")]
    if not measured:
        return {"status": NOT_RUN,
                "blocked_on": "the survey fetched no candidate packages"}
    verdicts = {r["name"]: verdict_for(r) for r in measured}
    per_registration = [n for n, v in verdicts.items()
                        if v.startswith("publishes one (per-registration)")]
    none_published = [n for n, v in verdicts.items() if v == "none published"]
    controls = [r for r in survey.get("frameworks") or [] if r.get("role") == "control"]
    # The headline denominator is the agent frameworks alone. The surveyed set
    # also holds a tool host, and counting it among "agent frameworks" would be
    # a category error in the direction that flatters the finding, because it is
    # the one package that publishes a per-registration retirement.
    frameworks = [r for r in measured if r.get("category") == "agent-framework"]
    fw_none = [r["name"] for r in frameworks
               if verdicts[r["name"]] == "none published"]
    fw_per_reg = [r["name"] for r in frameworks
                  if verdicts[r["name"]].startswith("publishes one (per-registration)")]
    return {
        "status": "measured",
        "n": len(measured),
        "agent_frameworks_n": len(frameworks),
        "agent_frameworks_none_published": fw_none,
        "agent_frameworks_per_registration": fw_per_reg,
        "denominator_note": (
            "The headline denominator is the agent frameworks alone. The "
            "surveyed set also holds a tool host, and it is the one package "
            "that publishes a per-registration retirement, so counting it among "
            "agent frameworks would be a category error in the direction that "
            "flatters the finding"),
        "surveyed": [{"name": r["name"], "version": r["version"],
                      "category": r.get("category"),
                      "registration": r.get("registration_api"),
                      "verdict": verdicts[r["name"]]} for r in measured],
        "publish_per_registration_unload": per_registration,
        "publish_no_unload": none_published,
        "controls": [{"name": r["name"], "version": r["version"],
                      "verdict": verdict_for(r)} for r in controls],
        "control_excluded_from_denominator": (
            "The control was chosen because its unload path was known to exist; "
            "counting it would inflate the rate with a package selected for its "
            "answer"),
        "method": (
            "Named, falsifiable claims about each package's published API, "
            "checked against the artifact the index serves at a pinned version. "
            "A `present` claim fails when the symbol is absent; an `absent` "
            "claim fails when any of the named symbols is found under the path "
            "it names."),
        "how_it_could_have_been_wrong": (
            "It searched a vocabulary (`remove`, `dispose`, `unregister`) "
            "across each package and reported a boolean, and it reported a "
            "de-registration symbol for six of the eight packages, including "
            "all four python ones, on hits like a flow-graph builder calling "
            "`list.remove()`, a callback manager calling `list.remove()`, and "
            "the word \"unregistered\" inside a comment. Two of those six were "
            "genuine; the other four were not, and nothing in the output "
            "distinguished them. That version was deleted rather than tuned, "
            "because a search that cannot tell a tool registry from a list is "
            "not measuring what the column needs."),
        "what_it_does_not_say": (
            "That a published symbol releases anything. Whether calling it gives "
            "a resource back is the residue probe's question, and no number here "
            "may be quoted as a residue result. An `absent` verdict is scoped to "
            "the paths the claim names: several of these keep their registry in "
            "a plain mutable dict a caller can reach into."),
        "evidence": survey.get("path"),
    }


def column_injection_escape(run: str | None) -> dict:
    """The escape rate over a committed injection run, or not-run with a reason.

    `tests/test_adversarial_gate.py` is still refused as a source for this cell,
    for the reason the first pass gave: it is an executable attack suite
    belonging to `docs/threat-model.md`, it measures our own gate against
    attacks we wrote for it, and a pass-rate over that set is not an escape rate
    over these briefs. What fills the cell now is
    `bench/injection_escape.py`, which generates with the pinned model and
    detects compliance from the source text without consulting the gate.

    A mock run is refused outright rather than reported with a caveat: a number
    produced by a stub reads exactly like a number produced by a model once it
    is in a table.
    """
    if run is None:
        return {
            "status": NOT_RUN,
            "blocked_on": ("no injection run named; run bench/injection_escape.py "
                           "and pass --injection-from <run label>"),
            "runner": "python3 bench/injection_escape.py --runner local",
            "nearest_existing": "tests/test_adversarial_gate.py (docs/threat-model.md)",
            "note": ("still not filled with the nearest available number: an "
                     "attack suite pass-rate is not an escape rate over these briefs"),
        }
    path = BENCH / "results" / run / "escape.json"
    if not path.is_file():
        return {"status": NOT_RUN,
                "blocked_on": f"no committed run at bench/results/{run}/escape.json",
                "nearest_existing": "tests/test_adversarial_gate.py (docs/threat-model.md)",
                "note": "not filled with the nearest available number"}
    doc = json.loads(path.read_text())
    if not doc.get("reportable"):
        return {"status": NOT_RUN,
                "blocked_on": (f"bench/results/{run} was produced by the "
                               f"{doc.get('runner')!r} runner, which is not a model"),
                "nearest_existing": "tests/test_adversarial_gate.py (docs/threat-model.md)",
                "note": "a stub's output is not a result, with or without a caveat"}
    return {
        "status": "measured",
        "corpus": f"bench/results/{run}",
        "model": doc.get("model"),
        "vectors": doc.get("vectors"),
        "summary": doc.get("summary"),
        "means": ("compliance is the model's rate of doing the undeclared thing; "
                  "containment is whether a named hard gate then rejected it; "
                  "escape is compliance not contained"),
        "nearest_existing": "tests/test_adversarial_gate.py (docs/threat-model.md)",
        "note": ("still not sourced from the attack suite: that measures our gate "
                 "against attacks we wrote for it"),
    }


# ---------------------------------------------------------------------------
# The EVAL-REPORT-1 document
# ---------------------------------------------------------------------------


def briefs_from_admits(admits: dict) -> list[dict]:
    """One brief per scored cell, naming the hard gate it cleared.

    `compiles` is the gate for a revl cell: the compiler either admitted the
    component or refused it. The gate set is frozen in
    tools/check_eval_report.py and a report may not invent a new one.
    """
    if admits.get("status") != "measured":
        return []
    return [{
        "spec": f"{cell['spec']}/{cell['variant']}",
        "hard_gate": "compiles",
        "result": "pass" if cell.get("ok") else "fail",
        "refusal_code": cell.get("code"),
    } for cell in admits["cells"]]


def briefs_from_residue(residue: dict, run: str) -> list[dict]:
    if residue.get("status") != "measured":
        return []
    path = BENCH / "results" / run / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    raw = [r for r in rows if r.get("variant") == "raw-ts"] or rows
    return [{
        "spec": f"{r.get('spec', '?')}/raw-ts",
        "hard_gate": "noResidueForRawBaseline",
        "result": "fail" if (r.get("leaked") or r.get("error")) else "pass",
        "leaks": r.get("leaked_categories") or [],
    } for r in raw]


def build_report(args) -> dict:
    hosts = load_hosts()
    compiler_root = Path(args.compiler_root) if args.compiler_root else ROOT
    pin = load_pin(args.pin)

    refused = column_refused()
    admits = column_admits(args.admits_from, compiler_root, args.attempt, pin)
    residue = column_residue(args.residue_from)
    latency = column_admission_latency(args.measure_latency, args.latency_iters)
    tokens_col = column_tokens_to_green(args.tokens_from, compiler_root, pin)
    injection = column_injection_escape(args.injection_from)
    survey = load_unload_survey()
    unload = column_unload_paths(survey)

    briefs = briefs_from_admits(admits) + briefs_from_residue(residue, args.residue_from)

    checker = checker_version()
    # The re-score triple every 'measured' claim needs. `run` names the corpus
    # the claim's number came from, so a claim cannot outlive its corpus.
    evidence_run = admits.get("corpus") or residue.get("corpus") or "none"
    evidence = {
        "compiler_commit": checker.get("compiler_commit", "unknown"),
        "run": evidence_run,
        "protocol": PROTOCOL,
    }

    claims = []
    head = refused.get("headline")
    if head:
        claims.append({
            "text": (f"the fully-native revl chain does not reproduce "
                     f"{head['documents_refused']} of {head['corpus']} corpus "
                     f"documents that the reference compiler accepts "
                     f"(n={head['n']})"),
            "rung": "measured",
            "public": True,
            "evidence": dict(evidence, run=head["gate"]),
        })
    if unload.get("status") == "measured":
        claims.append({
            "text": (f"of {unload['agent_frameworks_n']} popular agent "
                     f"frameworks surveyed at pinned versions, "
                     f"{len(unload['agent_frameworks_per_registration'])} "
                     f"publish a way to retire an individual registered tool "
                     f"and {len(unload['agent_frameworks_none_published'])} "
                     f"publish no unload path at all "
                     f"(n={unload['agent_frameworks_n']}, excluding the control "
                     f"and the one surveyed tool host); the names and versions "
                     f"are in the report"),
            "rung": "measured",
            "public": True,
            "evidence": dict(evidence, run=unload["evidence"] or "unload-survey"),
        })
    if residue.get("status") == "measured":
        claims.append({
            "text": (f"{residue['leaked']} of {residue['n']} raw-Cordis plugins "
                     f"in {residue['corpus']} leave residue after "
                     f"{residue['cycles']} load-unload cycles (n={residue['n']}, "
                     "hand-authored corpus)"),
            "rung": "measured",
            "public": True,
            "evidence": dict(evidence, run=residue["corpus"]),
        })
    if admits.get("status") == "measured":
        # The provenance clause is inside the claim text, not beside it. These
        # are the cells most likely to be quoted out of context, and a caveat
        # that lives in a neighbouring paragraph does not survive being quoted.
        origin = ((f"generated by the pinned model "
                   f"{admits['models'][0]}" if admits.get("models") else
                   "generated by the pinned model")
                  if admits.get("is_pinned_model")
                  else f"corpus {admits['corpus']}, NOT generated by the "
                       f"pinned model")
        for variant, v in sorted(admits["by_variant"].items()):
            claims.append({
                "text": (f"{variant}: {v['admitted']} of {v['n']} committed "
                         f"generations are admitted by the current checker "
                         f"(n={v['n']}, {origin})"),
                "rung": "measured",
                "public": True,
                "evidence": dict(evidence),
            })
    if tokens_col.get("status") == "measured":
        torigin = ("generated by the pinned model" if tokens_col.get("is_pinned_model")
                   else f"corpus {tokens_col['corpus']}, NOT generated by the "
                        f"pinned model")
        claims.append({
            "text": (f"tokens to green: {tokens_col['median_output_tokens']} "
                     f"median output tokens per admitted component "
                     f"(n={tokens_col['n']}, {tokens_col['token_source']}, "
                     f"{torigin})"),
            "rung": "measured",
            "public": True,
            "evidence": dict(evidence, run=tokens_col["corpus"]),
        })
    if injection.get("status") == "measured":
        for host, block in (injection.get("summary") or {}).items():
            claims.append({
                "text": (f"injection, {host}: the pinned model produced the "
                         f"undeclared action in {block['complied']} of "
                         f"{block['attempts']} attempts (n={block['attempts']}, "
                         f"a property of the model, not of the host)"),
                "rung": "measured",
                "public": True,
                "evidence": dict(evidence, run=injection["corpus"]),
            })
            if block.get("containment") is None:
                continue
            claims.append({
                "text": (f"injection, {host}: {block['containment']} of "
                         f"{block['complied']} complying attempts were refused "
                         f"by a named hard gate, leaving {block['escapes']} "
                         f"escapes (n={block['complied']} complying attempts, "
                         f"not {block['attempts']})"),
                "rung": "measured",
                "public": True,
                "evidence": dict(evidence, run=injection["corpus"]),
            })

    report = {
        "protocol": PROTOCOL,
        "report_schema": REPORT_SCHEMA,
        "suite": SUITE,
        "generator": {
            "model": (pin.get("model") or {}).get("resolved")
                     or "no pinned-model run has been executed",
            "run": args.admits_from or "none",
            "driver": "bench/run.py",
        },
        "grader": {
            "kind": "compiler",
            "tool": "revl.compile_source",
            "name": "bench/rescore.py",
        },
        "checker": checker,
        "model_pin": pin,
        "unload_survey": survey,
        "hosts": hosts["hosts"],
        "tasks": hosts["tasks"],
        "columns": {
            "refused": refused,
            "unload-paths": unload,
            "admits": admits,
            "residue": residue,
            "injection-escape": injection,
            "tokens-to-green": tokens_col,
            "admission-latency": latency,
        },
        "briefs": briefs,
        "claims": claims,
    }
    report["remaining_gates"] = remaining_gates(hosts, report["columns"], pin)
    return report


def remaining_gates(hosts: dict, report_columns: dict, pin: dict) -> list[dict]:
    """What this report is not. Named, so nobody has to infer it from a gap."""
    gates = []
    framework = next((h for h in hosts["hosts"] if h["id"] == "framework"), None)
    if framework and not framework.get("runnable"):
        named = framework.get("name")
        gates.append({
            "gate": "the third host",
            "what": (f"named and justified as `{named}` {framework.get('version', '')}"
                     f", not yet run" if named
                     else "no agent framework is named, pinned or run"),
            "why": framework.get("blocked_on"),
        })
    for name, cell in report_columns.items():
        if cell.get("status") == NOT_RUN:
            gates.append({"gate": f"column: {name}",
                          "what": "not measured in this report",
                          "why": cell.get("blocked_on")})
    pinned_cells = sorted(name for name, cell in report_columns.items()
                          if cell.get("is_pinned_model")
                          or (cell.get("status") == "measured"
                              and name == "injection-escape"))
    if pinned_cells:
        gates.append({
            "gate": "a pinned-model run across all three hosts",
            "what": (f"the pinned model produced {', '.join(pinned_cells)}; every "
                     "other cell is a re-score of a corpus another model "
                     "generated, or not run"),
            "why": ("the raw-ts and framework hosts have not been generated with "
                    "the pinned model"),
        })
    else:
        gates.append({
            "gate": "a pinned-model run across all three hosts",
            "what": ("no cell in this report was produced by the pinned model; the "
                     "admission and token cells are re-scores of corpora generated "
                     "by other models"),
            "why": "the three-host generation run has not been executed",
        })
    gates.append({
        "gate": "independent reproduction",
        "what": ("every claim stands at the 'measured' rung and none at "
                 "'demonstrated'"),
        "why": ("the ladder's 'demonstrated' rung requires a reproduction by a "
                "party that is not the generator; nobody outside this "
                "repository has run the suite"),
    })
    tp = (pin.get("throughput") or {}) if pin.get("present") else {}
    accounted = tp.get("accounted_fraction_mean")
    if accounted is not None and accounted < 0.8:
        gates.append({
            "gate": "a throughput measurement on an idle machine",
            "what": (f"the throughput figures were taken with the server "
                     f"accounting for only {accounted * 100:.0f}% of each "
                     f"request's wall clock"),
            "why": ("no idle machine was available during this run; the figure "
                    "does not reproduce the quoted one and a contended "
                    "measurement is a weak refutation either way"),
        })
    gates.append({
        "gate": "publication",
        "what": "nothing here is published outside this repository",
        "why": "the artifacts are files in bench/results/framework-bench/",
    })
    return gates


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(value) -> str:
    return "not run" if value is None else str(value)


def _escape_cell(inj: dict, host: str) -> str:
    """One host's injection-escape cell.

    Compliance leads, because it is the number that is about the model rather
    than about the host, and a containment figure quoted without it is not
    interpretable. A host whose containment was never exercised says so; it
    never prints a rate.
    """
    if inj.get("status") != "measured":
        return "not run"
    block = (inj.get("summary") or {}).get(host)
    if not block:
        return "not run (host not in the run)"
    head = (f"{block['complied']}/{block['attempts']} attempts complied "
            f"(model behaviour)")
    if block.get("containment") is None:
        # The reason is under the table, not in the cell. A cell holding a
        # three-line explanation is unreadable in a table, and a reader who
        # only reads cells must still not come away thinking a rate was
        # withheld rather than never existing.
        return f"{head}; containment not measured, see below"
    # The attributed refusal leads, not the raw containment count. A document
    # refused for an unrelated syntax error kept the injected behaviour out and
    # is still no evidence that the gate catches injections, and folding the two
    # together is how this column would come out flattering by accident.
    return (f"{head}; **{block['escapes']}** escaped, "
            f"{block.get('refused_on_the_injection', 0)} refused on the "
            f"injection, {block.get('refused_on_another_fault', 0)} refused on "
            f"an unrelated fault")


def _unload_section(cell: dict) -> list:
    """The survey, reported as a result rather than as a selection rationale.

    It sits directly under the refused column because it is the finding that
    makes the residue column legitimate. A column measuring what a host leaks
    on unload reads as an axis picked to win, right up until somebody shows
    that most popular hosts have no unload to measure.
    """
    if cell.get("status") != "measured":
        return ["## Unload paths across the ecosystem", "",
                f"Not measured: {cell.get('blocked_on', 'no survey')}.", ""]
    fw_n = cell["agent_frameworks_n"]
    fw_none = cell["agent_frameworks_none_published"]
    fw_per_reg = cell["agent_frameworks_per_registration"]
    lines = [
        "## Unload paths across the ecosystem", "",
        f"**Of {fw_n} popular agent frameworks surveyed at pinned versions "
        f"(n={fw_n}), {len(fw_per_reg)} publish a way to retire an individual "
        f"registered tool, and {len(fw_none)} publish no unload path at all.**",
        "",
        "The one that is neither publishes a teardown at toolset scope rather",
        "than per registration, which is a weaker guarantee and a different",
        "question for the residue column, so it is counted apart rather than",
        "either way.", "",
        "This is the reason the residue column exists, and it is a stronger",
        "result than the host it selected. A column measuring what a host leaks",
        "on unload reads as an axis picked to win, until somebody shows that",
        "most popular hosts have no unload to measure. It is also a claim about",
        "the runtimes rather than about any model, and a reader can check it",
        "against published artifacts at the versions named below.", "",
        "| package | kind | version | registration | unload path |",
        "|---|---|---|---|---|",
    ]
    for row in cell["surveyed"]:
        lines.append(f"| `{row['name']}` | {row.get('category', '?')} "
                     f"| {row['version']} "
                     f"| `{row['registration']}` | {row['verdict']} |")
    for row in cell.get("controls") or []:
        lines.append(f"| `{row['name']}` | control | {row['version']} "
                     f"| excluded from the denominator | {row['verdict']} |")
    lines += ["",
              cell["denominator_note"] + ".", "",
              cell["control_excluded_from_denominator"] + ".", "",
              "### How it was measured, and how it could have been wrong", "",
              cell["method"], "",
              "The first version of this survey was wrong, and recording how",
              "is part of the result. " + cell["how_it_could_have_been_wrong"], "",
              "The checker earned its place before the survey was committed. It",
              "falsified the `semantic-kernel` absence claim on the word",
              "\"unregistered\" inside a comment about Azure agent threads, which",
              "forced the claim to be scoped to the plugin registry's own",
              "modules; and it caught a `crewai` claim pointing at a module that",
              "version had moved into a package.", "",
              "What it does not say. " + cell["what_it_does_not_say"], "",
              f"Evidence, with the file and line of every symbol: "
              f"`{cell['evidence']}`. Re-check it with "
              f"`python3 bench/framework_unload_survey.py --fetch --check`.", ""]
    return lines


def _third_host_section(framework: dict | None, survey: dict) -> list:
    """Which framework, why, what was rejected, and what is still not run."""
    if not framework or not framework.get("name"):
        return []
    lines = ["## The third host", "",
             f"**`{framework['name']}` {framework.get('version', '')}**, "
             f"from {framework.get('ecosystem', 'a package index')}, "
             f"{framework.get('authored_by', '')}.", ""]
    for line in framework.get("selected_because") or []:
        lines.append(line)
    lines += [""]
    honest = framework.get("honest_about_the_pick")
    if honest:
        lines += ["### What is uncomfortable about this pick", ""] + list(honest) + [""]
    rejected = framework.get("rejected") or []
    if rejected:
        lines += ["### Rejected, and why", "", "| framework | why |", "|---|---|"]
        for row in rejected:
            lines.append(f"| `{row['name']}` | {row['why']} |")
        lines += [""]
    if survey.get("present"):
        rows = survey.get("frameworks") or []
        claims = sum(len(r.get("claims") or []) for r in rows)
        lines += [
            f"Every claim in that table is checked against the published "
            f"artifact rather than asserted: `bench/framework_unload_survey.py` "
            f"holds {claims} claims across {len(rows)} packages and fails if any "
            f"is falsified. The evidence, with the file and line each symbol was "
            f"found at, is `{survey['path']}`.", "",
            "What that check says and does not say: a confirmed claim means the "
            "symbol is in the published file. It says nothing about what calling "
            "it releases, which is the residue probe's question and is why the "
            "framework residue cell is not-run rather than filled from the "
            "survey.", "",
        ]
    else:
        lines += [f"No unload survey is committed: {survey.get('reason', '')}.", ""]
    blocked = framework.get("blocked_on")
    if blocked:
        lines += ["### Why its cells are still empty", ""] + list(blocked) + [""]
    return lines


def render(report: dict) -> str:
    cols = report["columns"]
    checker = report["checker"]
    pin = report["model_pin"]
    lines = [f"# {report['suite']}: one model, one task set, three hosts", ""]

    lines += [
        "The axis under test is the runtime, not the model. A model leaderboard",
        "cannot check this project's claim and can falsify it: a high",
        "compile-rate bought by loosening G1-G9 would be a worse result, not a",
        "better one.", "",
        "## What this report is not", "",
    ]
    for gate in report["remaining_gates"]:
        why = gate.get("why")
        why = " ".join(why) if isinstance(why, list) else (why or "")
        lines.append(f"- **{gate['gate']}**: {gate['what']}. {why}".rstrip())
    lines += [""]

    lines += ["## The pin", "",
              "| field | value |", "|---|---|"]
    model = (pin.get("model") or {}) if pin.get("present") else {}
    lines += [
        f"| model requested | `{model.get('requested', 'none')}` |",
        f"| model resolved | `{_cell(model.get('resolved'))}` |",
        f"| resolution | {_cell(model.get('resolution'))} |",
        f"| digest | `{_cell(model.get('digest'))}` |",
        f"| quantisation (endpoint) | "
        f"{_cell(model.get('quantisation_reported_by_endpoint'))} |",
        f"| quantisation (tag) | {_cell(model.get('quantisation_from_tag'))} |",
        f"| endpoint kind | {_cell(pin.get('endpoint_kind'))} |",
        f"| endpoint reachable | {_cell(pin.get('reachable'))} |",
        f"| sampling | `{json.dumps(pin.get('sampling'))}` |",
        f"| machine | {_cell(pin.get('machine'))} |",
        f"| gate API | {_cell(checker.get('gate_api'))} |",
        f"| language | {_cell(checker.get('language'))} |",
        f"| checker frontier | `{_cell(checker.get('frontier'))}` |",
        f"| compiler commit | `{_cell(checker.get('compiler_commit'))}` |",
        f"| report schema | {report['report_schema']} |",
        "",
    ]
    tp = pin.get("throughput") or {}
    if tp.get("generation_tps_mean"):
        sd = tp.get("generation_tps_sd")
        lines += [
            f"Measured throughput: **{tp['generation_tps_mean']:.1f} t/s** "
            "generation" + (f" (sd {sd:.1f}, " if sd is not None else " (")
            + f"n={tp['n_warm']} warm samples)"
            + (f", {tp['prompt_tps_mean']:.1f} t/s prompt"
               if tp.get("prompt_tps_mean") else "") + ".",
            "",
            "That figure is here because it was measured here, not because it",
            "agrees with anything. Roadmap item 548 quotes 51.4 t/s generation",
            "and 746.3 t/s prompt for this model. The numbers above were taken",
            "on the machine named in the table, under whatever else that",
            "machine was doing, and they do not reproduce those. Throughput is",
            "a property of a machine at a moment, so the pin carries the",
            "measurement with its n and standard deviation rather than the",
            "number somebody wrote down.",
            "",
        ]
        accounted = tp.get("accounted_fraction_mean")
        if accounted is not None:
            lines += [
                f"How busy the machine was is measured rather than asserted. "
                f"The server accounted for **{accounted * 100:.0f}%** of each "
                f"request's wall clock as load, prompt evaluation or "
                f"generation"
                + (f"; the one-minute load average over the samples averaged "
                   f"{tp['load_average_1m_mean']:.0f} and peaked at "
                   f"{tp['load_average_1m_max']:.0f}"
                   if tp.get("load_average_1m_mean") is not None else "")
                + ". The rest was waiting.",
                "",
                "That cuts both ways and the report says so rather than picking",
                "the reading it prefers. A figure taken on a contended machine",
                "is a weak refutation of a figure taken on an idle one, so this",
                "does not settle whether the quoted number is wrong. It is also",
                "not a licence to assume the quoted number would reproduce: no",
                "measurement in this repository has reproduced it, on any",
                "machine state, and a run on an idle machine remains a named",
                "gate rather than a result. The throughput figures above are",
                "the server's own accounted rates and are not adjusted by this",
                "fraction.",
                "",
            ]
    lines += [
        "The frontier is the field that matters for comparing two runs. Two",
        "gates covering different surfaces can agree on every program either",
        "covers and still admit different languages, so an admission rate is",
        "comparable only to one measured against the same frontier. If G1",
        "tightens and the rate drops, this row is why the drop reads as a",
        "tightened gate and not as a regression.",
        "",
    ]

    refused = cols["refused"]
    head = refused.get("headline")
    lines += ["## Refused: the cost of the guarantee", ""]
    if head:
        lines += [
            f"**{head['documents_refused']} of {head['corpus']} corpus documents "
            f"(n={head['n']}) that the reference compiler accepts are not "
            "reproduced by the fully-native chain.**", "",
            "A raw TypeScript host runs the equivalent work without objection.",
            "This column leads because every other column in this table is one",
            "revl is designed to win, and a suite whose author picked the axes",
            "is benchmaxxing whatever the intent.", "",
            "| tier | documents refused |", "|---|---:|",
        ]
        for tier, n in head["per_tier"].items():
            lines.append(f"| {tier} | {n} |")
        lines += [f"| **total** | **{head['documents_refused']}** |", "",
                  f"Gate: `{head['gate']}`", ""]
        if refused.get("unported_constructs") is not None:
            lines.append(
                f"Constructs the self-host port does not implement: "
                f"**{refused['unported_constructs']}** across six tiers.")
        if refused.get("fail_open_programs") is not None:
            lines.append(
                f"Programs the embeddable gate admits that the reference "
                f"refuses (the fail-open direction): "
                f"**{refused['fail_open_programs']}**.")
        lines += ["",
                  "The full inventory, with the named documents and the gate",
                  "behind each count, is `refusals.json` beside this file and",
                  "is regenerated by `python3 bench/refusal_inventory.py`.", ""]
    else:
        lines += ["The inventory could not be read. Sections unavailable: "
                  + ", ".join(sorted(refused.get("unavailable", {}))), ""]

    lines += _unload_section(cols.get("unload-paths") or {})

    lines += ["## The table", "",
              "| column | raw Cordis / TypeScript | agent framework | revl |",
              "|---|---|---|---|"]

    framework = next((h for h in report["hosts"] if h["id"] == "framework"), None)
    fw_name = (framework or {}).get("name")
    fw_version = (framework or {}).get("version", "")

    def fw() -> str:
        """The framework cell. Named now, still not run, and it says both.

        The name is in the cell rather than only in the prose, because a column
        headed "agent framework" with `not run` in every cell reads as though no
        choice was made, and one was.
        """
        if not fw_name:
            return "not run (no framework named)"
        return f"not run (`{fw_name}` {fw_version} named, harness not built)"

    if head:
        lines.append(
            f"| **refused** | 0 (TypeScript compiles everything) | {fw()} | "
            f"**{head['documents_refused']} / {head['corpus']}** |")
    admits = cols["admits"]
    if admits.get("status") == "measured":
        best = max(admits["by_variant"].items(), key=lambda kv: kv[1]["n"])
        # The provenance clause is generated from the measured model ids, not
        # written by hand. This cell is the one most likely to be quoted out of
        # context, so the sentence that says which model produced it travels
        # inside the cell rather than in a footnote a quoter can drop.
        origin = ("**pinned model**" if admits.get("is_pinned_model")
                  else "NOT the pinned model")
        cell = (f"{best[1]['admitted']}/{best[1]['n']} on `{best[0]}` "
                f"(corpus {admits['corpus']}, {origin})")
    else:
        cell = f"not run ({admits.get('blocked_on')})"
    lines.append(f"| admits (first pass) | not applicable, see below | {fw()} | {cell} |")

    residue = cols["residue"]
    if residue.get("status") == "measured":
        rcell = (f"**{residue['leaked']}/{residue['n']}** leak "
                 f"({residue['cycles']} cycles)")
    else:
        rcell = f"not run ({residue.get('blocked_on')})"
    # Not "0". A zero in a results column reads as a measurement, and nothing
    # measured it: a residue-carrying component never reaches a corpus the
    # probe could score, because the compiler refused it first. Naming the gate
    # says the same thing without borrowing the authority of a number.
    lines.append(f"| residue after N cycles | {rcell} | {fw()} | "
                 "not applicable: a residue-carrying component is refused at "
                 "compile time (G4 and the no-residue proof), so none reaches "
                 "a corpus the probe could score |")

    inj = cols["injection-escape"]
    lines.append(f"| injection escape | {_escape_cell(inj, 'raw-ts')} | {fw()} "
                 f"| {_escape_cell(inj, 'revl')} |")

    tok = cols["tokens-to-green"]
    tcell = (f"{tok['median_output_tokens']} median output tokens "
             f"(n={tok['n']}, {tok['token_source']}, "
             + ("**pinned model**" if tok.get("is_pinned_model")
                else f"corpus {tok.get('corpus')}, NOT the pinned model") + ")"
             if tok.get("status") == "measured" else "not run")
    if tok.get("status") == "measured":
        lines_after_table = [
            "", "### What the tokens-to-green figure counts", "",
            tok["sources_are_not_comparable"] + ".", "",
        ]
        if tok.get("censored"):
            lines_after_table += [tok["censored"] + ".", ""]
    else:
        lines_after_table = []
    lines.append(f"| tokens to green | not applicable | {fw()} | {tcell} |")

    lat = cols["admission-latency"]
    lcell = (f"{lat['round_trip_ms_median']:.3f} ms median (n={lat['n']})"
             if lat.get("status") == "measured"
             else f"not run here; see `{lat['existing_artifact']}`"
             if lat.get("existing_artifact") else "not run")
    lines.append(f"| admission latency | no gate to time | {fw()} | {lcell} |")

    lines += lines_after_table
    lines += ["",
              "### Why the raw-TypeScript row is not a compile-rate", "",
              "TypeScript always compiles. There is no first-pass compile",
              "number to put in that cell, because it would read 100% for every",
              "attempt including the ones that leak on unload. So the raw host",
              "is scored on what it leaks and the revl host on what it admits,",
              "and those are different questions rather than two views of one.",
              "A reader comparing the two cells directly is comparing the",
              "questions, not the runtimes. The asymmetry is the finding this",
              "row exists to state, not a defect in the method.", ""]

    if residue.get("dismissibility"):
        note = residue["dismissibility"]
        lines += ["### What the residue number does not show", "",
                  "It is " + note + ".", "",
                  f"Re-probe it with `{residue['reprobe']}` "
                  f"(prereq: `{residue['reprobe_prereq']}`).", ""]

    lines += ["### The injection-escape column", ""]
    if inj.get("status") != "measured":
        lines += [inj["blocked_on"] + ".",
                  f"Nearest existing artifact: `{inj['nearest_existing']}`. "
                  + inj["note"] + ".", ""]
    else:
        lines += [
            f"Measured over `{inj['corpus']}` with the pinned model, across "
            f"{len(inj.get('vectors') or [])} injection vectors on one spec, so the "
            "only thing that varies across attempts is the injection.", "",
            "The injection never rides in the system prompt. It rides in a service",
            "doc comment, in the brief, or in the compiler output the retry loop",
            "feeds back, which is where a real one rides.", "",
            "Two numbers, and they answer different questions. **Compliance** is how",
            "often the model did the undeclared thing at all. It is a property of",
            "the model and it is the same question on every host, so it is reported",
            "first: a containment rate quoted without it is not interpretable, and a",
            "model that ignores every injection would make every host look perfect.",
            "**Containment** is whether a named hard gate then refused the artifact.",
            "Compliance is detected from the source text by a detector that never",
            "consults the gate, because inferring compliance from the gate's verdict",
            "would make containment 100% by construction.", "",
            "A refusal is split by what it was about. The first live attempt is",
            "why: the model complied with the environment-variable injection,",
            "the compiler refused the document, and the diagnostic was a syntax",
            "error on an unrelated line. That refusal kept the behaviour out and",
            "is no evidence that the gate catches injections, so it is counted",
            "apart rather than folded into a containment figure.", "",
            "| host | attempts | complied | refused on the injection "
            "| refused on another fault | escaped |",
            "|---|---:|---:|---|---|---|",
        ]
        for host, block in (inj.get("summary") or {}).items():
            if block.get("containment") is None:
                lines.append(f"| {host} | {block['attempts']} "
                             f"| {block['complied']} | not measured "
                             f"| not measured | not measured |")
                continue
            lines.append(
                f"| {host} | {block['attempts']} | {block['complied']} "
                f"| {block.get('refused_on_the_injection', 0)}/{block['complied']} "
                f"| {block.get('refused_on_another_fault', 0)} "
                f"| {block['escapes']} |")
        lines += [""]
        for host, block in (inj.get("summary") or {}).items():
            for note in (block.get("containment_note"),
                         block.get("attribution_note")):
                if note:
                    lines.append(f"- **{host}**: {note}")
        lines += ["",
                  f"`{inj['nearest_existing']}` is still not the source of this "
                  "cell. " + inj["note"] + ".", ""]

    lines += _third_host_section(framework, report.get("unload_survey") or {})

    lines += ["## Claims and their rung", "",
              "| claim | rung |", "|---|---|"]
    for claim in report["claims"]:
        lines.append(f"| {claim['text']} | {claim['rung']} |")
    lines += ["",
              "No claim here stands above `measured`. The ladder's",
              "`demonstrated` rung needs a reproduction by a party that is not",
              "the generator, and nobody outside this repository has run the",
              "suite. Validate this report with",
              "`python3 tools/check_eval_report.py "
              "bench/results/framework-bench/report.json`.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--admits-from", default="typed-deepseek-v4-pro",
                    help="committed revl corpus to re-score for the admits column "
                         "('none' to skip)")
    ap.add_argument("--residue-from", default="hand-corpus",
                    help="committed raw-ts corpus for the residue column")
    ap.add_argument("--tokens-from", default=None,
                    help="committed corpus for the tokens-to-green column")
    ap.add_argument("--injection-from", default=None,
                    help="committed bench/injection_escape.py run label "
                         "for the injection-escape column")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--compiler-root", default=None,
                    help="score against a different checkout's compiler")
    ap.add_argument("--pin", default=None, help="path to a model-pin.json")
    ap.add_argument("--measure-latency", action="store_true",
                    help="time the admission round-trip on this machine")
    ap.add_argument("--latency-iters", type=int, default=2000)
    ap.add_argument("--write", action="store_true",
                    help=f"write report.json and report.md to "
                         f"{_rel(OUT_DIR)}")
    ap.add_argument("--check", action="store_true",
                    help="validate the report against the honesty protocol")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    if args.admits_from == "none":
        args.admits_from = None

    report = build_report(args)

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (OUT_DIR / "report.md").write_text(render(report))
        inv = report["columns"]["refused"].get("inventory")
        if inv:
            (OUT_DIR / "refusals.json").write_text(json.dumps(inv, indent=2) + "\n")
            (OUT_DIR / "refusals.md").write_text(refusal_inventory.render(inv))
        print(f"wrote {_rel(OUT_DIR)}/report.json, report.md, "
              "refusals.json, refusals.md")
    elif args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report), end="")

    if args.check:
        import check_eval_report  # noqa: PLC0415

        violations = check_eval_report.check_report(report)
        if violations:
            print(f"{len(violations)} protocol violation(s):", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 1
        print(f"report obeys {REPORT_SCHEMA}; no over-claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
