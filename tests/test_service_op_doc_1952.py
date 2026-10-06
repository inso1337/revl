"""Issue #1952: the IR keeps the comment block above a service operation.

A host that offers a composition's service operations to someone else — as MCP
tools, a CLI, or a generated client — reads the operation's name, its parameter
names and types, its return type and its class from the IR, and until this issue
nothing the author wrote about it: the lexer drops `//` comments outright, so
the IR had no text to carry. The parser now recovers the block from the source
and the IR carries it as a `doc` string on the operation.

Both halves of the contract are pinned here:

* the block DIRECTLY above the operation — contiguous `//` lines with no blank
  line between them and the `fn` — with the `// ` prefix removed and the line
  breaks kept;
* the key is ABSENT when there is no such block, so an IR without operation
  comments is byte-identical. That half is asserted on the SERIALIZED JSON, not
  on the dict, because "absent" is a property of the document a host reads.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402

# The issue's own reproducer, comment included.
REPRODUCER = (
    'service Foo {\n'
    '  // Look up a record by its id. Example: "42".\n'
    '  fn look(id: Str) -> Str\n'
    '  emission fn send(to: Str, body: Str) -> Str\n'
    '}\n'
)

# The same service with no operation comment: the IR must not move.
REPRODUCER_NO_COMMENT = (
    'service Foo {\n'
    '  fn look(id: Str) -> Str\n'
    '  emission fn send(to: Str, body: Str) -> Str\n'
    '}\n'
)


def _methods(source: str) -> dict:
    ir = compile_source(source, "<1952>.rvl")
    return ir["services"]["Foo"]["methods"]


def test_the_comment_above_an_operation_reaches_the_ir():
    """The issue's reproducer: `look`'s block is in the IR, prefix stripped."""
    methods = _methods(REPRODUCER)
    assert methods["look"]["doc"] == 'Look up a record by its id. Example: "42".'
    # The operation below it carries no comment of its own, so it gains no key.
    assert "doc" not in methods["send"]
    # The rest of the operation is untouched by the new key.
    assert methods["look"]["params"] == [{"name": "id", "type": "Str"}]
    assert methods["look"]["returns"] == "Str"
    assert methods["look"]["emission"] is False


def test_a_multiline_block_keeps_its_line_breaks_and_only_the_prefix_goes():
    """Contiguous `//` lines join with `\\n`; exactly the `// ` prefix is removed.

    The block must survive as the author's paragraphs, so the interior
    indentation of a continuation line is kept (only the first space after the
    slashes is the prefix), and a `//` written without the space still yields
    its text rather than a leading blank.
    """
    source = (
        'service Foo {\n'
        '  // line one\n'
        '  //   indented continuation\n'
        '  //no space after the slashes\n'
        '  //\n'
        '  fn look(id: Str) -> Str\n'
        '}\n'
    )
    assert _methods(source)["look"]["doc"] == (
        "line one\n  indented continuation\nno space after the slashes\n"
    )


def test_a_blank_line_ends_the_block():
    """The block must sit DIRECTLY above the operation: a blank line ends it.

    The two lines below are a comment about the file, not documentation of
    `look`, and the IR says so by carrying no `doc` at all.
    """
    source = (
        'service Foo {\n'
        '  // This paragraph is separated from the operation by a blank line.\n'
        '\n'
        '  fn look(id: Str) -> Str\n'
        '}\n'
    )
    assert "doc" not in _methods(source)["look"]


def test_a_comment_inside_the_body_is_not_a_doc():
    """A trailing comment after the signature is not the block above it."""
    source = (
        'service Foo {\n'
        '  fn look(id: Str) -> Str\n'
        '  // a note about the operation below, not above `look`\n'
        '  fn find(name: Str) -> Str\n'
        '}\n'
    )
    methods = _methods(source)
    assert "doc" not in methods["look"]
    assert methods["find"]["doc"] == (
        "a note about the operation below, not above `look`"
    )


def test_a_doc_survives_on_an_emission_operation():
    """`doc` is independent of the operation's class."""
    source = (
        'service Foo {\n'
        '  // Send the message. Crosses the boundary.\n'
        '  emission fn send(to: Str, body: Str) -> Str\n'
        '}\n'
    )
    method = _methods(source)["send"]
    assert method["doc"] == "Send the message. Crosses the boundary."
    assert method["emission"] is True


def test_an_ir_without_operation_comments_is_byte_identical():
    """Contract half (b), on the serialized JSON a host actually reads.

    `doc` is added to the document only where a block exists: the serialization
    of a service whose operations carry no comment is exactly the string it was
    before #1952, and the whole IR contains no `doc` key anywhere.
    """
    ir = compile_source(REPRODUCER_NO_COMMENT, "<1952>.rvl")
    assert '"doc"' not in json.dumps(ir, sort_keys=True)
    assert json.dumps(ir["services"], sort_keys=True) == (
        '{"Foo": {"methods": {'
        '"look": {"emission": false, "params": [{"name": "id", "type": "Str"}], '
        '"returns": "Str"}, '
        '"send": {"emission": true, "params": [{"name": "to", "type": "Str"}, '
        '{"name": "body", "type": "Str"}], "returns": "Str"}}}}'
    )
