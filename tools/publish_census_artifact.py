#!/usr/bin/env python3
"""Publish the census edition: the artifact, outside this repository (#1268).

`tools/census_artifact.py` renders the census report, and its `--verify` judges a
published copy of one, but neither puts a copy anywhere a reader who has not
cloned this repository can reach. This tool is that last step and nothing else:
it renders the report from the committed records, writes it beside a
human-readable page and a manifest of hashes, and can re-render the whole
edition and diff it byte for byte.

The edition is a COPY and it adds no claim to the ones the report already
carries. Every number on the page is read out of the report that
`tools/census_artifact.py --from-records` renders from `docs/census-artifact/`,
which is the committed record of a run, and `tools/check_eval_report.py` is run
over that report BEFORE anything is written: a report that over-claims is not
published rather than published and noticed.

The edition is NOT committed, and that is the same arrangement as the
playground wheel (`.github/workflows/pages.yml`'s header has the full weighing).
It is built into `site/census-artifact/`, which `.gitignore` ignores, by that
workflow on every push to `main`, from the sha being published, and served at

    https://inso1337.github.io/revl/census-artifact/

A committed copy of a rendered artifact is stale by the time the next change to
its inputs lands, and the inputs here are the corpus, the gate and the
reference. What a reader needs is not a frozen copy but the ability to render
and check one, which is what the commands on the page are for. Nothing this tool
writes is a timestamp or a path outside the repository, so two renders of one
tree are byte-identical and `--check` is exact.

Usage:

    python3 tools/publish_census_artifact.py --write   # site/census-artifact/
    python3 tools/publish_census_artifact.py --check   # re-render, diff, exit 1
    python3 tools/publish_census_artifact.py --json    # the manifest, stdout

`pytest` must be on the path: the reference classifier is IMPORTED from
`tests/test_selfhost_lower.py` (`_reference` in `tools/gate_reference_census.py`
says why it is imported rather than copied), and that module imports pytest.
Nothing else is needed — no venv and no cargo, because rendering reads the
committed records and never runs the gate:

    uv run --no-project --with pytest python3 tools/publish_census_artifact.py --check

Exit status: 0 on success, 1 when `--check` finds a difference, 2 on usage.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "site" / "census-artifact"

# The edition's own schema, and the frozen one its report is written against.
EDITION_SCHEMA = "CENSUS-EDITION-1"

# Where the edition is served and where it comes from. `URL` is the one
# published location; a test reads `.github/workflows/pages.yml` and fails if the
# deploy stops building this edition, so a silent 404 is a red instead.
URL = "https://inso1337.github.io/revl/census-artifact/"
REPO_URL = "https://github.com/inso1337/revl"

# The files an edition is, in the order a reader meets them.
FILES = ("index.html", "README.md", "census-artifact.md", "census-artifact.json",
         "MANIFEST.json")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def render():
    """`(census_artifact, report)`: the report the committed records render.

    The report is held to `tools/check_eval_report.py` here rather than only in
    the suite, so the edition cannot be the first place an over-claim appears.
    """
    artifact = _load("tools/census_artifact.py", "publish_edition_artifact")
    report = artifact.report_from_records()
    checker = _load("tools/check_eval_report.py", "publish_edition_checker")
    violations = checker.check_report(report)
    if violations:
        raise SystemExit(
            "publish_census_artifact: the report this tree renders does not "
            "pass tools/check_eval_report.py, so it is not published:\n  "
            + "\n  ".join(violations))
    return artifact, report


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _esc(value) -> str:
    return html.escape(str(value))


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """A table. Cells are HTML the caller has already escaped or written."""
    out = ["<table>", "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"]
    out += ["<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
            for row in rows]
    out.append("</table>")
    return "\n".join(out)


def _section(anchor: str, title: str, body: str) -> str:
    return (f'<section id="{anchor}">\n<h2>{_esc(title)}</h2>\n{body}\n</section>')


def _claims(report: dict) -> str:
    """The report's public claims, each with the rung its evidence reaches.

    Quoted, not restated: `tools/check_eval_report.py` is what holds a claim to
    the rung it names, and a page that paraphrased them would be outside it.
    """
    out = ['<ul class="claims">']
    for claim in report["claims"]:
        ev = claim["evidence"]
        parts = [f'run <code>{_esc(ev["run"])}</code>',
                 f'protocol <code>{_esc(ev["protocol"])}</code>',
                 f'<code>{_esc(ev["compiler_commit"])}</code>']
        if ev.get("reproduced_by"):
            parts.append(f'reproduced by <code>{_esc(ev["reproduced_by"])}</code>')
        out.append(
            f'<li><span class="rung">{_esc(claim["rung"])}</span> '
            f'{_esc(claim["text"])}'
            f'<div class="evidence">evidence: {"; ".join(parts)}.</div></li>')
    out.append("</ul>")
    return "\n".join(out)


def _mechanism(census: dict) -> str:
    """Why `false-admission` is structurally zero rather than observed zero.

    This is the whole argument (the issue's `What to publish`), so it is driven
    out of the report's own probe rather than asserted: the three moves that
    would record, tolerate or hide a false admission, and what each ran into.
    """
    fa = census["false_admission"]
    mech = fa["mechanism"]
    probe = mech["probe_case"]
    yes = lambda ok: "yes" if ok else "<strong>NO</strong>"  # noqa: E731
    rows = [
        ("record", f"<code>--record</code> over a census whose only finding is "
                   f"<code>{_esc(probe)}</code>",
         f"the member is dropped, not written ({yes(mech['record_refuses_to_write_it'])})"),
        ("check", "a baseline that lists that member",
         f"refused, and named by case id ({yes(mech['check_fails_against_a_baseline_that_lists_it'])})"),
        ("baseline", "the committed baseline, scanned for a never-baselined key",
         f"none present ({_esc(', '.join(mech['committed_baseline_never_baselined_keys']) or 'no keys found')})"),
    ]
    return (
        f'<p>The bucket is <code>{_esc(fa["bucket"])}</code>: the gate issued an '
        f'admission the reference refuses. It sits in <code>NEVER_BASELINED</code>'
        f' (<code>{_esc(", ".join(mech["never_baselined"]))}</code>), so zero '
        f'tolerance here is enforced by the tool and not by an operator. The '
        f'mechanism was DRIVEN for this report, not asserted, and it holds '
        f'({yes(mech["holds"])}).</p>'
        + _table(("move", "what was driven", "outcome"), rows)
        + f'<p>The refusal reads: <code>{_esc(mech["check_message"])}</code></p>'
        + f'<p>This is not a vacuum. The gate issued '
          f'{_esc(fa["issued_admissions_over_the_corpus"])} admissions over this '
          f'corpus, so the arm that could commit a false admission is live. '
          f'{_esc(fa["non_vacuity_note"])}</p>'
          f'<p>Measured members in this run: <strong>{_esc(fa["count"])}</strong>.</p>')


def _allowance(census: dict) -> str:
    """The standing allowance as it stands on the day, every residual named."""
    alw = census["false_admit_allowance"]
    rows = [
        ("measured <code>false-admit</code> members not baselined",
         _esc(alw["measured_but_not_baselined"])),
        ("baselined entries that no longer diverge",
         _esc(alw["baselined_but_not_measured"])),
        ("baselined entries", _esc(alw["baselined"])),
        ("families, by name", _esc(alw["families"])),
        ("agrees with the committed baseline", _esc(alw["agrees_with_committed_baseline"])),
    ]
    if alw["families"]:
        named = _table(("residual", "baselined members"),
                       [(f"<code>{_esc(fam)}</code>", _esc(n))
                        for fam, n in sorted(alw["families"].items())])
    else:
        named = ("<p><strong>The allowance is empty, so no residual is named: "
                 "there is none to name.</strong> Both lists above are empty, so "
                 "this is an empty allowance and not an unmeasured one.</p>")
    return (
        f'<p>The standing allowance is the number of <code>'
        f'{_esc(alw["bucket_prefix"])}</code> divergences this project admits to '
        f'carrying, each one named, each one capped by name in '
        f'<code>{_esc(alw["capped_by_name_in"])}</code>. On this run it is '
        f'<strong>{_esc(alw["total"])}</strong> of {_esc(census["n"])} programs, '
        f'against a committed baseline of <strong>'
        f'{_esc(alw["baselined_total"])}</strong> entries in '
        f'<code>{_esc(alw["baseline_file"])}</code>.</p>'
        + named
        + _table(("what the allowance would have to hold", "on this run"), rows)
        + '<p><code>--check</code> fails in <em>both</em> directions: a measured '
          'member the baseline does not list, and a baseline entry that no longer '
          'diverges. The allowance can only shrink in a diff somebody reads, '
          'which is why it is published as it stands rather than as it will '
          'stand.</p>')


def _provenance(census: dict) -> str:
    """The provenance fraction, beside the tool that measures it."""
    prov = census["provenance"]
    rows = []
    for row in prov["corpora"]:
        rows.append((
            f'<code>{_esc(row["corpus"])}</code>',
            _esc(row["loop_authored"]), _esc(row["human_authored"]),
            _esc(row["total"]),
            f'{_esc(row["independent_percent"])}%',
            f'{_esc(row["floor_percent"])}%',
            _esc(row["undeclared"]),
            ("over the floor" if row["crosses_floor"] else "<strong>below</strong>")))
    return (
        f'<p>A benchmark whose corpus was written by the thing it grades is worth '
        f'nothing, so the fraction of each corpus that is independent of the loop '
        f'is published beside the census rather than after it. Measured by '
        f'<code>{_esc(prov["tool"])}</code> against '
        f'<code>{_esc(prov["manifest"])}</code>, counting only documents that '
        f'declare a generation at or after generation '
        f'{_esc(prov["since_generation"])}.</p>'
        + _table(("corpus", "loop-authored", "human-authored", "total",
                  "independent of the loop", "floor", "undeclared", "verdict"),
                 rows)
        + '<p>The row named <code>census</code> is the corpus the table above is '
          'over. The others are the scoring corpora this project measures the '
          'same way. The two rightmost provenance columns are independent axes: '
          '<code>human-authored</code> is an orthogonal, currently empty list '
          'with no floor, and generation zero is a declaration about the tree as '
          'it stood rather than a measurement of who typed it. The claims above '
          'carry the detail and the report carries the rest.</p>')


def _run(census: dict, report: dict) -> str:
    """Which run, which checker, which engine, and over how many programs."""
    rows = [
        ("programs run (<code>n</code>)", f'<strong>{_esc(census["n"])}</strong>'),
        ("distinct programs", f'<strong>{_esc(census["n_distinct"])}</strong>'),
        ("case ids that appear more than once",
         f'{_esc(len(census["repeated_case_ids"]))}, named in the report'),
        ("run", f'<code>{_esc(census["run"])}</code>'),
        ("checker version", f'<code>{_esc(census["checker_version"])}</code>'),
        ("engine", f'<code>{_esc(census["engine"])}</code>'),
        ("reference", f'<code>{_esc(census["compiler_tree_digest"])}</code>'),
        ("report schema", f'<code>{_esc(report["report_schema"])}</code> under '
                          f'<code>{_esc(report["protocol"])}</code>'),
        ("generator", f'<code>{_esc(report["generator"]["tool"])}</code>'),
        ("grader", f'<code>{_esc(report["grader"]["tool"])}</code> '
                   f'({_esc(report["grader"]["name"])})'),
    ]
    briefs = _table(("frozen gate", "spec", "result", "n"),
                    [(f'<code>{_esc(b["hard_gate"])}</code>',
                      f'<code>{_esc(b["spec"])}</code>',
                      _esc(b["result"]), _esc(b["n"])) for b in report["briefs"]])
    return _table(("what", "value"), rows) + "<p>" + _esc(report["briefs_note"]) \
        + "</p>" + briefs


def _buckets(census: dict) -> str:
    rows = [(f'<code>{_esc(b["bucket"])}</code>', _esc(b["count"]))
            for b in census["buckets"]]
    return (f'<p>{_esc(census["cases_note"])}</p>'
            + _table(("bucket", "count"), rows))


def _reproduction(census: dict) -> str:
    rep = census["reproduction"]
    stale = census["stale_reproduction_records"]
    if rep is None:
        # No record at the current checker version: the section stays, because
        # a reader has to learn the crate claim is unbacked, and the fix is
        # named because `--write` does not write this record (issue #2166).
        version = _esc(census["checker_version"])
        return (
            f'<p>No crate reproduction is recorded at this checker version '
            f'<code>{version}</code>, so the fast engine\'s python mirror of '
            f'the native gate\'s guards is unbacked and every claim in this '
            f'report stands at <code>measured</code> and no higher.</p>'
            f'<p>Re-record it (cargo, minutes): '
            f'<code>python3 tools/regen_generated.py --only census</code>. '
            f'<code>tools/census_artifact.py --write</code> cannot make this '
            f'record: it writes only <code>docs/census-artifact/</code>.</p>'
            + (f'<p>{len(stale)} earlier reproduction record(s) are recorded '
               f'at a checker version this run has moved past, and lift no '
               f'claim.</p>' if stale else ""))
    current = rep["current_checker_version"] == census["checker_version"]
    rows = [
        ("programs", _esc(rep.get("programs", "not recorded"))),
        ("census programs this run read that the reproduction did not",
         _esc(rep["census_programs_not_in_reproduction"])),
        ("crate <code>false-admission</code> members",
         _esc(rep["crate_false_admissions"])),
        ("tracked buckets agree", _esc(rep["crate_tracked_buckets"] == {})),
        ("recorded at checker version",
         f'<code>{_esc(rep["current_checker_version"])}</code>'),
        ("current for this run", "yes" if current else "<strong>no</strong>"),
    ]
    return (f'<p>The fast engine is a python mirror of the native gate\'s guards, '
            f'so it could be wrong in the same direction as the thing it mirrors. '
            f'The reproduction asks the real crate, built by cargo: '
            f'<code>{_esc(rep["by"])}</code>.</p>'
            + _table(("what", "value"), rows)
            + f'<p>{_esc(rep["does_not_establish"])}</p>'
            + (f'<p>{len(stale)} earlier reproduction record(s) are recorded at a '
               f'checker version this run has moved past, and lift no claim.</p>'
               if stale else ""))


def _commands() -> str:
    return (
        '<p>No account, no key, no hosted service. From a checkout:</p>'
        '<pre><code>git clone ' + _esc(REPO_URL) + '.git\n'
        'cd revl\n'
        'python3 -m venv /tmp/revl-venv &amp;&amp; /tmp/revl-venv/bin/pip '
        'install -e . pytest\n'
        '/tmp/revl-venv/bin/python tools/gate_reference_census.py --check\n'
        '/tmp/revl-venv/bin/python tools/corpus_provenance.py --check\n'
        '/tmp/revl-venv/bin/python tools/census_artifact.py --check</code></pre>'
        '<p>The venv goes <strong>outside</strong> the checkout, and that is not '
        'tidiness. The census measures its inputs rather than listing them, '
        'through an audit hook on every file the run opens, so a <code>.venv'
        '</code> inside the clone puts pytest\'s own modules into that set: the '
        'pins gain files no record pins, and <code>--check</code> then fails on '
        '<code>pins.jsonl</code> with UNPINNED INPUT lines that are your '
        'environment and not the census. Anywhere outside the clone works, and '
        'the same three commands pass. The pin set is what the run read.</p>'
        '<p>Those three need python and pytest, and take seconds. <code>pytest'
        '</code> is there because the census imports the reference classifier '
        'from <code>tests/test_selfhost_lower.py</code> instead of copying it, '
        'so it cannot drift into disagreeing with the oracle about what it is '
        'classifying. A passing '
        '<code>--check</code> in your own clone is the whole verification: the '
        'table is yours, not ours. It exits 1 and names each record file that '
        'moved. The crate engine, <code>tools/gate_reference_census.py --engine '
        'crate --check</code>, additionally needs cargo, builds the shipped gate, '
        'and is the slow half.</p>'
        '<p>Three commands ask three different questions, and only one of them '
        'is the gate. <code>--moved-inputs</code> answers "does this diff touch '
        'an input the census hashes?" and exits 0 when it does; that is what CI '
        'asks to decide whether to spend the slow check at all. <code>--check'
        '</code> answers "have the committed records drifted?", cheaply, and it '
        'answers only that: a missing per-checker-version crate reproduction is '
        'not drift and <code>--check</code> cannot see one. <code>--verify '
        '--strict</code> is the verdict — records, pins, the crate reproduction '
        'at this checker version, and the strict flags, all of them, on this '
        'tree. <code>--check</code> is the cheap drift check; <code>--verify '
        '--strict</code> is the verdict; <code>--moved-inputs</code> is what '
        'chooses whether CI runs it.</p>'
        '<p>Holding this copy instead, ask whether its numbers were true on the '
        'inputs they name:</p>'
        '<pre><code>/tmp/revl-venv/bin/python tools/census_artifact.py --verify '
        'census-artifact.json</code></pre>'
        '<p><code>--verify</code> pins every file the verdicts depend on by '
        'sha256 and recomputes every row whose source is byte-identical in your '
        'clone. Its verdicts are <code>reproduced</code> (exit 0), '
        '<code>refuted</code> (1) — the same inputs, a different verdict, a file '
        'that contradicts itself, a false admission, or a mechanism that does '
        'not hold — and <code>different-inputs</code> or <code>partial</code> '
        '(3), which mean your clone is a new measurement rather than a check of '
        'this one.</p>')


def _not_established(census: dict) -> str:
    return ("<ul>" + "".join(f"<li>{_esc(item)}</li>"
                             for item in census["not_established"]) + "</ul>")


def _files() -> str:
    return (
        '<ul class="files">'
        '<li><code>census-artifact.json</code> — the machine copy, '
        '<code>EVAL-REPORT-1</code> with a <code>GATE-CENSUS-1</code> section, '
        'one row per program. This is what <code>--verify</code> takes.</li>'
        '<li><code>census-artifact.md</code> — the same report rendered for a '
        'reader, including the ways of cooking it and what stops each.</li>'
        '<li><code>README.md</code> — how to run and check it from a clone.</li>'
        '<li><code>MANIFEST.json</code> — sha256 and size of every file here, the '
        'run and checker version, and the URL this edition is served at.</li>'
        '</ul>')


CSS = """\
:root { color-scheme: light dark; }
body { margin: 0 auto; max-width: 54rem; padding: 2rem 1.25rem 4rem;
       font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
       Helvetica, Arial, sans-serif; }
h1 { font-size: 1.9rem; margin: 0 0 .25rem; line-height: 1.2; }
h2 { font-size: 1.15rem; margin: 2.25rem 0 .5rem; }
p.lede { font-size: 1.05rem; margin: .25rem 0 1.5rem; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: .87em; }
pre { padding: .75rem 1rem; overflow-x: auto; border: 1px solid #8884;
      border-radius: 4px; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0;
        font-size: .92rem; }
th, td { border: 1px solid #8884; padding: .3rem .5rem; text-align: left;
         vertical-align: top; }
th { font-weight: 600; }
.generated { border: 1px solid #8884; border-radius: 4px; padding: .6rem .9rem;
             font-size: .9rem; }
.rung { display: inline-block; border: 1px solid #8886; border-radius: 3px;
        padding: 0 .35rem; margin-right: .4rem; font-size: .78rem;
        text-transform: uppercase; letter-spacing: .03em; }
ul.claims { list-style: none; padding: 0; }
ul.claims > li { margin: 0 0 1rem; }
.evidence { font-size: .87rem; opacity: .85; margin-top: .2rem; }
footer { margin-top: 3rem; border-top: 1px solid #8884; padding-top: 1rem;
         font-size: .9rem; }
"""


def page(report: dict) -> str:
    """The published page: the table, the mechanism, the provenance, the run."""
    c = report["census"]
    title = "The gate/reference census"
    head = (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{_esc(title)}</title>\n<style>\n{CSS}</style>\n</head>\n<body>')
    lede = (
        f'<h1>{_esc(title)}</h1>'
        f'<p class="lede">revl measuring revl, adversarially: two independently '
        f'written implementations of its own semantics, run over the whole '
        f'corpus, with every disagreement classified — and with the one bucket '
        f'that would let the result be cooked in the direction that matters '
        f'closed by construction rather than by discipline.</p>'
        f'<p class="generated">Generated by '
        f'<code>tools/publish_census_artifact.py</code> from the census records '
        f'committed in <code>docs/census-artifact/</code>, and checked by '
        f'<code>tools/check_eval_report.py</code> before it was written. Do not '
        f'edit: it is regenerated on every push to <code>main</code>. '
        f'<a href="census-artifact.json">The machine copy</a>, '
        f'<a href="census-artifact.md">the same report as markdown</a>, '
        f'<a href="README.md">how to run it</a>, '
        f'<a href="MANIFEST.json">hashes</a>.</p>')
    body = "\n".join([
        _section("claim", "The claim", _claims(report)),
        _section("admission", "The bucket that matters, and why it cannot be "
                              "written", _mechanism(c)),
        _section("allowance", "The standing false-admit allowance",
                 _allowance(c)),
        _section("provenance", "Corpus provenance", _provenance(c)),
        _section("run", "The run this table is", _run(c, report)),
        _section("buckets", "Full bucket table", _buckets(c)),
        _section("reproduction", "The crate reproduction", _reproduction(c)),
        _section("reproduce", "Running it yourself", _commands()),
        _section("files", "This edition", _files()),
        _section("not", "What this does not establish", _not_established(c)),
    ])
    foot = (
        f'<footer><p><a href="{_esc(REPO_URL)}">{_esc(REPO_URL)}</a> · served at '
        f'<code>{_esc(URL)}</code>. The census is regenerated rather than edited: '
        f'the committed record is <code>docs/census-artifact/</code> and '
        f'<code>tools/census_artifact.py --verify --strict</code> is its '
        f'verdict.</p></footer>\n</body>\n</html>\n')
    return head + "\n" + lede + "\n<main>\n" + body + "\n</main>\n" + foot


def readme(report: dict) -> str:
    """The outsider's entry point: what this is, and the commands."""
    c = report["census"]
    alw = c["false_admit_allowance"]
    residual = (", ".join(f"`{fam}`x{n}"
                          for fam, n in sorted(alw["families"].items()))
                if alw["families"] else "none, and the allowance is empty")
    census_prov = next(r for r in c["provenance"]["corpora"]
                       if r["corpus"] == "census")
    return f"""# The gate/reference census

Published at <{URL}>, served from
<{REPO_URL}> by `.github/workflows/pages.yml`.

revl has two independently written implementations of its own semantics:
`src/revl/*.py` is the reference compiler, and `selfhost/*.rvl` compiled into
`crates/revl-gate` is the self-host gate. The census runs both over the same
corpus and classifies every disagreement — on **tag and message**, not merely on
verdict. This is not a comparison against anything else and carries no
comparative claim: it is revl measuring revl.

## What this copy says

| what | value |
|---|---|
| programs run (`n`) | {c["n"]} |
| distinct programs | {c["n_distinct"]} |
| checker version | `{c["checker_version"]}` |
| run | `{c["run"]}` |
| engine | `{c["engine"]}` |
| `false-admission` members | {c["false_admission"]["count"]}, and structurally zero: the bucket is in `NEVER_BASELINED` |
| standing `false-admit` allowance | {alw["total"]} — {residual} ({alw["baseline_file"]}) |
| corpus independent of the loop | {census_prov["independent_percent"]}% of {census_prov["total"]}, by `{c["provenance"]["tool"]}` |

`census-artifact.md` is the whole argument, including the moves that would cook
this result and what stops each. `census-artifact.json` is the machine copy:
an `EVAL-REPORT-1` document under the frozen eval-honesty protocol, with the
census results in its `census` section under `GATE-CENSUS-1`, one row per
program. `MANIFEST.json` carries a sha256 for every file here.

## Running it yourself

No account, no key, no hosted service:

```
git clone {REPO_URL}.git
cd revl
python3 -m venv /tmp/revl-venv && /tmp/revl-venv/bin/pip install -e . pytest
/tmp/revl-venv/bin/python tools/gate_reference_census.py --check
/tmp/revl-venv/bin/python tools/corpus_provenance.py --check
/tmp/revl-venv/bin/python tools/census_artifact.py --check
```

The venv goes **outside** the checkout, and that is not tidiness. The census
measures its inputs rather than listing them, through an audit hook on every file
the run opens, so a `.venv` inside the clone puts pytest's own modules into that
set: the pins gain files no record pins, and `--check` then fails on
`pins.jsonl` with UNPINNED INPUT lines that are your environment and not the
census. Anywhere outside the clone works — `/tmp/revl-venv`,
`$HOME/.venvs/revl` — and the same three commands pass. The pin set is what the
run read; there is no flag that makes a venv inside the checkout invisible, and
none is wanted.

Those three need python and pytest and take seconds. `pytest` is there because
the census imports the reference classifier from `tests/test_selfhost_lower.py`
instead of copying it, so it cannot drift into disagreeing with the oracle about
what it is classifying. The crate engine,
`tools/gate_reference_census.py --engine crate --check`, additionally needs
cargo, builds the shipped gate, and is the slow half.

`--check` re-runs the census and compares it against the committed records,
which every number above is computed from. A passing `--check` in your own clone
is the whole verification: the table is yours, not ours. The corpus is the
repository — the `.rvl` files under fixed directories plus the inline program
lists in `tests/test_selfhost_lower.py` and `tools/gate_reference_census.py` —
so there is no download and no hosted copy to go stale.

## Which of these is the verdict

Three commands, three different questions, and only one of them is the gate:

| command | the question it answers |
|---|---|
| `tools/census_artifact.py --moved-inputs` | given a diff, does it touch an input this census hashes? Exit 0 means yes. This is what CI asks to decide whether to run the slow check at all. |
| `tools/census_artifact.py --check` | have the committed records drifted from what today's tree produces? Cheap, and it answers only that: a missing per-checker-version crate reproduction is not drift, and `--check` cannot see one. |
| `tools/census_artifact.py --verify --strict` | the verdict. The records, the pins, the crate reproduction at this checker version, and the strict flags, all of them, on this tree. |

`--check` is the cheap drift check. `--verify --strict` is the verdict.
`--moved-inputs` is what chooses whether CI runs it.

## Checking THIS copy rather than reproducing it

`--check` asks whether this file is what today's tree produces, and it fails as
soon as the corpus grows. A reader holding this copy needs a different question
answered: were the numbers true on the inputs they name?

```
/tmp/revl-venv/bin/python tools/census_artifact.py --verify census-artifact.json
```

`--verify` re-runs the census in your clone and compares in two steps: the pins,
every file the verdicts depend on, each by sha256; then the verdicts, one row per
program, recomputing every row whose source is byte-identical in your clone. The
verdicts are `reproduced` (exit 0), `refuted` (1: same inputs, a different
verdict, a self-contradicting file, a false admission, or a mechanism that does
not hold), and `partial` or `different-inputs` (3: your clone is a new
measurement rather than a check of this one). The pinned files are measured
rather than listed, through an audit hook on every file the run opens, so a file
your run reads that this copy does not pin is named as an UNPINNED INPUT.

The same check against the records this edition was rendered from, with the
strict flags CI uses, is:

```
/tmp/revl-venv/bin/python tools/census_artifact.py --verify --strict
```

It exits 0 only if the committed records are current *and* a crate reproduction
exists at this checker version. That is the verdict; the `--check` above is the
cheap drift check.

## What this does not establish

{c["not_established"][0]}

The rest is in the report's own `What this does not establish` section, which is
part of the artifact rather than commentary on it:

```
/tmp/revl-venv/bin/python tools/census_artifact.py --from-records | sed -n '/What this does not establish/,$p'
```
"""


def manifest(files: dict[str, str], report: dict) -> str:
    """The manifest: what each file hashes to, and what the edition is."""
    c = report["census"]
    payload = {
        "schema": EDITION_SCHEMA,
        "url": URL,
        "source": {
            "repository": REPO_URL,
            "records": "docs/census-artifact/",
            "rendered_by": "tools/census_artifact.py --from-records",
            "built_by": "tools/publish_census_artifact.py",
            "checked_by": "tools/check_eval_report.py",
            "regenerated_by": ".github/workflows/pages.yml on every push to main",
        },
        "report": {"schema": report["report_schema"],
                   "protocol": report["protocol"]},
        "census": {
            "run": c["run"],
            "engine": c["engine"],
            "checker_version": c["checker_version"],
            "reference": c["compiler_tree_digest"],
            "n": c["n"],
            "n_distinct": c["n_distinct"],
            "repeated_case_ids": len(c["repeated_case_ids"]),
            "false_admission_members": c["false_admission"]["count"],
            "false_admit_allowance": c["false_admit_allowance"]["total"],
            "pins": {group: len(pinned) for group, pinned
                     in sorted(c["pins"].items()) if isinstance(pinned, dict)},
            "provenance_tool": c["provenance"]["tool"],
        },
        "files": {name: {"sha256": _sha256(files[name]),
                         "bytes": len(files[name].encode("utf-8"))}
                  for name in FILES if name in files},
        "verify": {
            "reproduce": "python3 tools/census_artifact.py --check",
            "this_copy": "python3 tools/census_artifact.py --verify "
                         "census-artifact.json",
            "reproduce_this_edition": "python3 tools/publish_census_artifact.py "
                                      "--check",
        },
        "note": ("A copy, not a new measurement: every number here is rendered "
                 "from the committed records and adds no claim to the ones the "
                 "report carries. The edition is not committed to the "
                 "repository; it is built from the sha being published, and "
                 "nothing in it is a timestamp, so two renders of one tree are "
                 "byte-identical."),
    }
    return json.dumps(payload, indent=1, sort_keys=True) + "\n"


def edition(artifact, report: dict) -> dict[str, str]:
    """The edition as `{filename: text}`, in the order `FILES` names it."""
    files = {
        "index.html": page(report),
        "README.md": readme(report),
        "census-artifact.md": artifact.render_markdown(report) + "\n",
        "census-artifact.json": artifact._serialise(report),
    }
    files["MANIFEST.json"] = manifest(files, report)
    return files


def write(files: dict[str, str], dest: Path) -> int:
    """Replace `dest` with this edition."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in FILES:
        (dest / name).write_text(files[name], encoding="utf-8")
    manifest_ = json.loads(files["MANIFEST.json"])
    census = manifest_["census"]
    print(f"publish_census_artifact: wrote {len(files)} files to "
          f"{dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}")
    print(f"  run {census['run']}, checker {census['checker_version']}, "
          f"n {census['n']} ({census['n_distinct']} distinct)")
    print(f"  false-admission members {census['false_admission_members']}, "
          f"standing allowance {census['false_admit_allowance']}")
    print(f"  served at {URL}")
    return 0


def check(files: dict[str, str], dest: Path) -> int:
    """Does `dest` hold exactly what this tree renders?"""
    dest = Path(dest)
    if not dest.is_dir():
        print(f"publish_census_artifact: {dest} does not exist; render it with "
              f"--write")
        return 1
    problems = []
    for name in FILES:
        path = dest / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif path.read_text(encoding="utf-8") != files[name]:
            problems.append(f"{name} is not what this tree renders")
    for extra in sorted(p.name for p in dest.iterdir()
                        if p.is_file() and p.name not in FILES):
        problems.append(f"{extra} is in the edition and this tool does not "
                        f"write it")
    if problems:
        print("publish_census_artifact: the edition does not match this tree:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"publish_census_artifact: the edition in "
          f"{dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest} is "
          f"what this tree renders")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true",
                      help=f"render the edition into --dest (default {DEST})")
    mode.add_argument("--check", action="store_true",
                      help="re-render and compare with --dest, byte for byte")
    mode.add_argument("--json", action="store_true",
                      help="print the manifest and stop")
    ap.add_argument("--dest", type=Path, default=DEST,
                    help=f"where the edition lives (default: {DEST})")
    args = ap.parse_args(argv)

    artifact, report = render()
    files = edition(artifact, report)
    if args.json:
        sys.stdout.write(files["MANIFEST.json"])
        return 0
    return write(files, args.dest) if args.write else check(files, args.dest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
