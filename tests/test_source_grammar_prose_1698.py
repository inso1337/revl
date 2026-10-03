"""The exported source grammar refuses prose (issue #1698).

A grammar-constrained decoder can only emit text the grammar allows, so a
grammar that allows prose does not stop a model from writing prose. Three
things let it through:

* nine parser reads were modelled as ANY token (`--notes` listed them). Each
  is now the token class the parser tests for there: the closed registries
  (`on breach divert|pause|halt`, `on_failure(withdraw|result)`, ...), the
  compound-assignment operators, `observe|decide`;
* the component body was read as `stmt` with the method flag unknown, so it
  also admitted the method-only `x = 1`. It is `stmt(in_method=False)` now,
  as the parser's component loop calls it;
* the parser reads `a b` as two expression statements, so `the quick brown
  fox` inside a function body is a run of statements. In both formats an
  expression statement that follows another statement must start a new line
  (or follow a `;`); in Lark a line break is the `NL` lexeme. That is
  narrower than the parser, so the corpus is held to it below: every
  parseable document that writes an expression statement on the line of the
  statement before it is one the compiler refuses. A match block arm is
  exempt, since its last statement is its value.

The negative corpus (tests/fixtures/grammar_negative/) is English, Markdown and
code in other languages. Each sample is checked to be refused by the compiler
too, so the grammar refusing it refuses nothing revl admits.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import warnings
from pathlib import Path

import llguidance
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _gbnf_earley import Grammar, parse_gbnf  # noqa: E402
from revl import compile_source  # noqa: E402
from revl import parser as revl_parser  # noqa: E402
from revl import source_grammar as sg  # noqa: E402
from revl.errors import RevlError  # noqa: E402

NEGATIVE = sorted((ROOT / "tests" / "fixtures" / "grammar_negative").iterdir())


class _ByteTokenizer:
    eos_token_id = 256
    bos_token_id = None
    tokens = [bytes([i]) for i in range(256)] + [b"<eos>"]
    special_token_ids = [256]

    def __call__(self, text):
        return list(text if isinstance(text, bytes) else text.encode())


TOKENIZER = llguidance.LLTokenizer(llguidance.TokenizerWrapper(_ByteTokenizer()), slices=[])


@pytest.fixture(scope="module")
def lark():
    return {c: llguidance.LLMatcher.grammar_from_lark(sg.render("lark", c))
            for c in sg.CATEGORIES}


@pytest.fixture(scope="module")
def gbnf():
    return {c: Grammar(parse_gbnf(sg.render("gbnf", c))) for c in sg.CATEGORIES}


def _lark_accepts(grammar, text):
    matcher = llguidance.LLMatcher(TOKENIZER, grammar, log_level=0)
    fed = matcher.consume_tokens(list(text.encode()))
    return bool(fed) and not matcher.is_error() and matcher.is_accepting()


def _parses(text):
    try:
        revl_parser.Parser(text, "x.rvl").parse()
    except (RevlError, RecursionError):
        return False
    return True


# ---- the any-token reads ------------------------------------------------------

def test_no_read_is_modelled_as_any_token():
    _, _, unguarded = sg.derive()
    assert unguarded == []
    for fmt in sg.FORMATS:
        assert "any_token" not in sg.render(fmt, "program").split("any_token:")[0]


@pytest.mark.parametrize("text, refused", [
    ("component C {\n  report_colocation = true\n}", True),
    ("component C {\n  let w = effect spawn D with { } undo w.dispose()\n}", False),
    ("component C {\n  provide s {\n    fn f() { var x = 1\n      x = 2\n      return x }\n  }\n}",
     False),
])
def test_an_assignment_is_a_method_statement_only(lark, gbnf, text, refused):
    assert _parses(text) is not refused
    assert _lark_accepts(lark["program"], text) is not refused
    assert gbnf["program"].accepts(text) is not refused


# ---- the negative corpus ------------------------------------------------------

def _compiles(text):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            compile_source(text, "x.rvl")
        except (RevlError, RecursionError):
            return False
    return True


@pytest.mark.parametrize("sample", NEGATIVE, ids=lambda p: p.name)
def test_the_sample_is_not_revl(sample):
    """The compiler refuses each sample as a program, as a function body and
    as a component body. (Two of them, `code_sh` and `code_sql`, do PARSE
    as a function body: the parser reads `revl check app.rvl` as three
    expression statements on one line, which is the shape the line rule
    refuses.)"""
    text = sample.read_text(encoding="utf-8")
    for wrapped in (text, "fn f() -> Int {\n" + text + "\nreturn 1\n}",
                    "component C {\n" + text + "\n}"):
        assert not _compiles(wrapped), wrapped[:80]


@pytest.mark.parametrize("sample", NEGATIVE, ids=lambda p: p.name)
def test_every_category_refuses_the_sample(lark, gbnf, sample):
    text = sample.read_text(encoding="utf-8")
    assert [c for c in sg.CATEGORIES if _lark_accepts(lark[c], text)] == []
    assert [c for c in sg.CATEGORIES if gbnf[c].accepts(text)] == []


# English and Markdown (`prose_*`, `md_*`); `code_*` is other languages.
# Every sample is a `.txt` file, so no linter, compiler or doc sweep picks
# up the Python, Go or Markdown in it.
PROSE = [p for p in NEGATIVE if p.name.startswith(("prose_", "md_"))]


@pytest.mark.parametrize("sample", PROSE, ids=lambda p: p.name)
def test_a_decoder_held_to_gbnf_stops_inside_the_first_line_of_prose(gbnf, sample):
    """A constrained decoder only ever extends a prefix the grammar allows.
    In every category the GBNF refuses each prose sample before its first line
    ends, so a model held to it cannot write a sentence."""
    text = sample.read_text(encoding="utf-8")
    first_line = len(text.split("\n", 1)[0])
    for category, grammar in gbnf.items():
        assert not grammar.accepts(text)
        assert grammar.stopped_at <= first_line, (category, text[:grammar.stopped_at])


@pytest.mark.parametrize("text, accepted", [
    ("hello world", False),
    ("The gate does not sandbox host code", False),
    ("f(1) g(2)", False),
    ("hello\nworld", True),
    ("f(1); g(2)", True),
    ("neg = true  i = 1", True),
    ("let r = w1.dispose() return 1", True),
    ("if (x) { return 1 } return 2", True),
])
def test_an_expression_statement_starts_a_new_line(lark, gbnf, text, accepted):
    """The parser reads every one of these; the grammar, in both formats,
    refuses an expression statement on the line of the statement before it."""
    assert _parses("fn f() -> Int {\n" + text + "\nreturn 0\n}")
    assert gbnf["statements"].accepts(text) is accepted
    assert _lark_accepts(lark["statements"], text) is accepted


def test_a_match_block_arm_may_end_on_the_line_of_its_last_statement(lark, gbnf):
    """The arm's last statement is its value (`d + 1`), not a statement."""
    text = "x = match y { Some(n) => { let d = n * 2 d + 1 }, None => 0 }"
    assert gbnf["statements"].accepts(text)
    assert _lark_accepts(lark["statements"], text)


# ---- the corpus keeps to the GBNF's line rule ---------------------------------

_STATEMENTS = tuple(cls for name, cls in vars(revl_parser).items()
                    if isinstance(cls, type) and dataclasses.is_dataclass(cls)
                    and name.endswith("Stmt"))


def _same_line_expression_statements(node, out):
    """Each expression statement that starts on the line of the statement
    before it, anywhere in `node`. A match block arm is skipped: its last
    statement is the arm's value, and the grammar's line rule exempts it the
    same way (`source_grammar._VALUE_BLOCKS`)."""
    if isinstance(node, revl_parser.ExprBlockArm):
        return
    if isinstance(node, list):
        if len(node) > 1 and all(isinstance(x, _STATEMENTS) for x in node):
            for before, after in zip(node, node[1:]):
                if (isinstance(after, revl_parser.ExprStmt)
                        and after.line == before.line):
                    out.append(after.line)
        for item in node:
            _same_line_expression_statements(item, out)
    elif isinstance(node, tuple):
        for item in node:
            _same_line_expression_statements(item, out)
    elif dataclasses.is_dataclass(node):
        for field in dataclasses.fields(node):
            _same_line_expression_statements(getattr(node, field.name), out)
    elif hasattr(node, "__dict__") and not isinstance(node, type):
        for value in vars(node).values():
            _same_line_expression_statements(value, out)


def test_every_corpus_document_the_line_rule_refuses_is_refused_by_the_compiler():
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.rvl"],
                            capture_output=True, text=True, check=True).stdout.split()
    parsed, flagged, admitted = 0, [], []
    for name in listed:
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        try:
            program = revl_parser.Parser(text, name).parse()
        except (RevlError, RecursionError):
            continue
        parsed += 1
        lines: list = []
        _same_line_expression_statements(program, lines)
        if not lines:
            continue
        flagged.append(name)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                compile_source(text, name)
            except RevlError:
                continue
        admitted.append((name, lines))
    assert parsed > 1000, parsed
    assert flagged, "the census should see the rejection fixtures that do this"
    assert admitted == [], admitted
