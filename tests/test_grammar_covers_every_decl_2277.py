"""`revl grammar --prompt` names every top-level declaration the parser accepts
(issue #2277, caveat 2).

The prompt-sized grammar is what an agent working from revl's own description
reads, so a declaration missing from it cannot be discovered: `secret NAME for
CAP` (item 256) was missing for months while the parser accepted it. This test
makes the omission a failure instead of a surprise, from two independent
enumerations of what the parser accepts at the top level:

  * the declaration LISTS of `parser.Program` (one per declaration kind the
    parser builds), and
  * the keywords the top-level dispatch `Parser._parse_program` branches on
    (every word that can head a declaration, including contextual ones such as
    `secret`, `event`, `boot` and modifiers such as `pub`, `verified`).

A third enumeration (issue #2280) covers the statements that are NOT
top-level declarations but are security forms all the same: the clause heads
`Parser.composition_decl` reads inside a `composition { ... }` (`place`,
`grant`, ...) and the operations `Parser.layer_decl`/`_layer_op` read inside a
`layer` (site layer `place`/`grant`). `grant` and `place` are not `Program`
lists, so the two enumerations above could never have caught them.

Each must appear, quoted as a keyword, in the text `revl grammar --prompt`
prints. A kind that is deliberately left out goes in `EXEMPT_*` below with the
reason; a new Program list or dispatch keyword fails here until it is either put
in the grammar or exempted by someone who has to say why.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import parser  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.grammar_summary import PROMPT_GRAMMAR, PROSE_GRAMMAR  # noqa: E402

#: Program list field -> the keyword(s) that head that declaration. Every
#: entry must appear quoted in the prompt grammar; a two-word head is written
#: `a' 'b` so the adjacent pair is checked, not each word on its own.
DECL_LISTS = {
    "services": ("service",),
    "components": ("component",),
    "type_decls": ("type",),
    "event_decls": ("event",),
    "fn_decls": ("fn",),
    "uses": ("use",),
    "externs": ("extern",),
    "secrets": ("secret",),
    "retentions": ("retention",),
    "model_roles": ("model' 'role",),
    "model_councils": ("model' 'council",),
    "tests": ("test",),
    "fault_tests": ("fault' 'test",),
    "prop_tests": ("prop' 'test",),
    "compositions": ("composition",),
    "layers": ("layer",),
    "capability_decls": ("capability",),
}

#: Program list fields that are not declarations, with the reason.
EXEMPT_LISTS = {
    "assets": "a registry of the `asset \"...\"` EXPRESSIONS the parser met, "
              "not a declaration; the expression is in the grammar",
}

#: Dispatch keywords of `_parse_program` left out of the prompt grammar on
#: purpose, with the reason. Empty today: every word that heads a top-level
#: declaration is short enough to name.
EXEMPT_KEYWORDS: dict[str, str] = {}

#: Clause heads of `composition_decl` left out of the prompt grammar, with the
#: reason. Empty today: every clause head is named in the `composition`
#: production.
EXEMPT_COMPOSITION_KEYWORDS: dict[str, str] = {}


def _prompt_grammar_text(capsys) -> str:
    assert main(["grammar", "--prompt"]) == 0
    return capsys.readouterr().out


def _declaration_list_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(parser.Program)
            if str(f.type).startswith("list")}


def _dispatch_keywords() -> set[str]:
    """Every word `_parse_program` tests with `self.at("kw"|"ident", WORD)` or
    by comparing a looked-ahead token's `.value` to WORD (`site layer`,
    `lifecycle test`, `boot component`). A head hidden behind a helper
    predicate is still caught through the Program list it appends to."""
    source = inspect.getsource(parser.Parser._parse_program)
    words = set(re.findall(r'self\.at\("(?:kw|ident)",\s*"(\w+)"\)', source))
    return words | set(re.findall(r'\.value == "(\w+)"', source))


def _clause_heads(*methods) -> set[str]:
    """Every contextual word the given parser methods branch on with
    `self.at("kw"|"ident", WORD)`."""
    words: set[str] = set()
    for method in methods:
        words |= set(re.findall(r'self\.at\("(?:kw|ident)",\s*"(\w+)"\)',
                                inspect.getsource(method)))
    return words


def _composition_heads() -> set[str]:
    return _clause_heads(parser.Parser.composition_decl)


def _layer_op_heads() -> set[str]:
    """The operations a layer body reads: `touches` (in `layer_decl`) and every
    operation head in `_layer_op`, minus words that are not heads there
    (`for` after the layer name, `row` after `add`, `with`, `to`, `over`)."""
    heads = _clause_heads(parser.Parser.layer_decl, parser.Parser._layer_op)
    return heads - {"for", "row", "with", "to", "over"}


def _quoted(word: str) -> str:
    return f"'{word}'"


def _productions(text: str) -> list[str]:
    """The grammar split into its productions: a line that starts in column 0
    with `name :=` opens one, indented lines continue it. Comment-only lines
    (`-- ...`) belong to no production, so a keyword that is only mentioned in
    prose does not count as covered."""
    blocks: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\w+\s*:=", line):
            blocks.append(line)
        elif blocks and line.startswith(" ") and not line.lstrip().startswith("--"):
            blocks[-1] += "\n" + line
    return blocks


def _in_a_production(text: str, phrase: str) -> bool:
    """`phrase` (quoted) occurs inside the code part of some production, with
    trailing `--` comments cut off."""
    code = [re.sub(r"\s--.*", "", block) for block in _productions(text)]
    return any(_quoted(phrase) in block for block in code)


def test_every_program_declaration_list_is_classified():
    # a new declaration kind adds a list to Program; it cannot be added
    # without deciding here whether the grammar names it.
    lists = _declaration_list_fields()
    classified = set(DECL_LISTS) | set(EXEMPT_LISTS)
    assert lists == classified, (
        f"unclassified Program lists: {sorted(lists - classified)}; "
        f"stale entries: {sorted(classified - lists)}")


def test_prompt_grammar_names_every_declaration_kind_the_parser_builds(capsys):
    text = _prompt_grammar_text(capsys)
    missing = {field: [w for w in words if not _in_a_production(text, w)]
               for field, words in DECL_LISTS.items()}
    missing = {field: words for field, words in missing.items() if words}
    assert not missing, (
        f"`revl grammar --prompt` omits declaration kinds the parser accepts: "
        f"{missing}")


def test_prompt_grammar_names_every_keyword_the_top_level_dispatch_reads(capsys):
    text = _prompt_grammar_text(capsys)
    words = _dispatch_keywords()
    assert {"secret", "event", "use", "service", "site", "lifecycle"} <= words, \
        f"the dispatch scrape found too little: {sorted(words)}"
    missing = sorted(w for w in words
                     if w not in EXEMPT_KEYWORDS and not _in_a_production(text, w))
    assert not missing, (
        f"`revl grammar --prompt` omits top-level keywords the parser "
        f"dispatches on: {missing}")


def test_the_keyword_exemptions_are_reasoned_and_not_stale():
    assert all(reason.strip() for reason in EXEMPT_KEYWORDS.values())
    assert all(reason.strip() for reason in EXEMPT_LISTS.values())
    assert set(EXEMPT_KEYWORDS) <= _dispatch_keywords()


def test_the_served_text_is_the_constant_the_test_reads(capsys):
    assert _prompt_grammar_text(capsys).rstrip("\n") == PROMPT_GRAMMAR.rstrip("\n")


def test_the_prose_summary_shows_a_secret_too():
    assert "secret api_key for net.send" in PROSE_GRAMMAR


def _production(text: str, name: str) -> str:
    """The code part of the production called `name`, or fail."""
    for block in _productions(text):
        if re.match(rf"^{name}\s*:=", block):
            return re.sub(r"\s--.*", "", block)
    raise AssertionError(f"`revl grammar --prompt` has no `{name}` production")


def test_prompt_grammar_names_every_clause_a_composition_reads(capsys):
    """issue #2280: `place` and `grant` are clause heads of a composition, not
    Program lists, so they were absent while the parser accepted them."""
    text = _prompt_grammar_text(capsys)
    heads = _composition_heads()
    assert {"row", "remote", "place", "grant", "stack", "site"} <= heads, \
        f"the composition scrape found too little: {sorted(heads)}"
    body = _production(text, "composition")
    missing = sorted(
        w for w in heads - set(EXEMPT_COMPOSITION_KEYWORDS)
        if not re.search(rf"\b{w}\b", body) or not _in_a_production(text, w))
    assert not missing, (
        f"the `composition` production omits clause heads the parser reads: "
        f"{missing}")


def test_the_composition_exemptions_are_reasoned_and_not_stale():
    assert all(r.strip() for r in EXEMPT_COMPOSITION_KEYWORDS.values())
    assert set(EXEMPT_COMPOSITION_KEYWORDS) <= _composition_heads()


def test_prompt_grammar_names_every_operation_a_layer_reads(capsys):
    """issue #2280: the site layer writes `place` and `grant` as operations;
    each must be a member of the `op` production, by quoted word or by naming
    the production that spells it."""
    text = _prompt_grammar_text(capsys)
    heads = _layer_op_heads()
    assert {"add", "remove", "replace", "configure", "resolve", "place",
            "grant", "touches"} <= heads, \
        f"the layer scrape found too little: {sorted(heads)}"
    code = _production(text, "op") + _production(text, "touches")
    missing = sorted(w for w in heads
                     if _quoted(w) not in code
                     and not re.search(rf"\|\s*(?:\w+\s*\|\s*)*{w}\b", code))
    assert not missing, (
        f"the layer `op` production omits operations the parser reads: "
        f"{missing}")


def test_the_security_forms_show_the_shape_the_parser_accepts(capsys):
    text = _prompt_grammar_text(capsys)
    assert "'place' address 'on' 'process' STRING ['backend' IDENT]" in text
    assert "'grant' address 'with' '{' IDENT (',' IDENT)* '}'" in text
