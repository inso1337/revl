"""fillSpec version 3: each hole names the grammar category of its fill (issue #1664).

`grammarCategory` is a key of `revl.source_grammar.CATEGORIES`. A client
passes it to `revl grammar --format F --category C` (or the MCP `revl_grammar`
tool) to constrain a decoder to the hole's slot. It is not a table entry:
`source_grammar.hole_category` reads it off the grammar derived from the
parser, as the narrowest category every parse of the `hole` keyword passes
through.

The exit tests from the issue:

1. every obligation `revl_scaffold` and `revl_check` report carries a
   `grammarCategory`, and it is a key of `CATEGORIES`;
2. for each scaffold fixture, the text that fills each hole in a known-good
   completion is accepted by the grammar of that category under llguidance;
3. the version moves to 3 (the docs follow in docs/holes.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import llguidance
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import source_grammar as sg  # noqa: E402
from revl.mcp import fillspec  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.scaffold import build_skeleton, build_spec, scaffold_document  # noqa: E402


class _ByteTokenizer:
    eos_token_id = 256
    bos_token_id = None
    tokens = [bytes([i]) for i in range(256)] + [b"<eos>"]
    special_token_ids = [256]

    def __call__(self, text):
        return list(text if isinstance(text, bytes) else text.encode())


TOKENIZER = llguidance.LLTokenizer(llguidance.TokenizerWrapper(_ByteTokenizer()), slices=[])
_GRAMMARS: dict = {}


def _accepts(category: str, text: str) -> bool:
    if category not in _GRAMMARS:
        grammar = llguidance.LLMatcher.grammar_from_lark(sg.render("lark", category))
        assert llguidance.LLMatcher.validate_grammar(grammar, TOKENIZER) == ""
        _GRAMMARS[category] = grammar
    matcher = llguidance.LLMatcher(TOKENIZER, _GRAMMARS[category], log_level=0)
    fed = matcher.consume_tokens(list(text.encode()))
    return bool(fed) and not matcher.is_error() and matcher.is_accepting()


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ---- the derivation ----------------------------------------------------------

def test_a_hole_is_an_expression_in_this_parser():
    assert sg.hole_category() == "expression"
    assert sg.hole_category(sg.derive()) == "expression"


def test_the_category_follows_the_parser_not_a_table():
    """Let a function body also read `hole` as a statement head: the hole is
    then reachable outside any expression, so no category narrower than a
    whole program holds every hole, and the derivation says so."""
    source = (ROOT / "src" / "revl" / "parser.py").read_text()
    head = "    def _fn_stmt(self):\n        tok = self.peek()\n"
    assert head in source
    edited = source.replace(
        head, head + '        if self.at("kw", "hole"):\n'
                     "            return self._hole_expr()\n", 1)
    assert sg.hole_category(sg.derive(edited)) == "program"


# ---- 1. every reported hole carries it ----------------------------------------

EVERY_POSITION = (
    "service Db { fn q(sql: Str) -> Str }\n"
    "service Cache { fn get(key: Str) -> Str }\n"
    "fn helper(x: Int) -> Int = hole[Int] \"a function body\"\n"
    "component C requires db: Db provides c: Cache {\n"
    "  config { ttl: Int }\n"
    "  provide c {\n"
    "    fn get(key) {\n"
    "      let raw = db.q(key)\n"
    "      if (raw == \"\") { return hole[Str] \"a nested branch\" }\n"
    "      return hole[Str] \"a method body\"\n"
    "    }\n"
    "  }\n"
    "}\n")


def test_every_revl_check_obligation_names_its_category():
    payload = _call("revl_check", {"source": EVERY_POSITION})
    obligations = payload["holes"]
    assert len(obligations) == 3, obligations
    for ob in obligations:
        spec = ob["fillSpec"]
        assert spec["version"] == fillspec.FILL_SPEC_VERSION == 3
        assert spec["grammarCategory"] in sg.CATEGORIES
        assert spec["grammarCategory"] == sg.hole_category()


def test_every_revl_scaffold_obligation_names_its_category():
    payload = _call("revl_scaffold", {"service": "Analysis", "requires": ["filesystem"],
                                      "provides": "analysis",
                                      "capabilities": ["filesystem.read"]})
    assert payload["obligations"], payload
    for ob in payload["obligations"]:
        assert ob["fillSpec"]["grammarCategory"] in sg.CATEGORIES


# ---- 2. known-good fills are in the category's language ------------------------

# Each fixture is a scaffold spec and, per method, a fill that completes it.
# The completed document must compile with no hole left, which is what makes
# the fill known-good.
SCAFFOLDS = [
    (dict(service="Analysis", requires=["filesystem"], provides="analysis",
          capabilities=["filesystem.read"], effect=False),
     {"run": "input"}),
    (dict(service="Store", provides="store", config=["ttl: Int"], effect=False,
          methods=["get(k: Str) -> Str", "size() -> Int", "has(k: Str) -> Bool",
                   "names() -> List[Str]"]),
     {"get": 'k == "" ? "empty" : `key ${k}`',
      "size": "config.ttl * 2 + 1",
      "has": '!(k == "")',
      "names": '["a", "b"]'}),
]

_HOLE = re.compile(r'hole\[(?:[^\[\]]|\[[^\[\]]*\])*\](?: "(?:[^"\\]|\\.)*")?')


def _fill(source: str, obligations: list, fill_for) -> tuple[str, list]:
    """Replace each obligation's hole with its fill; return the completed
    source and the (category, fill) pairs used."""
    lines = source.split("\n")
    used = []
    for ob in obligations:
        index = ob["line"] - 1
        holes = _HOLE.findall(lines[index])
        assert len(holes) == 1, lines[index]
        fill = fill_for(lines[index])
        lines[index] = lines[index].replace(holes[0], fill, 1)
        used.append((ob["fillSpec"]["grammarCategory"], fill))
    return "\n".join(lines), used


@pytest.mark.parametrize("spec_args, fills", SCAFFOLDS, ids=["analysis", "store"])
def test_known_good_scaffold_fills_are_in_their_category(spec_args, fills):
    spec = build_spec(**spec_args)
    doc = scaffold_document(spec)
    source = doc["source"]
    assert source == build_skeleton(spec)

    def fill_for(line):
        method = re.search(r"fn (\w+)\(", line).group(1)
        return fills[method]

    completed, used = _fill(source, doc["obligations"], fill_for)
    assert len(used) == len(fills)
    assert compile_source(completed, "completed.rvl").get("holes", []) == []
    refused = [(c, f) for c, f in used if not _accepts(c, f)]
    assert refused == []


def test_an_effect_resource_fill_is_in_its_category():
    """The acquired resource's hole. Its type is the scaffold's own
    placeholder, so the completion is checked at the parser: the document
    with the fill in place parses, and the fill is in the hole's category."""
    spec = build_spec(service="Store", provides="store", methods=["get(k: Str) -> Str"])
    doc = scaffold_document(spec)
    effect = [ob for ob in doc["obligations"] if ob["expected"] == "StoreResource"]
    assert len(effect) == 1
    completed, used = _fill(doc["source"], effect, lambda _line: "Map.new()")
    Parser(completed, "completed.rvl").parse()
    assert used == [("expression", "Map.new()")]
    assert _accepts("expression", "Map.new()")


EMITTING = (
    "service Db { emission fn put(k: Str, v: Str) -> Str }\n"
    "service Cache { emission[db] fn set(key: Str) -> Str }\n"
    "component C requires db: Db provides c: Cache {\n"
    "  provide c {\n"
    "    fn set(key) = hole[Str] \"store it\"\n"
    "  }\n"
    "}\n")


def test_a_crossing_fill_is_in_its_category():
    """Where the fillSpec permits a crossing, the crossing form it gives
    (`emit <key>.<operation>(...)`) is in the category's language too."""
    obligations = fillspec.enrich(compile_source(EMITTING, "e.rvl"))
    assert obligations[0]["fillSpec"]["crossing"]["permitted"] is True
    completed, used = _fill(EMITTING, obligations, lambda _line: 'emit db.put(key, "v")')
    assert compile_source(completed, "e.rvl").get("holes", []) == []
    assert [_accepts(c, f) for c, f in used] == [True]


def test_text_outside_the_category_is_refused():
    assert not _accepts("expression", "let x = 1")
    assert not _accepts("expression", "requires k: S")
