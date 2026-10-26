"""Evidence for three promotion-bar entries that no other reward component reads.

    python3 tools/promotion_evidence.py measure --tree T --kind cases
    python3 tools/promotion_evidence.py measure --tree T --kind audit|markers --cases CASES.json
    python3 tools/promotion_evidence.py reach-diff|resource-diff BASE.json HEAD.json

Item 536 lists seven changes that green tests alone must not carry through a
promotion (docs/design/534-evolution-reward.md, slice 4). Four were already read
by a component. These are the other three, and `tools/evolution_reward.py` turns
each into its own component, so a failure names one reason:

    reach          a crossing was added, a reach bound loosened or a capability
                   scope widened in the audit of a conformance case
    resource       a per-activation emission ceiling widened, bounded to a
                   larger bound or to `unbounded`, or a verdict went missing
    host-fallback  an emitted artifact carries an `<<UNSUPPORTED...>>` marker
                   it did not carry before, or a tier that emitted a case at
                   base now refuses it

The corpus is `tools/conformance.py`'s `CASES` as the BASE has it, and both
sides are measured on those sources: `measure --kind cases` prints them from the
base tree and every other `measure` takes them back through `--cases`. A
candidate that edits a case so that it no longer exercises the crossing, the
loop or the construct is therefore measured on the original, and a candidate
that adds a case is not measured on it (new work). The compiler and the
emitters are the measured tree's own, found through `PYTHONPATH`. Nothing here
reads the candidate's prose.

`measure` prints `{"cases": {label: evidence}}`. For `audit` the evidence is the
full `audit_report` of the compiled case, or `{"error": message}` when the
frontend refuses it. For `markers` it is `{tier: {"markers": [...], "refused":
bool}}`, or `{"error": ...}` for a case that does not compile. `markers` lists
every occurrence, so a second copy of a marker that was already there counts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

#: What the tiers print in place of a construct they cannot emit. The
#: self-hosted emitters spell it `<<UNSUPPORTED-<reason>:<detail>>>`
#: (docs/selfhost-findings.md); a marker copied into otherwise valid source is
#: still a marker, so this is a text scan and not a parse.
MARKER = re.compile(r"<<UNSUPPORTED.*?>>")


def _load_corpus(tree: Path):
    """`tools/conformance.py` of `tree`, loaded by path so that nothing in the
    scorer's own `tools/` directory can shadow it."""
    path = tree / "tools" / "conformance.py"
    if not path.is_file():
        raise SystemExit(f"{path} is not present, so there is no corpus to measure")
    sys.path.insert(0, str(tree / "tools"))
    spec = importlib.util.spec_from_file_location("promotion_corpus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text(artifact) -> str:
    if isinstance(artifact, str):
        return artifact
    return json.dumps(artifact, sort_keys=True, default=str)


def _compile(corpus, source: str):
    try:
        return corpus.compile_source(source), None
    except Exception as exc:  # noqa: BLE001 - a refusal is the datum
        return None, (str(exc).splitlines() or [type(exc).__name__])[0]


def _cases(args, corpus):
    if args.cases:
        raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))
        return [tuple(c) for c in raw["cases"]]
    return list(corpus.CASES)


def measure_cases(corpus, cases) -> dict:
    return {"cases": [list(c) for c in cases]}


def measure_audit(corpus, cases) -> dict:
    from revl.audit_diff import audit_report  # noqa: PLC0415

    out = {}
    for group, name, source in cases:
        ir, error = _compile(corpus, source)
        out[f"{group}/{name}"] = {"error": error} if error else audit_report(ir)
    return {"cases": out}


def measure_markers(corpus, cases) -> dict:
    out = {}
    for index, (group, name, source) in enumerate(cases):
        ir, error = _compile(corpus, source)
        if error:
            out[f"{group}/{name}"] = {"error": error}
            continue
        per_tier = {}
        for tier in corpus.TIERS:
            try:
                emitted = corpus.emitter(tier).emit(
                    ir, **corpus._emit_kwargs(tier, index))
            except Exception:  # noqa: BLE001 - a refusal is the datum
                per_tier[tier] = {"markers": [], "refused": True}
                continue
            per_tier[tier] = {"markers": sorted(MARKER.findall(_text(emitted))),
                              "refused": False}
        out[f"{group}/{name}"] = per_tier
    return {"cases": out}


def reach_diff(base: dict, head: dict) -> dict:
    """`label -> result` for every case measured on both sides: the decision
    `revl audit --diff` makes (`audit_diff.evaluate`: added crossings and loosened
    reach bounds) plus `diff_capability_scopes`, which that decision does not
    read: a scope widened on a crossing whose token did not change.
    `unacknowledged` holds every widening; no acknowledgement is honoured."""
    from revl.audit_diff import diff_capability_scopes, evaluate  # noqa: PLC0415

    out = {}
    for label, before in sorted(base["cases"].items()):
        after = head["cases"].get(label)
        if after is None or "error" in before or "error" in after:
            continue
        result = evaluate(before, after)
        scopes = diff_capability_scopes(before, after)
        result["unacknowledged"] = sorted(
            set(result["unacknowledged"]) | set(scopes["scope_widened"]))
        out[label] = result
    return out


def _cells(audit: dict) -> dict:
    return {(comp, token): cell
            for comp, entry in (audit.get("cardinality") or {}).items()
            for token, cell in (entry.get("per_capability") or {}).items()}


def resource_diff(base: dict, head: dict) -> dict:
    """`label -> {compared, widened, missing}` for every case measured on both
    sides. `widened` is `audit_diff.diff_cardinality`'s own bucket, so a ceiling
    that moved from one number to a larger one counts as well as one that became
    `unbounded`. `missing` is a `(component, capability)` verdict present at
    base and absent at head: the evidence went away."""
    from revl.audit_diff import diff_cardinality  # noqa: PLC0415

    out = {}
    for label, before in sorted(base["cases"].items()):
        after = head["cases"].get(label)
        if after is None or "error" in before or "error" in after:
            continue
        old, new = _cells(before), _cells(after)
        out[label] = {
            "compared": len(old.keys() & new.keys()),
            "widened": diff_cardinality(before, after)["cardinality_widened"],
            "missing": sorted(f"cardinality:{c}:{t}"
                              for c, t in old.keys() - new.keys()),
        }
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure")
    measure.add_argument("--tree", required=True)
    measure.add_argument("--kind", choices=("cases", "audit", "markers"),
                         required=True)
    measure.add_argument("--cases")
    for name in ("reach-diff", "resource-diff"):
        diff = sub.add_parser(name)
        diff.add_argument("base")
        diff.add_argument("head")
    args = parser.parse_args(argv)
    if args.command == "measure":
        corpus = _load_corpus(Path(args.tree).resolve())
        fn = {"cases": measure_cases, "audit": measure_audit,
              "markers": measure_markers}[args.kind]
        print(json.dumps(fn(corpus, _cases(args, corpus)), sort_keys=True))
        return 0
    base = json.loads(Path(args.base).read_text(encoding="utf-8"))
    head = json.loads(Path(args.head).read_text(encoding="utf-8"))
    fn = reach_diff if args.command == "reach-diff" else resource_diff
    print(json.dumps(fn(base, head), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
