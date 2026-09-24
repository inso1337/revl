#!/usr/bin/env python3
"""The refused column: what revl will not run that the other hosts run fine.

Roadmap item 548 (issue #1267) makes one demand of this benchmark above all the
others: the refusal column leads. The reasoning is not modesty. Every other
column in the table is one revl is *designed* to win, so a suite whose author
picked the axes is benchmaxxing whatever the author intended, and adding a token
loss does not repair it. What repairs it is publishing the cost of the guarantee
as a headline: the documents and constructs this project refuses, counted, with
the tier they are refused on, next to the fact that a raw TypeScript host or an
agent framework runs the same work without complaint.

So this module does not accept a number written in prose. Every count it
publishes is recomputed from a committed artifact at the moment the report is
built. That is the whole design:

  * a residual figure quoted in a roadmap item, a design note or a doc is a
    snapshot of the day somebody typed it, and this repository currently holds
    at least three different totals for the same quantity in three different
    files;
  * a figure recomputed from the ledger the tests gate cannot disagree with the
    tests, because it is reading what the tests read.

If a source is absent or has changed shape, the source is reported as
unavailable and the report says so. Nothing here falls back to a literal.

## What is counted, and what each count means

`native-chain-residual` counts documents the fully-native chain (`selfhost/lower.rvl`
plus the native emitter) does not reproduce, per host tier, against the corpus
size for that tier. These are documents the *reference* compiler accepts: the
cost is paid by anyone who wants the Python-free path, and it is the sharpest
honest number in the table. Source: the `LOWER_GAP_DOCS` ledger in
`tests/test_selfhost_compile.py`, which
`test_the_residual_is_located_in_lower_not_in_the_emitter` recomputes and pins
in both directions.

`unported-constructs` counts constructs the reference emitter implements and the
self-host port does not, per tier, grouped by the reason recorded against them.
Source: `tests/fixtures/selfhost_blind_spots.json`, gated by
`tools/selfhost_coverage.py --check`.

`unreached-constructs` counts dispatch arms an oracle's reference implementation can
take that no document in that oracle's corpus exhibits. Not a refusal: an
unexercised path, which is a different and weaker claim, and is labelled as
such. Source: `tests/fixtures/oracle_construct_reach_ledger.json`, gated by
`tools/oracle_construct_reach.py --check`.

`gate-reference-divergence` is the fail-open direction, and the reason this file
is not a list of wins. `false-admit` buckets are programs the embeddable gate
lets through that the reference compiler refuses. A benchmark that published
only the refusals and hid the places the gate is *too permissive* would be
making the same selective argument it claims to be correcting. Source:
`tools/gate_reference_census_baseline.json`, gated by
`tools/gate_reference_census.py --check`.

Usage:
  python3 bench/refusal_inventory.py                  # the table
  python3 bench/refusal_inventory.py --json out.json  # machine-readable
  python3 bench/refusal_inventory.py --write          # commit the artifact
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
DEFAULT_OUT = BENCH / "results" / "framework-bench" / "refusals.json"

SELFHOST_COMPILE_TEST = ROOT / "tests" / "test_selfhost_compile.py"
BLIND_SPOTS = ROOT / "tests" / "fixtures" / "selfhost_blind_spots.json"
REACH_LEDGER = ROOT / "tests" / "fixtures" / "oracle_construct_reach_ledger.json"
CENSUS_BASELINE = ROOT / "tools" / "gate_reference_census_baseline.json"

# The gate each source is held by. A count is only worth publishing if
# something fails when it drifts, so the report carries the command next to the
# number and a reader can run it.
GATES = {
    "native-chain-residual":
        "pytest tests/test_selfhost_compile.py::"
        "test_the_residual_is_located_in_lower_not_in_the_emitter",
    "unported-constructs": "python3 tools/selfhost_coverage.py --check",
    "unreached-constructs": "python3 tools/oracle_construct_reach.py --check",
    "gate-reference-divergence": "python3 tools/gate_reference_census.py --check",
}


def _rel(path: Path) -> str:
    """A repository-relative path when the path is in the repository, and the
    path itself otherwise. A source can be pointed elsewhere (a test does
    exactly that), and a path formatter is not a place to raise from."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


class SourceUnavailable(RuntimeError):
    """A source artifact is missing or has changed shape.

    Raised rather than defaulted. A zero that means "I could not read the
    ledger" is indistinguishable in a published table from a zero that means
    "there are no refusals left", and the second is a much stronger claim.
    """


def _module_assignment(path: Path, name: str):
    """The value of a module-level literal assignment, read without executing.

    `tests/test_selfhost_compile.py` imports pytest and the whole compiler, so
    importing it to read one dictionary would make this tool depend on the test
    environment. `ast.literal_eval` over the parsed module gets the same value
    and cannot run anything.
    """
    if not path.is_file():
        raise SourceUnavailable(f"{_rel(path)} is not present")
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as exc:
        raise SourceUnavailable(f"{_rel(path)} does not parse: {exc}") from exc
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if node.value is None:
            continue
        try:
            return ast.literal_eval(node.value)
        except ValueError as exc:
            raise SourceUnavailable(
                f"{_rel(path)}:{name} is not a literal: {exc}") from exc
    raise SourceUnavailable(f"{_rel(path)} has no module-level {name}")


def native_chain_residual() -> dict:
    """Per-tier documents the fully-native chain cannot reproduce.

    The corpus size each residual is measured against lives in that tier's own
    emit test, so it is read from there rather than assumed: a residual of 9 on
    a corpus of 59 and a residual of 9 on a corpus of 12 are different results.
    """
    gaps = _module_assignment(SELFHOST_COMPILE_TEST, "LOWER_GAP_DOCS")
    if not isinstance(gaps, dict) or not gaps:
        raise SourceUnavailable("LOWER_GAP_DOCS is empty or not a mapping")

    tiers: dict[str, dict] = {}
    for tier in sorted(gaps):
        docs = sorted(gaps[tier])
        corpus_path = ROOT / "tests" / f"test_selfhost_emit_{tier}.py"
        try:
            corpus = _module_assignment(corpus_path, "CORPUS")
            corpus_n = len(corpus)
        except SourceUnavailable:
            corpus_n = None
        tiers[tier] = {
            "residual": len(docs),
            "corpus": corpus_n,
            "reproduced": (corpus_n - len(docs)) if corpus_n is not None else None,
            "documents": docs,
        }
    total_residual = sum(t["residual"] for t in tiers.values())
    corpora = [t["corpus"] for t in tiers.values()]
    total_corpus = sum(c for c in corpora if c is not None) if all(
        c is not None for c in corpora) else None
    return {
        "source": _rel(SELFHOST_COMPILE_TEST) + ":LOWER_GAP_DOCS",
        "gate": GATES["native-chain-residual"],
        "means": ("documents the reference compiler accepts and the fully-native "
                  "chain does not reproduce"),
        "tiers": tiers,
        "total_residual": total_residual,
        "total_corpus": total_corpus,
        "clean_tiers": sorted(t for t, v in tiers.items() if v["residual"] == 0),
    }


def unported_constructs() -> dict:
    """Constructs the reference emitter implements and the port does not."""
    if not BLIND_SPOTS.is_file():
        raise SourceUnavailable(f"{_rel(BLIND_SPOTS)} is not present")
    data = json.loads(BLIND_SPOTS.read_text())
    tiers: dict[str, dict] = {}
    for tier, entry in sorted(data.items()):
        if not isinstance(entry, dict):
            continue
        unported = entry.get("unported")
        blind = entry.get("blind")
        if not isinstance(unported, dict):
            continue
        tiers[tier] = {
            "unported_constructs": sum(len(v) for v in unported.values()),
            "unported_reasons": len(unported),
            "blind_constructs": (sum(len(v) for v in blind.values())
                                 if isinstance(blind, dict) else None),
            "reasons": {k: sorted(v) for k, v in sorted(unported.items())},
        }
    if not tiers:
        raise SourceUnavailable(
            f"{_rel(BLIND_SPOTS)} holds no per-tier 'unported' map")
    return {
        "source": _rel(BLIND_SPOTS),
        "gate": GATES["unported-constructs"],
        "means": ("constructs the reference emitter implements and the "
                  "self-host port does not"),
        "tiers": tiers,
        "total_unported": sum(t["unported_constructs"] for t in tiers.values()),
    }


def unreached_constructs() -> dict:
    """Dispatch arms no corpus document exercises. Weaker than a refusal."""
    if not REACH_LEDGER.is_file():
        raise SourceUnavailable(f"{_rel(REACH_LEDGER)} is not present")
    data = json.loads(REACH_LEDGER.read_text())
    if not isinstance(data, dict):
        raise SourceUnavailable(
            f"{_rel(REACH_LEDGER)} is not a JSON object")
    # The ledger is a flat oracle -> list-of-arms map. Keys starting with an
    # underscore are the file's own prose and are not oracles.
    per = {name: len(arms) for name, arms in sorted(data.items())
           if not name.startswith("_") and isinstance(arms, list)}
    if not per:
        raise SourceUnavailable(
            f"{_rel(REACH_LEDGER)} holds no per-oracle counts")
    return {
        "source": _rel(REACH_LEDGER),
        "gate": GATES["unreached-constructs"],
        "means": ("dispatch arms an oracle's reference implementation can take "
                  "that no corpus document exhibits; an unexercised path, not a "
                  "refusal"),
        "oracles": per,
        "total_unreached": sum(per.values()),
    }


def gate_reference_divergence() -> dict:
    """The fail-open direction: where the gate is more permissive than the
    reference. Published because a table of refusals alone would be the same
    selective argument this suite exists to correct."""
    if not CENSUS_BASELINE.is_file():
        raise SourceUnavailable(f"{_rel(CENSUS_BASELINE)} is not present")
    data = json.loads(CENSUS_BASELINE.read_text())
    buckets = data.get("buckets")
    if not isinstance(buckets, dict):
        raise SourceUnavailable(
            f"{_rel(CENSUS_BASELINE)} holds no 'buckets' map")
    # A bucket maps to the list of programs in it, so the count and the names
    # come from the same place and cannot disagree.
    false_admit = {k: sorted(v) for k, v in sorted(buckets.items())
                   if k.startswith("false-admit") and isinstance(v, list)}
    named = sorted({p for programs in false_admit.values() for p in programs})
    return {
        "source": _rel(CENSUS_BASELINE),
        "gate": GATES["gate-reference-divergence"],
        "means": ("programs the embeddable gate admits that the reference "
                  "compiler refuses; the fail-open direction"),
        "false_admit_buckets": {k: len(v) for k, v in false_admit.items()},
        "false_admit_programs": false_admit,
        "total_false_admit": len(named),
        "named_programs": named,
    }


SECTIONS = {
    "native-chain-residual": native_chain_residual,
    "unported-constructs": unported_constructs,
    "unreached-constructs": unreached_constructs,
    "gate-reference-divergence": gate_reference_divergence,
}


def build() -> dict:
    """Every section, with unavailable sources recorded rather than dropped."""
    out: dict = {"sections": {}, "unavailable": {}}
    for name, fn in SECTIONS.items():
        try:
            out["sections"][name] = fn()
        except SourceUnavailable as exc:
            out["unavailable"][name] = str(exc)
        except (json.JSONDecodeError, OSError) as exc:
            out["unavailable"][name] = f"{name}: {exc}"
    return out


def render(inv: dict) -> str:
    lines = ["# The refused column: the cost of the guarantee", ""]
    lines += [
        "Every count below is recomputed from a committed ledger at the moment",
        "this file was generated. None of them is transcribed from prose in a",
        "roadmap item or a design note, because those go stale and this",
        "repository has held several different totals for the same quantity at",
        "the same time. Each row names the gate that fails when the ledger",
        "drifts.",
        "",
    ]

    res = inv["sections"].get("native-chain-residual")
    if res:
        lines += [
            "## Documents the fully-native chain will not reproduce", "",
            f"Source: `{res['source']}`  ",
            f"Gate: `{res['gate']}`", "",
            "These are documents the reference compiler accepts. A raw",
            "TypeScript host and an agent framework run the equivalent work",
            "without objection; the fully-native revl chain does not reproduce",
            "them. That is the cost, stated first.", "",
            "| tier | residual | corpus | reproduced |", "|---|---:|---:|---:|",
        ]
        for tier, t in res["tiers"].items():
            corpus = t["corpus"] if t["corpus"] is not None else "?"
            repro = t["reproduced"] if t["reproduced"] is not None else "?"
            lines.append(f"| {tier} | **{t['residual']}** | {corpus} | {repro} |")
        total_corpus = res["total_corpus"]
        lines.append(
            f"| **total** | **{res['total_residual']}** | "
            f"{total_corpus if total_corpus is not None else '?'} | "
            f"{(total_corpus - res['total_residual']) if total_corpus is not None else '?'} |")
        clean = ", ".join(res["clean_tiers"]) or "none"
        lines += ["", f"Tiers with no residual: {clean}.", ""]

    unp = inv["sections"].get("unported-constructs")
    if unp:
        lines += [
            "## Constructs the self-host port does not implement", "",
            f"Source: `{unp['source']}`  ",
            f"Gate: `{unp['gate']}`", "",
            "| tier | unported constructs | distinct reasons | blind |",
            "|---|---:|---:|---:|",
        ]
        for tier, t in unp["tiers"].items():
            blind = t["blind_constructs"]
            lines.append(
                f"| {tier} | **{t['unported_constructs']}** | "
                f"{t['unported_reasons']} | {blind if blind is not None else '?'} |")
        lines += [f"| **total** | **{unp['total_unported']}** | | |", ""]

    reach = inv["sections"].get("unreached-constructs")
    if reach:
        lines += [
            "## Dispatch arms no corpus document exercises", "",
            f"Source: `{reach['source']}`  ",
            f"Gate: `{reach['gate']}`", "",
            "This is a weaker statement than a refusal and is listed",
            "separately for that reason: an unreached arm is untested, not",
            "known-broken.", "",
            "| oracle | unreached arms |", "|---|---:|",
        ]
        for name, n in sorted(reach["oracles"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {name} | {n} |")
        lines += [f"| **total** | **{reach['total_unreached']}** |", ""]

    div = inv["sections"].get("gate-reference-divergence")
    if div:
        lines += [
            "## Where the gate is more permissive than the reference", "",
            f"Source: `{div['source']}`  ",
            f"Gate: `{div['gate']}`", "",
            "The fail-open direction. A suite that published only refusals and",
            "left this out would be making the selective argument it claims to",
            "be correcting.", "",
            "| bucket | programs |", "|---|---:|",
        ]
        for bucket, n in div["false_admit_buckets"].items():
            lines.append(f"| `{bucket}` | {n} |")
        lines += [f"| **total** | **{div['total_false_admit']}** |", ""]
        if div.get("named_programs"):
            lines += ["Named programs: " +
                      ", ".join(f"`{p}`" for p in div["named_programs"]) + ".", ""]

    if inv["unavailable"]:
        lines += ["## Sources this run could not read", ""]
        for name, why in sorted(inv["unavailable"].items()):
            lines.append(f"- `{name}`: {why}")
        lines += ["",
                  "A count is absent above rather than reported as zero. A zero",
                  "that means \"the ledger would not read\" is indistinguishable",
                  "in a table from a zero that means \"there is nothing left to",
                  "refuse\", and the second is a far stronger claim.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", default=None, help="write the inventory as JSON here")
    ap.add_argument("--write", action="store_true",
                    help=f"write JSON to {_rel(DEFAULT_OUT)}")
    ap.add_argument("--markdown", action="store_true",
                    help="print the rendered table instead of a summary")
    args = ap.parse_args(argv)

    inv = build()
    if args.markdown:
        print(render(inv), end="")
    else:
        res = inv["sections"].get("native-chain-residual")
        if res:
            print(f"native-chain residual: {res['total_residual']} documents "
                  f"of {res['total_corpus']} across {len(res['tiers'])} tiers "
                  f"(clean: {', '.join(res['clean_tiers']) or 'none'})")
        for name in ("unported-constructs", "unreached-constructs",
                     "gate-reference-divergence"):
            sec = inv["sections"].get(name)
            if not sec:
                continue
            total = next(v for k, v in sec.items() if k.startswith("total_"))
            print(f"{name}: {total}")
        for name, why in sorted(inv["unavailable"].items()):
            print(f"UNAVAILABLE {name}: {why}", file=sys.stderr)

    for target in ([Path(args.json)] if args.json else []) + (
            [DEFAULT_OUT] if args.write else []):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(inv, indent=2) + "\n")
        print(f"wrote {target}")
    return 1 if inv["unavailable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
