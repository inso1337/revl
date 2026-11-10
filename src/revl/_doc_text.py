"""Text helpers shared by the document importers and the type generator.

`revl.import_openapi` uses and re-exports these; they live here so
`revl.gen_types`, which every compile reaches through `revl.compiler`, does not
import the whole OpenAPI importer to get them (issue #2196).
"""

from __future__ import annotations

import json
import re


_COMMENT_LINE_BREAK = re.compile(r"[\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]+")


def _comment_safe(text: object) -> str:
    """Collapse every line terminator in `text` to a single space.

    Item 416f (OpenAPI codegen injection). The generated `.rvl` carries
    document-derived strings (`info.title`, `info.version`, `openapi`, the
    source filename, resolved server URLs, schema-note text) verbatim inside
    `//` comments and inside `@<backend> { ... }` stub bodies. A `//` comment
    runs to the next newline (`lexer.py:351`), so a newline embedded in ANY of
    those strings ends the comment and drops whatever follows it onto its own
    line of compiled source. An attacker who supplies the OpenAPI document (the
    ordinary case: a vendor's published spec imported with `revl import
    openapi`) could therefore inject a top-level `component`, or an
    `extern ... = @py { ... }` whose host body runs arbitrary code the first
    time the importing developer does `revl run`. Demonstrated end to end via
    `info.title`, `info.version`, `openapi` and `servers[].url`.

    A comment is single-line by construction, so the fix is to make every
    document-derived value that reaches a comment single-line too: everything
    `str.splitlines` treats as a line boundary (CR, LF, VT, FF, FS, GS, RS,
    U+0085, U+2028, U+2029) becomes a space. All of these are line boundaries
    for `str.splitlines`, which the emitters use to split a verbatim body;
    leaving any of them in a comment would let the emitted module execute text
    the `.rvl` source shows as a comment (issue #3131). This only narrows the
    comment text; it changes nothing about the typed boundary the importer
    generates, which never interpolates these strings."""
    return _COMMENT_LINE_BREAK.sub(" ", str(text))


def _pointer(*parts: object) -> str:
    """A JSON pointer, RFC 6901-escaped.

    An OpenAPI document has no line numbers that survive `json.loads`, so a
    pointer is the authoritative "where" in every refusal; `_line_of` adds a
    best-effort line on top of it.
    """
    out = "#"
    for part in parts:
        token = str(part).replace("~", "~0").replace("/", "~1")
        out += f"/{token}"
    return out


def _line_of(source: str, pointer: str) -> int:
    """Best-effort source line for a JSON pointer.

    A greedy forward scan for each key in turn: good enough to put a reader's
    cursor in the right region of a hand-written document, and never trusted
    for more than that: the pointer in the message is the precise location.
    """
    if not source:
        return 0
    position = 0
    for token in pointer.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        found = source.find(json.dumps(key), position)
        if found < 0:
            break
        position = found
    return source.count("\n", 0, position) + 1 if position else 0
