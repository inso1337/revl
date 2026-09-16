"""No `.rvl` in this repository carries a frontend document inside a string.

Item 459's exit sentence ends "... and no JS extracted from revl strings". Until
now that conjunct was guarded by `test_no_inline_frontend_blob` in
`tests/test_app_frontend_725.py` and `tests/test_webui_entry_asset_ref_459.py`:
a seven-entry substring blacklist (`<!doctype`, `<!DOCTYPE`, `<html`, `<script`,
`<style`, `<div`, `<template`) applied to ONE emitted artifact. `<span`,
`<a href=`, `onclick=` and `document.createElement` all pass it, and 1,033 other
`.rvl` files were never looked at.

`docs/design/459-frontend-asset-integration.md` says why that shape is the wrong
one, about the template slot grammar: a form is refused "as a SHAPE, not as a
blacklist of the raw forms somebody thought of". A blacklist is a list of what
its author remembered. This file states the property instead.

Those two tests are NOT deleted and NOT weakened. Each keeps its seven cases and
gains one leg that runs this detector over the same artifact, so the artifact is
now covered by both the literal list and the shape. This file is the repo-wide
half they never had.


THE RULE, in full, because a gate that cannot be argued with is a gate nobody
can maintain
--------------------------------------------------------------------------------

A revl string is an EMBEDDED FRONTEND DOCUMENT when its text has the structure
of a markup document. Structure, here, means closed elements arranged in a tree
— not the presence of an angle bracket, and not the name of any particular tag.
Nothing below asks what an element is called, so there is no tag somebody can
reach for to get underneath it.

The unit of judgement is one string literal: a single-quoted, double-quoted or
triple-quoted revl string, or one backtick template with its `${...}` holes removed and its literal
chunks rejoined (a template is judged whole, because `` `<div>${x}</div>` ``
splits its markup across the holes and each chunk on its own is nothing). Every
`.rvl` file is ALSO judged on the concatenation of all of its literals, so
chopping a document into a hundred small strings does not get underneath rules
A and B.

An ELEMENT PAIR is an `<name ...>` matched by a later `</name>`, found by
popping a stack — the same matching an HTML parser does, minus the spec's
implied end tags. A pair is NESTED when it closes while another pair's opener is
still on the stack.

  Rule A — TREE.  Four or more element pairs whose names are not all the same,
      with nesting depth 2 or more. Four distinct closed elements arranged in a
      tree is a page fragment; it is not an illustration and it is not a
      sentence that happens to contain a bracket.

  Rule B — MASS.  Twelve or more element pairs, at any depth and under any
      names. A two-hundred-row table built by concatenation is a document even
      though it only ever names `<tr>` and `<td>`.

  Rule C — DECLARATION.  A document type declaration (`<!doctype`, any case).
      This is one literal string and it is deliberately not derived from
      anything: `<!doctype html>` is the sentence "what follows is an HTML
      document", so a string containing it is a document by its own statement.

  Rule D — INTERPOLATION INTO MARKUP.  One or more element pairs in a TEMPLATE,
      with at least one `${...}` hole landing inside the markup: inside a tag's
      attribute list, or between a pair's opener and its closer. The bar is one
      pair rather than four because this is not a size question. Interpolating a
      value into markup assembled in a string is the escaping decision that
      `stdlib/template.rvl` exists to take out of the author's hands, and it is
      the exact defect the sibling harness repo shipped: five sites
      concatenating server-supplied text into `innerHTML`, contradicting a rule
      stated a few hundred lines earlier in the same file.

WHAT IS ALLOWED, stated positively so it is not left to inference:

  - Angle brackets in prose, diagnostics and usage lines. `truc <add NAME | rm
    NAME | ...>` in `src/revl/truc/components/assembler.rvl` has metavariables
    in brackets and nothing closes, so there is no pair and no rule applies.
  - Foreign-language type syntax. `selfhost/emit_java.rvl` and
    `selfhost/emit_rust.rvl` are built out of `java.util.List<T>`, `Vec<T>` and
    `Box<dyn ...>`. Generic arguments never produce a `</T>`, so they never
    produce a pair. This matters more than it sounds: this repository contains
    six emitters that legitimately assemble foreign source out of revl strings,
    so "no code in a string" is a rule revl cannot hold. "No document in a
    string" is one it can.
  - A documentation example that shows an element of output. `"renders as
    <li>the escaped row text</li>"` is one pair with nothing inside it: no tree,
    no mass, no declaration, no hole.
  - The placeholder markers the self-hosted emitters use for an un-lowered
    construct, `<<DEFER-...>>` and `<<UNSUPPORTED-...>>`. `<<DEFER` is not a tag
    shape (`<` then `<`), and nothing closes it.

  `tests/fixtures/angle_brackets_not_a_document_1120.rvl` is all six of those in
  one file. It is scanned by the repo-wide gate like any other `.rvl`, on no
  skip list; it passing is the assertion.

WHAT THIS MISSES, which is the part a blacklist never says about itself:

  1. A pure JavaScript blob with no markup in it at all — a string of
     `document.createElement` calls and nothing else. There is no structural
     property that separates that from the TypeScript `selfhost/emit_ts.rvl`
     emits by design, and a gate that fired on both would be removed within a
     week. This is a known hole, stated rather than papered over.
  2. Markup assembled by `+` across several `"..."` strings, where each one
     holds less than a pair. The per-file aggregate catches the large version of
     this (rules A and B are applied to the concatenation of a file's literals),
     but a few dozen fragments spread over a file whose total is under the
     thresholds will pass.
  3. Markup produced by a helper — `el("div", el("span", x))` — which is a
     function call tree, not a string, and is not this gate's subject.
  4. Markup that is never closed. An unclosed `<div>` produces no pair, so a
     string of nothing but opening tags falls only to rule C, if it declares
     itself. Rules A, B and D all rest on the closer, because the closer is what
     makes the shape a tree rather than a sentence with a bracket in it.
  5. A `.rvl` that does not lex. Three files under `bench/results/` are model
     output that does not, and for those the whole file text is judged as one
     candidate instead — a superset of its strings, so the fallback can only
     over-report, never skip. `test_the_scan_reaches_the_files_that_do_not_lex`
     keeps that path honest.

MEASURES CONSIDERED AND NOT USED. A ratio of markup characters to prose
characters: it makes a minified document (nearly all markup, no prose) and a
comment-heavy one score at opposite ends for no reason the rule could explain.
Raw nesting depth on its own: `Map<String, List<Map<String, V>>>` in
`selfhost/emit_java.rvl` reaches depth 4 with zero closers, so depth without
pairing measures generics. Pairing is what separates the two, and every rule
here is built on it.


PROOF THAT IT CAN FAIL
----------------------

`tests/fixtures/embedded_frontend_document_1120.rvl` is a reduced transcription
of `src/components/web_page.rvl` in the sibling harness repository — a whole
page, stylesheet and script inside one revl template with values interpolated
into it. It is a real, compiling `.rvl` file on disk, so the planted violation
goes through the same directory walk, the same lexer and the same predicate as
everything else; it is not a string handed straight to the function. It is the
ONLY path the repo-wide scan skips, and `test_the_skip_list_is_one_proven_entry`
asserts the list has exactly that one entry and that the detector fires on it
under every rule it is supposed to. A detector that stopped detecting reds
there before it could pass over a clean tree.

`test_each_rule_fires_on_its_own` drives all four rules independently, so a rule
cannot rot behind another rule that happens to cover the same fixture.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.lexer import lex  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PLANTED = FIXTURES / "embedded_frontend_document_1120.rvl"
CONTROL = FIXTURES / "angle_brackets_not_a_document_1120.rvl"

#: The only `.rvl` the repo-wide scan skips, and it is skipped because it is a
#: violation on purpose. Adding an entry here means claiming a second file is
#: allowed to hold a frontend document; `test_the_skip_list_is_one_proven_entry`
#: makes that claim expensive, which is the point.
SKIPPED = frozenset({PLANTED.relative_to(ROOT).as_posix()})

#: Thresholds for rules A and B. Named so a future argument about them is an
#: argument about two numbers and not about the shape of the detector.
TREE_PAIRS = 4          # rule A: element pairs, not all the same name
TREE_DEPTH = 2          # rule A: nesting depth
MASS_PAIRS = 12         # rule B: element pairs at any depth, any names

#: Where a `${...}` hole sat, once the template's literal chunks are rejoined.
#: NUL cannot appear in revl source (the lexer would have to carry it through a
#: literal), so it cannot be forged by the text being judged.
HOLE = "\x00"

#: An element tag. The name is read but never compared against a list of names:
#: only whether an opener is later closed matters. Attribute text may not
#: contain `<` or `>`, which loses a tag whose attribute VALUE holds a bracket
#: and is the conservative direction (a lost tag can only lower a score).
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9._:-]*)([^<>]*)>", re.S)

_DOCTYPE = re.compile(r"<!\s*doctype", re.I)


@dataclass(frozen=True)
class Shape:
    """What the tag stack found in one candidate text."""

    pairs: int = 0                      # closed elements
    names: frozenset[str] = frozenset()  # distinct names among them
    depth: int = 0                      # deepest pair-inside-pair nesting
    hole_in_markup: bool = False        # a `${...}` inside a tag or a pair
    doctype: bool = False


def shape_of(text: str) -> Shape:
    """Match `<name ...>` openers to `</name>` closers with a stack.

    The walk is the one an HTML parser does minus the spec's implied end tags:
    a closer pops to the nearest opener of the same name and discards whatever
    sat between them unclosed. `<b><i></b>` therefore yields one pair (`b`),
    which is the reading that under-counts, and under-counting is the safe
    direction for a gate whose failure mode to avoid is the false alarm.
    """
    stack: list[tuple[str, int, int]] = []   # (name, uid, index just past `>`)
    closed: list[tuple[str, int, tuple[int, ...], int, int]] = []
    uid = 0
    for m in _TAG.finditer(text):
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            if any(entry[0] == name for entry in stack):
                while stack[-1][0] != name:
                    stack.pop()
                _, own, open_end = stack.pop()
                ancestors = tuple(entry[1] for entry in stack)
                closed.append((name, own, ancestors, open_end, m.start()))
        elif not attrs.rstrip().endswith("/"):
            stack.append((name, uid, m.end()))
            uid += 1
    if not closed:
        return Shape(doctype=bool(_DOCTYPE.search(text)))

    paired = {own for _, own, _, _, _ in closed}
    depth = max(
        1 + sum(1 for a in ancestors if a in paired)
        for _, _, ancestors, _, _ in closed
    )
    # A hole inside a tag's attribute list, or between a pair's opener and its
    # closer. Both are positions where an interpolated value becomes markup.
    hole = HOLE in text and (
        any(HOLE in m.group(0) for m in _TAG.finditer(text))
        or any(
            HOLE in text[open_end:close_start]
            for _, _, _, open_end, close_start in closed
        )
    )
    return Shape(
        pairs=len(closed),
        names=frozenset(name for name, _, _, _, _ in closed),
        depth=depth,
        hole_in_markup=hole,
        doctype=bool(_DOCTYPE.search(text)),
    )


def rules_fired(shape: Shape) -> tuple[str, ...]:
    """Which of A/B/C/D this shape trips. Empty means it is not a document."""
    fired = []
    if shape.pairs >= TREE_PAIRS and len(shape.names) > 1 and shape.depth >= TREE_DEPTH:
        fired.append("A")
    if shape.pairs >= MASS_PAIRS:
        fired.append("B")
    if shape.doctype:
        fired.append("C")
    if shape.pairs >= 1 and shape.hole_in_markup:
        fired.append("D")
    return tuple(fired)


@dataclass
class Finding:
    path: str
    line: int | None
    rules: tuple[str, ...]
    shape: Shape
    excerpt: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return (
            f"{where}: embedded frontend document "
            f"(rule {'+'.join(self.rules)}; {self.shape.pairs} closed elements, "
            f"{len(self.shape.names)} distinct, depth {self.shape.depth}"
            f"{', interpolated' if self.shape.hole_in_markup else ''}"
            f"{', doctype' if self.shape.doctype else ''})\n"
            f"    {self.excerpt}"
        )


@dataclass
class Scan:
    """One pass over a set of `.rvl` files, with its own denominator."""

    files: int = 0
    lexed: int = 0
    fell_back: list[str] = field(default_factory=list)
    strings: int = 0
    findings: list[Finding] = field(default_factory=list)


def _candidates(source: str, filename: str) -> list[tuple[int, str]]:
    """Every revl string literal in `source`, as (line, text-with-holes-marked).

    A `template` token is rejoined into ONE candidate with `HOLE` standing where
    each `${...}` was: the markup of `` `<li>${row}</li>` `` only exists once the
    chunks are back together. `hostbody` tokens are deliberately not candidates —
    a `@ts { ... }` body is foreign source behind the extern jail
    (`tests/test_extern_host_jail_residuals_412.py`), not a revl string, and
    item 459's clause is about revl strings.
    """
    out: list[tuple[int, str]] = []
    for tok in lex(source, filename):
        if tok.kind == "string":
            out.append((tok.line, tok.value))
        elif tok.kind == "template":
            out.append((
                tok.line,
                "".join(v if k == "text" else HOLE for k, v in tok.value),
            ))
    return out


def scan_text(text: str, path: str, line: int | None = None) -> Finding | None:
    """The predicate on one candidate. Public: the artifact gates call this."""
    shape = shape_of(text)
    fired = rules_fired(shape)
    if not fired:
        return None
    excerpt = " ".join(text[:160].split())
    return Finding(path, line, fired, shape, excerpt)


def scan_file(path: Path, display: str | None = None) -> tuple[list[Finding], int, bool]:
    """Scan one `.rvl`. Returns (findings, strings examined, lexed cleanly).

    A file that does not lex is judged as one whole-text candidate. That is a
    superset of its string literals, so the fallback over-reports rather than
    skipping — see miss 5 in this module's docstring.
    """
    name = display or path.as_posix()
    source = path.read_text(encoding="utf-8")
    try:
        candidates = _candidates(source, str(path))
    except Exception:
        found = scan_text(source, name)
        return ([found] if found else []), 0, False

    findings = [
        f for line, text in candidates
        if (f := scan_text(text, name, line)) is not None
    ]
    # The same file judged whole, so a document chopped into small strings still
    # has to clear rules A and B in aggregate. ONLY A and B: rule C would just
    # restate a per-string hit, and rule D across a join is meaningless — a hole
    # in one string and a pair in another never sat next to each other in the
    # source. Reported only when no individual string already did, so one
    # document is one finding rather than two.
    if not findings:
        aggregate = shape_of("\n".join(t for _, t in candidates))
        scale = tuple(r for r in rules_fired(aggregate) if r in ("A", "B"))
        if scale:
            findings.append(Finding(
                name, None, scale, aggregate,
                f"{aggregate.pairs} closed elements spread across "
                f"{len(candidates)} string literals in this file",
            ))
    return findings, len(candidates), True


def scan_tree(root: Path, skip: frozenset[str] = SKIPPED) -> Scan:
    """Every `.rvl` under `root` except the proven-violation fixture."""
    scan = Scan()
    for path in sorted(root.rglob("*.rvl")):
        if ".git" in path.parts:
            continue
        display = path.relative_to(root).as_posix()
        if display in skip:
            continue
        scan.files += 1
        findings, strings, lexed = scan_file(path, display)
        scan.strings += strings
        if lexed:
            scan.lexed += 1
        else:
            scan.fell_back.append(display)
        scan.findings.extend(findings)
    return scan


@pytest.fixture(scope="module")
def scan() -> Scan:
    return scan_tree(ROOT)


# --- the gate --------------------------------------------------------------- #
def test_no_rvl_in_the_repository_embeds_a_frontend_document(scan):
    """The claim. Read this module's docstring before changing a threshold.

    If a new `.rvl` legitimately needs markup, the answer is an asset: a real
    `.html`/`.ts`/`.vue` file referenced through the item-459 asset handle, which
    a bundler, a source map and `vue-tsc` can all see. If it needs a value in
    markup, the answer is `stdlib/template.rvl`, whose slot grammar takes the
    escaping decision away from the call site.
    """
    assert not scan.findings, (
        "a frontend document is embedded in revl source:\n"
        + "\n".join(str(f) for f in scan.findings)
    )


# --- anti-vacuity: the scan has a denominator -------------------------------- #
def test_the_scan_examined_the_tree_it_claims_to_have_examined(scan):
    """A gate over zero files passes. The floors are far below today's counts
    (1,033 files / ~15,900 strings) so ordinary churn does not touch them; they
    exist so a broken glob or a lexer change that raises on everything reds here
    instead of reporting a clean tree."""
    assert scan.files >= 500, f"only {scan.files} .rvl files were scanned"
    assert scan.strings >= 5_000, f"only {scan.strings} strings were examined"
    assert scan.lexed >= scan.files - 25, (
        f"{scan.files - scan.lexed} of {scan.files} files fell back to the "
        "whole-text scan. That path is for the handful of unlexable bench "
        "results; at this rate the lexer or the corpus changed shape and the "
        "gate is no longer reading strings."
    )


def test_the_scan_reaches_strings_that_do_contain_angle_brackets(scan):
    """The other half of the denominator. A scan whose extraction silently
    returned empty text for every string would satisfy the counts above and
    still detect nothing, so assert it reaches the corpus's real bracket-bearing
    strings — the generics in the self-hosted emitters."""
    emitter = ROOT / "selfhost" / "emit_java.rvl"
    assert emitter.is_file(), "selfhost/emit_java.rvl moved; re-derive this test"
    texts = [t for _, t in _candidates(emitter.read_text(encoding="utf-8"), str(emitter))]
    bracketed = [t for t in texts if _TAG.search(t)]
    assert len(bracketed) >= 20, (
        f"only {len(bracketed)} strings in selfhost/emit_java.rvl carry a "
        "tag-shaped token; the extraction is not seeing the text it scans."
    )
    # ... and none of them is a document. This is the control at repo scale.
    assert not [t for t in bracketed if rules_fired(shape_of(t))]


def test_the_scan_reaches_the_files_that_do_not_lex(scan):
    """The fallback is a real path, not a `try` nobody enters. Some bench
    results are model output that does not lex; they are judged whole rather
    than skipped, and they are still clean."""
    assert scan.fell_back, (
        "no file took the unlexable fallback. If every .rvl now lexes that is "
        "good news, but check the fallback still works before deleting this: "
        "a scan that silently swallows a lexer error detects nothing."
    )
    assert not [f for f in scan.findings if f.path in set(scan.fell_back)]


# --- proof that it can fail -------------------------------------------------- #
def test_the_skip_list_is_one_proven_entry():
    """The only file the repo-wide scan skips is the planted violation, and it
    is required to BE a violation. This is what stops the gate from being
    quietly emptied: a second entry has to be argued for here, and the one entry
    has to keep firing."""
    assert SKIPPED == {PLANTED.relative_to(ROOT).as_posix()}, (
        "the skip list grew. A `.rvl` that needs to be skipped is a `.rvl` that "
        "holds a frontend document; move the markup into an asset instead."
    )
    assert PLANTED.is_file(), (
        f"{PLANTED.relative_to(ROOT)} is gone, so the gate has nothing proving "
        "it can fail. Restore it or replace it with another planted violation."
    )


def test_the_planted_violation_is_caught_by_the_whole_pipeline():
    """Through the same directory walk, lexer and predicate as everything else —
    not by handing a string to `scan_text`."""
    scanned = scan_tree(ROOT, skip=frozenset())
    hits = [f for f in scanned.findings if f.path == PLANTED.relative_to(ROOT).as_posix()]
    assert hits, (
        f"{PLANTED.relative_to(ROOT)} is a full HTML document inside one revl "
        "template and the detector did not fire on it. The gate is vacuous "
        "until this reds."
    )
    fired = set().union(*(set(f.rules) for f in hits))
    assert {"A", "B", "C", "D"} <= fired, (
        f"the planted violation only tripped rules {sorted(fired)}. It is "
        "written to trip all four; a rule that stopped firing on it is a rule "
        "that has stopped working."
    )


def test_the_planted_violation_is_the_only_thing_the_skip_hides():
    """Skipping it must change the verdict and nothing else. If the tree were
    dirty elsewhere, the two scans would differ by more than this one file."""
    with_it = scan_tree(ROOT, skip=frozenset())
    without = scan_tree(ROOT)
    assert {f.path for f in with_it.findings} == {PLANTED.relative_to(ROOT).as_posix()}
    assert without.findings == []
    assert with_it.files == without.files + 1


def test_the_per_file_aggregate_catches_a_document_chopped_into_fragments(tmp_path):
    """The other path that can fire, driven on its own. No single string here
    reaches a threshold — the largest holds one pair — and the file does. This
    is what stops "split it across twenty strings" from being the answer to the
    gate."""
    fragments = [
        "<section><h2>a</h2></section>",
        "<article><p>b</p></article>",
        "<aside><span>c</span></aside>",
    ]
    for one in fragments:
        assert rules_fired(shape_of(one)) == (), one
    doc = tmp_path / "chopped.rvl"
    body = "\n".join(f'    fn f{i}() = "{s}"' for i, s in enumerate(fragments))
    doc.write_text(
        "service S {\n"
        + "\n".join(f"  fn f{i}() -> Str" for i in range(len(fragments)))
        + "\n}\n\ncomponent C provides s: S {\n  provide s {\n"
        + body
        + "\n  }\n}\n",
        encoding="utf-8",
    )
    findings, strings, lexed = scan_file(doc, "chopped.rvl")
    assert lexed and strings == len(fragments)
    assert findings and findings[0].rules == ("A",), findings
    assert findings[0].line is None, "an aggregate finding names the file, not a line"


def test_the_control_passes_and_is_not_skipped(scan):
    """A legitimate `.rvl` whose every string carries angle brackets — usage
    lines, diagnostics, `<<DEFER-...>>` markers, Java and Rust generics, and a
    one-element documentation example. It is scanned like any other file."""
    assert CONTROL.is_file()
    display = CONTROL.relative_to(ROOT).as_posix()
    assert display not in SKIPPED
    findings, strings, lexed = scan_file(CONTROL, display)
    assert lexed and strings >= 6
    assert not findings, "\n".join(str(f) for f in findings)
    # and it really is bracket-bearing, so passing is not passing on nothing.
    texts = [t for _, t in _candidates(CONTROL.read_text(encoding="utf-8"), str(CONTROL))]
    assert sum(1 for t in texts if _TAG.search(t)) >= 4


# --- each rule, driven on its own -------------------------------------------- #
#
# Four rules with one fixture between them is three rules that could rot
# unnoticed. Each case below is minimal: it trips exactly one rule and the
# near-miss beside it trips none.

_RULE_CASES = {
    "A": "<section><h2>t</h2><p>a <em>b</em> c</p></section>",
    # one name, flat: under rule A on both counts, over rule B on mass alone
    "B": "<tr>a row</tr>" * 12,
    "C": "<!DOCTYPE html>",
    "D": f"<li>{HOLE}</li>",
}

_NEAR_MISSES = {
    # three pairs, nested, but one name short of rule A
    "three closed elements": "<ul><li>one</li><li>two</li></ul>",
    # four pairs, four names, but flat: no tree
    "four flat elements": "<b>a</b><i>b</i><s>c</s><u>d</u>",
    # deep, distinct, and nothing closes: generics, not markup
    "generics": "java.util.Map<String, java.util.List<java.util.Map<String, V>>> m",
    # a hole next to markup but not inside it
    "hole outside the markup": f"{HOLE} rendered as <li>row</li>",
    # a closer with no opener
    "stray closer": "</div> is how it ends",
    # the emitters' placeholder markers
    "defer marker": "<<DEFER-embedded-document-lowering>>",
}


@pytest.mark.parametrize("rule", sorted(_RULE_CASES))
def test_each_rule_fires_on_its_own(rule):
    assert rules_fired(shape_of(_RULE_CASES[rule])) == (rule,), (
        f"rule {rule}'s minimal case now fires {rules_fired(shape_of(_RULE_CASES[rule]))}"
    )


@pytest.mark.parametrize("name", sorted(_NEAR_MISSES))
def test_the_near_misses_stay_misses(name):
    """Each of these sits just under a threshold on purpose. They are the cost
    of the rule: raising a threshold to catch something must be checked against
    them, and lowering one to catch something reds here first."""
    assert rules_fired(shape_of(_NEAR_MISSES[name])) == ()


#: The list this file supersedes, as both artifact gates still spell it. Kept
#: here so the claim "the shape catches what the list cannot" is checked against
#: the real list rather than against a copy that drifted.
SUPERSEDED_BLACKLIST = ("<!doctype", "<!DOCTYPE", "<html", "<script", "<style", "<div")

_ARTIFACT_GATES = (
    ROOT / "tests" / "test_app_frontend_725.py",
    ROOT / "tests" / "test_webui_entry_asset_ref_459.py",
)


def test_the_substring_list_is_still_there_and_still_says_what_it_said():
    """The two artifact gates were WIDENED, not replaced: each keeps its
    parametrized list and gained a leg that runs `scan_text` over the same
    artifact. If someone deletes the list, this reds — the list is a better
    error message than the shape when it does hit."""
    for gate in _ARTIFACT_GATES:
        src = gate.read_text(encoding="utf-8")
        assert "def test_no_inline_frontend_blob(" in src, (
            f"{gate.name} no longer carries test_no_inline_frontend_blob"
        )
        for form in SUPERSEDED_BLACKLIST:
            assert f'"{form}"' in src, f"{gate.name} dropped {form!r}"
        assert "def test_no_embedded_frontend_document_in_the_artifact(" in src, (
            f"{gate.name} no longer runs the structural detector over its "
            "artifact, so that artifact is back to the substring list alone."
        )


def test_the_shape_catches_what_the_substring_list_cannot():
    """Why this file exists. A page built out of `<section>`, `<header>`,
    `<span>` and `<a href=...>` — with an inline event handler on it — contains
    none of the six strings the artifact gates look for, and is a document."""
    evasion = (
        '<section><header><span>Signed in as </span>'
        '<a href="/me" onclick="go()">you</a></header>'
        '<ul><li><span>row</span></li></ul></section>'
    )
    assert not [f for f in SUPERSEDED_BLACKLIST if f in evasion], (
        "the sample was written to evade the substring list; it no longer does"
    )
    assert rules_fired(shape_of(evasion)) == ("A",)


def test_the_detector_does_not_read_tag_names():
    """The property that separates this from the blacklist it supersedes.
    Renaming every element to something nobody has heard of changes nothing,
    which is what "by structure" has to mean to be worth the file."""
    real = "<section><h2>t</h2><p>a <em>b</em> c</p></section>"
    renamed = (
        "<zork-a><zork-b>t</zork-b><zork-c>a <zork-d>b</zork-d> c</zork-c></zork-a>"
    )
    assert rules_fired(shape_of(real)) == rules_fired(shape_of(renamed)) == ("A",)
    assert shape_of(real).pairs == shape_of(renamed).pairs
