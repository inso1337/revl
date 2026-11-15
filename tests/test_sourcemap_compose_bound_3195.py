"""A crafted map cannot make `sourcemap.compose` quadratic (issue #3195).

`compose` rewrites every outer mapping that points into the generated file by
asking the inner map what that original position came from. It used to answer
each question by scanning the WHOLE inner line, so a pair of maps that keeps
every segment on generated line 0 (the normal shape of a minified bundle)
cost outer segments times inner segments. The module's header says these are
attacker-shaped build artifacts, and the VLQ decoder already bounds one value's
digits "so a reader is not made to allocate"; composition had no such bound on
work, so a dependency's `dist/` map could stall `revl sourcemap compose`.

The composer now indexes each inner line once and bisects it per mapping, so
the walk is linear in the documents' segment counts plus one bisection per
mapping. The check below measures that as COUNTED WORK, not as time, so the
machine cannot decide the outcome: it composes the one-line shape at two sizes
with every decoded position replaced by an int that counts its own ordering
comparisons, and asserts that doubling the input roughly doubles the
comparisons (below 3x) instead of quadrupling them. The seam is
`sourcemap.decode_mappings`, the module-level helper `compose` reads both
documents through on either side of the fix, so the same count runs against
the old whole-line scan (where nearly every comparison is
`segment[0] <= column`) and the indexed walk (where they are the one order
check per line and the bisections). An earlier version of this test pinned a
wall-clock ceiling; the old scan ran under it on a fast machine, so it did not
catch the defect it was written for.

The same test asserts the composed document against a resolver written here
from the format, and a control test pins that an out-of-order inner line
(which v3 forbids but this module has always tolerated) is still answered in
DOCUMENT order, so indexing does not buy the bound by changing the answer.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import sourcemap  # noqa: E402
from revl.sourcemap import (  # noqa: E402
    compose,
    decode_mappings,
    encode_mappings,
)

#: Two sizes, the second twice the first. The old scan costs n * n
#: comparisons (ratio 4); the indexed walk costs a few per segment plus one
#: bisection per mapping (ratio a little above 2, the log factor). Small sizes
#: keep the test fast; the ratio does not depend on the size.
_N = 1000
_MAX_RATIO = 3.0


class _CountingInt(int):
    """An int that counts every ordering comparison made on it, from Python
    or from C (`bisect`, `sorted`), in `_CountingInt.comparisons`."""

    comparisons = 0

    def __lt__(self, other):
        _CountingInt.comparisons += 1
        return int.__lt__(self, other)

    def __le__(self, other):
        _CountingInt.comparisons += 1
        return int.__le__(self, other)

    def __gt__(self, other):
        _CountingInt.comparisons += 1
        return int.__gt__(self, other)

    def __ge__(self, other):
        _CountingInt.comparisons += 1
        return int.__ge__(self, other)


def _counting_decode(real_decode):
    def decode(mappings, where="mappings"):
        return [[tuple(None if value is None else _CountingInt(value)
                       for value in segment)
                 for segment in line]
                for line in real_decode(mappings, where)]
    return decode


def _comparisons_to_compose(n: int, monkeypatch) -> tuple[int, dict]:
    outer, inner = _one_line_maps(n)
    with monkeypatch.context() as patch:
        patch.setattr(sourcemap, "decode_mappings",
                      _counting_decode(decode_mappings))
        _CountingInt.comparisons = 0
        composed = compose(outer, inner, "gen.ts")
        counted = _CountingInt.comparisons
    return counted, composed


def _lookup(lines: list[list[tuple]], line: int, column: int):
    """What a consumer answers, written from the format rather than from the
    module under test: the last segment on `line` at or before `column` in
    DOCUMENT order. Consumers do not interpolate."""
    if line < 0 or line >= len(lines):
        return None
    found = None
    for segment in lines[line]:
        if segment[0] <= column:
            found = segment
    return found


def _one_line_maps(n: int) -> tuple[dict, dict]:
    """An outer and an inner map that both keep every segment on line 0.

    A segment every two generated columns of the outer map points at the next
    column of `gen.ts`; a segment every two generated columns of the inner map
    points at the next line of `page.tpl`. Chained, each of the n outer
    mappings asks the inner line about a different column, which is the shape
    that made each query scan the whole line.
    """
    outer = {
        "version": 3,
        "file": "bundle.js",
        "sources": ["gen.ts"],
        "sourcesContent": [""],
        "names": [],
        "mappings": encode_mappings(
            [[(2 * i, 0, 0, i, None) for i in range(n)]]),
    }
    inner = {
        "version": 3,
        "file": "gen.ts",
        "sources": ["page.tpl"],
        "sourcesContent": [""],
        "names": [],
        "mappings": encode_mappings(
            [[(2 * i, 0, i, 0, None) for i in range(n)]]),
    }
    return outer, inner


def _assert_answers_the_walk(outer: dict, inner: dict, composed: dict,
                             columns) -> None:
    """Every sampled position answers what the two maps answer by hand."""
    outer_lines = decode_mappings(outer["mappings"])
    inner_lines = decode_mappings(inner["mappings"])
    composed_lines = decode_mappings(composed["mappings"])
    for column in columns:
        outer_hit = _lookup(outer_lines, 0, column)
        assert outer_hit is not None, column
        expected = _lookup(inner_lines, outer_hit[2], outer_hit[3])
        got = _lookup(composed_lines, 0, column)
        assert got is not None, (column, expected)
        if expected is None or expected[1] is None:
            assert got[1] is None, (column, got, expected)
            continue
        assert got[2:4] == expected[2:4], (column, got, expected)
        assert composed["sources"][got[1]] == inner["sources"][expected[1]], \
            (column, got, expected)


def test_a_one_line_map_cannot_make_composition_quadratic(monkeypatch):
    small, _ = _comparisons_to_compose(_N, monkeypatch)
    large, composed = _comparisons_to_compose(2 * _N, monkeypatch)

    assert small > 0, "the counting seam saw no comparison; it is not aimed"
    assert large < _MAX_RATIO * small, (
        f"composing {_N} then {2 * _N} outer segments against as many inner "
        f"segments on one line took {small} then {large} comparisons, a ratio "
        f"of {large / small:.2f}; linear work doubles, the per-mapping "
        "whole-line scan quadruples (see issue #3195)")

    outer, inner = _one_line_maps(2 * _N)
    assert "page.tpl" in composed["sources"], composed["sources"]
    assert "gen.ts" not in composed["sources"], composed["sources"]
    sampled = [0, 1, 2, 997, _N - 1, 2 * _N - 1, 4 * _N - 1]
    _assert_answers_the_walk(outer, inner, composed, sampled)


def test_an_out_of_order_inner_line_is_still_answered_in_document_order():
    """Control, holds on both sides of issue #3195's fix: indexing an inner
    line must not quietly replace the document-order answer with "largest
    column at or before", which agrees only on the sorted input v3 requires.

    The inner line's columns are 4, 1, 3. For column 4 the scan finds the
    segment at 3 last, even though the segment at 4 came first; a sorted
    lookup that dropped document order would answer the segment at 4.
    """
    inner = {
        "version": 3,
        "file": "gen.ts",
        "sources": ["page.tpl"],
        "sourcesContent": [""],
        "names": [],
        "mappings": encode_mappings(
            [[(4, 0, 10, 0, None), (1, 0, 11, 0, None),
              (3, 0, 12, 0, None)]]),
    }
    outer = {
        "version": 3,
        "file": "bundle.js",
        "sources": ["gen.ts"],
        "sourcesContent": [""],
        "names": [],
        "mappings": encode_mappings(
            [[(column, 0, 0, column, None) for column in range(6)]]),
    }

    composed = compose(outer, inner, "gen.ts")

    inner_lines = decode_mappings(inner["mappings"])
    assert _lookup(inner_lines, 0, 4)[2] == 12, (
        "the premise of the control: in document order the last mapping at or "
        "before column 4 is the one at 3, not the one at 4")
    _assert_answers_the_walk(outer, inner, composed, range(6))
