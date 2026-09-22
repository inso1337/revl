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
--check-readme`, `tools/regen_goldens.py --check`, `tools/build_gate_crate.py
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


# Docs the inventory deliberately does not cover.
#
# DOC-STATUS.md itself, which would otherwise have to describe its own em-dash
# count as it is being written.
#
# v2.0-roadmap.md, because it is the reasoning-of-record rather than
# reader-facing prose: it is appended to by nearly every PR, so an exact
# em-dash column on it would redden this gate on almost every commit. A gate
# that fails constantly gets routinely bypassed, and a bypassed gate is the
# failure this one exists to prevent. The style column serves docs people
# read; the roadmap is not one of them.
#
# Issue #296 asked whether the roadmap can rejoin now that the block is
# membership plus em-dashes and nothing else. Measured, it cannot, because the
# roadmap is a churn source in its own right and not merely a victim of one:
# across the 40 most recent first-parent landings on main its own em-dash count
# moved on 13 of them, against 18 for the whole of the rest of docs/ put
# together. Re-joining would add a third again as much churn to a gate every
# open PR pays for. The exclusion stays.
DOC_STATUS_EXCLUDED = frozenset({"DOC-STATUS.md", "v2.0-roadmap.md"})


def doc_files() -> list[str]:
    """Every doc the DOC-STATUS inventory covers: `docs/*.md` minus
    `DOC_STATUS_EXCLUDED`."""
    return sorted(
        p.name for p in (ROOT / "docs").glob("*.md")
        if p.name not in DOC_STATUS_EXCLUDED
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

    NOT derived, deliberately, and never has been: a line count, a byte count,
    a word count or any other measure of a doc's size. `--check` byte-compares
    this block in the required `frontend` job, so whatever the block tracks is
    paid for by every open PR at once and not only by the branch that moved it
    (issue #296). The em-dash count earns that price: 18 of the 40 most recent
    first-parent landings on main moved an em-dash count or the doc list, and
    17 of them RAISED a count, which is exactly the AI-tell regression the
    style pass exists to catch. A size column would have moved the block on 23
    of those same 40 and detected nothing a diff does not already show. Both
    directions are pinned by `tests/test_docgen_doc_status_shape.py`; the churn
    a landing cannot regenerate away is a merge-ordering problem, fixed by the
    merge queue, not by weakening what this block says.
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


def block_authoring_mcp_count(current: str) -> str:
    """docs/authoring-for-agents.md's verb total. It was hand-maintained and
    drifted (issue #939): the page still claimed the authoring verbs were
    CLI-only after item 345 exposed them over MCP, and its total lagged the
    registry. Generating it ties the page to `TOOLS` like the other two."""
    n = len(mcp_tools())
    return (
        f"`revl mcp serve` advertises {n} verbs in total; the full list is in\n"
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


class VisionTierError(SystemExit):
    """docs/vision.md's tier table cannot be rendered from the register."""


# The runtime each conformance tier is called in docs/vision.md. The register
# spells a tier `py`; the vision document is about runtimes and spells the same
# tier `cordis-py`. A tier the register grows with no label here is a loud
# failure rather than a silently missing row: a new tier that never reaches the
# vision table is the drift this block exists to make impossible.
#: The six runtime tiers and the runtime each one targets. Ordered as the
#: self-host residual table prints them, because `RESIDUAL_TIERS` below is
#: derived from these keys rather than restating them (issue #1332).
VISION_TIER_RUNTIME = {
    "py": "cordis-py",
    "ts": "cordis (TypeScript, v4)",
    "go": "cordis-go (Go)",
    "java": "cordis4j (Java)",
    "rust": "cordis-rs (Rust)",
    "wasm": "cordis-wasm",
}

# The self-host column of the register is the compiler compiling itself, not a
# runtime revl targets, so it is not one of the six tiers this table is about.
VISION_TIER_SKIP = frozenset({"revl (self-host)"})


def conformance_per_tier(root: Path | None = None) -> list[tuple[str, str, str, str]]:
    """The per-tier totals out of `docs/conformance.md`'s generated matrix, as
    (tier, ok, deliberate limit, real gap).

    Read from the document rather than by re-running the emitters, because
    `python3 tools/conformance.py --check-readme` already holds that block to a
    fresh walk of the register: the number here cannot differ from the register
    without CI failing on the middle document first. Re-deriving it would cost
    a full conformance sweep and a working revl install in every job that wants
    the vision table checked, including `lint`, which installs neither.
    """
    base = root if root is not None else ROOT
    text = (base / "docs" / "conformance.md").read_text(encoding="utf-8")
    header = "| tier | ok | deliberate limit | real gap |"
    if header not in text:
        raise VisionTierError(
            "docgen: docs/conformance.md has no per-tier totals table "
            f"({header!r}). It is generated by `python3 tools/conformance.py "
            "--write-readme`; regenerate it with `make matrix`."
        )
    rows: list[tuple[str, str, str, str]] = []
    lines = text[text.index(header):].splitlines()[1:]
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            break
        if set(line) <= set("|- "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 4:
            break
        rows.append((cells[0], cells[1], cells[2], cells[3]))
    if not rows:
        raise VisionTierError(
            "docgen: docs/conformance.md's per-tier totals table has no rows."
        )
    return rows


def block_vision_tiers(current: str, root: Path | None = None) -> str:
    """docs/vision.md's six-tier table.

    Derived: which tiers exist and what the conformance register says each one
    does with the corpus (`ok`, a deliberate tier limit, a real gap). Carried:
    the tier's role word and its "what it proves" sentence, which are a human's
    reading and cannot be generated.

    The table was wholly hand-written (issue #1204), sitting beside a generated
    conformance matrix that nothing compared it to, so a guarantee moving
    between proved, deliberate limit and gap moved one document and not the
    other. Generating the derived half makes that drift structurally impossible
    rather than merely detectable.
    """
    rows = parse_rows(current)
    out = [
        "| runtime | tier | conformance today | what it proves |",
        "|---|---|---|---|",
    ]
    for tier, ok, limit, gap in conformance_per_tier(root):
        if tier in VISION_TIER_SKIP:
            continue
        runtime = VISION_TIER_RUNTIME.get(tier)
        if runtime is None:
            raise VisionTierError(
                f"docgen: the conformance register has a tier `{tier}` with no "
                "entry in VISION_TIER_RUNTIME (tools/docgen.py). Add the name "
                "docs/vision.md gives that runtime, or add the tier to "
                "VISION_TIER_SKIP with a reason."
            )
        role = carried(rows, runtime, 1, "TODO: name this tier")
        proves = carried(rows, runtime, 3, "TODO: say what this tier proves")
        out.append(f"| {runtime} | {role} | {ok} ok / {limit} limit / {gap} gap "
                   f"| {proves} |")
    return "\n".join(out)


class SelfhostResidualError(SystemExit):
    """The self-host residual cannot be measured from the committed tables."""


# The order the two self-host documents print the tiers in. It is the order the
# corpora were built in, not alphabetical, and it is fixed here so the table is
# a function of the ledger and nothing else.
#: The tiers the self-host residual is reported over, in print order.
#: Taken from `VISION_TIER_RUNTIME` rather than typed again: this file
#: already carried the tier set once, and a second copy in the same
#: module is a second thing to keep in step with the eight other
#: declarations of it in the tree (issue #1332).
RESIDUAL_TIERS = tuple(VISION_TIER_RUNTIME)

NATIVE_CHAIN_TEST = "tests/test_selfhost_compile.py"


def _literal(rel: str, name: str, root: Path | None = None):
    """A module-level literal assignment, read with `ast.literal_eval` rather
    than by importing the module: the ledger and the six corpus lists live in
    pytest modules that import revl, and every consumer of this number (the
    docs, `tools/evolution_progress.py`, this gate) must be able to read them
    with nothing installed."""
    base = root if root is not None else ROOT
    path = base / rel
    if not path.is_file():
        raise SelfhostResidualError(f"docgen: {rel} is not present")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            try:
                return ast.literal_eval(value)
            except ValueError as exc:
                raise SelfhostResidualError(
                    f"docgen: {rel} defines `{name}` as something this gate "
                    f"cannot evaluate literally: {exc}") from exc
    raise SelfhostResidualError(f"docgen: {rel} defines no `{name}`")


def selfhost_residual(root: Path | None = None) -> list[tuple[str, int, int]]:
    """`(tier, corpus size, residual size)` for each of the six tiers.

    Both halves come from the tables the tests already gate. The corpus is
    `tests/test_selfhost_emit_<tier>.py::CORPUS`, the enumerated document list
    the byte-agreement oracle holds to identity. The residual is
    `LOWER_GAP_DOCS[tier]` in `tests/test_selfhost_compile.py`, which
    `test_the_residual_is_located_in_lower_not_in_the_emitter` RECOMPUTES over
    that same corpus on every run: a document that starts or stops diverging
    reds that test, so the ledger is a measurement and not a note.

    This function exists because the number was not. Four places in the tree
    stated the residual in prose and gave three different answers (issue
    #1300): the ledger said 41, the roadmap said 59, and both self-host
    documents said 63, all of them typed by hand from a measurement taken on a
    day that has passed. Correcting the three would have reset the clock on the
    same defect, so the prose is rendered from the ledger instead.
    """
    ledger = _literal(NATIVE_CHAIN_TEST, "LOWER_GAP_DOCS", root)
    rows: list[tuple[str, int, int]] = []
    for tier in RESIDUAL_TIERS:
        if tier not in ledger:
            raise SelfhostResidualError(
                f"docgen: {NATIVE_CHAIN_TEST}'s LOWER_GAP_DOCS has no entry for "
                f"the `{tier}` tier. A tier with no residual is `(),` not a "
                "missing key: absent reads as zero and that is the one thing "
                "this table must never invent.")
        corpus = _literal(f"tests/test_selfhost_emit_{tier}.py", "CORPUS", root)
        stray = [d for d in ledger[tier] if d not in corpus]
        if stray:
            raise SelfhostResidualError(
                f"docgen: LOWER_GAP_DOCS[{tier!r}] names {stray}, which "
                f"tests/test_selfhost_emit_{tier}.py::CORPUS does not contain. "
                "The residual is only a fraction of that corpus while the two "
                "enumerate the same documents.")
        rows.append((tier, len(corpus), len(ledger[tier])))
    for tier in ledger:
        if tier not in RESIDUAL_TIERS:
            raise SelfhostResidualError(
                f"docgen: LOWER_GAP_DOCS has a tier `{tier}` that RESIDUAL_TIERS "
                "(tools/docgen.py) does not list. Add it there so it reaches the "
                "generated table, rather than leaving it out of the total.")
    return rows


def residual_totals(root: Path | None = None) -> tuple[int, int]:
    """`(corpus, residual)` summed over the six tiers."""
    rows = selfhost_residual(root)
    return sum(c for _, c, _ in rows), sum(g for _, _, g in rows)


def _residual_table(rows: list[tuple[str, int, int]]) -> list[str]:
    head = ["tier", "corpus", "emitter vs the reference IR",
            "the fully-native chain"]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["-" * (len(head[0]) + 2)]
                          + ["-" * (len(h) + 1) + ":" for h in head[1:]]) + "|"]
    for tier, corpus, gap in rows:
        exact = corpus - gap
        cells = [tier.ljust(len(head[0])),
                 str(corpus).rjust(len(head[1])),
                 f"{corpus} (100%)".rjust(len(head[2])),
                 f"{exact} ({exact / corpus * 100:.1f}%)".rjust(len(head[3]))]
        out.append("| " + " | ".join(cells) + " |")
    corpus = sum(c for _, c, _ in rows)
    exact = corpus - sum(g for _, _, g in rows)
    out.append(f"| **total** | **{corpus}** | **{corpus} (100%)** "
               f"| **{exact} ({exact / corpus * 100:.1f}%)** |")
    return out


def block_selfhost_residual(current: str, root: Path | None = None) -> str:
    """The per-tier residual table and its total, for `docs/selfhost-compile.md`
    and `docs/selfhost-findings.md`.

    Nothing is carried: every cell is a count of a committed list, so the block
    is a rendering of `LOWER_GAP_DOCS` and the six `CORPUS` lists and a reader
    who wants the documents themselves can read the same tables.

    The middle column is the emitter half of roadmap item 146, and it reads
    100% because `test_the_residual_is_located_in_lower_not_in_the_emitter`
    asserts it per document, for every document, on every run. It is rendered
    rather than counted separately on purpose: if that assertion ever fails the
    suite is red, which is a louder answer than a column quietly dropping to
    99%.
    """
    rows = selfhost_residual(root)
    corpus = sum(c for _, c, _ in rows)
    gap = sum(g for _, _, g in rows)
    out = _residual_table(rows)
    out += [
        "",
        f"Every one of the {corpus} documents is reproduced byte-for-byte by its",
        "self-host emitter when the emitter is fed the **reference** IR. "
        f"{corpus - gap} of",
        f"them survive the **fully-native** chain, so all {gap} residual documents",
        "are `selfhost/lower.rvl` gaps, the native IR producer, and not emitter",
        "gaps.",
        "",
        "Both columns and both totals are generated by `tools/docgen.py` from",
        f"[`LOWER_GAP_DOCS`](../{NATIVE_CHAIN_TEST}) and from each tier's",
        "`tests/test_selfhost_emit_<tier>.py::CORPUS`. Do not edit them here:",
        "change the ledger, then run `make docs-gen`.",
    ]
    return "\n".join(out)


def block_selfhost_residual_docs(current: str, root: Path | None = None) -> str:
    """The residual named document by document, for `docs/selfhost-findings.md`.

    The document this replaces grouped the java residual into families by hand
    and put a count beside each. Every one of those counts, and most of the
    documents, were stale within days: the families it named (realm placement,
    host roots acquired in a component) have since left the ledger entirely.
    The families are worth writing down, and they are written down, in the
    comments of `LOWER_GAP_DOCS` itself, next to the documents they describe,
    where the same edit that moves a document moves its explanation.
    """
    ledger = _literal(NATIVE_CHAIN_TEST, "LOWER_GAP_DOCS", root)
    rows = selfhost_residual(root)
    out: list[str] = []
    for tier, corpus, gap in rows:
        out.append(f"`{tier}`, {gap} residual of {corpus}:")
        out.append("")
        if not gap:
            out.append("- none; the fully-native chain reproduces the whole corpus.")
        else:
            out += [f"- `{doc}`" for doc in ledger[tier]]
        out.append("")
    return "\n".join(out).strip("\n")


BLOCKS: list[tuple[str, str, str, object]] = [
    ("doc-status", "docs/DOC-STATUS.md", "docs/*.md", block_doc_status),
    ("mcp-verbs", "docs/mcp-reference.md", "revl.mcp.server.TOOLS", block_mcp_verbs),
    ("mcp-verb-count", "docs/mcp-reference.md", "revl.mcp.server.TOOLS", block_mcp_count),
    ("agents-mcp-count", "docs/guide-ai-agents.md", "revl.mcp.server.TOOLS",
     block_agents_mcp_count),
    ("authoring-mcp-count", "docs/authoring-for-agents.md", "revl.mcp.server.TOOLS",
     block_authoring_mcp_count),
    ("cli-verbs", "docs/commands-reference.md", "revl.cli.parser.build_parser()",
     block_cli_verbs),
    ("guarantees", "docs/rejections.md", "revl.diagnostics.GUARANTEES",
     block_guarantees_rejections),
    ("guarantees-design", "DESIGN.md", "revl.diagnostics.GUARANTEES",
     block_guarantees_design),
    ("guarantees-humans", "docs/guide-humans.md", "revl.diagnostics.GUARANTEES",
     block_guarantees_humans),
    ("mcp-test-count", "docs/guide-humans.md", "tests/test_mcp.py", block_mcp_test_count),
    ("vision-tiers", "docs/vision.md", "docs/conformance.md per-tier totals",
     block_vision_tiers),
    ("selfhost-residual", "docs/selfhost-compile.md",
     "LOWER_GAP_DOCS + the six emitter corpora", block_selfhost_residual),
    ("selfhost-residual", "docs/selfhost-findings.md",
     "LOWER_GAP_DOCS + the six emitter corpora", block_selfhost_residual),
    ("selfhost-residual-docs", "docs/selfhost-findings.md",
     "LOWER_GAP_DOCS", block_selfhost_residual_docs),
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


# --------------------------------------------------------------------------- #
# The self-host residual, wherever a document states it in prose.              #
# --------------------------------------------------------------------------- #
#
# The generated block above owns the table. This owns everything else: a
# sentence, a bullet, a hand-copied row somewhere the block is not. Issue #1300
# found four statements of one number and three different answers, and two of
# the three wrong ones were prose beside the table rather than the table
# itself, so gating only the block would have left the defect where it was.
#
# `docs/v2.0-roadmap.md` is excluded, for the reason DOC_STATUS_EXCLUDED gives:
# it is the reasoning-of-record, appended to by nearly every PR, and it records
# what was true when an item was written rather than what is true now. Its own
# citations are gated by `tools/check_roadmap_claims.py`.
RESIDUAL_PROSE_EXCLUDED = frozenset({"docs/v2.0-roadmap.md"})

# A paragraph is only read for residual figures when it is about the native
# chain. "Residual" is a word this repository uses for a dozen unrelated
# leftovers (a residual risk, a residual whitespace drift, a residual jail
# gap), and a rule that read all of them would fire on prose it knows nothing
# about.
#
# WHAT THIS CANNOT KNOW, in the spirit of the note at the top of this file: a
# paragraph that states the residual without naming the native chain, the
# ledger or a residual document is not read at all, and no regular expression
# over prose can promise otherwise. The claim here is the narrow one, that no
# figure this gate CAN read disagrees with the ledger. The broad claim is made
# structurally instead, by the generated block: the documents that state the
# residual state it from `LOWER_GAP_DOCS`, so there is nothing left for a
# reader to retype.
_RESIDUAL_ANCHOR = re.compile(
    r"fully[- ]native|native chain|LOWER_GAP_DOCS|residual document")

_RESIDUAL_TOTAL = re.compile(r"\b(\d+)\s+residual(?:\s+documents?\b|s\b)")
_RESIDUAL_SURVIVE = re.compile(
    r"\b(\d+)\s+of\s+(\d+)\s+documents?\s+(?:survive|compile|reproduce|are)")
_RESIDUAL_ONLY = re.compile(r"\bOnly\s+(\d+)\s+survive\b")
_RESIDUAL_TIER = re.compile(
    r"\b(\d+)\s+(py|ts|go|java|rust|wasm)\s+documents?\b")
_RESIDUAL_ROW = re.compile(
    r"^\|\s*(py|ts|go|java|rust|wasm)\s*\|\s*(\d+)\s*\|", re.M)

_RESIDUAL_FIX = (
    "the residual is generated: state it inside the "
    "`<!-- docgen:selfhost-residual -->` block, or drop the figure. "
    f"Source: LOWER_GAP_DOCS in {NATIVE_CHAIN_TEST}."
)


def _paragraphs(text: str):
    """(paragraph, 1-based line number of its first line)."""
    line = 1
    for chunk in re.split(r"\n[ \t]*\n", text):
        yield chunk, line
        line += chunk.count("\n") + 2


def check_residual_claims(root: Path | None = None) -> list[str]:
    """Every hand-typed self-host residual figure agrees with the ledger."""
    base = root if root is not None else ROOT
    rows = selfhost_residual(root)
    per_tier = {t: (c, g) for t, c, g in rows}
    corpus = sum(c for _, c, _ in rows)
    gap = sum(g for _, _, g in rows)
    exact = corpus - gap

    paths = sorted(base.glob("*.md")) + sorted(base.glob("docs/**/*.md"))
    out: list[str] = []
    for path in paths:
        rel = path.relative_to(base).as_posix()
        if rel in RESIDUAL_PROSE_EXCLUDED:
            continue
        for para, line in _paragraphs(path.read_text(encoding="utf-8")):
            if not _RESIDUAL_ANCHOR.search(para):
                continue
            here = f"{rel}:{line}"

            def bad(claim: str, want: int, what: str) -> None:
                out.append(f"{here}: says {claim}, but {what} is {want}. "
                           f"{_RESIDUAL_FIX}")

            for m in _RESIDUAL_TOTAL.finditer(para):
                if int(m.group(1)) != gap:
                    bad(f"`{m.group(0)}`", gap, "the residual")
            for m in _RESIDUAL_SURVIVE.finditer(para):
                if (int(m.group(1)), int(m.group(2))) != (exact, corpus):
                    bad(f"`{m.group(0)}`", exact,
                        f"the number reproduced, of {corpus}")
            for m in _RESIDUAL_ONLY.finditer(para):
                if int(m.group(1)) != exact:
                    bad(f"`{m.group(0)}`", exact, "the number reproduced")
            for m in _RESIDUAL_TIER.finditer(para):
                n, tier = int(m.group(1)), m.group(2)
                tier_corpus, tier_gap = per_tier[tier]
                if n not in (tier_corpus, tier_gap, tier_corpus - tier_gap):
                    bad(f"`{m.group(0)}`", tier_gap,
                        f"the {tier} residual (its corpus is {tier_corpus}, "
                        f"{tier_corpus - tier_gap} reproduced)")
            for m in _RESIDUAL_ROW.finditer(para):
                tier, n = m.group(1), int(m.group(2))
                if n != per_tier[tier][0]:
                    bad(f"a `{tier}` table row opening `| {n} |`",
                        per_tier[tier][0], f"the {tier} corpus")
    return out


CHECKS: list[tuple[str, str, str, object]] = [
    ("residual-claims", "docs/*.md",
     "no prose residual figure disagrees with LOWER_GAP_DOCS",
     check_residual_claims),
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
    """Regenerate (or compare) every block, returning the ones that differed
    from the committed bytes. Ordered so that DOC-STATUS runs last: its em-dash
    counts read the other docs, so it has to see them after this pass has
    rewritten them."""
    changed: list[str] = []
    ordered = sorted(BLOCKS, key=lambda b: b[0] == "doc-status")
    for key, rel, source, render in ordered:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        body = render(extract(text, key))
        updated = splice(text, key, body)
        if updated == text:
            continue
        if write:
            path.write_text(updated, encoding="utf-8")
            changed.append(f"{rel}: block '{key}' regenerated (source: {source})")
        else:
            changed.append(f"{rel}: block '{key}' is stale (source: {source})")
    return changed


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
        written = run_blocks(write=True)
        for line in written:
            print(f"  {line}")
        print(f"docgen: {len(written)} block(s) regenerated."
              if written else "docgen: blocks already current.")
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
        # A stale block is very often NOT this branch's doing. The blocks are a
        # pure function of their sources, so any landing that touches a source
        # re-stales them for every open PR at once -- and this check runs in the
        # required `frontend` job, so the author sees a red they did not cause.
        # Say so here rather than letting each author rediscover it.
        print("", file=sys.stderr)
        print("  NOTE: this may be INHERITED from main rather than caused by "
              "your branch.", file=sys.stderr)
        print("  These blocks are a pure function of their sources, so any "
              "merge that touches", file=sys.stderr)
        print("  a source re-stales them for every open PR. Check main first:",
              file=sys.stderr)
        print("      git fetch origin && git worktree add --detach "
              "/tmp/dg origin/main \\", file=sys.stderr)
        print("        && (cd /tmp/dg && python3 tools/docgen.py --check) "
              "; git worktree remove /tmp/dg", file=sys.stderr)
        print("  If main is stale too, it is main's to fix (a regenerate "
              "commit), not yours.", file=sys.stderr)
    if failures:
        print("docgen: a documentation CHECK FAILED. Either a subcommand or verb "
              "exists in the code with nothing describing it, or a document "
              "states a derived figure the source disagrees with.",
              file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print("  fix: write the missing section or row, or take the figure from "
              "the block that generates it. Never delete the check.",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
