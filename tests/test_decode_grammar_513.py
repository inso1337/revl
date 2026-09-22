"""Item 513, slice 1: the typed model boundary compiled to a decoding grammar.

Item 257 validates a completion after the fact. This slice derives, from the
same declared type, the grammar a provider constrains the decode with, and
refuses at compile time a type that has no unambiguous one.

Three things are pinned here, and one deliberately is not.

  * `decode_grammar.py`: the grammar-side admission walk and the GBNF
      rendering, including that the renderer raises rather than emitting a
      permissive rule for a node it does not know. The walk is shadowed by item
      257's gate and runs as a drift assertion (issue #1348); the sweep below is
      the evidence, and one test forces the gate open so the assertion's own
      branch is executed rather than merely carried.
  * agreement: a miniature GBNF recogniser (below) checks that the derived
      grammar accepts exactly the completions item 257's validator accepts, on
      a corpus that includes the near misses. This is the non-vacuity evidence
      for the rendering: a grammar that accepted everything would fail it.
  * `lower.py`: the crossing carries `response_grammar` beside
      `response_schema`, and a null-ambiguous response type is refused with the
      position named (by item 257, which is upstream of the walk since issue
      #1263).

  NOT pinned, and not testable from here: that a real decoder honours the
  grammar. revl emits text and a digest; enforcement is the provider's, and
  nothing in this file or in the compiler observes it. Item 257's validator
  stays on for exactly that reason, so a provider that ignores the grammar
  still surfaces as a named validation fault rather than as an accepted value.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import RevlError, compile_source  # noqa: E402
from revl.decode_grammar import (  # noqa: E402
    GRAMMAR_FORMAT,
    GRAMMAR_ROOT,
    GrammarDerivationError,
    _admits_null,
    decode_grammar_for,
    gbnf_from_schema,
    grammar_digest,
    grammar_refusal_reason,
)
from revl.mcp.schema import (  # noqa: E402
    admits_json_null,
    fully_expressible,
    json_schema_for,
)

from runtime import ResponseValidationError, validate_response  # noqa: E402


CALL = {"kind": "record", "fields": {"tool": "Str", "args": "Str"}}
AGENT_TURN = {
    "kind": "variant",
    "cases": [
        {"name": "Final", "payload": "Str"},
        {"name": "ToolCalls", "payload": "List[Call]"},
    ],
}
FLAGSHIP = {"Call": CALL, "AgentTurn": AGENT_TURN}


# --------------------------------------------------------------------------
# A miniature GBNF recogniser.
#
# Only the constructs this derivation emits: sequence, alternation, grouping,
# `*` / `+` / `?`, string literals, character classes and rule references. It
# answers "does this grammar accept this exact string", which is the property
# a constrained decoder enforces. Position-set matching rather than
# backtracking, so an ambiguous grammar cannot make it exponential.
# --------------------------------------------------------------------------

def _tokenize(text):
    toks, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t":
            i += 1
        elif ch == "\n":
            toks.append(("nl", "\n"))
            i += 1
        elif text.startswith("::=", i):
            toks.append(("def", "::="))
            i += 3
        elif ch in "()|*+?":
            toks.append(("op", ch))
            i += 1
        elif ch == '"':
            j, out = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    out.append(_unescape(text[j + 1]))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(("lit", "".join(out)))
            i = j + 1
        elif ch == "[":
            j, out = i + 1, []
            while text[j] != "]":
                if text[j] == "\\":
                    if text[j + 1] == "x":
                        out.append(chr(int(text[j + 2:j + 4], 16)))
                        j += 4
                        continue
                    out.append(_unescape(text[j + 1]))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(("cls", "".join(out)))
            i = j + 1
        elif ch.isalnum() or ch in "_-":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            toks.append(("id", text[i:j]))
            i = j
        else:  # pragma: no cover - a construct this derivation never emits
            raise AssertionError(f"unexpected character {ch!r} in grammar")
    return toks


_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "/": "/",
        "b": "\b", "f": "\f", "]": "]", "[": "[", "^": "^", "x": "x"}


def _unescape(ch):
    return _ESC.get(ch, ch)


class _Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def expr(self):
        alts = [self.seq()]
        while self.peek() == ("op", "|"):
            self.i += 1
            alts.append(self.seq())
        return alts[0] if len(alts) == 1 else ("alt", alts)

    def seq(self):
        items = []
        while True:
            kind, val = self.peek()
            if kind in (None, "nl", "def") or (kind, val) in (("op", "|"), ("op", ")")):
                break
            if kind == "id" and self.i + 1 < len(self.toks) \
                    and self.toks[self.i + 1] == ("def", "::="):
                break                      # the next rule's head
            items.append(self.postfix())
        return items[0] if len(items) == 1 else ("seq", items)

    def postfix(self):
        node = self.atom()
        while self.peek()[0] == "op" and self.peek()[1] in "*+?":
            node = (self.peek()[1], node)
            self.i += 1
        return node

    def atom(self):
        kind, val = self.peek()
        self.i += 1
        if kind == "lit":
            return ("lit", val)
        if kind == "cls":
            return ("cls", val)
        if kind == "id":
            return ("ref", val)
        if (kind, val) == ("op", "("):
            inner = self.expr()
            assert self.peek() == ("op", ")"), "unbalanced group"
            self.i += 1
            return inner
        raise AssertionError(f"unexpected token {(kind, val)!r}")


def _parse_grammar(text):
    toks = [t for t in _tokenize(text) if t[0] != "nl"]
    rules = {}
    parser = _Parser(toks)
    while parser.i < len(toks):
        kind, name = parser.peek()
        assert kind == "id", f"expected a rule head, got {(kind, name)!r}"
        parser.i += 1
        assert parser.peek() == ("def", "::="), f"rule {name} has no `::=`"
        parser.i += 1
        assert name not in rules, f"rule {name} defined twice"
        rules[name] = parser.expr()
    return rules


def _class_match(spec, ch):
    negate = spec.startswith("^")
    body = spec[1:] if negate else spec
    hit, k = False, 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            if body[k] <= ch <= body[k + 2]:
                hit = True
            k += 3
        else:
            if body[k] == ch:
                hit = True
            k += 1
    return hit != negate


def _ends(node, rules, s, starts):
    """Every position the node can end at, given the set of start positions."""
    if not starts:
        return frozenset()
    kind = node[0]
    if kind == "lit":
        lit = node[1]
        return frozenset(i + len(lit) for i in starts if s.startswith(lit, i))
    if kind == "cls":
        return frozenset(i + 1 for i in starts
                         if i < len(s) and _class_match(node[1], s[i]))
    if kind == "ref":
        return _ends(rules[node[1]], rules, s, starts)
    if kind == "seq":
        cur = frozenset(starts)
        for item in node[1]:
            cur = _ends(item, rules, s, cur)
        return cur
    if kind == "alt":
        out = frozenset()
        for branch in node[1]:
            out |= _ends(branch, rules, s, starts)
        return out
    if kind == "?":
        return frozenset(starts) | _ends(node[1], rules, s, starts)
    if kind in ("*", "+"):
        seen = frozenset(starts) if kind == "*" else frozenset()
        frontier = frozenset(starts)
        while frontier:
            frontier = _ends(node[1], rules, s, frontier) - seen
            seen |= frontier
        return seen
    raise AssertionError(f"unknown node {kind!r}")  # pragma: no cover


def accepts(grammar_text, candidate):
    """Does the grammar derive exactly `candidate`?"""
    rules = _parse_grammar(grammar_text)
    assert GRAMMAR_ROOT in rules, "the grammar has no root rule"
    return len(candidate) in _ends(rules[GRAMMAR_ROOT], rules,
                                   candidate, frozenset({0}))


# --------------------------------------------------------------------------
# The recogniser itself, on a hand-written grammar, so a failure below is a
# failure of the derivation and not of the harness.
# --------------------------------------------------------------------------

def test_recogniser_accepts_and_rejects_on_a_hand_written_grammar():
    g = 'root ::= "a" b* "c"\nb ::= "x" | "y"\n'
    assert accepts(g, "ac")
    assert accepts(g, "axyxc")
    assert not accepts(g, "axz c".replace(" ", ""))
    assert not accepts(g, "a")
    assert not accepts(g, "acc")


def test_recogniser_handles_classes_and_negation():
    g = r'root ::= [0-9]+ [^0-9]' + "\n"
    assert accepts(g, "123a")
    assert not accepts(g, "123")
    assert not accepts(g, "1234")


# --------------------------------------------------------------------------
# The derived grammar accepts exactly what the validator accepts.
# --------------------------------------------------------------------------

FLAGSHIP_SCHEMA = json_schema_for("AgentTurn", FLAGSHIP, validated=True)
FLAGSHIP_GRAMMAR = gbnf_from_schema(FLAGSHIP_SCHEMA)

#: Completions and near misses. The near misses are the point: each is a thing
#: a prose-asked model actually produces, and each must be outside the grammar.
FLAGSHIP_CORPUS = [
    {"tag": "Final", "value": "the answer"},
    {"tag": "ToolCalls", "value": []},
    {"tag": "ToolCalls", "value": [{"tool": "search", "args": "{}"}]},
    {"tag": "ToolCalls", "value": [{"tool": "a", "args": "b"},
                                   {"tool": "c", "args": "d"}]},
    {"tag": "Final", "value": "with \"quotes\" and \\ backslash"},
    # near misses
    {"tag": "Final"},                                   # payload dropped
    {"tag": "Final", "value": 7},                       # wrong scalar
    {"tag": "Finall", "value": "x"},                    # tag typo
    {"tag": "Final", "value": "x", "note": "sure!"},    # chatty extra member
    {"value": "x"},                                     # tag dropped
    {"tag": "ToolCalls", "value": [{"tool": "a"}]},     # arm field dropped
    {"tag": "ToolCalls", "value": {"tool": "a", "args": "b"}},  # list unwrapped
    "the answer",                                        # bare prose
    [{"tag": "Final", "value": "x"}],                    # wrapped in a list
]


@pytest.mark.parametrize("value", FLAGSHIP_CORPUS,
                         ids=lambda v: json.dumps(v)[:44])
def test_grammar_and_validator_agree_on_the_flagship(value):
    text = json.dumps(value)
    try:
        validate_response(json.loads(text), FLAGSHIP_SCHEMA, where="t")
        valid = True
    except ResponseValidationError:
        valid = False
    assert accepts(FLAGSHIP_GRAMMAR, text) is valid, (
        f"grammar and validator disagree on {text}")


def test_the_corpus_is_not_one_sided():
    """Non-vacuity of the corpus: it holds both verdicts, so an
    accept-everything grammar and a reject-everything grammar both fail."""
    verdicts = [accepts(FLAGSHIP_GRAMMAR, json.dumps(v)) for v in FLAGSHIP_CORPUS]
    assert verdicts.count(True) == 5
    assert verdicts.count(False) == 9


def test_grammar_admits_whitespace_the_validator_admits():
    pretty = json.dumps({"tag": "Final", "value": "x"}, indent=2)
    assert "\n" in pretty
    assert accepts(FLAGSHIP_GRAMMAR, pretty)


@pytest.mark.parametrize("surface,ok,bad", [
    ("Str", ['"hi"', '""', '"a\\nb"'], ['hi', '"a', "'hi'", '"a\nb"']),
    ("Int", ["0", "-12", "7"], ["01", "1.5", '"7"', "+1"]),
    ("Float", ["0", "-1.5", "1e9", "2.5E-3"], ['"1.5"', "1.", ".5"]),
    ("Bool", ["true", "false"], ["True", '"true"', "1"]),
    ("Unit", ["null"], ["", '"null"', "0"]),
    ("Bytes", ['"QUJD"', '"QQ=="', '""'], ['"A"', '"A!=="', "QUJD"]),
    ("List[Int]", ["[]", "[1]", "[1, 2]", "[ 1,2 ]"], ["[1,]", "[,]", "[1 2]"]),
    ("Opt[Int]", ["null", "3"], ['"3"', "None"]),
    ("Map[Str, Int]", ["{}", '{"a": 1}', '{"a":1,"b":2}'], ['{a: 1}', '{"a"}']),
])
def test_scalar_and_container_grammars(surface, ok, bad):
    grammar = gbnf_from_schema(json_schema_for(surface, {}, validated=True))
    for candidate in ok:
        assert accepts(grammar, candidate), f"{surface} should accept {candidate!r}"
    for candidate in bad:
        assert not accepts(grammar, candidate), \
            f"{surface} should reject {candidate!r}"


# --------------------------------------------------------------------------
# The grammar-side admission walk, and what shadows it.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("surface", [
    "Opt[Opt[Str]]", "Opt[Unit]", "Opt[Opt[Opt[Int]]]",
    "List[Opt[Opt[Str]]]", "Map[Str, Opt[Unit]]",
])
def test_null_ambiguous_opt_is_refused_by_257_and_named_by_the_walk(surface):
    """The grammar walk was written on the premise that item 257 ACCEPTS a
    null-ambiguous `Opt`, so it was the only place the ambiguity was visible.
    Issue #1263 (PR #1270) then made 257 refuse the same shape for the stronger
    reason that it has no exact schema at all, and recorded the grammar walk as
    staying with 257 upstream of it
    (docs/design/1263-opt-nesting-at-a-json-boundary.md, "What follows").

    Both still answer. 257 is the one an author hears (issue #1348 measured the
    walk as unreachable behind it, see the sweep below); the walk is pinned here
    on its own so that relaxing 257 cannot retire the rule by accident.
    """
    assert fully_expressible(surface, {}) is False
    reason = grammar_refusal_reason(surface, {})
    assert reason is not None
    assert "`null`" in reason


_SWEEP_TYPES = {
    "Row": {"kind": "record", "fields": {"a": "Str", "b": "Opt[Unit]"}},
    "Var": {"kind": "variant", "cases": [
        {"name": "N", "payload": None},
        {"name": "P", "payload": "Opt[Opt[Int]]"}]},
    "Clean": {"kind": "record", "fields": {"a": "Str"}},
    "Deep": {"kind": "record", "fields": {"r": "Row", "v": "Var", "c": "Clean"}},
    "Rec": {"kind": "variant", "cases": [{"name": "R", "payload": "List[Rec]"}]},
}


def _sweep_pool():
    """Every surface position both walks descend through, three levels deep:
    `Opt` inner, `List` element, `Map` value (with an expressible and an
    inexpressible key), `Result` arm, plus nominal records and variants carrying
    a null-ambiguous payload, a clean one, a nested nominal and a cycle."""
    pool = {"Str", "Int", "Bool", "Float", "Bytes", "Unit",
            "Row", "Var", "Clean", "Deep", "Rec", "Unknown"}
    for _ in range(3):
        pool |= {shape.format(t) for t in list(pool) for shape in
                 ("Opt[{}]", "List[{}]", "Map[Str, {}]", "Map[Int, {}]",
                  "Result[{}, Str]")}
    return sorted(pool)


def test_the_two_admission_predicates_are_one_predicate():
    """The mechanism behind the sweep below, pinned separately so a change to
    either predicate says WHICH half moved rather than only that the
    containment broke. `decode_grammar._admits_null` and
    `mcp.schema.admits_json_null` are the same function written twice: `Unit`,
    and any `Opt[_]`, and nothing else."""
    pool = _sweep_pool()
    assert len(pool) > 1500
    disagree = [t for t in pool if _admits_null(t) != admits_json_null(t)]
    assert disagree == []
    # and the predicate is not vacuously equal on both sides
    assert [t for t in pool if _admits_null(t)]
    assert [t for t in pool if not _admits_null(t)]
    assert _admits_null(None) is False and admits_json_null(None) is False


def test_the_grammar_gate_is_shadowed_by_257_on_every_shape_it_refuses():
    """Recorded, not celebrated, and the reason `lower.py` treats a non-`None`
    answer as drift rather than as a refusal (issue #1348).

    `decode_grammar._admits_null` and `mcp.schema.admits_json_null` are the same
    predicate over the same surface positions, so on a `validated` emission the
    grammar walk cannot be reached: 257 refuses first on every shape. This sweep
    is the evidence for that claim and the alarm if it stops holding. A type that
    passes 257 and fails the grammar walk is a real 513 admission, belongs in the
    list above, and means the walk has to go back to being an author-facing
    refusal.

    Both directions are counted. `reaches_the_grammar_walk` empty is the claim;
    `refused_by_257_alone` non-empty is the containment's direction, and it is
    what says removing the walk would lose no surface while removing 257's
    refusal would lose a great deal."""
    types = _SWEEP_TYPES
    pool = _sweep_pool()
    reaches_the_grammar_walk = [
        t for t in pool
        if fully_expressible(t, types) and grammar_refusal_reason(t, types)]
    refused_by_257_alone = [
        t for t in pool
        if not fully_expressible(t, types) and not grammar_refusal_reason(t, types)]
    assert len(pool) > 1500
    assert reaches_the_grammar_walk == []
    # 257 refuses an untagged `Result`, a non-`Str` map key, a cycle and an
    # unknown nominal, none of which the grammar walk has an opinion about.
    assert len(refused_by_257_alone) > 1000
    # and the walk is not vacuous: it does refuse, 257 just refuses first.
    assert [t for t in pool if grammar_refusal_reason(t, types)]


def test_the_shadowed_walk_still_runs_at_the_call_site(monkeypatch):
    """The walk is unreachable, not removed, and this is the one test that
    executes the branch behind it: with 257's gate forced open, a null-ambiguous
    response type reaches the grammar walk and `lower.py` raises the internal
    drift message rather than compiling a crossing whose grammar derives `null`
    twice.

    Without this, the assertion would be code no test runs, which is the shape
    issue #1348 was filed about."""
    import revl.lower as lower

    monkeypatch.setattr(lower, "fully_expressible", lambda *a, **k: True)
    with pytest.raises(RevlError) as exc:
        compile_source(_method_program("Opt[Opt[Str]]"), "g.rvl")
    monkeypatch.undo()
    message = str(exc.value)
    assert "internal:" in message
    assert "gate drift" in message
    assert "derives the string `null`" in message
    # unchanged with the gate back in place: the author hears 257
    ir = compile_source(_method_program("AgentTurn"), "g.rvl")
    assert ir["services"]["Model"]["methods"]["complete"]["response_grammar"]


def test_the_schema_still_cannot_express_the_nesting():
    """The reason the gate walks the surface type. The derivation used to
    collapse the two `Opt` layers into one `nullable`, so `Opt[Opt[Str]]` and
    `Opt[Str]` were one schema. Issue #1263 stopped it claiming that: it
    degrades to the honest `x-revlType` stub instead. Either way the schema
    carries no nesting a validator could check, which is why the ambiguity is
    visible only on the surface type."""
    nested = json_schema_for("Opt[Opt[Str]]", {}, validated=True)
    assert nested == {"x-revlType": "Opt[Opt[Str]]"}
    assert nested != json_schema_for("Opt[Str]", {}, validated=True)


@pytest.mark.parametrize("surface", [
    "Str", "Int", "Opt[Str]", "List[Opt[Int]]", "Map[Str, Opt[Str]]",
    "Opt[List[Opt[Str]]]",
])
def test_unambiguous_types_pass_the_gate(surface):
    assert grammar_refusal_reason(surface, {}) is None


def test_gate_reaches_a_record_field_and_a_variant_payload():
    types = {
        "Row": {"kind": "record", "fields": {"id": "Str", "note": "Opt[Opt[Str]]"}},
        "Wrap": {"kind": "variant", "cases": [{"name": "W", "payload": "Row"}]},
    }
    assert grammar_refusal_reason("Row", types) is not None
    assert grammar_refusal_reason("Wrap", types) is not None
    assert grammar_refusal_reason("AgentTurn", FLAGSHIP) is None


def test_gate_terminates_on_a_recursive_type():
    """Belt and braces: 257 refuses recursion before this gate runs, but the
    gate is total on its own so a later slice that lifts recursion does not
    turn a refusal into a compile-time hang."""
    recursive = {"Json": {"kind": "variant", "cases": [
        {"name": "Null", "payload": None},
        {"name": "Arr", "payload": "List[Json]"},
    ]}}
    assert grammar_refusal_reason("Json", recursive) is None
    assert fully_expressible("Json", recursive) is False


# --------------------------------------------------------------------------
# The renderer never falls back to a permissive rule.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("node", [
    {"x-revlType": "Foo"},      # 257's honest stub: no grammar, not a wildcard
    {"oneOf": []},              # accepts no string at all
    {},                         # nothing to constrain
    {"type": "widget"},         # a node kind that does not exist
    {"nullable": True},         # a bare nullable with no base type
    {"type": "array"},          # an array with no item schema
])
def test_renderer_refuses_an_unknown_node_rather_than_widening(node):
    with pytest.raises(GrammarDerivationError):
        gbnf_from_schema(node)


def test_digest_is_stable_and_discriminating():
    a = decode_grammar_for(json_schema_for("AgentTurn", FLAGSHIP, validated=True))
    b = decode_grammar_for(json_schema_for("AgentTurn", FLAGSHIP, validated=True))
    c = decode_grammar_for(json_schema_for("Call", FLAGSHIP, validated=True))
    assert a == b
    assert a["digest"] == grammar_digest(a["text"])
    assert a["digest"] != c["digest"]
    assert a["format"] == GRAMMAR_FORMAT and a["root"] == GRAMMAR_ROOT


def test_a_shape_reached_twice_becomes_one_rule():
    types = {"Pair": {"kind": "record", "fields": {"a": "Call", "b": "Call"},},
             "Call": CALL}
    text = gbnf_from_schema(json_schema_for("Pair", types, validated=True))
    heads = [line.split(" ::=")[0] for line in text.strip().splitlines()]
    assert len(heads) == len(set(heads)), "a rule was defined twice"
    # `Call` is reached twice and rendered once.
    assert sum(1 for line in text.splitlines() if "tool" in line) == 1


# --------------------------------------------------------------------------
# The compiler: what a crossing carries, and what it refuses.
# --------------------------------------------------------------------------

def _method_program(ret, validated=True):
    modifier = "validated " if validated else ""
    return f"""
type Call = {{ tool: Str, args: Str }}
type AgentTurn = Final(Str) | ToolCalls(List[Call])

service Model {{ emission {modifier}fn complete(h: Str) -> {ret} }}
service Loop {{ emission fn run(p: Str) -> Str }}
component Agent requires model: Model provides agent: Loop {{
  provide agent {{
    fn run(session_id) {{
      let r = emit model.complete("x")
      return "ok"
    }}
  }}
}}
"""


def test_a_validated_method_carries_the_grammar():
    ir = compile_source(_method_program("AgentTurn"), "g.rvl")
    op = ir["services"]["Model"]["methods"]["complete"]
    assert op["validated"] is True
    grammar = op["response_grammar"]
    assert grammar["format"] == "gbnf"
    assert grammar["digest"] == grammar_digest(grammar["text"])
    # and it is the grammar of the schema beside it, not a second derivation
    assert grammar["text"] == gbnf_from_schema(op["response_schema"])
    assert accepts(grammar["text"], '{"tag": "Final", "value": "hi"}')
    assert not accepts(grammar["text"], '{"tag": "Final"}')


def test_an_unvalidated_method_carries_neither_key():
    ir = compile_source(_method_program("AgentTurn", validated=False), "g.rvl")
    op = ir["services"]["Model"]["methods"]["complete"]
    assert "response_grammar" not in op
    assert "response_schema" not in op
    assert "validated" not in op


def test_a_validated_extern_carries_the_grammar():
    src = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
extern emission validated fn complete(h: Str) -> AgentTurn = @py {
  return {"tag": "Final", "value": h}
}
"""
    ir = compile_source(src, "g.rvl")
    extern = next(e for e in ir["externs"] if e["name"] == "complete")
    assert extern["validated"] is True
    assert extern["response_grammar"]["text"] == gbnf_from_schema(
        extern["response_schema"])


@pytest.mark.parametrize("ret", [
    "Opt[Opt[Str]]", "Opt[Unit]", "Opt[Opt[Opt[Int]]]",
    "List[Opt[Opt[Str]]]", "Map[Str, Opt[Unit]]",
])
def test_a_null_ambiguous_response_type_is_refused_at_compile_time(ret):
    """Refused at compile time with the position named. The sentence is item
    257's, not this item's: issue #1263 put the expressibility gate upstream of
    the grammar gate deliberately, and 257's refusal is the one that covers all
    three validated consumers (an emission's response, an `event` item schema,
    a routed endpoint's bound parameters) rather than only the emission this
    item compiles a grammar for."""
    with pytest.raises(RevlError) as exc:
        compile_source(_method_program(ret), "g.rvl")
    message = str(exc.value)
    assert "`validated` emission `complete`" in message
    assert "already accepts `null`" in message
    assert "257-typed-model-boundary" in message


@pytest.mark.parametrize("ret", ["Str", "Opt[Str]", "AgentTurn", "List[Call]"])
def test_the_control_types_still_compile(ret):
    ir = compile_source(_method_program(ret), "g.rvl")
    assert ir["services"]["Model"]["methods"]["complete"]["response_grammar"]


def test_the_refusal_does_not_reach_an_unvalidated_emission():
    """The gate is a property of a CONSTRAINED crossing, not of the type: a
    plain emission returning the same type is untouched."""
    ir = compile_source(_method_program("Opt[Opt[Str]]", validated=False), "g.rvl")
    assert "response_grammar" not in ir["services"]["Model"]["methods"]["complete"]
