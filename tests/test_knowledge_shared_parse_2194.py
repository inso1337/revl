"""The knowledge index reads a text it is sent once (issue #2194).

The check's read-only readers share one parse of the text they are sent
(PR #2943), but the knowledge index was left out of that share: it parsed the
text through `nested.parse` and then lexed it again through `nested.tokens_of`
for the first member-anchored comment, so the index lexed it twice. Both reads
now go through `revl.shared_parse`, so the index lexes the text once and the
entries are exactly what they were.

The second half pins the index as a READER of that share: it reuses a program
and a token list the call already read, damaging the share changes its answers,
it leaves the share untouched, and a text that does not parse or does not lex
is indexed as before.
"""

import hashlib
import json
import pickle
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import knowledge, lexer, parser, shared_parse  # noqa: E402
from revl.diagnostics import GUARANTEES  # noqa: E402
from revl.parser import Parser  # noqa: E402

CODES = set(GUARANTEES)
#: the name a `revl_check` candidate carries in the index (its working-set key)
NAME = "source"


def _source(n: int) -> str:
    """A service, a component's provide block and a record type, each with n
    members, and a comment above every member."""
    parts = ["// The file as a whole.\n",
             "service Counter {\n"]
    for i in range(n):
        parts.append(f"    // bump {i}\n"
                     f"    fn bump{i}(by: Int) -> Int\n")
    parts.append("}\n\n"
                 "// A counter that adds.\n"
                 "component Adder provides counter: Counter {\n"
                 "  provide counter {\n")
    for i in range(n):
        parts.append(f"    // add {i}\n"
                     f"    fn bump{i}(by) = by + {i}\n\n")
    parts.append("  }\n}\n\n"
                 "type Wide = {\n")
    for i in range(n):
        parts.append(f"  // field {i}\n"
                     f"  f{i}: Int,\n")
    parts.append("}\n")
    return "".join(parts)


@pytest.fixture
def counted(monkeypatch):
    """Every `lex` and `Parser.parse` call. The parser binds `lex` at import and
    `shared_parse` looks it up per call, so both names are patched."""
    calls = []
    lex = lexer.lex
    parse = Parser.parse

    def counting_lex(*a, **k):
        calls.append(a[0] if a else k.get("source"))
        return lex(*a, **k)

    def counting_parse(self, *a, **k):
        calls.append(("parse",))
        return parse(self, *a, **k)

    monkeypatch.setattr(lexer, "lex", counting_lex)
    monkeypatch.setattr(parser, "lex", counting_lex)
    monkeypatch.setattr(Parser, "parse", counting_parse)
    return calls


def _lexes_of(calls, text: str) -> list:
    return [call for call in calls if call == text]


def _parses(calls) -> list:
    return [call for call in calls if call == ("parse",)]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def test_the_index_lexes_the_text_it_is_sent_once(counted):
    text = _source(3)
    with shared_parse.scope():
        entries = knowledge.index_text(NAME, text, None, CODES)
    assert len(entries) == 3 * 3 + 2, entries
    assert len(_parses(counted)) == 1, counted      # the program, once
    assert len(_lexes_of(counted, text)) == 1, counted   # was 2


def test_the_index_reuses_a_program_the_call_already_parsed(counted):
    text = _source(3)
    with shared_parse.scope():
        shared_parse.program(text, NAME)            # the jail's read, say
        entries = knowledge.index_text(NAME, text, None, CODES)
    assert len(entries) == 3 * 3 + 2, entries
    assert len(_parses(counted)) == 1, counted      # was 2
    assert len(_lexes_of(counted, text)) == 1, counted   # was 2


# Digests taken on the base commit (f216253dd), before the change: the entries
# for `_source(5)` under the name a check gives it. A change to `_source` moves
# them; recompute on the commit before the change, never on the change itself.
EXPECTED = "04656109fd5393cf881d7cd4356c5e1391e2b0717eca29e60615c348cccf74b9"


def test_the_entries_are_identical_with_and_without_the_share():
    text = _source(5)
    with shared_parse.scope():
        shared = knowledge.index_text(NAME, text, None, CODES)
    plain = knowledge.index_text(NAME, text, None, CODES)
    assert shared == plain
    assert _digest(shared) == EXPECTED


UNPARSEABLE = "// The file as a whole.\ncomponent X { fn }\n"


def test_a_text_that_does_not_parse_is_still_indexed(counted):
    """`nested.parse` gives None for text it rejects; the index then anchors
    every block to the file. The shared read must give the same, and its
    failure is the one a later reader of the same text meets."""
    with shared_parse.scope():
        entries = knowledge.index_text(NAME, UNPARSEABLE, None, CODES)
        assert [e["anchor"]["symbol"] for e in entries] == [None], entries
        assert entries[0]["anchor"]["fingerprint"] is None
        with pytest.raises(Exception) as first:
            shared_parse.program(UNPARSEABLE, NAME)
        with pytest.raises(Exception) as again:
            shared_parse.program(UNPARSEABLE, NAME)
        assert again.value is first.value
    assert len(_lexes_of(counted, UNPARSEABLE)) == 1, counted   # was 2


UNLEXABLE = ("// The file as a whole.\n"
             "service S { fn f() -> Str { `never closed } }\n")


def test_a_text_that_does_not_lex_is_still_indexed(counted):
    """A lex error is a `RevlError`, so the index treated the text as unparsed
    before the change; it must keep doing so, and the failure stays the share's
    to hand on."""
    from revl.errors import RevlError  # noqa: PLC0415

    with shared_parse.scope():
        entries = knowledge.index_text(NAME, UNLEXABLE, None, CODES)
        assert [e["anchor"]["symbol"] for e in entries] == [None], entries
        with pytest.raises(RevlError):
            shared_parse.tokens(UNLEXABLE, NAME)
    assert len(_lexes_of(counted, UNLEXABLE)) == 1, counted     # was 2


MULTILINE = ("// The file as a whole.\n"
             "component Adder provides counter: Counter {\n"
             "  provide counter {\n"
             "    // add one\n"
             "    fn bump(by) = (\n"
             "      by + 1\n"
             "    )\n"
             "\n"
             "  }\n}\n")


def test_damaging_the_shared_program_changes_the_anchors():
    """Positive control: if the index did not read the shared program, replacing
    it in the memo would change nothing. The declarations come from it."""
    text = _source(3)
    clean = knowledge.index_text(NAME, text, None, CODES)
    damaged_program = Parser("service Z { fn g() -> Int }\n", "z.rvl").parse()
    with shared_parse.scope():
        shared_parse.program(text, NAME)
        shared_parse._MEMO.get()[(text, NAME)].program = damaged_program
        damaged = knowledge.index_text(NAME, text, None, CODES)
    assert damaged != clean


def test_damaging_the_shared_tokens_changes_the_spans():
    """The same control for the member spans: a member that runs over several
    lines collapses to its first line when the shared tokens are emptied, so the
    answer depends on them."""
    clean = knowledge.index_text(NAME, MULTILINE, None, CODES)
    assert clean[1]["anchor"]["symbol"] == "Adder.counter.bump", clean
    with shared_parse.scope():
        shared_parse.tokens(MULTILINE, NAME).clear()   # damage the share in place
        damaged = knowledge.index_text(NAME, MULTILINE, None, CODES)
    assert damaged != clean


def test_the_index_leaves_the_shared_parse_untouched():
    """`shared_parse` shares a result only with readers that do not mutate it;
    the index is one. The index reads what the call already parsed and hands
    the same objects back."""
    text = _source(3)
    with shared_parse.scope():
        program = shared_parse.program(text, NAME)
        tokens = shared_parse.tokens(text, NAME)
        before = pickle.dumps((program, list(tokens)))
        knowledge.index_text(NAME, text, None, CODES)
        assert pickle.dumps((program, tokens)) == before
        assert shared_parse.program(text, NAME) is program
        assert shared_parse.tokens(text, NAME) is tokens
