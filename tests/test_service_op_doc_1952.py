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

from revl.admission import _service_equal, _service_from_ir  # noqa: E402
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


def _methods(source: str, service: str = "Foo") -> dict:
    ir = compile_source(source, "<1952>.rvl")
    return ir["services"][service]["methods"]


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


def test_a_block_of_bare_slashes_is_a_block_with_nothing_in_it():
    """The absent key means "no block"; a run of bare `//` lines IS one.

    The contract defines the block as contiguous `//` lines, and a line that
    holds only the prefix is one of them, so the key is PRESENT and its text is
    what the prefix left — nothing. Stated here so the rule is a decision on the
    record rather than something a reader has to infer from `is not None`.
    """
    source = (
        'service Foo {\n'
        '  //\n'
        '  fn look(id: Str) -> Str\n'
        '}\n'
    )
    assert _methods(source)["look"]["doc"] == ""


def test_the_doc_survives_the_ir_to_declaration_round_trip():
    """`_service_from_ir` projects an IR entry back onto a ServiceDecl.

    A projection that dropped `doc` would lose the author's text on that path,
    and the IR is exactly the surface a host reads, so the rebuild is faithful
    to the entry it came from — including the absent key.
    """
    ir = compile_source(REPRODUCER, "<1952>.rvl")
    rebuilt = _service_from_ir("Foo", ir["services"]["Foo"])
    assert rebuilt.methods["look"].doc == (
        'Look up a record by its id. Example: "42".'
    )
    assert rebuilt.methods["send"].doc is None


def test_a_doc_only_change_is_not_an_interface_change():
    """`doc` is documentation, not interface.

    The admission gate compares two projections of an IR entry, so carrying the
    text through `_service_from_ir` must not make adding a comment to an
    operation read as a service replacement.
    """
    plain = compile_source(REPRODUCER_NO_COMMENT, "<1952>.rvl")
    documented = compile_source(REPRODUCER, "<1952>.rvl")
    assert _service_equal(
        _service_from_ir("Foo", plain["services"]["Foo"]),
        _service_from_ir("Foo", documented["services"]["Foo"]),
    )


def test_a_block_above_the_service_line_is_not_an_operations_doc():
    """The block belongs to the operation only when the operation opens its line.

    In `service Cache { fn size() -> Int }` the `fn` shares the `service` header
    line, so the block above that line is the SERVICE's — the issue's optional
    half, not implemented — and reading it as `size`'s would silently turn any
    comment above a `service` line into a member's doc. The damage would be
    visible, not cosmetic: `format_source(..., comments=False)` is the
    `revl_source` read path and drops comments, so the comment-free rendering of
    such a program would stop compiling to the same program (issue #1714).
    """
    one_line = (
        '// a doc comment\n'
        'service Cache { fn size() -> Int }\n'
    )
    assert "doc" not in _methods(one_line, "Cache")["size"]

    # a block INSIDE the service, directly above an operation that opens its
    # own line, is that operation's — the rule is about the line, not about
    # how far the block sits from the `fn`
    own_line = (
        'service Cache {\n'
        '  // a doc comment\n'
        '  fn size() -> Int\n'
        '}\n'
    )
    assert _methods(own_line, "Cache")["size"]["doc"] == "a doc comment"

    # a block above the `service` header is the SERVICE's block, so it is not
    # any member's, however the body is laid out
    above_header = (
        '// a doc comment\n'
        'service Cache {\n'
        '  fn size() -> Int\n'
        '}\n'
    )
    assert "doc" not in _methods(above_header, "Cache")["size"]

    # ... and a block between the header and a later operation still lands on
    # that operation, so the rule is about the LINE, not about the service
    later = (
        'service Cache { fn a() -> Int\n'
        '  // doc for b\n'
        '  fn b() -> Int }\n'
    )
    assert "doc" not in _methods(later, "Cache")["a"]
    assert _methods(later, "Cache")["b"]["doc"] == "doc for b"


def test_control_a_block_above_an_own_line_operation_is_that_operations_doc():
    """Control (i) for the declaration bound: it must not over-suppress.

    Bounding the walk at the enclosing `service` line is what keeps a block above
    a one-line `service ... { fn ... }` off the member; the honest failure mode
    of that bound is a bound set one line too high, which would silently lose the
    block for an operation that DOES open its own line inside the body.
    """
    source = (
        'service Cache {\n'
        '  fn evict(k: Str) -> Bool\n'
        '  // Size of the cache, in entries.\n'
        '  fn size() -> Int\n'
        '}\n'
    )
    methods = _methods(source, "Cache")
    assert methods["size"]["doc"] == "Size of the cache, in entries."
    assert "doc" not in methods["evict"]


def test_control_a_blank_line_between_the_block_and_the_operation_means_no_doc():
    """Control (ii) for the declaration bound: the block must be DIRECTLY above.

    A blank line between the block and the `fn` line ends the block, so the
    operation carries no `doc` — and the text does not leak onto the operation
    below it either, which is the shape a bound set too low would produce.
    """
    source = (
        'service Cache {\n'
        '  // A note about the file, separated from the operation by a blank line.\n'
        '\n'
        '  fn size() -> Int\n'
        '}\n'
    )
    assert "doc" not in _methods(source, "Cache")["size"]
