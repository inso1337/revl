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


def column_admits(run: str | None, compiler_root: Path, attempt: int) -> dict:
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
    return {
        "status": "measured",
        "corpus": f"bench/results/{run}",
        "attempt": attempt,
        "generated_by": run,
        "is_pinned_model": False,
        "note": ("a re-score of a committed corpus against the current checker; "
                 "the generating model is the corpus label, not the pinned model"),
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


def column_tokens_to_green(run: str | None, compiler_root: Path) -> dict:
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
    values, source = ((recorded, "recorded by the provider") if recorded
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
        "admitted_cells": len(admitted),
        "total_cells": len(cells),
        "is_pinned_model": False,
        "means": "output tokens spent per admitted component",
    }


def column_injection_escape() -> dict:
    """Not implemented, and said so rather than filled with a related number.

    `docs/prompt-injection-resistance.md` states the claim this column would
    measure, and carries no runnable check: the repository's own doc inventory
    grades it `needs-work`, and nothing in `tools/` or `tests/` references it by
    name. `tests/test_adversarial_gate.py` is an executable attack suite, but it
    belongs to `docs/threat-model.md` and is not an injection-escape rate over
    this task set. Reporting it in this cell would be answering a different
    question in the column's name.
    """
    return {
        "status": NOT_RUN,
        "blocked_on": (
            "no runnable injection-escape measure exists over this task set; "
            "docs/prompt-injection-resistance.md states the claim and has no "
            "check behind it"),
        "nearest_existing": "tests/test_adversarial_gate.py (docs/threat-model.md)",
        "note": ("not filled with the nearest available number: an attack suite "
                 "pass-rate is not an escape rate over these 30 briefs"),
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

    refused = column_refused()
    admits = column_admits(args.admits_from, compiler_root, args.attempt)
    residue = column_residue(args.residue_from)
    latency = column_admission_latency(args.measure_latency, args.latency_iters)
    tokens_col = column_tokens_to_green(args.tokens_from, compiler_root)
    injection = column_injection_escape()

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
        for variant, v in sorted(admits["by_variant"].items()):
            claims.append({
                "text": (f"{variant}: {v['admitted']} of {v['n']} committed "
                         f"generations are admitted by the current checker "
                         f"(n={v['n']}, corpus {admits['corpus']}, not the "
                         "pinned model)"),
                "rung": "measured",
                "public": True,
                "evidence": dict(evidence),
            })

    report = {
        "protocol": PROTOCOL,
        "report_schema": REPORT_SCHEMA,
        "suite": SUITE,
        "generator": {
            "model": (load_pin(args.pin).get("model") or {}).get("resolved")
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
        "model_pin": load_pin(args.pin),
        "hosts": hosts["hosts"],
        "tasks": hosts["tasks"],
        "columns": {
            "refused": refused,
            "admits": admits,
            "residue": residue,
            "injection-escape": injection,
            "tokens-to-green": tokens_col,
            "admission-latency": latency,
        },
        "briefs": briefs,
        "claims": claims,
    }
    report["remaining_gates"] = remaining_gates(hosts, report["columns"])
    return report


def remaining_gates(hosts: dict, report_columns: dict) -> list[dict]:
    """What this report is not. Named, so nobody has to infer it from a gap."""
    gates = []
    framework = next((h for h in hosts["hosts"] if h["id"] == "framework"), None)
    if framework and not framework.get("runnable"):
        gates.append({
            "gate": "the third host",
            "what": "no agent framework is named, pinned or run",
            "why": framework.get("blocked_on"),
        })
    for name, cell in report_columns.items():
        if cell.get("status") == NOT_RUN:
            gates.append({"gate": f"column: {name}",
                          "what": "not measured in this report",
                          "why": cell.get("blocked_on")})
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

    lines += ["## The table", "",
              "| column | raw Cordis / TypeScript | agent framework | revl |",
              "|---|---|---|---|"]

    def fw() -> str:
        return "not run (no framework named)"

    if head:
        lines.append(
            f"| **refused** | 0 (TypeScript compiles everything) | {fw()} | "
            f"**{head['documents_refused']} / {head['corpus']}** |")
    admits = cols["admits"]
    if admits.get("status") == "measured":
        best = max(admits["by_variant"].items(), key=lambda kv: kv[1]["n"])
        cell = (f"{best[1]['admitted']}/{best[1]['n']} on `{best[0]}` "
                f"(corpus {admits['corpus']}, not the pinned model)")
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
    lines.append(f"| injection escape | not run | {fw()} | not run |")

    tok = cols["tokens-to-green"]
    tcell = (f"{tok['median_output_tokens']} median output tokens "
             f"(n={tok['n']}, {tok['token_source']})"
             if tok.get("status") == "measured" else "not run")
    lines.append(f"| tokens to green | not applicable | {fw()} | {tcell} |")

    lat = cols["admission-latency"]
    lcell = (f"{lat['round_trip_ms_median']:.3f} ms median (n={lat['n']})"
             if lat.get("status") == "measured"
             else f"not run here; see `{lat['existing_artifact']}`"
             if lat.get("existing_artifact") else "not run")
    lines.append(f"| admission latency | no gate to time | {fw()} | {lcell} |")

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

    lines += ["### The injection-escape column", "",
              inj["blocked_on"] + ".",
              f"Nearest existing artifact: `{inj['nearest_existing']}`. "
              + inj["note"] + ".", ""]

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
