#!/usr/bin/env python3
"""Drift gate for the source-derived blocks in the docs.

Five documents carried content that is a pure function of the source tree and
committed it by hand, with nothing to keep it honest (issue #255). Each was
wrong when the issue was filed, and `docs/DOC-STATUS.md` had already drifted
back within a day of a manual correction pass, because the correction fixed the
data and not the mechanism. A marker a human has to remember to update is a
note, not a check.

So this tool owns that content instead. It is the same contract the repo
already applies to every other generated artifact (`tools/conformance.py
--check-readme`, `tools/check_site_wheel.py`, `tools/build_gate_crate.py
--check`): the committed bytes must equal a fresh generation, and CI fails when
they do not.

Two kinds of claim live here, and they are gated differently because they are
different kinds of claim.

GENERATED BLOCKS are byte-compared. The content between a pair of
`<!-- docgen:KEY begin -->` / `<!-- docgen:KEY end -->` markers is rendered from
the source of truth and must match exactly. Where a table mixes derived columns
with human judgement (DOC-STATUS's `status`, rejections.md's `refused by`), the
judgement columns are CARRIED OVER from the committed table rather than
invented: the generation stays a deterministic function of (source tree,
committed judgement), so it is idempotent, and a row that appears for a new doc
or a new diagnostic code arrives with a placeholder that a human must replace.

COVERAGE CHECKS are set comparisons, for the prose tables that cannot be
generated because each row carries curated explanation. The claim being checked
is not "this table is byte-correct" but the weaker, still mechanical "every
subcommand / every verb is documented somewhere in this file". That is exactly
what rotted: verbs were added to the code and no row was added to the guide.

WHAT THIS CANNOT KNOW. A coverage check cannot tell whether the row it found
DESCRIBES the verb correctly, only that a row exists. A carried-over judgement
column is only as good as the last human who wrote it. Neither is a claim that
the docs are right; both are a claim that they are not silently behind the
code. Do not widen either one in the reporting, and never narrow a check to
make a red line pass: regenerate, or fix the doc.

Usage:
    python3 tools/docgen.py --check    # CI gate, exit 1 when stale
    python3 tools/docgen.py --write    # regenerate every block in place
    python3 tools/docgen.py --list     # the blocks and checks, and their sources
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

WRITE_HINT = "python3 tools/docgen.py --write   (or: make docs-gen)"


# --------------------------------------------------------------------------- #
# Marker plumbing.                                                             #
# --------------------------------------------------------------------------- #
def _begin(key: str) -> str:
    return f"<!-- docgen:{key} begin -->"


def _end(key: str) -> str:
    return f"<!-- docgen:{key} end -->"


def extract(text: str, key: str) -> str:
    """The current body between KEY's markers, without the markers themselves."""
    b, e = _begin(key), _end(key)
    if b not in text or e not in text:
        raise SystemExit(
            f"docgen: markers for block '{key}' are missing from the document. "
            f"Add `{b}` and `{e}` around the generated content."
        )
    start = text.index(b) + len(b)
    return text[start:text.index(e)].strip("\n")


def splice(text: str, key: str, body: str) -> str:
    b, e = _begin(key), _end(key)
    pre = text[:text.index(b) + len(b)]
    post = text[text.index(e):]
    return f"{pre}\n{body}\n{post}"


# --------------------------------------------------------------------------- #
# Sources of truth.                                                            #
# --------------------------------------------------------------------------- #
def mcp_tools() -> list[dict]:
    from revl.mcp.server import TOOLS
    return list(TOOLS)


def cli_subcommands() -> list[str]:
    import argparse as _ap
    from revl.cli.parser import build_parser
    for action in build_parser()._actions:
        if isinstance(action, _ap._SubParsersAction):
            return list(action.choices)
    raise SystemExit("docgen: build_parser() declares no subparsers")


def guarantees() -> dict[str, str]:
    from revl.diagnostics import GUARANTEES
    return dict(GUARANTEES)


def doc_files() -> list[str]:
    """Every doc the DOC-STATUS inventory covers: `docs/*.md` minus the
    inventory itself, which would otherwise have to describe its own em-dash
    count as it is being written."""
    return sorted(
        p.name for p in (ROOT / "docs").glob("*.md") if p.name != "DOC-STATUS.md"
    )


def test_count(rel: str) -> int:
    """Top-level `def test_*` functions in a test module, counted from the AST
    rather than by grepping, so a commented-out or nested definition does not
    move the number."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return sum(
        1 for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


# --------------------------------------------------------------------------- #
# Small helpers.                                                               #
# --------------------------------------------------------------------------- #
def parse_rows(body: str) -> dict[str, list[str]]:
    """Committed markdown table rows, keyed by the first cell's bare text.

    Used to carry human-judgement columns across a regeneration. A row whose
    key the source no longer knows about is dropped, which is the point: the
    inventory follows the tree.
    """
    rows: dict[str, list[str]] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        key = cells[0].strip("`")
        rows[key] = cells
    return rows


def carried(rows: dict[str, list[str]], key: str, index: int, default: str) -> str:
    cells = rows.get(key)
    if cells is None or index >= len(cells):
        return default
    return cells[index]


def expand_braces(text: str) -> str:
    """Expand `revl_query_{emitters,withdraw}` shorthand into the full names, so
    a coverage check reads the guide the way a human does."""
    def sub(m: re.Match[str]) -> str:
        prefix, inner = m.group(1), m.group(2)
        return " ".join(prefix + part.strip() for part in inner.split(","))
    return re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\{([^{}]*)\}", sub, text)


def fill(words: list[str], width: int = 72) -> str:
    lines, cur = [], ""
    for w in words:
        candidate = f"{cur}  {w}" if cur else w
        if len(candidate) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Generated blocks.                                                            #
# --------------------------------------------------------------------------- #
def block_doc_status(current: str) -> str:
    """docs/DOC-STATUS.md's inventory table.

    Derived: which docs exist, and each one's em-dash count (the column is
    literally `read_text().count("—")`). Carried: `status` and `tier-limit
    notes`, which are a human's reading. A doc with no committed row arrives as
    `needs-work`, which is the honest default: not audited.
    """
    rows = parse_rows(current)
    out = ["| doc | status | em-dashes | tier-limit notes |", "|---|---|---|---|"]
    for name in doc_files():
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        status = carried(rows, name, 1, "needs-work")
        notes = carried(rows, name, 3, "")
        out.append(f"| {name} | {status} | {text.count('—')} | {notes} |")
    return "\n".join(out)


def block_mcp_verbs(current: str) -> str:
    """docs/mcp-reference.md's at-a-glance table, wholly from `TOOLS`.

    Nothing is carried: name, both safety annotations and the required inputs
    are all in the registry, so the table is a rendering of it.
    """
    out = ["| verb | read-only | destructive | required inputs |", "|---|---|---|---|"]
    for tool in mcp_tools():
        ann = tool.get("annotations") or {}
        schema = tool.get("inputSchema") or {}
        required = list(schema.get("required") or [])
        props = schema.get("properties") or {}
        cells = ", ".join(f"`{r}`" for r in required) or "-"
        if "source" in props and "source" not in required:
            cells += " (source)"
        ro = "yes" if ann.get("readOnlyHint") else "no"
        de = "yes" if ann.get("destructiveHint") else "no"
        out.append(f"| `{tool['name']}` | {ro} | {de} | {cells} |")
    return "\n".join(out)


def block_mcp_count(current: str) -> str:
    n = len(mcp_tools())
    return (f"The advertised list is exactly the {n} verbs below, one section each.")


def block_agents_mcp_count(current: str) -> str:
    n = len(mcp_tools())
    return (
        f"The complete advertised verb set is {n} verbs, from\n"
        "`src/revl/mcp/server.py` and `query_tools.py`. It is grouped below by what\n"
        "you reach for; each verb's exact inputs and outputs are in\n"
        "[mcp-reference.md](mcp-reference.md)."
    )


def block_cli_verbs(current: str) -> str:
    """docs/commands-reference.md's verb list, in the order the parser declares
    it. The fence is the index; the per-command sections below it are gated by
    the `commands-documented` coverage check."""
    return "```text\n" + fill(cli_subcommands()) + "\n```"


def _g_codes(codes) -> list[str]:
    return [c for c in codes if re.fullmatch(r"G\d+", c)]


def block_guarantees_rejections(current: str) -> str:
    """docs/rejections.md's family table. The `guarantee` column is verbatim the
    `GUARANTEES` value, so a plain equality check is the whole gate. `refused
    by` names the phase and is carried."""
    rows = parse_rows(current)
    out = ["| code | guarantee | refused by |", "| ---- | --------- | ---------- |"]
    for code, text in guarantees().items():
        out.append(f"| {code} | {text} | {carried(rows, code, 2, 'TODO: name the phase')} |")
    return "\n".join(out)


def block_guarantees_design(current: str) -> str:
    """DESIGN.md section 4. Scoped to the `G` guarantees, which is what the
    section is about; the wording is the registry's, so the two cannot drift
    apart in phrasing either. `Checked` and `Paper anchor` are carried."""
    rows = parse_rows(current)
    g = guarantees()
    out = ["| # | Guarantee | Checked | Paper anchor |", "|---|---|---|---|"]
    for code in _g_codes(g):
        out.append(
            f"| {code} | {g[code]} | {carried(rows, code, 2, 'TODO')} "
            f"| {carried(rows, code, 3, 'TODO')} |"
        )
    return "\n".join(out)


def block_guarantees_humans(current: str) -> str:
    """docs/guide-humans.md's rejection table, plus the sentence naming the
    lifecycle rules. The rules are listed rather than given as a range: `A1-A8`
    read as a range hid both that A4 and A7 do not exist and that A9 does."""
    g = guarantees()
    out = ["| # | Guarantee |", "|---|---|"]
    for code in _g_codes(g):
        out.append(f"| {code} | {g[code]} |")
    a = [c for c in g if re.fullmatch(r"A\d+", c)]
    listed = ", ".join(a[:-1]) + f" and {a[-1]}"
    t = [c for c in g if re.fullmatch(r"T\d+", c)]
    t_listed = ", ".join(t[:-1]) + f" and {t[-1]}"
    out.append("")
    out.append(
        f"...plus the lifecycle rules {listed} (await boundaries, no acquisition\n"
        f"after `provide`, `fail` semantics, and so on), the confidentiality rules\n"
        "`G-SECRET` and `G-SECRET-FLOW`, and the typing rules "
        f"{t_listed}. The rejection\n"
        "suite in [`examples/rejections/`](../examples/rejections/) is the\n"
        "executable spec, and [rejections.md](rejections.md) is the full table."
    )
    return "\n".join(out)


def block_mcp_test_count(current: str) -> str:
    n = test_count("tests/test_mcp.py")
    return (
        "The `mcp serve` tool surface, its annotations and its structured rejections\n"
        f"are gated by `tests/test_mcp.py` ({n} tests)."
    )


BLOCKS: list[tuple[str, str, str, object]] = [
    ("doc-status", "docs/DOC-STATUS.md", "docs/*.md", block_doc_status),
    ("mcp-verbs", "docs/mcp-reference.md", "revl.mcp.server.TOOLS", block_mcp_verbs),
    ("mcp-verb-count", "docs/mcp-reference.md", "revl.mcp.server.TOOLS", block_mcp_count),
    ("agents-mcp-count", "docs/guide-ai-agents.md", "revl.mcp.server.TOOLS",
     block_agents_mcp_count),
    ("cli-verbs", "docs/commands-reference.md", "revl.cli.parser.build_parser()",
     block_cli_verbs),
    ("guarantees", "docs/rejections.md", "revl.diagnostics.GUARANTEES",
     block_guarantees_rejections),
    ("guarantees-design", "DESIGN.md", "revl.diagnostics.GUARANTEES",
     block_guarantees_design),
    ("guarantees-humans", "docs/guide-humans.md", "revl.diagnostics.GUARANTEES",
     block_guarantees_humans),
    ("mcp-test-count", "docs/guide-humans.md", "tests/test_mcp.py", block_mcp_test_count),
]


# --------------------------------------------------------------------------- #
# Coverage checks.                                                             #
# --------------------------------------------------------------------------- #
def _headings(path: str) -> list[str]:
    return [
        line for line in (ROOT / path).read_text(encoding="utf-8").splitlines()
        if line.startswith("### ")
    ]


def check_commands_documented() -> list[str]:
    heads = _headings("docs/commands-reference.md")
    return [
        f"docs/commands-reference.md has no `### `revl {c}`` section"
        for c in cli_subcommands()
        if not any(f"`revl {c}`" in h or f"`revl {c} " in h for h in heads)
    ]


def check_commands_in_guide() -> list[str]:
    text = (ROOT / "docs" / "guide-humans.md").read_text(encoding="utf-8")
    return [
        f"docs/guide-humans.md never mentions `revl {c}`"
        for c in cli_subcommands()
        if f"`revl {c}`" not in text and f"`revl {c} " not in text
    ]


def check_verbs_documented() -> list[str]:
    heads = _headings("docs/mcp-reference.md")
    return [
        f"docs/mcp-reference.md has no `### ` section for `{t['name']}`"
        for t in mcp_tools()
        if not any(t["name"] in h for h in heads)
    ]


def check_verbs_in_guide() -> list[str]:
    text = expand_braces(
        (ROOT / "docs" / "guide-ai-agents.md").read_text(encoding="utf-8")
    )
    return [
        f"docs/guide-ai-agents.md never mentions `{t['name']}`"
        for t in mcp_tools() if t["name"] not in text
    ]


CHECKS: list[tuple[str, str, str, object]] = [
    ("commands-documented", "docs/commands-reference.md",
     "every build_parser() subcommand has its own section", check_commands_documented),
    ("commands-in-guide", "docs/guide-humans.md",
     "every build_parser() subcommand is named in the guide", check_commands_in_guide),
    ("verbs-documented", "docs/mcp-reference.md",
     "every TOOLS verb has its own section", check_verbs_documented),
    ("verbs-in-guide", "docs/guide-ai-agents.md",
     "every TOOLS verb is named in the guide", check_verbs_in_guide),
]


# --------------------------------------------------------------------------- #
# Drivers.                                                                     #
# --------------------------------------------------------------------------- #
def run_blocks(*, write: bool) -> list[str]:
    """Regenerate (or compare) every block. Ordered so that DOC-STATUS runs
    last: its em-dash counts read the other docs, so it has to see them after
    this pass has rewritten them."""
    stale: list[str] = []
    ordered = sorted(BLOCKS, key=lambda b: b[0] == "doc-status")
    for key, rel, _source, render in ordered:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        body = render(extract(text, key))
        updated = splice(text, key, body)
        if updated == text:
            continue
        if write:
            path.write_text(updated, encoding="utf-8")
        else:
            stale.append(f"{rel}: block '{key}' is stale (source: {_source})")
    return stale


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if any generated block is stale or any "
                         "coverage check fails; the CI gate")
    ap.add_argument("--write", action="store_true",
                    help="regenerate every generated block in place")
    ap.add_argument("--list", action="store_true",
                    help="print the blocks and coverage checks with their sources")
    args = ap.parse_args()

    if args.list:
        print("generated blocks (byte-compared):")
        for key, rel, source, _ in BLOCKS:
            print(f"  {key:<20} {rel:<28} <- {source}")
        print("coverage checks (set comparison):")
        for key, rel, what, _ in CHECKS:
            print(f"  {key:<20} {rel:<28} {what}")
        return 0

    if args.write:
        stale = run_blocks(write=True)
        print("docgen: blocks regenerated." if stale else "docgen: blocks already current.")
        failures = [f for _, _, _, fn in CHECKS for f in fn()]
        if failures:
            print("\ndocgen: coverage checks still fail. These need PROSE, not a "
                  "regeneration:", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            return 1
        return 0

    if not args.check:
        ap.print_help()
        return 2

    stale = run_blocks(write=False)
    failures = [f for _, _, _, fn in CHECKS for f in fn()]
    if not stale and not failures:
        print(f"docgen: {len(BLOCKS)} generated blocks current, "
              f"{len(CHECKS)} coverage checks pass.")
        return 0
    if stale:
        print("docgen: generated blocks are STALE.", file=sys.stderr)
        for s in stale:
            print(f"  {s}", file=sys.stderr)
        print(f"  fix: {WRITE_HINT}", file=sys.stderr)
    if failures:
        print("docgen: documentation coverage FAILED. A subcommand or verb exists "
              "in the code with nothing describing it.", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print("  fix: write the missing section or row. Never delete the check.",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
