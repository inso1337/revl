#!/usr/bin/env python3
"""Refusal telemetry — turn the rejection log into a feature-request queue.

`rescore.py` prints a *failure taxonomy*: it buckets refused generations by
`(code, category)`. That is the seed. This extends it into a **ranked demand
table**: it reads what the models that write revl keep *reaching for* and being
refused — an unknown stdlib method, an invented syntax form, a missing type —
and ranks those reached-for symbols by how often they are refused across the
committed corpora. The stdlib/syntax roadmap can then be ordered by MEASURED
demand, the same "measured, not assumed" move the Int32 entry made.

Two things separate a demand table from the taxonomy:

  * It sub-classifies below `(code, category)`. The taxonomy lumps a
    "no builtin method take" refusal and the parse error "found 'kv'" into the
    same (G6, guarantee) bucket; the demand miner splits them into a
    *stdlib-method* demand for `take` and a *syntax-form* demand for `kv`,
    because they order two different roadmaps.
  * It mines the *symbol* out of the message — the method, the form, the type —
    so the ranking is "how often was `take` reached for", not "how often did a
    G6 fire".

This is free. It calls no provider: it recompiles the `.rvl` files already
committed under `bench/results/<run>/` with the checker in this tree, exactly
as `rescore.py` does, and reads the refusals. By default it mines *every*
attempt (`attempt-1.rvl`, `attempt-2.rvl`, ...): every refused attempt is a
data point of demand, not just the first-pass number.

An opt-in *second* source folds in the MCP session's own diagnostics
(`--mcp-diagnostics PATH`, off by default, read-only): the same structured
records `revl_check`/`revl_load` return, captured from a live session. The
corpus is the always-on source; the live session is a lens an operator can add.

Usage:
  python3 bench/demand.py                                  # rank across model runs
  python3 bench/demand.py --run all --attempts all         # (the default)
  python3 bench/demand.py --kind stdlib                     # only stdlib-method demand
  python3 bench/demand.py --json demand.json                # machine-readable form
  python3 bench/demand.py --mcp-diagnostics session.jsonl   # + opt-in live source
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# reuse the corpus loader and per-file scorer — a demand table is a second
# reading of the very machinery rescore.py already uses, so it stays in step
# with how the taxonomy is measured.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rescore  # noqa: E402


# -- refusal-kind classification -------------------------------------------
#
# Below (code, category) sits the reached-for symbol. Each matcher pulls the
# symbol out of the diagnostic message and names the *kind* of demand it is —
# the row of the roadmap it belongs to. Order matters: the first match wins,
# so the specific host/stdlib/type shapes are tried before the generic parse
# fallback.

_HOST_METHOD = re.compile(r"`(?P<family>[A-Za-z_][\w.]*)` has no method `(?P<method>[^`]+)`")
_HOST_ARITY = re.compile(r"host `(?P<symbol>[A-Za-z_][\w.]+)` takes \d+ argument")
_STDLIB_METHOD = re.compile(r"no builtin method `(?P<method>[^`]+)`(?: on `?(?P<recv>[^`]+?)`?)?(?: —|$)")
_NOT_A_TYPE = re.compile(r"`(?P<type>[^`]+)` is not a type")
_PARSE_FOUND = re.compile(r"found ['`](?P<token>[^'`]+)['`]")
_LEX = re.compile(r"^(?P<what>unexpected character|unterminated string)")


def _load_compiler(root: Path):
    """The compiler entry points, in step with `rescore.py`.

    For a *foreign* `--compiler-root` we defer to `rescore.load_compiler`,
    which purges and re-imports `revl` from that tree. For the live in-tree
    root we import `revl` in place instead: the purge-and-reload is only needed
    to swap trees, and doing it against the same tree would evict a `revl`
    already imported by the surrounding process (a test suite) out from under
    the references it holds.
    """
    if root == rescore.ROOT:
        from revl import RevlError, compile_source  # noqa: PLC0415
        from revl.diagnostics import classify  # noqa: PLC0415
        return compile_source, RevlError, classify
    return rescore.load_compiler(root)


def _clip(text: str, width: int = 96) -> str:
    text = text.splitlines()[0].strip()
    return text if len(text) <= width else text[: width - 3] + "..."


def classify_demand(code: str, category: str, message: str) -> tuple[str, str]:
    """(kind, symbol) — what a refusal shows the model reached for.

    `kind` is the roadmap row: host-method, stdlib-method, syntax-form,
    missing-type, or, when no symbol can be mined, a `guarantee:<code>` bucket
    so guarantee/semantic refusals still rank (they are demand for a different
    roadmap, and the machine-readable form keeps them separable).
    """
    first = message.splitlines()[0] if message else ""

    m = _HOST_METHOD.search(first)
    if m:
        return "host-method", f"{m.group('family')}.{m.group('method')}"
    m = _HOST_ARITY.search(first)
    if m:
        return "host-method", m.group("symbol")

    m = _STDLIB_METHOD.search(first)
    if m:
        recv = (m.group("recv") or "").strip()
        recv = recv if recv and recv != "values" else "*"
        return "stdlib-method", f"{recv}.{m.group('method')}" if recv != "*" else m.group("method")

    m = _NOT_A_TYPE.search(first)
    if m:
        return "missing-type", m.group("type")

    # a parse/lex refusal is an invented syntax form — the token the model
    # reached for that the grammar has no place for.
    if (category in ("parse", "lex")) or code == "SYNTAX" or "found" in first or _LEX.search(first):
        m = _PARSE_FOUND.search(first)
        if m:
            return "syntax-form", m.group("token")
        m = _LEX.search(first)
        if m:
            return "syntax-form", m.group("what")

    # no symbol to mine: rank it by its guarantee, still visible in the table.
    return f"guarantee:{code}", f"{code}/{category}"


# -- mining -----------------------------------------------------------------


class Demand:
    """One reached-for symbol and everything refused for it."""

    __slots__ = ("kind", "symbol", "count", "codes", "example", "sources", "cells")

    def __init__(self, kind: str, symbol: str) -> None:
        self.kind = kind
        self.symbol = symbol
        self.count = 0
        self.codes: Counter = Counter()
        self.example = ""
        self.sources: Counter = Counter()
        self.cells: list[str] = []

    def add(self, code: str, category: str, message: str, source: str, cell: str) -> None:
        self.count += 1
        self.codes[f"{code}/{category}"] += 1
        self.sources[source] += 1
        if len(self.cells) < 8:
            self.cells.append(cell)
        # first refusal (in deterministic iteration order) is the example.
        if not self.example:
            self.example = _clip(message)


def _attempts_for(variant_dir: Path, which: str) -> list[Path]:
    files = sorted(variant_dir.glob("attempt-*.rvl"),
                   key=lambda p: int(re.search(r"attempt-(\d+)", p.name).group(1)))
    if which == "1":
        files = [p for p in files if p.name == "attempt-1.rvl"]
    return files


def mine_corpus(runs: list[str], attempts: str, compiler_root: Path) -> tuple[dict, list[dict]]:
    """Recompile every committed attempt and record each refusal as demand.

    Returns (table, refusals) where `table` maps (kind, symbol) -> Demand and
    `refusals` is the flat per-attempt refusal log (machine-readable).
    """
    compile_source, RevlError, classify = _load_compiler(compiler_root)
    table: dict[tuple[str, str], Demand] = {}
    refusals: list[dict] = []
    for run in runs:
        run_dir = rescore.RESULTS / run
        if not run_dir.is_dir():
            raise SystemExit(f"no such run: {run_dir}")
        for spec_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            for variant in rescore.VARIANTS:
                variant_dir = spec_dir / variant
                if not variant_dir.is_dir():
                    continue
                for path in _attempts_for(variant_dir, attempts):
                    result = rescore.score_one(path, compile_source, RevlError, classify)
                    if result["ok"]:
                        continue
                    _record(table, refusals, result["code"], result["category"],
                            result["message"], "corpus",
                            f"{run}/{spec_dir.name}/{variant}/{path.name}")
    return table, refusals


def mine_mcp(paths: list[str], table: dict, refusals: list[dict]) -> int:
    """Fold the MCP session's own diagnostics in as a second source (opt-in).

    Read-only: it consumes the structured records the session already emits
    (the shape `revl_check`/`revl_load` return, or a `{diagnostics: [...]}`
    report document), never the live session itself. Accepts JSONL (one record
    per line) or a JSON array/object. Returns the number of refusals folded in.
    """
    before = len(refusals)
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            raise SystemExit(f"no such MCP diagnostics file: {p}")
        for diag, origin in _iter_mcp_diagnostics(p):
            code = diag.get("code") or "REVL"
            category = diag.get("category") or "check"
            message = diag.get("message") or ""
            if not message and diag.get("severity") == "obligation":
                continue  # an open hole is not a refusal
            _record(table, refusals, code, category, message, "mcp", origin)
    return len(refusals) - before


def _iter_mcp_diagnostics(path: Path):
    """Yield (diagnostic-dict, origin-label) from one capture file."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return
    docs: list = []
    try:
        loaded = json.loads(text)
        docs = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    for i, doc in enumerate(docs):
        origin = f"{path.name}#{i}"
        for diag in _diagnostics_of(doc):
            yield diag, origin


def _diagnostics_of(doc: dict):
    """Pull classify-shaped records out of the several shapes the MCP emits:
    a bare diagnostic, a `report()` `{diagnostics: [...]}` doc, or a restore
    error `{diagnostic: {...}}`."""
    if not isinstance(doc, dict):
        return
    if isinstance(doc.get("diagnostics"), list):
        for d in doc["diagnostics"]:
            if isinstance(d, dict):
                yield d
        return
    if isinstance(doc.get("diagnostic"), dict):
        yield doc["diagnostic"]
        return
    if doc.get("code") or doc.get("message"):
        yield doc


def _record(table, refusals, code, category, message, source, cell) -> None:
    kind, symbol = classify_demand(code, category, message)
    key = (kind, symbol)
    slot = table.get(key)
    if slot is None:
        slot = table[key] = Demand(kind, symbol)
    slot.add(code, category, message, source, cell)
    refusals.append({"kind": kind, "symbol": symbol, "code": code,
                     "category": category, "source": source, "cell": cell,
                     "message": message.splitlines()[0] if message else ""})


# -- ranking + rendering ----------------------------------------------------

# stdlib/syntax roadmap rows first; guarantee buckets are demand for a
# different roadmap and sort after, but stay in the table.
_KIND_ORDER = {"stdlib-method": 0, "syntax-form": 1, "host-method": 2,
               "missing-type": 3}


def rank(table: dict) -> list[Demand]:
    """Deterministic demand ranking: most-refused first, ties broken by kind
    then symbol so the order never depends on dict insertion or timing."""
    return sorted(
        table.values(),
        key=lambda d: (-d.count, _KIND_ORDER.get(d.kind, 9), d.kind, d.symbol),
    )


_ROADMAP_KINDS = {"stdlib-method", "syntax-form", "host-method", "missing-type"}


def render(ranked: list[Demand], sha: str, scope: str, mcp_note: str,
           kind_filter: str) -> list[str]:
    lines = [
        "## refusal demand ranking",
        f"compiler: `{sha}`  ·  scope: {scope}{mcp_note}",
        "",
        "What the models that write revl reached for and were refused, ranked by "
        "frequency. Order the stdlib/syntax roadmap by the top rows.",
        "",
        "| rank | kind | reached-for | refusals | codes | example |",
        "|---:|---|---|---:|---|---|",
    ]
    shown = 0
    for i, d in enumerate(ranked, 1):
        if kind_filter != "all" and not _kind_matches(d.kind, kind_filter):
            continue
        shown += 1
        codes = ", ".join(f"{c}×{n}" for c, n in d.codes.most_common())
        symbol = d.symbol.replace("|", "\\|")
        example = d.example.replace("|", "\\|")
        src = "" if set(d.sources) == {"corpus"} else \
            "  _(" + "+".join(sorted(d.sources)) + ")_"
        lines.append(f"| {shown} | {d.kind} | `{symbol}`{src} | {d.count} | {codes} | {example} |")
    if shown == 0:
        lines.append("| — | — | _no refusals in scope_ | 0 | | |")
    lines += ["", "Roadmap-actionable rows (stdlib / syntax / host / type demand):", ""]
    actionable = [d for d in ranked if d.kind in _ROADMAP_KINDS
                  and (kind_filter == "all" or _kind_matches(d.kind, kind_filter))]
    if not actionable:
        lines.append("_none in scope._")
    for d in actionable:
        lines.append(f"- **{d.symbol}** ({d.kind}) — {d.count} refusal(s); "
                     f"e.g. `{d.cells[0]}`")
    return lines


def _kind_matches(kind: str, kind_filter: str) -> bool:
    return {
        "stdlib": kind == "stdlib-method",
        "syntax": kind == "syntax-form",
        "host": kind == "host-method",
        "type": kind == "missing-type",
        "guarantee": kind.startswith("guarantee:"),
    }.get(kind_filter, True)


def to_json(ranked: list[Demand], refusals: list[dict], sha: str,
            scope: str) -> dict:
    return {
        "compiler": sha,
        "scope": scope,
        "ranking": [
            {
                "rank": i,
                "kind": d.kind,
                "symbol": d.symbol,
                "count": d.count,
                "codes": dict(d.codes),
                "sources": dict(d.sources),
                "example": d.example,
                "cells": d.cells,
                "roadmap_actionable": d.kind in _ROADMAP_KINDS,
            }
            for i, d in enumerate(ranked, 1)
        ],
        "refusals": refusals,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="all",
                    help="run dir under bench/results, or 'all' for "
                         f"{' + '.join(rescore.MODEL_RUNS)} (default: all)")
    ap.add_argument("--attempts", choices=["all", "1"], default="all",
                    help="mine every attempt (default) or only attempt-1")
    ap.add_argument("--kind", choices=["all", "stdlib", "syntax", "host", "type",
                                       "guarantee"], default="all",
                    help="restrict the printed table to one refusal kind")
    ap.add_argument("--mcp-diagnostics", action="append", default=[],
                    metavar="PATH",
                    help="OPT-IN second source: fold in captured MCP session "
                         "diagnostics (JSON/JSONL). Off by default; read-only.")
    ap.add_argument("--compiler-root", default=None,
                    help="score against <dir>/src/revl instead of this tree")
    ap.add_argument("--json", default=None, help="also write the ranking as JSON")
    args = ap.parse_args()

    root = Path(args.compiler_root).resolve() if args.compiler_root else rescore.ROOT
    if not (root / "src" / "revl").is_dir():
        raise SystemExit(f"no src/revl under {root}")

    runs = rescore.MODEL_RUNS if args.run == "all" else [args.run]
    table, refusals = mine_corpus(runs, args.attempts, root)

    mcp_note = ""
    if args.mcp_diagnostics:
        folded = mine_mcp(args.mcp_diagnostics, table, refusals)
        mcp_note = f"  ·  + MCP diagnostics: {folded} refusal(s) from " \
                   f"{len(args.mcp_diagnostics)} capture(s)"

    sha = rescore.compiler_sha(root)
    scope = f"{'+'.join(runs)} · attempts={args.attempts}"
    ranked = rank(table)

    print("\n".join(render(ranked, sha, scope, mcp_note, args.kind)))
    if args.json:
        Path(args.json).write_text(json.dumps(to_json(ranked, refusals, sha, scope),
                                              indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
