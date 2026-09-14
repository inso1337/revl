"""Chaining the template's map into the bundler's (roadmap item 459 F2, #722).

`stdlib/template.rvl`'s `render_mapped`/`source_map` landed the INSERTION-SITE
half of F2: a Source Map v3 document saying which template position produced
each byte of the rendered output. Design note 459 and docs/frontend-assets.md
both close that section with the same sentence, and it is the gap this suite is
the guard for:

    "A template rendered into a `.ts` that Vite then bundles has two maps and
    nothing chains them, so devtools follow Vite's back to the generated file
    and stop."

`src/revl/sourcemap.py` chains them, and `revl sourcemap compose` is the door.
The claim under test is narrow and checkable: the composed map answers, for
every generated position, exactly what a consumer would answer by walking the
two maps BY HAND.

    compose(outer, inner) . resolve(l, c)  ==  inner . resolve(outer . resolve(l, c))

That equality is the definition, not an approximation of one, so it is what is
driven here position by position over a corpus whose INNER maps are produced by
the real `stdlib/template.rvl` (compiled and executed on the py tier) rather
than hand-written to agree.

How the checks avoid agreeing with the code they check:

  * the base64-VLQ reader, the mapping decoder and the resolver below are
    written from the Source Map v3 field layout and share no code with
    `revl.sourcemap`. A decoder derived from the encoder agrees with any
    encoder;
  * the inner maps come from the shipped stdlib module, so a change that made
    the composer and the template map agree with each other while disagreeing
    with the format would still be caught;
  * a neutering proof: nineteen deliberately broken copies of the composer, each
    required to fail at least one check, against a baseline asserting the
    shipped module fails none. A composition gate that no breakage can trip is
    the failure mode this repository has shipped before.

One leg builds for real. With the frontend toolchain installed it renders a
template into a `.ts`, runs a real `vite build` over it, and composes the
bundler's own map with the template's, then asserts a position in the BUNDLE
resolves to the line of the `.tpl` that produced it, with the hole's name. That
is the whole claim of F2's remainder, executed rather than described. It is
gated exactly like the other frontend legs (`REVL_REQUIRE_FRONTEND_TOOLCHAIN=1`
turns the skip off in the `frontend-assets` job).

CONFINEMENT. A source map is a build artifact whose `sources`, `sourceRoot`,
`file` and `sourceMappingURL` fields are attacker-shaped strings that look like
paths. The composer never opens one: `test_composition_opens_no_path_a_map_names`
makes every filesystem door raise for the duration of a `compose` call, and one
of the nineteen mutations is a composer that fills a missing `sourcesContent`
from disk, so that check is known to be able to fail.

The control tests at the end exercise only surface that existed before this
change, so they hold on both sides of it and a green run is not explained by the
harness composing nothing.
"""

from __future__ import annotations

import builtins
import functools
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

TEMPLATE = ROOT / "stdlib" / "template.rvl"
ESCAPE = ROOT / "stdlib" / "escape.rvl"
COMPOSER = ROOT / "src" / "revl" / "sourcemap.py"
FRONTEND = ROOT / "examples" / "app" / "frontend"

#: Same gate as tests/test_app_frontend_725.py: a real `vite build` needs the
#: installed tree, and a skip is the right answer on a contributor's machine and
#: the wrong one in the job that exists to run it.
_npm = shutil.which("npm")
_REQUIRE_TOOLCHAIN = os.environ.get("REVL_REQUIRE_FRONTEND_TOOLCHAIN") == "1"
_HAVE_TOOLCHAIN = _npm is not None and (FRONTEND / "node_modules" / "vite").is_dir()
needs_frontend_toolchain = pytest.mark.skipif(
    not _HAVE_TOOLCHAIN and not _REQUIRE_TOOLCHAIN,
    reason="needs the frontend node toolchain: run "
           "`npm ci` in examples/app/frontend",
)


# ------------------------------------------------------- independent machinery
#
# Written from the Source Map v3 field layout. Shares no code with
# src/revl/sourcemap.py, which is the point.

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _read_vlq(field: str) -> list[int]:
    values, shift, acc, started = [], 0, 0, False
    for ch in field:
        digit = _B64.index(ch)
        started = True
        acc += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        values.append(-(acc >> 1) if acc & 1 else acc >> 1)
        shift, acc, started = 0, 0, False
    assert not started, f"unterminated VLQ in {field!r}"
    return values


def _write_vlq(value: int) -> str:
    rest = (-value * 2) + 1 if value < 0 else value * 2
    out = ""
    while True:
        digit = rest % 32
        rest //= 32
        if rest:
            digit += 32
        out += _B64[digit]
        if not rest:
            return out


def _read_mappings(mappings: str) -> list[list[tuple]]:
    """One list of absolute segments per generated line."""
    lines, src, oline, ocol, name = [], 0, 0, 0, 0
    for raw in mappings.split(";"):
        segments, column = [], 0
        if raw:
            for field in raw.split(","):
                values = _read_vlq(field)
                assert len(values) in (1, 4, 5), (field, values)
                column += values[0]
                if len(values) == 1:
                    segments.append((column, None, None, None, None))
                    continue
                src += values[1]
                oline += values[2]
                ocol += values[3]
                this = None
                if len(values) == 5:
                    name += values[4]
                    this = name
                segments.append((column, src, oline, ocol, this))
        lines.append(segments)
    return lines


def _write_mappings(lines: list[list[tuple]]) -> str:
    out, src, oline, ocol, name = [], 0, 0, 0, 0
    for segments in lines:
        column, fields = 0, []
        for gcol, s, ol, oc, n in segments:
            field = _write_vlq(gcol - column)
            column = gcol
            if s is not None:
                field += _write_vlq(s - src)
                src = s
                field += _write_vlq(ol - oline)
                oline = ol
                field += _write_vlq(oc - ocol)
                ocol = oc
                if n is not None:
                    field += _write_vlq(n - name)
                    name = n
            fields.append(field)
        out.append(",".join(fields))
    return ";".join(out)


def _lookup(lines: list[list[tuple]], line: int, column: int):
    """What a consumer answers: the last segment on `line` at or before
    `column`. Consumers do not interpolate."""
    if line < 0 or line >= len(lines):
        return None
    found = None
    for segment in lines[line]:
        if segment[0] <= column:
            found = segment
    return found


def _fold(document: dict) -> list[str | None]:
    """`sources` with `sourceRoot` folded in, as plain text."""
    root = document.get("sourceRoot") or ""
    out = []
    for source in document["sources"]:
        if source is None:
            out.append(None)
        elif root and "://" not in source and not source.startswith("/"):
            out.append(os.path.normpath(root.rstrip("/") + "/" + source)
                       .replace(os.sep, "/"))
        elif "://" in source:
            out.append(source)
        else:
            out.append(os.path.normpath(source).replace(os.sep, "/"))
    return out


def _answer(document: dict, line: int, column: int):
    """`(source_name, original_line, original_column, name)` or None."""
    hit = _lookup(_read_mappings(document["mappings"]), line, column)
    if hit is None or hit[1] is None:
        return None
    sources = _fold(document)
    assert hit[1] < len(sources), f"source index {hit[1]} out of range"
    name = None
    if hit[4] is not None:
        assert hit[4] < len(document["names"]), "name index out of range"
        name = document["names"][hit[4]]
    return (sources[hit[1]], hit[2], hit[3], name)


def _by_hand(outer: dict, inner: dict, generated: str, line: int, column: int):
    """The two-step walk a consumer would do holding both documents."""
    step = _answer(outer, line, column)
    if step is None:
        return None
    source, oline, ocol, name = step
    if not (source == generated or source.endswith("/" + generated)):
        return step
    through = _answer(inner, oline, ocol)
    if through is None:
        return None
    return (through[0], through[1], through[2],
            through[3] if through[3] is not None else name)


# ------------------------------------------------- the real template inner map

CONSUMER = """\
use "stdlib/template.rvl" {
  Binding, TemplateError, Rendered, render_mapped, source_map,
}

fn tpl_render_mapped(t: Str, vs: List[Binding]) -> Result[Rendered, TemplateError] {
  return render_mapped(t, vs)
}

fn tpl_source_map(r: Rendered, s: Str, g: Str) -> Str { return source_map(r, s, g) }

fn bind(n: Str, v: Str) -> Binding { return { name: n, value: v } }
"""

#: only the surface that existed before this change, so a green control run says
#: nothing about the composer.
CONTROL_CONSUMER = """\
use "stdlib/template.rvl" { Binding, TemplateError, render }

fn c_render(t: Str, vs: List[Binding]) -> Result[Str, TemplateError] {
  return render(t, vs)
}

fn c_bind(n: Str, v: Str) -> Binding { return { name: n, value: v } }
"""


def _exec_python(ir: dict, label: str):
    spec = importlib.util.spec_from_file_location(
        f"pyemit_{label}", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), f"{label}.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@functools.lru_cache(maxsize=None)
def _template_ns(consumer: str = CONSUMER):
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="revl-sourcemap-chain-"))
    main = work / "main.rvl"
    main.write_text(consumer, encoding="utf-8")
    return _exec_python(compile_files([str(main)]), "sourcemap_chain")


def _render(template: str, bindings: dict, source: str, generated: str):
    """(rendered text, the template's own Source Map v3 document)."""
    namespace = _template_ns()
    result = namespace["tpl_render_mapped"](
        template, [namespace["bind"](k, v) for k, v in bindings.items()])
    assert type(result).__name__ == "Ok", result
    rendered = result.value
    document = json.loads(namespace["tpl_source_map"](rendered, source, generated))
    return rendered["text"], document


# ------------------------------------------------------------------ the corpus


def _bundle_map(text: str, generated: str, *, vendor="vendor/runtime.js"):
    """A synthetic bundler's map over `text`, as a Source Map v3 document.

    It is deliberately not a faithful model of any bundler. What it has to be is
    VARIED, because the composition is a per-mapping rewrite: mappings into the
    generated file at many columns (including columns past the end of a line and
    a generated line the inner map never reaches), mappings into a second source
    that must survive untouched, 1-field mappings that carry no original at all,
    and an outer NAME on a mapping whose inner counterpart has none.
    """
    lines = text.split("\n")
    out: list[list[tuple]] = [[
        # the banner: another source entirely, and one mapping with no original.
        (0, 1, 0, 0, None), (4, 1, 0, 4, 0), (9, None, None, None, None),
    ]]
    for i, line in enumerate(lines):
        segments = [(0, 0, i, 0, None)]
        for column in (2, 5, 11, 19, 20, 21, 34, 60):
            if column <= len(line) + 2:
                # the outer name rides on one column so the "inner name wins,
                # outer name survives when there is none" rule is exercised.
                segments.append((column, 0, i, column, 0 if column == 5 else None))
        out.append(sorted(set(segments)))
    # a mapping into a generated line the inner map never reaches, and one into
    # a column far past anything it recorded: both must come out BLANK.
    out.append([(0, 0, len(lines) + 7, 0, None)])
    out.append([(0, 1, 3, 3, None), (8, 0, len(lines) + 9, 40, None)])
    return {
        "version": 3,
        "file": "bundle.js",
        "sources": [generated, vendor],
        "sourcesContent": [text, "/* vendor */\n"],
        "names": ["bundlerSymbol"],
        "mappings": _write_mappings(out),
    }


class _Case:
    def __init__(self, label, template, bindings, generated, source="page.tpl"):
        self.label = label
        self.template = template
        self.bindings = bindings
        self.generated = generated
        self.source = source
        self.text, self.inner = _render(template, bindings, source, "generated.ts")
        self.outer = _bundle_map(self.text, generated)


_CASE_SPECS = (
    ("one hole on a multi-line template",
     '// generated, do not edit\nexport const WHO = "{{script:who}}";\n'
     'export function greet(): string {\n  return "hello " + WHO;\n}\n',
     {"who": 'world "x" </script>'},
     "generated.ts"),
    ("the bundler spells the input relative to the bundle",
     'header\nexport const {{uri:sym}} = 1;\nfooter\n',
     {"sym": "theAnswer"},
     "../build/generated.ts"),
    ("a value carrying its own line breaks",
     'a\n<pre>{{html:body}}</pre>\nb\n',
     {"body": "first\nsecond\nthird"},
     "generated.ts"),
    ("adjacent holes and an empty value",
     'x\n{{html:a}}{{html:b}}{{html:c}}\ny\n',
     {"a": "1", "b": "", "c": "3"},
     "generated.ts"),
    ("non-ASCII before the holes",
     'café ✓\nété {{html:n}} fin\n',
     {"n": "né"},
     "generated.ts"),
    ("a template that is only a hole",
     '{{html:only}}',
     {"only": "just this"},
     "generated.ts"),
    ("CRLF template",
     'one\r\ntwo {{html:v}}\r\nthree',
     {"v": "V"},
     "generated.ts"),
)


@functools.lru_cache(maxsize=None)
def _corpus() -> tuple:
    return tuple(_Case(*spec) for spec in _CASE_SPECS)


def _sweep(text: str):
    """Every generated position of the bundle, plus a few off the end."""
    lines = text.split("\n")
    for line in range(len(lines) + 4):
        width = len(lines[line - 1]) if 1 <= line <= len(lines) else 4
        for column in range(width + 3):
            yield line, column


# --------------------------------------------------------------- the composer

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load(COMPOSER, "revl_sourcemap_under_test")


# ------------------------------------------------------------------ the checks
#
# Each takes the composer module, so a mutated copy can be run through exactly
# the checks the shipped one is.

def _compose_all(mod):
    return [(case, mod.compose(case.outer, case.inner, case.generated))
            for case in _corpus()]


def _assert_the_composed_map_answers_the_two_step_walk(mod):
    for case, composed in _compose_all(mod):
        for line, column in _sweep(case.text):
            assert _answer(composed, line, column) == \
                _by_hand(case.outer, case.inner, case.generated, line, column), \
                f"{case.label}: generated ({line},{column})"


def _assert_mappings_into_other_sources_pass_through(mod):
    for case, composed in _compose_all(mod):
        kept = [_answer(composed, 0, c) for c in (0, 4, 8)]
        assert kept[0] == ("vendor/runtime.js", 0, 0, None), case.label
        assert kept[1] == ("vendor/runtime.js", 0, 4, "bundlerSymbol"), case.label
        # the 1-field mapping at column 9 still shadows what came before it.
        assert _answer(composed, 0, 9) is None, case.label
        assert _answer(composed, 0, 20) is None, case.label


def _assert_the_generated_file_is_replaced_by_the_template(mod):
    for case, composed in _compose_all(mod):
        sources = _fold(composed)
        assert case.source in sources, (case.label, sources)
        for source in sources:
            assert source is None or not source.endswith("generated.ts"), \
                (case.label, sources)
        assert "vendor/runtime.js" in sources, (case.label, sources)


def _assert_sources_content_is_the_template_not_the_generated_text(mod):
    for case, composed in _compose_all(mod):
        at = _fold(composed).index(case.source)
        content = composed["sourcesContent"][at]
        assert content == case.template, case.label
        # the point of the whole exercise: the reader is shown the file with the
        # holes still in it, not the rendered output the bundler read.
        if "{{" in case.template:
            assert "{{" in content, case.label


def _assert_every_index_the_document_carries_is_in_range(mod):
    for case, composed in _compose_all(mod):
        sources = composed["sources"]
        names = composed["names"]
        for segments in _read_mappings(composed["mappings"]):
            for gcol, src, oline, ocol, name in segments:
                assert gcol >= 0, case.label
                if src is None:
                    assert (oline, ocol, name) == (None, None, None), case.label
                    continue
                assert 0 <= src < len(sources), (case.label, src)
                assert oline >= 0 and ocol >= 0, case.label
                if name is not None:
                    assert 0 <= name < len(names), (case.label, name)


def _assert_an_uncovered_mapping_is_blanked_not_deleted(mod):
    """The outer map's last two lines point at generated lines the inner map
    never reaches. They must come out as 1-field mappings at the same generated
    columns: deleting them would let the previous mapping on the line spill
    over a region nothing knows anything about."""
    for case, composed in _compose_all(mod):
        lines = _read_mappings(composed["mappings"])
        blanked = lines[-2]
        assert blanked == [(0, None, None, None, None)], (case.label, blanked)
        tail = lines[-1]
        assert len(tail) == 2, (case.label, tail)
        assert tail[0][1] is not None, (case.label, tail)   # the vendor mapping
        assert tail[1] == (8, None, None, None, None), (case.label, tail)


def _assert_the_document_is_a_valid_source_map_v3(mod):
    for case, composed in _compose_all(mod):
        text = json.dumps(composed)
        again = json.loads(text)
        assert again["version"] == 3, case.label
        assert again["file"] == "bundle.js", case.label
        assert isinstance(again["mappings"], str), case.label
        assert len(again["sourcesContent"]) == len(again["sources"]), case.label
        assert "sourceRoot" not in again, case.label
        # one generated line per `;`-separated run, and the bundle's own line
        # count is what the outer map had.
        assert again["mappings"].count(";") == \
            case.outer["mappings"].count(";"), case.label


def _assert_the_mappings_are_deltas_not_absolutes(mod):
    """A map whose fields were absolute would still decode under a decoder that
    also treated them as absolute, so this reads the wire form directly: the
    second mapping of the banner line sits at generated column 4 and the third
    at 9, so the encoded deltas are 4 and 5, never 4 and 9."""
    case, composed = _compose_all(mod)[0]
    first_line = composed["mappings"].split(";")[0]
    fields = first_line.split(",")
    assert len(fields) == 3, fields
    assert _read_vlq(fields[0])[0] == 0, fields
    assert _read_vlq(fields[1])[0] == 4, fields
    assert _read_vlq(fields[2])[0] == 5, fields


def _assert_the_inner_name_wins_and_the_outer_survives_alone(mod):
    """Rendering `{{uri:sym}}` at a column the bundler also mapped gives one
    position with a name on BOTH sides; the template's hole name is the one that
    names something in the file the composed map opens."""
    case = _corpus()[1]
    composed = mod.compose(case.outer, case.inner, case.generated)
    names = set(composed["names"])
    assert "sym" in names, names
    # the outer's own name rides on a mapping whose inner counterpart has none,
    # and must not be lost with it.
    assert "bundlerSymbol" in names, names
    found = [a for a in (_answer(composed, line, column)
                         for line, column in _sweep(case.text))
             if a is not None and a[3] == "sym"]
    assert found, "no composed mapping carries the hole's name"
    for source, _, _, _ in found:
        assert source == case.source


def _assert_source_root_is_folded_into_the_names(mod):
    case = _corpus()[0]
    outer = dict(case.outer)
    outer["sourceRoot"] = "https://example.invalid/root"
    outer["sources"] = ["generated.ts", "vendor/runtime.js"]
    inner = dict(case.inner)
    inner["sourceRoot"] = "tpl"
    composed = mod.compose(outer, inner, "https://example.invalid/root/generated.ts")
    assert "sourceRoot" not in composed
    assert "tpl/page.tpl" in composed["sources"], composed["sources"]
    assert "https://example.invalid/root/vendor/runtime.js" in composed["sources"], \
        composed["sources"]


def _assert_a_document_it_cannot_trust_is_refused(mod):
    case = _corpus()[0]

    def refused(outer, inner, generated=None):
        try:
            mod.compose(outer, inner, generated)
        except Exception as error:      # noqa: BLE001 - any refusal counts
            assert isinstance(error, mod.SourceMapError), repr(error)
            return str(error)
        raise AssertionError("composed a document it should have refused")

    refused({**case.outer, "version": 2}, case.inner, case.generated)
    refused(case.outer, {**case.inner, "version": "3"}, case.generated)
    refused({**case.outer, "sections": []}, case.inner, case.generated)
    refused(case.outer, case.inner, "nothing/like/it.ts")
    refused({**case.outer, "mappings": "A!A"}, case.inner, case.generated)
    refused({**case.outer, "mappings": "g"}, case.inner, case.generated)
    refused({**case.outer, "mappings": "ggggggggB"}, case.inner, case.generated)
    refused({**case.outer, "mappings": "AAAA,"}, case.inner, case.generated)
    refused({**case.outer, "mappings": _write_mappings([[(0, 0, 0, 0, None)]])
                                       + ",AAD"}, case.inner, case.generated)
    refused({**case.outer, "mappings": _write_mappings([[(0, 9, 0, 0, None)]])},
            case.inner, case.generated)
    refused({**case.outer, "mappings": _write_mappings([[(0, 0, 0, 0, 9)]])},
            case.inner, case.generated)
    refused({**case.outer, "sourcesContent": ["only one"]}, case.inner,
            case.generated)
    # an inner map with no `file` cannot say which source it describes.
    refused(case.outer, {k: v for k, v in case.inner.items() if k != "file"})
    # a suffix that matches two different sources is ambiguous, not a coin toss.
    ambiguous = {**case.outer,
                 "sources": ["a/generated.ts", "b/generated.ts"],
                 "sourcesContent": [None, None]}
    refused(ambiguous, case.inner, "generated.ts")


def _assert_composition_opens_no_path_a_map_names(mod):
    """Every filesystem door is made to raise for the duration of the call.

    A source map arrives from a bundler, so its `sources` are attacker-shaped
    strings that look like paths. A composer that opened one to fill in a
    missing `sourcesContent` would be a file-read primitive reachable from a
    build artifact.
    """
    case = _corpus()[0]
    hostile = {
        **case.outer,
        "sourceRoot": "/etc",
        "sources": ["generated.ts", "../../../../etc/passwd"],
        "sourcesContent": [None, None],
        "file": "/etc/shadow",
    }
    hostile_inner = {**case.inner, "sources": ["/etc/hosts"],
                     "sourcesContent": [None]}

    doors = [(builtins, "open"), (io, "open"), (os, "open"), (os, "stat"),
             (os, "listdir"), (os, "scandir"), (pathlib.Path, "open"),
             (pathlib.Path, "read_text"), (pathlib.Path, "read_bytes")]
    saved = [(owner, attribute, getattr(owner, attribute))
             for owner, attribute in doors]

    def refuse(*args, **kwargs):
        raise AssertionError("the composer touched the filesystem")

    try:
        for owner, attribute, _ in saved:
            setattr(owner, attribute, refuse)
        composed = mod.compose(hostile, hostile_inner, "/etc/generated.ts")
    finally:
        for owner, attribute, original in saved:
            setattr(owner, attribute, original)

    assert "/etc/hosts" in composed["sources"], composed["sources"]
    for content in composed["sourcesContent"]:
        assert content is None or content == "/* vendor */\n", content


def _assert_two_generated_files_chain_in_sequence(mod):
    """A bundle built from two rendered files is composed by applying the step
    twice, so the door has to be usable that way: neither step may disturb the
    other's source."""
    first, second = _corpus()[0], _corpus()[2]
    outer = {
        "version": 3,
        "file": "bundle.js",
        "sources": ["one.ts", "two.ts"],
        "sourcesContent": [first.text, second.text],
        "names": [],
        "mappings": _write_mappings([
            [(0, 0, 1, 0, None), (6, 1, 1, 0, None)],
            [(0, 1, 1, 2, None), (3, 0, 1, 4, None)],
        ]),
    }
    once = mod.compose(outer, first.inner, "one.ts")
    twice = mod.compose(once, second.inner, "two.ts")
    sources = _fold(twice)
    assert sources.count("page.tpl") == 1, sources
    assert not any(s.endswith(".ts") for s in sources if s), sources
    for line, column in ((0, 0), (0, 6), (1, 0), (1, 3)):
        assert _answer(twice, line, column) is not None, (line, column)


CHECKS = (
    ("the composed map answers the two-step walk",
     _assert_the_composed_map_answers_the_two_step_walk),
    ("mappings into other sources pass through",
     _assert_mappings_into_other_sources_pass_through),
    ("the generated file is replaced by the template",
     _assert_the_generated_file_is_replaced_by_the_template),
    ("sourcesContent is the template, not the generated text",
     _assert_sources_content_is_the_template_not_the_generated_text),
    ("every index the document carries is in range",
     _assert_every_index_the_document_carries_is_in_range),
    ("an uncovered mapping is blanked, not deleted",
     _assert_an_uncovered_mapping_is_blanked_not_deleted),
    ("the document is a valid source map v3",
     _assert_the_document_is_a_valid_source_map_v3),
    ("the mappings are deltas, not absolutes",
     _assert_the_mappings_are_deltas_not_absolutes),
    ("the inner name wins and the outer survives alone",
     _assert_the_inner_name_wins_and_the_outer_survives_alone),
    ("sourceRoot is folded into the names",
     _assert_source_root_is_folded_into_the_names),
    ("a document it cannot trust is refused",
     _assert_a_document_it_cannot_trust_is_refused),
    ("composition opens no path a map names",
     _assert_composition_opens_no_path_a_map_names),
    ("two generated files chain in sequence",
     _assert_two_generated_files_chain_in_sequence),
)


@pytest.mark.parametrize("label,check", CHECKS, ids=[c[0] for c in CHECKS])
def test_check(mod, label, check):
    check(mod)


# ---------------------------------------------------------------- the CLI door

def _revl(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, "-P", "-m", "revl", *argv],
        cwd=str(cwd or ROOT), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})


def test_the_cli_composes_and_writes_one_document(tmp_path):
    case = _corpus()[0]
    (tmp_path / "bundle.js.map").write_text(json.dumps(case.outer),
                                            encoding="utf-8")
    (tmp_path / "generated.ts.map").write_text(json.dumps(case.inner),
                                               encoding="utf-8")
    out = tmp_path / "chained.map"
    done = _revl("sourcemap", "compose", str(tmp_path / "bundle.js.map"),
                 "--through", str(tmp_path / "generated.ts.map"),
                 "-o", str(out))
    assert done.returncode == 0, done.stderr
    composed = json.loads(out.read_text(encoding="utf-8"))
    assert _fold(composed) == ["vendor/runtime.js", "page.tpl"], composed["sources"]
    assert composed["sourcesContent"][_fold(composed).index("page.tpl")] == \
        case.template


def test_the_cli_refuses_a_generated_name_the_outer_map_does_not_carry(tmp_path):
    """Composing against a source that is not there would return the outer map
    unchanged, which reads like a success and ships a map that chains nothing."""
    case = _corpus()[0]
    (tmp_path / "b.map").write_text(json.dumps(case.outer), encoding="utf-8")
    (tmp_path / "i.map").write_text(json.dumps(case.inner), encoding="utf-8")
    done = _revl("sourcemap", "compose", str(tmp_path / "b.map"),
                 "--through", f"not-there.ts={tmp_path / 'i.map'}")
    assert done.returncode == 1, done.stdout
    assert "no source named" in done.stderr, done.stderr
    assert done.stdout.strip() == "", done.stdout


def test_the_cli_refuses_a_map_it_cannot_read(tmp_path):
    case = _corpus()[0]
    (tmp_path / "i.map").write_text(json.dumps(case.inner), encoding="utf-8")
    done = _revl("sourcemap", "compose", str(tmp_path / "absent.map"),
                 "--through", str(tmp_path / "i.map"))
    assert done.returncode == 1
    assert "cannot read" in done.stderr, done.stderr


# --------------------------------------------------------- the real bundler leg

_VITE_CONFIG = """\
export default {
  build: {
    sourcemap: true,
    minify: false,
    outDir: 'dist',
    emptyOutDir: true,
    lib: { entry: './generated.ts', formats: ['es'], fileName: 'chained' },
  },
}
"""

_E2E_TEMPLATE = """\
// rendered from page.tpl by stdlib/template.rvl - do not edit
export const WHO = "{{script:who}}";
export const {{uri:sym}} = 41 + 1;
export function greet(): string {
  return "hello " + WHO;
}
"""


@needs_frontend_toolchain
def test_a_bundled_stack_position_walks_all_the_way_back_to_the_template(tmp_path):
    """The whole of F2's remainder, executed.

    Render a template into a `.ts`, let a REAL `vite build` bundle it, compose
    the bundler's map with the template's, and resolve a position in the BUNDLE.
    Before the composition it names `generated.ts`, a file nobody wrote; after
    it, the `.tpl` line that produced it, with the hole's name and the template
    text (holes intact) inlined for a reader who cannot fetch it.
    """
    assert (FRONTEND / "node_modules" / "vite").is_dir(), (
        "the frontend toolchain is missing: run `npm ci` in "
        "examples/app/frontend (this leg is required here because "
        "REVL_REQUIRE_FRONTEND_TOOLCHAIN=1)")

    project = tmp_path / "project"
    project.mkdir()
    (project / "node_modules").symlink_to(FRONTEND / "node_modules")
    (project / "vite.config.mjs").write_text(_VITE_CONFIG, encoding="utf-8")
    (project / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")

    text, inner = _render(
        _E2E_TEMPLATE, {"who": 'world "x" </script>', "sym": "theAnswer"},
        "page.tpl", "generated.ts")
    (project / "page.tpl").write_text(_E2E_TEMPLATE, encoding="utf-8")
    (project / "generated.ts").write_text(text, encoding="utf-8")
    (project / "generated.ts.map").write_text(json.dumps(inner), encoding="utf-8")

    build = subprocess.run(
        [str(FRONTEND / "node_modules" / ".bin" / "vite"), "build",
         "--config", "vite.config.mjs"],
        cwd=str(project), capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr

    bundle_map = next((project / "dist").glob("chained.*js.map"))
    outer = json.loads(bundle_map.read_text(encoding="utf-8"))

    # before: the bundler's own map stops at the generated file.
    assert any(s.endswith("generated.ts") for s in outer["sources"]), \
        outer["sources"]
    assert not any(s.endswith("page.tpl") for s in outer["sources"]), \
        outer["sources"]

    done = _revl("sourcemap", "compose", str(bundle_map),
                 "--through", f"generated.ts={project / 'generated.ts.map'}",
                 "-o", str(project / "chained.map"))
    assert done.returncode == 0, done.stderr
    composed = json.loads((project / "chained.map").read_text(encoding="utf-8"))

    # after: one document, naming the template and carrying its text with the
    # holes still in it.
    assert _fold(composed) == ["page.tpl"], composed["sources"]
    assert composed["sourcesContent"] == [_E2E_TEMPLATE]

    # and the mapping the bundler recorded for `theAnswer` resolves to the hole
    # that produced it, by name.
    named = [_answer(composed, line, column)
             for line, column in _sweep(
                 (project / "dist" / bundle_map.name[: -len(".map")])
                 .read_text(encoding="utf-8"))]
    hole_line = _E2E_TEMPLATE.split("\n").index("export const {{uri:sym}} = 41 + 1;")
    hits = [a for a in named if a is not None and a[3] == "sym"]
    assert hits, [a for a in named if a is not None]
    for source, line, column, _ in hits:
        assert source == "page.tpl"
        assert line == hole_line, (line, hole_line)

    # every composed mapping names the template and no mapping was left behind
    # pointing at the generated file.
    for answer in named:
        assert answer is None or answer[0] == "page.tpl", answer


# ----------------------------------------------------------------- the controls
#
# Pre-existing surface only: they hold before and after this change, so a run in
# which they are the only green tests is a run in which nothing was composed.

def test_control_the_template_module_still_renders_and_escapes_per_context():
    namespace = _template_ns(CONTROL_CONSUMER)
    result = namespace["c_render"](
        "<p>{{html:x}}</p>", [namespace["c_bind"]("x", "<&>")])
    assert type(result).__name__ == "Ok", result
    assert result.value == "<p>&lt;&amp;&gt;</p>"


def test_control_the_cli_still_answers_its_own_version():
    done = _revl("version", "--help")
    assert done.returncode == 0, done.stderr


# ------------------------------------------------------------ the surface pins

def test_the_composer_is_reachable_as_a_library_and_as_a_verb():
    from revl.sourcemap import SourceMapError, compose, decode_mappings  # noqa: F401

    done = _revl("sourcemap", "compose", "--help")
    assert done.returncode == 0, done.stderr
    assert "--through" in done.stdout


# ---------------------------------------------------------- the neutering proof

def _sub(old, new):
    def mutate(text):
        assert old in text, f"neutering anchor not found: {old!r}"
        return text.replace(old, new, 1)
    return mutate


MUTATIONS = {
    "N01 resolve keeps the FIRST segment at or before the column": _sub(
        "    found = None\n"
        "    for segment in lines[line]:\n"
        "        if segment[0] <= column:\n"
        "            found = segment\n"
        "    return found",
        "    found = None\n"
        "    for segment in lines[line]:\n"
        "        if segment[0] <= column and found is None:\n"
        "            found = segment\n"
        "    return found"),
    "N02 resolve is off by one at the segment boundary": _sub(
        "    for segment in lines[line]:\n"
        "        if segment[0] <= column:\n"
        "            found = segment",
        "    for segment in lines[line]:\n"
        "        if segment[0] < column:\n"
        "            found = segment"),
    "N03 resolve ignores the generated line": _sub(
        "    if line < 0 or line >= len(lines):\n        return None",
        "    if line < 0 or line >= len(lines):\n        return None\n"
        "    line = 0"),
    "N04 an uncovered mapping is deleted instead of blanked": _sub(
        "                out_segments.append((gcol, None, None, None, None))\n"
        "                continue\n"
        "            if hit[1] >= len(inner_sources):",
        "                continue\n"
        "            if hit[1] >= len(inner_sources):"),
    "N05 an uncovered mapping keeps pointing at the generated file": _sub(
        "                out_segments.append((gcol, None, None, None, None))\n"
        "                continue\n"
        "            if hit[1] >= len(inner_sources):",
        "                out_segments.append((gcol, 0, oline, ocol, None))\n"
        "                continue\n"
        "            if hit[1] >= len(inner_sources):"),
    "N06 the composed mapping keeps the OUTER original position": _sub(
        "            out_segments.append(\n"
        "                (gcol, inner_index[hit[1]], hit[2], hit[3], name))",
        "            out_segments.append(\n"
        "                (gcol, inner_index[hit[1]], oline, ocol, name))"),
    "N07 the inner name is never preferred": _sub(
        "            chosen = inner_name if inner_name is not None else outer_name",
        "            chosen = outer_name"),
    "N08 the outer name is dropped on a pass-through mapping": _sub(
        "                name = None if outer_name is None else _add_name(outer_name)",
        "                name = None"),
    "N09 encode_mappings writes the original column absolute": _sub(
        "                field += _encode_vlq(ocol - original_column)\n"
        "                original_column = ocol",
        "                field += _encode_vlq(ocol)\n"
        "                original_column = ocol"),
    "N10 encode_mappings writes the original line absolute": _sub(
        "                field += _encode_vlq(oline - original_line)\n"
        "                original_line = oline",
        "                field += _encode_vlq(oline)\n"
        "                original_line = oline"),
    "N11 encode_mappings loses the generated line boundaries": _sub(
        '    return ";".join(out)', '    return ",".join(out)'),
    "N12 the VLQ sign bit is dropped": _sub(
        "    rest = (-value << 1) + 1 if value < 0 else value << 1",
        "    rest = (-value << 1) if value < 0 else value << 1"),
    "N13 the VLQ continuation bit is ignored on the way in": _sub(
        "        if index & 32:\n            shift += 5\n            continue",
        "        if False:\n            shift += 5\n            continue"),
    "N14 a missing sourcesContent is filled in from disk": _sub(
        "    def _add_source(name: str | None, content) -> int:\n"
        "        if name in index_of:",
        "    def _add_source(name: str | None, content) -> int:\n"
        "        if content is None and name:\n"
        "            try:\n"
        "                with open(name, encoding='utf-8') as handle:\n"
        "                    content = handle.read()\n"
        "            except OSError:\n"
        "                content = None\n"
        "        if name in index_of:"),
    "N15 the inner sources are never added": _sub(
        "    inner_index: dict[int, int] = {}\n"
        "    for i, name in enumerate(inner_sources):\n"
        "        inner_index[i] = _add_source(name, inner_contents[i])",
        "    inner_index: dict[int, int] = {}\n"
        "    for i, name in enumerate(inner_sources):\n"
        "        inner_index[i] = _add_source(name, None)"),
    "N16 the generated file stays in the composed sources": _sub(
        "    for i, name in enumerate(outer_sources):\n"
        "        if i in targets:\n"
        "            continue",
        "    for i, name in enumerate(outer_sources):\n"
        "        if False:\n"
        "            continue"),
    "N17 a --generated that names nothing passes the outer map through": _sub(
        "    if not targets:\n"
        "        listed = ", "    if False:\n"
        "        listed = "),
    "N18 an ambiguous --generated picks one": _sub(
        "    if len(distinct) > 1:\n        raise SourceMapError(",
        "    if False:\n        raise SourceMapError("),
    "N19 sourceRoot is ignored": _sub(
        '    if root and "://" not in source and not source.startswith("/"):\n'
        '        joined = root.rstrip("/") + "/" + source',
        '    if False:\n'
        '        joined = root.rstrip("/") + "/" + source'),
}


def _failed_checks(module):
    failed = []
    for label, check in CHECKS:
        try:
            check(module)
        except Exception:               # noqa: BLE001 - any failure counts
            failed.append(label)
    return failed


def test_the_proof_has_a_baseline_the_shipped_composer_passes_every_check(mod):
    """Half one. With no mutation every check holds, so a mutation failing is
    evidence about the mutation and not about the harness."""
    assert _failed_checks(mod) == []


@pytest.mark.parametrize("label", sorted(MUTATIONS), ids=sorted(MUTATIONS))
def test_a_broken_composer_is_caught(tmp_path, label):
    """Half two. Each deliberately broken composer fails at least one check. A
    composition gate that no breakage can trip is worse than no gate, because it
    is green."""
    mutated = MUTATIONS[label](COMPOSER.read_text(encoding="utf-8"))
    path = tmp_path / "broken_sourcemap.py"
    path.write_text(mutated, encoding="utf-8")
    try:
        broken = _load(path, f"broken_sourcemap_{abs(hash(label))}")
    except Exception:                   # noqa: BLE001 - refusing to import counts
        return
    assert _failed_checks(broken), f"{label} was not caught by any check"
