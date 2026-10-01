"""`revl grammar --format lark|gbnf|ebnf`: a grammar of revl source derived from
`revl.parser` (issue #1661).

What is checked here:

* **Drift.** `grammar/` holds the three rendered grammars. They must equal a
  fresh derivation from the parser in this checkout, and a parser change must
  change the derivation.
* **Corpus.** Every tracked `.rvl` document the parser accepts is accepted by
  the Lark grammar, run through llguidance, the engine a constrained decoder
  would use. The GBNF is checked on a sample of small documents with a
  character-level recogniser (`tests/_gbnf_earley.py`), because GBNF has no
  tokenizer and llguidance's GBNF import reads it with lexer semantics.
* **Refusals.** A requirement written inside a component body, which the
  parser refuses, is refused by the grammar in every format and category.

llguidance is imported without `importorskip`: it is in the `test` extra, and a
skipped corpus check reads the same as a passing one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import llguidance
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _gbnf_earley import Grammar, parse_gbnf  # noqa: E402
from revl import source_grammar as sg  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402

REFUSED_PROGRAMS = [
    "service S { fn f(x: Int) -> Int }\ncomponent C { requires X }",
    "component C { requires X }",
    "component C {\n  requires k: S\n}",
    "component C { provides p: S }",
    "componentC requires k: S {}",
    "fn f() -> Int { requires }",
]


class _ByteTokenizer:
    """One token per byte, so a document is fed to the matcher exactly."""

    eos_token_id = 256
    bos_token_id = None
    tokens = [bytes([i]) for i in range(256)] + [b"<eos>"]
    special_token_ids = [256]

    def __call__(self, text):
        return list(text if isinstance(text, bytes) else text.encode())


TOKENIZER = llguidance.LLTokenizer(llguidance.TokenizerWrapper(_ByteTokenizer()), slices=[])


def _compiled(lark_text):
    grammar = llguidance.LLMatcher.grammar_from_lark(lark_text)
    assert llguidance.LLMatcher.validate_grammar(grammar, TOKENIZER) == ""
    return grammar


def _accepts(grammar, text):
    matcher = llguidance.LLMatcher(TOKENIZER, grammar, log_level=0)
    fed = matcher.consume_tokens(list(text.encode()))
    return bool(fed) and not matcher.is_error() and matcher.is_accepting()


def _parses(text, name="doc.rvl"):
    try:
        Parser(text, name).parse()
    except (RevlError, RecursionError):
        return False
    return True


def _parseable_corpus():
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.rvl"],
                            capture_output=True, text=True, check=True).stdout.split()
    out = []
    for name in listed:
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        if _parses(text, name):
            out.append((name, text))
    return listed, out


# ---- drift -----------------------------------------------------------------

def test_committed_grammars_equal_a_fresh_derivation():
    stale = sg.drifted(ROOT)
    assert stale == [], f"run `revl grammar --write`: {stale}"
    assert sorted(p.name for p in (ROOT / "grammar").iterdir()) == [
        "revl.ebnf", "revl.gbnf", "revl.lark"]


def test_a_parser_change_changes_the_derivation():
    source = (ROOT / "src" / "revl" / "parser.py").read_text()
    edited = source.replace('self.expect("kw", "every")', 'self.expect("kw", "everyday")', 1)
    assert edited != source
    rendered = sg.render("lark", derived=sg.derive(edited))
    assert '"everyday"' in rendered
    assert '"everyday"' not in (ROOT / "grammar" / "revl.lark").read_text()


def test_cli_check_reports_no_drift(capsys):
    assert main(["grammar", "--check"]) == 0
    assert "matches" in capsys.readouterr().out


# ---- the Lark grammar under llguidance --------------------------------------

def test_every_parseable_corpus_document_is_accepted_by_the_lark_grammar():
    grammar = _compiled(sg.render("lark"))
    listed, parseable = _parseable_corpus()
    refused = [name for name, text in parseable if not _accepts(grammar, text)]
    # a named count, so an empty corpus cannot pass
    assert len(listed) > 1000 and len(parseable) > 1000, (len(listed), len(parseable))
    assert refused == [], f"{len(refused)} of {len(parseable)} refused: {refused[:20]}"


@pytest.mark.parametrize("text", REFUSED_PROGRAMS)
def test_the_lark_grammar_refuses_what_the_parser_refuses(text):
    assert not _parses(text)
    assert not _accepts(_compiled(sg.render("lark")), text)


CATEGORY_CASES = {
    "expression": (["f(1) + 2", "x.y(z) * 3", "`a ${b} c`"],
                   ["requires", "requires k: S", "let x = 1"]),
    "component-body": (["provide s {\n  fn f() = 1\n}",
                        "let w = effect spawn Child with { } undo w.dispose()",
                        'every 30s { emit log.write("t") }'],
                       ["requires k: S", "requires X", "component C {}"]),
    "statements": (["let x = 1\nreturn x", 'emit log.write("x")'],
                   ["requires k: S", "component C {}"]),
    "type": (["Int", "List[Int]", "Map[Str, List[Int]]"], ["requires", "1 + 2"]),
}


@pytest.mark.parametrize("category", sorted(CATEGORY_CASES))
def test_a_category_grammar_admits_its_slice_and_refuses_requires(category):
    grammar = _compiled(sg.render("lark", category))
    accepted, refused = CATEGORY_CASES[category]
    assert [t for t in accepted if not _accepts(grammar, t)] == []
    assert [t for t in refused if _accepts(grammar, t)] == []


# ---- the GBNF grammar --------------------------------------------------------

@pytest.fixture(scope="module")
def gbnf():
    return Grammar(parse_gbnf(sg.render("gbnf")))


def _refs(expr):
    tag = expr[0]
    if tag == "ref":
        yield expr[1]
    elif tag in ("alt", "seq"):
        for item in expr[1]:
            yield from _refs(item)
    elif tag == "rep":
        yield from _refs(expr[1])


@pytest.mark.parametrize("category", sorted(sg.CATEGORIES))
def test_every_gbnf_grammar_is_closed_and_compiles_in_llguidance(category):
    text = sg.render("gbnf", category)
    rules = parse_gbnf(text)
    used = {name for expr in rules.values() for name in _refs(expr)}
    assert "root" in rules and used <= set(rules), sorted(used - set(rules))
    _compiled(llguidance.gbnf_to_lark.gbnf_to_lark(text))


def test_small_corpus_documents_are_accepted_by_the_gbnf_grammar(gbnf):
    _, parseable = _parseable_corpus()
    small = [(n, t) for n, t in parseable if 200 <= len(t) <= 700]
    sample = small[::max(1, len(small) // 12)][:12]
    assert len(sample) >= 10, len(sample)
    refused = [name for name, text in sample if not gbnf.accepts(text)]
    assert refused == [], refused


def test_the_gbnf_grammar_needs_word_breaks_but_not_after_numbers(gbnf):
    assert gbnf.accepts("component C requires k: S {\n  every 30s { emit k.f() }\n}")
    assert not gbnf.accepts("componentC requires k: S {}")
    assert not gbnf.accepts("component C requiresk: S {}")


def test_a_gbnf_template_interpolates_an_expression(gbnf):
    assert gbnf.accepts("fn f() -> Str { return `a ${g(1) + 2} b` }")
    assert not gbnf.accepts("fn f() -> Str { return `a ${requires} b` }")


@pytest.mark.parametrize("text", REFUSED_PROGRAMS)
def test_the_gbnf_grammar_refuses_what_the_parser_refuses(gbnf, text):
    assert not gbnf.accepts(text)


def test_the_gbnf_expression_grammar_refuses_requires():
    grammar = Grammar(parse_gbnf(sg.render("gbnf", "expression")))
    assert grammar.accepts("f(1) + g(x)")
    assert not grammar.accepts("requires k: S")


# ---- EBNF and the CLI --------------------------------------------------------

def test_every_ebnf_rule_reference_is_defined():
    import re

    text = sg.render("ebnf")
    defined = set(re.findall(r"^([A-Za-z_][\w-]*) =", text, re.M))
    body = re.sub(r'"(?:[^"\\]|\\.)*"|/(?:[^/\\\n]|\\.)*/', "", text)
    used = set(re.findall(r"\b(r_[\w]+)\b", body))
    assert used <= defined, sorted(used - defined)


def test_cli_prints_each_format_and_category(capsys):
    for fmt in sg.FORMATS:
        assert main(["grammar", "--format", fmt, "--category", "expression"]) == 0
        assert capsys.readouterr().out.strip() == sg.render(fmt, "expression").strip()


def test_cli_notes_list_every_loose_read(capsys):
    assert main(["grammar", "--notes"]) == 0
    out = capsys.readouterr().out
    _, _, unguarded = sg.derive()
    assert unguarded, "the notes check needs at least one loose read to name"
    for method in {m for m, _ in unguarded}:
        assert method in out
