"""Chaining a generated file's source map into the bundler's (item 459, F2).

`stdlib/template.rvl` answers "which line of which template produced this byte
of the output" and writes the answer as a Source Map v3 document. That map is
about ONE step: rendered text <- template. A frontend build has a second step,
bundled asset <- the files the bundler read, and the bundler writes its own map
for it. When the rendered text is itself one of those files, a browser holds two
maps and composes neither: devtools follow the bundler's map back to the
GENERATED file and stop there, which is the exact provenance gap the
string-embedded frontend had one level down.

This module composes them. Given

    outer:  bundle.js      <- generated.ts, vue-runtime.js, ...   (the bundler's)
    inner:  generated.ts   <- page.html.tpl                       (the template's)

`compose` returns

    bundle.js  <- page.html.tpl, vue-runtime.js, ...

so one map walks all the way back. It is a TOOLCHAIN step, not a stdlib one:
the inner map is produced by a revl program at render time and the outer one by
a bundler afterwards, so nothing in either process sees both. `revl sourcemap
compose` is the door (docs/commands-reference.md).

--------------------------------------------------------------- what it answers

A source-map consumer resolves a generated position by taking the LAST mapping
on that line at or before the column. It does not interpolate: between two
mappings the relationship to the original is not knowable from the map. So the
composition is defined by the walk a consumer would do by hand:

    compose(outer, inner) . resolve(l, c)  ==  inner . resolve(outer . resolve(l, c))

and that equality is what `tests/test_sourcemap_chain_459.py` drives, position by
position, with a resolver and a VLQ reader written from the format rather than
from this file. Anything else would be a composer that agrees only with itself.

Three consequences follow from taking that as the definition rather than as a
property to approximate:

  * A mapping into the generated file that the inner map does not cover is
    BLANKED, not deleted: it becomes a 1-field mapping, a generated column with
    no original position. The two wrong answers it sits between are worth
    naming. Keeping it as it was would leave a mapping pointing at a file the
    composed map no longer lists, which is worse than an unmapped byte, because
    an unmapped byte shows the reader nothing and a dangling one shows them the
    wrong file. Deleting it outright would let the PREVIOUS mapping on the line
    spill forward over a region it says nothing about, which is the same lie
    told more quietly.
  * A 1-field mapping (a generated column with no original position) is kept as
    it is, and it also SHADOWS earlier mappings on its line when the inner map
    is consulted, because that is what "this region has no original" means to a
    consumer.
  * The name comes from the inner mapping when it has one, and from the outer
    one otherwise. The inner map is the one closer to the source a reader is
    being sent to, so its name is the one that names something in the file the
    composed map opens.

--------------------------------------------------------------- confinement

A source map is a BUILD ARTIFACT: it arrives from a bundler, from a dependency's
`dist/`, or from whatever wrote the file being debugged. Its `sources`,
`sourceRoot`, `file` and `sourceMappingURL` fields are attacker-shaped strings
that LOOK like paths.

This module never opens one. Source names are compared as text and copied as
text; `sourcesContent` is only ever the content the two documents handed in
already carried. A composer that resolved `sources` against the filesystem to
fill in missing content would be a file-read primitive reachable from a build
artifact, which is the same class of defect the asset jail exists to prevent —
the difference being that here there is no jail to get right, because there is
no filesystem access to jail. The CLI reads exactly the paths given on ITS
command line and nothing a document names.

`sourceRoot` is folded into the source strings and dropped from the output, so
the composed document cannot be read two ways.

Everything else fails closed: a version that is not 3, an index map
(`sections`), a malformed VLQ run, a field count outside {1, 4, 5}, an index
outside its array, a delta that drives a line or column negative, and a
`--generated` naming no source in the outer map are all refusals naming what was
wrong, never a silently unchanged document.
"""

from __future__ import annotations

import posixpath
from typing import Any

__all__ = [
    "SourceMapError",
    "compose",
    "decode_mappings",
    "encode_mappings",
    "resolve",
]

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_INDEX = {ch: i for i, ch in enumerate(_B64)}

#: A signed 32-bit value needs seven 5-bit digits and no more. A run longer than
#: that is not a large number, it is a malformed document (or one built to make
#: a reader allocate), so it is refused rather than decoded.
_MAX_VLQ_DIGITS = 7


class SourceMapError(Exception):
    """A source map document this module refuses, naming what is wrong."""


# --------------------------------------------------------------- VLQ + mappings

def _decode_vlq_field(field: str, where: str) -> list[int]:
    """Every signed value in one comma-free `mappings` field, in order."""
    values: list[int] = []
    shift = 0
    acc = 0
    digits = 0
    for ch in field:
        index = _B64_INDEX.get(ch)
        if index is None:
            raise SourceMapError(
                f"{where}: {ch!r} is not a base64 digit, so the `mappings` "
                f"field {field!r} is not a VLQ run")
        digits += 1
        if digits > _MAX_VLQ_DIGITS:
            raise SourceMapError(
                f"{where}: VLQ run {field!r} carries more than "
                f"{_MAX_VLQ_DIGITS} digits for one value")
        acc += (index & 31) << shift
        if index & 32:
            shift += 5
            continue
        values.append(-(acc >> 1) if acc & 1 else acc >> 1)
        shift = 0
        acc = 0
        digits = 0
    if digits:
        raise SourceMapError(
            f"{where}: VLQ run {field!r} ends inside a value (the last digit "
            "asks for a continuation that is not there)")
    return values


def _encode_vlq(value: int) -> str:
    rest = (-value << 1) + 1 if value < 0 else value << 1
    out = []
    while True:
        digit = rest & 31
        rest >>= 5
        if rest:
            digit |= 32
        out.append(_B64[digit])
        if not rest:
            return "".join(out)


def decode_mappings(mappings: str, where: str = "mappings") -> list[list[tuple]]:
    """`mappings` as one list of segments per generated line, absolute.

    A segment is `(generated_column, source, original_line, original_column,
    name)` where `source`, `original_line`, `original_column` and `name` are
    `None` for a 1-field segment and `name` is `None` for a 4-field one. The
    deltas the format carries are resolved here, so nothing downstream has to
    remember which fields reset at a line boundary (only the generated column
    does).
    """
    if not isinstance(mappings, str):
        raise SourceMapError(f"{where}: `mappings` must be a string")
    lines: list[list[tuple]] = []
    source = 0
    original_line = 0
    original_column = 0
    name = 0
    for line_number, raw in enumerate(mappings.split(";")):
        segments: list[tuple] = []
        column = 0
        if raw:
            for field in raw.split(","):
                if not field:
                    raise SourceMapError(
                        f"{where}: empty segment on generated line "
                        f"{line_number}")
                values = _decode_vlq_field(
                    field, f"{where} line {line_number}")
                if len(values) not in (1, 4, 5):
                    raise SourceMapError(
                        f"{where}: generated line {line_number} carries a "
                        f"{len(values)}-field segment; Source Map v3 allows "
                        "1, 4 or 5")
                column += values[0]
                if column < 0:
                    raise SourceMapError(
                        f"{where}: generated line {line_number} has a negative "
                        "generated column")
                if len(values) == 1:
                    segments.append((column, None, None, None, None))
                    continue
                source += values[1]
                original_line += values[2]
                original_column += values[3]
                if source < 0 or original_line < 0 or original_column < 0:
                    raise SourceMapError(
                        f"{where}: generated line {line_number} resolves to a "
                        "negative source index, line or column")
                this_name = None
                if len(values) == 5:
                    name += values[4]
                    if name < 0:
                        raise SourceMapError(
                            f"{where}: generated line {line_number} resolves "
                            "to a negative name index")
                    this_name = name
                segments.append(
                    (column, source, original_line, original_column, this_name))
        lines.append(segments)
    return lines


def encode_mappings(lines: list[list[tuple]]) -> str:
    """The inverse of `decode_mappings`: absolute segments back to deltas."""
    out: list[str] = []
    source = 0
    original_line = 0
    original_column = 0
    name = 0
    for segments in lines:
        column = 0
        fields: list[str] = []
        for segment in segments:
            gcol, src, oline, ocol, nindex = segment
            field = _encode_vlq(gcol - column)
            column = gcol
            if src is not None:
                field += _encode_vlq(src - source)
                source = src
                field += _encode_vlq(oline - original_line)
                original_line = oline
                field += _encode_vlq(ocol - original_column)
                original_column = ocol
                if nindex is not None:
                    field += _encode_vlq(nindex - name)
                    name = nindex
            fields.append(field)
        out.append(",".join(fields))
    return ";".join(out)


def resolve(lines: list[list[tuple]], line: int, column: int):
    """What a consumer answers for a generated position: the last segment on
    `line` at or before `column`, or `None` when there is none.

    No interpolation, because the format does not license any: between two
    mappings the correspondence is unstated.

    The whole line is scanned rather than stopped at the first segment past
    `column`. Source Map v3 requires the segments of a line to be ordered by
    generated column and every generator emits them that way, so the two read
    the same on a well-formed document; on a malformed one, scanning makes the
    rule "the last segment at or before the column" true as written instead of
    true only for sorted input.
    """
    if line < 0 or line >= len(lines):
        return None
    found = None
    for segment in lines[line]:
        if segment[0] <= column:
            found = segment
    return found


# ------------------------------------------------------------- source naming

def _normalise_source(root: str, source: Any, where: str) -> str | None:
    """One source name, with `sourceRoot` folded in, as TEXT.

    Nothing here touches a filesystem: `posixpath.normpath` is string algebra
    over `/`, and a `..` that walks off the front stays in the name rather than
    being refused or resolved against anything real. A source name is a LABEL a
    devtools client may or may not be able to fetch, not a path this process
    opens.
    """
    if source is None:
        return None
    if not isinstance(source, str):
        raise SourceMapError(f"{where}: a `sources` entry must be a string or null")
    joined = source
    if root and "://" not in source and not source.startswith("/"):
        joined = root.rstrip("/") + "/" + source
    if "://" in joined:
        return joined
    return posixpath.normpath(joined)


def _sources_of(document: dict, where: str) -> tuple[list[str | None], list]:
    sources = document.get("sources")
    if not isinstance(sources, list):
        raise SourceMapError(f"{where}: `sources` must be an array")
    root = document.get("sourceRoot") or ""
    if not isinstance(root, str):
        raise SourceMapError(f"{where}: `sourceRoot` must be a string")
    names = [_normalise_source(root, s, where) for s in sources]
    contents = document.get("sourcesContent")
    if contents is None:
        contents = [None] * len(sources)
    if not isinstance(contents, list):
        raise SourceMapError(f"{where}: `sourcesContent` must be an array")
    if len(contents) != len(sources):
        raise SourceMapError(
            f"{where}: `sourcesContent` has {len(contents)} entries for "
            f"{len(sources)} sources; the two arrays are positional")
    return names, list(contents)


def _check_document(document: Any, where: str) -> None:
    if not isinstance(document, dict):
        raise SourceMapError(f"{where}: not a JSON object")
    if "sections" in document:
        raise SourceMapError(
            f"{where}: this is an INDEX map (`sections`). Composing one means "
            "composing each section against its own offset, which this does "
            "not do; flatten it first rather than getting a map that is right "
            "only for the first section")
    version = document.get("version")
    if version != 3:
        raise SourceMapError(
            f"{where}: `version` is {version!r}, not 3")


def _names_of(document: dict, where: str) -> list[str]:
    names = document.get("names", [])
    if not isinstance(names, list):
        raise SourceMapError(f"{where}: `names` must be an array")
    for entry in names:
        if not isinstance(entry, str):
            raise SourceMapError(f"{where}: every `names` entry must be a string")
    return list(names)


def _match_generated(sources: list[str | None], wanted: str) -> list[int]:
    """Which entries of `sources` name the generated file `wanted`.

    Exact (normalised) equality first. Failing that, a WHOLE-SEGMENT suffix
    match, because a bundler writes its inputs relative to the bundle and a
    caller holds the name the renderer used: `dist/../build/page.ts` and
    `page.ts` are the same file written from two directories. A suffix that
    matches more than one distinct source is refused as ambiguous rather than
    resolved by picking one.
    """
    target = _normalise_source("", wanted, "--generated")
    exact = [i for i, s in enumerate(sources) if s is not None and s == target]
    if exact:
        return exact
    suffix = "/" + target
    hits = [i for i, s in enumerate(sources)
            if s is not None and s.endswith(suffix)]
    distinct = {sources[i] for i in hits}
    if len(distinct) > 1:
        raise SourceMapError(
            "--generated " + repr(wanted) + " matches more than one source of "
            "the outer map (" + ", ".join(sorted(str(d) for d in distinct)) +
            "); name it exactly")
    return hits


# ------------------------------------------------------------------- compose

def compose(outer: Any, inner: Any, generated: str | None = None) -> dict:
    """`outer` with every mapping into `generated` rewritten through `inner`.

    `outer` is the bundler's map (bundle <- its inputs) and `inner` is the map
    of ONE of those inputs (the generated file <- what produced it). `generated`
    names that input as `outer` spells it; it defaults to `inner`'s own `file`.

    Mappings into any other source pass through untouched, which is what makes
    the result a map of the whole bundle rather than of the generated fragment.
    """
    _check_document(outer, "outer map")
    _check_document(inner, "inner map")

    if generated is None:
        generated = inner.get("file")
        if not isinstance(generated, str) or not generated:
            raise SourceMapError(
                "the inner map has no `file`, so there is nothing to say which "
                "source of the outer map it describes; pass --generated")

    outer_sources, outer_contents = _sources_of(outer, "outer map")
    inner_sources, inner_contents = _sources_of(inner, "inner map")
    outer_names = _names_of(outer, "outer map")
    inner_names = _names_of(inner, "inner map")

    targets = set(_match_generated(outer_sources, generated))
    if not targets:
        listed = ", ".join(repr(s) for s in outer_sources) or "(none)"
        raise SourceMapError(
            f"the outer map has no source named {generated!r}; its sources are "
            f"{listed}. Composing against a source that is not there would "
            "return the outer map unchanged, which reads like a success")

    outer_lines = decode_mappings(outer["mappings"], "outer map")
    inner_lines = decode_mappings(inner["mappings"], "inner map")

    # The composed `sources`: what the outer map named, minus the generated
    # file, plus what the inner map named. Built up front rather than in
    # first-use order so the document is a function of its inputs and not of
    # which mapping happened to come first.
    composed_sources: list[str | None] = []
    composed_contents: list = []
    index_of: dict[str | None, int] = {}

    def _add_source(name: str | None, content) -> int:
        if name in index_of:
            at = index_of[name]
            if composed_contents[at] is None and content is not None:
                composed_contents[at] = content
            return at
        index_of[name] = len(composed_sources)
        composed_sources.append(name)
        composed_contents.append(content)
        return index_of[name]

    outer_index: dict[int, int] = {}
    for i, name in enumerate(outer_sources):
        if i in targets:
            continue
        outer_index[i] = _add_source(name, outer_contents[i])
    inner_index: dict[int, int] = {}
    for i, name in enumerate(inner_sources):
        inner_index[i] = _add_source(name, inner_contents[i])

    composed_names: list[str] = []
    name_index: dict[str, int] = {}

    def _add_name(name: str) -> int:
        if name not in name_index:
            name_index[name] = len(composed_names)
            composed_names.append(name)
        return name_index[name]

    def _named(names: list[str], index: int | None, where: str) -> str | None:
        if index is None:
            return None
        if index >= len(names):
            raise SourceMapError(
                f"{where}: name index {index} is outside a `names` array of "
                f"{len(names)}")
        return names[index]

    composed_lines: list[list[tuple]] = []
    for gline, segments in enumerate(outer_lines):
        out_segments: list[tuple] = []
        for gcol, src, oline, ocol, nindex in segments:
            if src is None:
                out_segments.append((gcol, None, None, None, None))
                continue
            if src >= len(outer_sources):
                raise SourceMapError(
                    f"outer map: generated line {gline} names source {src}, "
                    f"outside a `sources` array of {len(outer_sources)}")
            outer_name = _named(outer_names, nindex, "outer map")
            if src not in targets:
                name = None if outer_name is None else _add_name(outer_name)
                out_segments.append(
                    (gcol, outer_index[src], oline, ocol, name))
                continue
            hit = resolve(inner_lines, oline, ocol)
            if hit is None or hit[1] is None:
                # No inner mapping covers it. Blanking is the fail-closed
                # answer: the generated file is not in the composed `sources`,
                # so keeping the mapping would point a reader at a file the
                # document does not carry, and deleting it would hand the
                # region to whichever mapping came before it on this line.
                out_segments.append((gcol, None, None, None, None))
                continue
            if hit[1] >= len(inner_sources):
                raise SourceMapError(
                    f"inner map: a mapping names source {hit[1]}, outside a "
                    f"`sources` array of {len(inner_sources)}")
            inner_name = _named(inner_names, hit[4], "inner map")
            chosen = inner_name if inner_name is not None else outer_name
            name = None if chosen is None else _add_name(chosen)
            out_segments.append(
                (gcol, inner_index[hit[1]], hit[2], hit[3], name))
        composed_lines.append(out_segments)

    document = {
        "version": 3,
        "sources": composed_sources,
        "sourcesContent": composed_contents,
        "names": composed_names,
        "mappings": encode_mappings(composed_lines),
    }
    if isinstance(outer.get("file"), str):
        document["file"] = outer["file"]
    return document
