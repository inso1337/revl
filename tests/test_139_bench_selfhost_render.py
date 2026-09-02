"""Roadmap item 139 — `tools/bench_selfhost.py` must be able to render its own
parser corpus.

The bench's `_render` walks a *freshly parsed* AST. Item 75(a) split
`ExprArrow`'s written annotations from the checker's resolved ones, so
`param_types` is `None` on a parse-only AST and `written_param_types` carries
what the author wrote. The bench kept zipping `param_types`, so every one of the
10 arrow expressions in `PARSER_EXPRS` raised `TypeError: 'NoneType' object is
not iterable` — and because the parser gate runs before the table is printed,
the tool produced NO output at all on current main.

Nothing exercised the bench's renderer, which is the same harness-versus-
reference drift as item 271. This is the fixture: it renders every corpus entry,
which is fast (a parse per string) and needs none of the bench's stage
compilation. It fails on the pre-fix bench with the reported TypeError.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _bench():
    spec = importlib.util.spec_from_file_location(
        "revl_bench_selfhost_139", ROOT / "tools" / "bench_selfhost.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BENCH = _bench()


@pytest.mark.parametrize("src", BENCH.PARSER_EXPRS)
def test_every_parser_corpus_expression_renders(src):
    """`_reference_parse_render` swallows a `RevlError` as `(bad)`, but not a
    renderer bug — so a `TypeError` here is exactly item 139's failure."""
    rendered = BENCH._reference_parse_render(src)
    assert rendered and rendered != "(bad)", src


def test_the_arrow_corpus_renders_its_written_annotations():
    """The specific node: `param_types` is the checker's field and is `None` on
    a parsed AST, so the renderer must read `written_param_types`."""
    arrows = [s for s in BENCH.PARSER_EXPRS if "=>" in s and "match" not in s]
    assert arrows, "the parser corpus no longer covers arrows"
    for src in arrows:
        assert "(arrow " in BENCH._reference_parse_render(src), src
    # an annotated arrow keeps the author's spelling; an unannotated one is `_`
    assert BENCH._reference_parse_render("(v: Int) => v + 1") == \
        "(arrow (p v Int) (bin + (var v) (int 1)))"
    assert BENCH._reference_parse_render("x => x") == \
        "(arrow (p x _) (var x))"
