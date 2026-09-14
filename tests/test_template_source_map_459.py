"""The insertion-site source map (roadmap item 459 F2, issue #722).

Stage 1 of item 459 recorded `Hole.start`/`Hole.end` and said, in the module's
own header, that they exist "so a later stage can map generated output back to
[the site]", with "nothing maps those offsets to line/column yet". Design note
459 calls that F2 and names the missing half twice: mapping an insertion site to
a line/column in the original, and feeding it to something that consumes a
source map. This suite is the guard for that half.

The claim is NOT Vite's. `build.sourcemap: true` maps a bundle back to the
frontend sources it was built from, which says nothing about where in a TEMPLATE
a rendered byte came from. `render_mapped` answers that, and `source_map` writes
the answer as a Source Map v3 document a bundler or a devtools client reads.

What is pinned here, and how:

  * `position_at` is offset -> 0-based line/column in CODEPOINTS, breaking on
    U+000A only, and it REFUSES an out-of-range offset rather than clamping;
  * `render_mapped` produces byte-identical text to `render` (it is the same
    renderer, and the suite proves the two cannot diverge) and refuses the same
    inputs in the same order;
  * the emitted map is decoded by an INDEPENDENT base64-VLQ reader written here
    from the Source Map v3 field layout, sharing no code with the module, and
    every generated position of a hand-built oracle render is resolved through
    it back to the template position it came from;
  * the map is valid JSON whose `sourcesContent` round-trips a template holding
    quotes, backslashes, a `</script>` and non-ASCII;
  * a neutering proof: thirteen deliberately broken copies of the module, each of
    which must fail at least one check above. A map check that cannot fail is
    the failure mode this repo has shipped before.

Two control tests exercise only the pre-existing surface, so they hold both
before and after the change and a green run is not explained by the harness
compiling nothing.
"""

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

MODULE = ROOT / "stdlib" / "template.rvl"
ESCAPE = ROOT / "stdlib" / "escape.rvl"

HOLE_RE = re.compile(r"\{\{([a-z]+):([A-Za-z_][A-Za-z0-9_.]*)\}\}")

#: a consumer reaching the F2 surface through one named `use`.
CONSUMER = """\
use "stdlib/template.rvl" {
  Binding, TemplateError, Pos, Segment, Rendered, Hole,
  position_at, render_mapped, source_map, render, scan,
}

fn tpl_position_at(t: Str, o: Int) -> Result[Pos, TemplateError] {
  return position_at(t, o)
}

fn tpl_render_mapped(t: Str, vs: List[Binding]) -> Result[Rendered, TemplateError] {
  return render_mapped(t, vs)
}

fn tpl_source_map(r: Rendered, s: Str, g: Str) -> Str { return source_map(r, s, g) }

fn tpl_render(t: Str, vs: List[Binding]) -> Result[Str, TemplateError] {
  return render(t, vs)
}

fn tpl_scan(t: Str) -> Result[List[Hole], TemplateError] { return scan(t) }

fn bind(n: Str, v: Str) -> Binding { return { name: n, value: v } }
"""

#: the pre-existing surface only. Compiles against the module as it stood
#: BEFORE this change, which is what makes the control tests controls.
CONTROL_CONSUMER = """\
use "stdlib/template.rvl" { Binding, TemplateError, Hole, scan, render }

fn c_scan(t: Str) -> Result[List[Hole], TemplateError] { return scan(t) }

fn c_render(t: Str, vs: List[Binding]) -> Result[Str, TemplateError] {
  return render(t, vs)
}

fn c_bind(n: Str, v: Str) -> Binding { return { name: n, value: v } }
"""


def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_template_map", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "template_map.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def _build(tmp_path, consumer, template_text=None):
    escape_text = ESCAPE.read_text(encoding="utf-8")
    (tmp_path / "stdlib").mkdir(exist_ok=True)
    (tmp_path / "stdlib" / "template.rvl").write_text(
        template_text if template_text is not None
        else MODULE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "stdlib" / "escape.rvl").write_text(escape_text, encoding="utf-8")
    # template.rvl's own `use` resolves against ITS directory first, so the
    # escape copy has to exist one level deeper too or the import falls through
    # to the shipped stdlib and a mutation to the copy would not take effect.
    nested = tmp_path / "stdlib" / "stdlib"
    nested.mkdir(exist_ok=True)
    (nested / "escape.rvl").write_text(escape_text, encoding="utf-8")
    main = tmp_path / "main.rvl"
    main.write_text(consumer, encoding="utf-8")
    return _exec_python(compile_files([str(main)]))


@pytest.fixture(scope="module")
def ns(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("tpl_map"), CONSUMER)


@pytest.fixture(scope="module")
def control_ns(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("tpl_map_control"), CONTROL_CONSUMER)


def _ok(result):
    assert type(result).__name__ == "Ok", f"expected Ok, got {result!r}"
    return result.value


def _err(result):
    assert type(result).__name__ == "Err", f"expected Err, got {result!r}"
    return result.value


# ------------------------------------------------------- independent machinery
#
# Everything below is written from the Source Map v3 field layout and from
# stdlib/escape.rvl's documented per-context rules. It shares no code with the
# module under test: that is the point, because a decoder derived from the
# encoder would agree with any encoder.

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _decode_vlq(field: str):
    """Every signed value in one VLQ field run, in order."""
    values, shift, acc = [], 0, 0
    for ch in field:
        digit = _B64.index(ch)
        acc += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        values.append(-(acc >> 1) if acc & 1 else acc >> 1)
        shift, acc = 0, 0
    assert acc == 0 and shift == 0, f"unterminated VLQ in {field!r}"
    return values


def _decode_mappings(mappings: str):
    """[(gen_line, gen_col, src, orig_line, orig_col, name_or_None)] in order."""
    out = []
    src = orig_line = orig_col = name = 0
    for gen_line, line in enumerate(mappings.split(";")):
        gen_col = 0
        if not line:
            continue
        for field in line.split(","):
            v = _decode_vlq(field)
            assert len(v) in (1, 4, 5), (field, v)
            gen_col += v[0]
            if len(v) == 1:
                out.append((gen_line, gen_col, None, None, None, None))
                continue
            src += v[1]
            orig_line += v[2]
            orig_col += v[3]
            this_name = None
            if len(v) == 5:
                name += v[4]
                this_name = name
            out.append((gen_line, gen_col, src, orig_line, orig_col, this_name))
    return out


def _resolve(decoded, line, column):
    """The mapping a source-map consumer returns for a generated position: the
    last one on `line` at or before `column`. Consumers do not interpolate."""
    best = None
    for entry in decoded:
        if entry[0] == line and entry[1] <= column:
            best = entry
    return best


def _line_col(text: str, offset: int):
    nl = text.rfind("\n", 0, offset)
    return (text.count("\n", 0, offset), offset - (nl + 1))


def _py_escape_html(s: str) -> str:
    out = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out.replace('"', "&quot;").replace("'", "&#39;")


_JS_FIXED = {
    "\\": "\\\\", '"': '\\"', "<": "\\u003C", ">": "\\u003E", "&": "\\u0026",
    "\n": "\\n", "\r": "\\r", "\t": "\\t",
    "\u2028": "\\u2028", "\u2029": "\\u2029",   # JS line terminators
}


def _py_escape_js(s: str) -> str:
    out = []
    for ch in s:
        if ch in _JS_FIXED:
            out.append(_JS_FIXED[ch])
        elif ord(ch) < 32:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


_URI_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                "0123456789-._~")


def _py_escape_uri(s: str) -> str:
    out = []
    for ch in s:
        if ch in _URI_SAFE:
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


_PY_ESCAPERS = {"html": _py_escape_html, "script": _py_escape_js,
                "uri": _py_escape_uri}


def _oracle(tpl: str, values: dict):
    """The render and the (generated -> template) correspondence, rebuilt here.

    Returns (text, spans) where each span is
    (gen_start, gen_len, tpl_start, is_copied_text).
    """
    parts, spans, gen, pos = [], [], 0, 0
    for m in HOLE_RE.finditer(tpl):
        text = tpl[pos:m.start()]
        if text:
            spans.append((gen, len(text), pos, True))
            parts.append(text)
            gen += len(text)
        escaped = _PY_ESCAPERS[m.group(1)](values[m.group(2)])
        spans.append((gen, len(escaped), m.start(), False))
        parts.append(escaped)
        gen += len(escaped)
        pos = m.end()
    tail = tpl[pos:]
    if tail:
        spans.append((gen, len(tail), pos, True))
        parts.append(tail)
    return "".join(parts), spans


def _span_of(spans, offset):
    for start, length, tpl_start, copied in spans:
        if start <= offset < start + length:
            return (start, length, tpl_start, copied)
    raise AssertionError(f"offset {offset} is in no span")


# ------------------------------------------------------------------ the corpus
#
# Each case is (label, template, bindings). Nearly every case has a line break,
# because a single-line map exercises none of the line bookkeeping.

CASES = (
    ("multi-line with three contexts",
     'line0 {{html:title}}\n<script>boot("{{script:tok}}")</script>\n'
     'last {{uri:path}} end',
     {"title": "A&B <tag>", "tok": "</script> <!-- x\u2028y", "path": "a/b?c=1"}),
    ("hole first on its line",
     '{{html:a}} tail\nsecond {{html:b}}\n',
     {"a": "one", "b": "two"}),
    ("adjacent holes with nothing between",
     'x\n{{html:a}}{{html:b}}\ny',
     {"a": "1", "b": "2"}),
    ("value carrying its own line breaks",
     'head\n<pre>{{html:body}}</pre>\ntail',
     {"body": "first\nsecond\nthird"}),
    ("empty value",
     'a\nb{{html:e}}c\nd',
     {"e": ""}),
    ("CRLF template",
     'one\r\ntwo {{html:v}}\r\nthree',
     {"v": "V"}),
    ("non-ASCII before a hole",
     'café ✓\nété {{html:n}} fin',
     {"n": "né"}),
    ("template that is only a hole",
     '{{html:only}}',
     {"only": "just this"}),
    ("trailing newline after the last hole",
     '{{html:v}}\n',
     {"v": "v"}),
    ("consecutive blank lines between holes",
     '{{html:a}}\n\n\n{{html:b}}',
     {"a": "A", "b": "B"}),
    ("one name used at two sites",
     'x {{html:n}}\ny {{html:n}} z',
     {"n": "N"}),
)


def _bind_all(ns, values):
    return [ns["bind"](k, v) for k, v in values.items()]


# --------------------------------------------------------------- the checks
#
# Factored out so the neutering proof at the end re-derives the SAME assertions
# against a broken module rather than a hand-copied parallel of them.

def _assert_position_at_is_line_and_column(ns):
    tpl = "ab\ncde\n\nf"
    expected = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (1, 3),
                (2, 0), (3, 0)]
    for offset, (line, column) in enumerate(expected):
        p = _ok(ns["tpl_position_at"](tpl, offset))
        assert (p["line"], p["column"]) == (line, column), (offset, p)
    # offset == length is IN range: it is where a trailing `Hole.end` points.
    end = _ok(ns["tpl_position_at"](tpl, len(tpl)))
    assert (end["line"], end["column"]) == (3, 1)


def _assert_position_at_breaks_on_lf_only(ns):
    # a bare CR is an ordinary character; in a CRLF template it is the last
    # column of the line, not a line of its own.
    tpl = "a\rb\r\nc"
    lines = [_ok(ns["tpl_position_at"](tpl, i))["line"] for i in range(len(tpl))]
    assert lines == [0, 0, 0, 0, 0, 1], lines
    assert _ok(ns["tpl_position_at"](tpl, 3))["column"] == 3   # the CR of CRLF
    assert _ok(ns["tpl_position_at"](tpl, 5))["column"] == 0   # after the LF


def _assert_position_at_counts_codepoints(ns):
    # the unit is the one `length`/`slice`/`charCodeAt` and `Hole.start` use.
    tpl = "é✓x\ny"          # two multi-byte characters, then x
    assert _ok(ns["tpl_position_at"](tpl, 2))["column"] == 2
    at4 = _ok(ns["tpl_position_at"](tpl, 4))
    assert (at4["line"], at4["column"]) == (1, 0)


def _assert_position_at_refuses_out_of_range(ns):
    # FAILURE DIRECTION: refuse. Never clamp to the nearest valid position -- a
    # clamp turns a caller's arithmetic slip into a map that points confidently
    # at the wrong line.
    tpl = "abc"
    for bad in (-1, -100, 4, 99):
        e = _err(ns["tpl_position_at"](tpl, bad))
        assert e["code"] == "offset-out-of-range", (bad, e)
        assert str(bad) in e["message"] and "3" in e["message"]
    assert type(ns["tpl_position_at"]("", 0)).__name__ == "Ok"
    assert type(ns["tpl_position_at"]("", 1)).__name__ == "Err"


def _assert_mapped_text_equals_render(ns):
    for label, tpl, values in CASES:
        binds = _bind_all(ns, values)
        mapped = _ok(ns["tpl_render_mapped"](tpl, binds))
        assert mapped["text"] == _ok(ns["tpl_render"](tpl, binds)), label
        assert mapped["template"] == tpl, label
        # and it is the text an independent renderer produces
        assert mapped["text"] == _oracle(tpl, values)[0], label


def _assert_mapped_refuses_exactly_what_render_refuses(ns):
    bad = (
        ("{{html:x}}", [], "unknown-binding"),
        ("{{html:x}}", [("x", "1"), ("x", "2")], "duplicate-binding"),
        ("{{html:x", [("x", "1")], "malformed-hole"),
        ("{{nope:x}}", [("x", "1")], "unknown-context"),
        ("{{x}}", [("x", "1")], "malformed-hole"),
    )
    for tpl, pairs, code in bad:
        binds = [ns["bind"](k, v) for k, v in pairs]
        e = _err(ns["tpl_render_mapped"](tpl, binds))
        assert e["code"] == code, (tpl, e)
        assert _err(ns["tpl_render"](tpl, binds)) == e, tpl


def _assert_every_generated_line_has_a_column_zero_segment(ns):
    for label, tpl, values in CASES:
        r = _ok(ns["tpl_render_mapped"](tpl, _bind_all(ns, values)))
        segs = r["segments"]
        starts = {(s["generated"]["line"], s["generated"]["column"]) for s in segs}
        for i, line in enumerate(r["text"].split("\n")):
            if line == "":
                continue        # a line with no character needs no mapping
            assert (i, 0) in starts, (label, i, sorted(starts))
        # segments arrive in generated order, which is what `mappings` assumes
        order = [(s["generated"]["line"], s["generated"]["column"]) for s in segs]
        assert order == sorted(order), (label, order)
        # a "text" segment names no hole and a "value" segment always does
        for s in segs:
            assert s["kind"] in ("text", "value"), (label, s)
            assert (s["name"] == "") == (s["kind"] == "text"), (label, s)


def _assert_map_resolves_every_generated_position(ns):
    """The load-bearing check: decode the emitted map with the independent
    reader and resolve EVERY generated position back to the template."""
    for label, tpl, values in CASES:
        r = _ok(ns["tpl_render_mapped"](tpl, _bind_all(ns, values)))
        doc = json.loads(ns["tpl_source_map"](r, "t.tpl", "t.out"))
        decoded = _decode_mappings(doc["mappings"])
        assert decoded, label
        text, spans = _oracle(tpl, values)
        assert text == r["text"], label
        for offset in range(len(text)):
            if text[offset] == "\n":
                continue      # a line terminator belongs to no column
            gl, gc = _line_col(text, offset)
            entry = _resolve(decoded, gl, gc)
            assert entry is not None, (label, offset, gl, gc)
            _, m_col, src, o_line, o_col, name = entry
            assert src == 0, (label, offset, entry)
            gen_start, _, tpl_start, copied = _span_of(spans, offset)
            if copied:
                # copied text is verbatim, so the column delta carries across
                want = _line_col(tpl, tpl_start + (offset - gen_start))
                assert (o_line, o_col + (gc - m_col)) == want, \
                    (label, offset, entry, want)
            else:
                # an inserted value is one crossing: every position in it
                # resolves to the hole that produced it, with its name.
                want = _line_col(tpl, tpl_start)
                assert (o_line, o_col) == want, (label, offset, entry, want)
                assert name is not None, (label, offset, entry)
                hole_name = HOLE_RE.match(tpl, tpl_start).group(2)
                assert doc["names"][name] == hole_name, (label, offset, entry)


def _assert_map_is_a_valid_source_map_v3_document(ns):
    _, tpl, values = CASES[0]
    r = _ok(ns["tpl_render_mapped"](tpl, _bind_all(ns, values)))
    doc = json.loads(ns["tpl_source_map"](r, "console.html.tpl", "console.html"))
    assert doc["version"] == 3
    assert doc["file"] == "console.html"
    assert doc["sources"] == ["console.html.tpl"]
    assert doc["sourcesContent"] == [tpl]
    assert doc["names"] == ["title", "tok", "path"]   # first-appearance order
    # the `;` count and the generated-line count agree, so the map covers the
    # whole output rather than stopping at the first line.
    assert doc["mappings"].count(";") == r["text"].count("\n")
    assert set(doc["mappings"]) <= set(_B64 + ",;")


def _assert_source_map_json_survives_hostile_text(ns):
    # quotes, a backslash, a `</script>` and non-ASCII, in the template, in the
    # source name and in the generated-file name.
    tpl = ('a "q" \\ b\n<script>x = "{{script:v}}";</script>\n'
           'é café {{html:w}}')
    r = _ok(ns["tpl_render_mapped"](tpl, [ns["bind"]("v", '</script>"'),
                                          ns["bind"]("w", '<i>&</i>')]))
    raw = ns["tpl_source_map"](r, 'we"ird\\name.tpl', 'out"put\\x.html')
    doc = json.loads(raw)
    assert doc["sourcesContent"] == [tpl]
    assert doc["sources"] == ['we"ird\\name.tpl']
    assert doc["file"] == 'out"put\\x.html'
    # nothing in the serialised map can terminate a <script> element carrying it
    for byte in ("<", ">", "&"):
        assert byte not in raw, (byte, raw)
    assert _decode_mappings(doc["mappings"])


def _assert_map_uses_deltas_not_absolutes(ns):
    # the decoder above already assumes deltas, so an absolute encoder fails the
    # resolution check. This states the property directly too: the decoded
    # generated columns of one line must be the real output columns.
    tpl = "aaaaaaaaaa{{html:x}}bbbb{{html:y}}"
    r = _ok(ns["tpl_render_mapped"](tpl, [ns["bind"]("x", "X"),
                                          ns["bind"]("y", "Y")]))
    doc = json.loads(ns["tpl_source_map"](r, "t", "o"))
    cols = [e[1] for e in _decode_mappings(doc["mappings"])]
    assert cols == [0, 10, 11, 15], (cols, r["text"])


CHECKS = (
    ("position_at is line and column", _assert_position_at_is_line_and_column),
    ("position_at breaks on LF only", _assert_position_at_breaks_on_lf_only),
    ("position_at counts codepoints", _assert_position_at_counts_codepoints),
    ("position_at refuses an out-of-range offset",
     _assert_position_at_refuses_out_of_range),
    ("mapped text equals render", _assert_mapped_text_equals_render),
    ("render_mapped refuses what render refuses",
     _assert_mapped_refuses_exactly_what_render_refuses),
    ("every generated line has a column-zero segment",
     _assert_every_generated_line_has_a_column_zero_segment),
    ("the map resolves every generated position",
     _assert_map_resolves_every_generated_position),
    ("the map is a valid source map v3 document",
     _assert_map_is_a_valid_source_map_v3_document),
    ("the map survives hostile text",
     _assert_source_map_json_survives_hostile_text),
    ("the map uses deltas", _assert_map_uses_deltas_not_absolutes),
)


@pytest.mark.parametrize("label,check", CHECKS, ids=[c[0] for c in CHECKS])
def test_check(ns, label, check):
    check(ns)


# ------------------------------------------------------------------ the surface

def test_module_file_is_the_documented_f2_surface():
    text = MODULE.read_text(encoding="utf-8")
    assert "pub type Pos = { line: Int, column: Int }" in text
    assert ("pub type Segment = { kind: Str, name: Str, context: Str, "
            "generated: Pos, original: Pos }") in text
    assert ("pub type Rendered = { template: Str, text: Str, "
            "segments: List[Segment] }") in text
    assert "pub fn position_at(tpl: Str, offset: Int) -> Result[Pos, TemplateError]" in text
    assert ("pub fn render_mapped(tpl: Str, values: List[Binding]) "
            "-> Result[Rendered, TemplateError]") in text
    assert ("pub fn source_map(rendered: Rendered, source: Str, "
            "generated_file: Str) -> Str") in text


def test_the_f2_surface_is_pure_revl_on_every_tier(tmp_path):
    # the map is arithmetic and string building (revl has no bitwise operators,
    # so the VLQ shift and mask are `div_floor`/`mod`), so it introduces no host
    # body and no tier restriction: F6 does not reach it.
    (tmp_path / "stdlib").mkdir()
    (tmp_path / "stdlib" / "template.rvl").write_text(
        MODULE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "stdlib" / "escape.rvl").write_text(
        ESCAPE.read_text(encoding="utf-8"), encoding="utf-8")
    main = tmp_path / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    ir = compile_files([str(main)])
    assert ir.get("externs", []) in ([], None), ir.get("externs")
    names = {f["name"] for f in ir["functions"]}
    assert {"position_at", "render_mapped", "source_map", "vlq"} <= names


def test_render_is_render_mapped_with_the_map_dropped():
    # the two doors are one renderer, so they cannot answer different text.
    text = MODULE.read_text(encoding="utf-8")
    body = text.split("pub fn render(tpl: Str, values: List[Binding])")[1]
    body = body.split("\n}\n")[0]
    assert "render_mapped(tpl, values)" in body


# ----------------------------------------------------------------- the controls
#
# These use ONLY the surface that existed before this change, so they hold both
# before and after it. A run in which they are the only green tests is a run in
# which nothing about F2 was exercised.

def test_control_scan_still_reports_hole_offsets(control_ns):
    holes = _ok(control_ns["c_scan"]("ab{{html:x}}cd{{uri:y}}"))
    assert [(h["name"], h["context"], h["start"], h["end"]) for h in holes] == [
        ("x", "html", 2, 12), ("y", "uri", 14, 23)]


def test_control_render_still_escapes_per_context(control_ns):
    binds = [control_ns["c_bind"]("x", "<&>")]
    assert _ok(control_ns["c_render"]("<p>{{html:x}}</p>", binds)) == \
        "<p>&lt;&amp;&gt;</p>"


# ---------------------------------------------------------- the neutering proof

def _sub(old, new):
    def mutate(text):
        assert old in text, f"neutering anchor not found: {old!r}"
        return text.replace(old, new, 1)
    return mutate


MUTATIONS = {
    "N01 position_of counts CR as a line break": _sub(
        "    if (tpl.charCodeAt(i) == 10) {\n      line += 1\n      column = 0",
        "    if (tpl.charCodeAt(i) == 10 || tpl.charCodeAt(i) == 13) {\n"
        "      line += 1\n      column = 0"),
    "N02 position_of does not reset the column at a break": _sub(
        "    if (tpl.charCodeAt(i) == 10) {\n      line += 1\n      column = 0\n"
        "    } else {\n      column += 1\n    }",
        "    if (tpl.charCodeAt(i) == 10) {\n      line += 1\n      column += 1\n"
        "    } else {\n      column += 1\n    }"),
    "N03 position_at clamps instead of refusing": _sub(
        "  if (offset < 0 || offset > n) {", "  if (false) {"),
    "N04 the generated column field is absolute": _sub(
        "    out = out.concat(vlq(s.generated.column - gcolumn))",
        "    out = out.concat(vlq(s.generated.column))"),
    "N05 the original column field is absolute": _sub(
        "    out = out.concat(vlq(s.original.column - ocolumn))",
        "    out = out.concat(vlq(s.original.column))"),
    "N06 the original line field is absolute": _sub(
        "    out = out.concat(vlq(s.original.line - oline))",
        "    out = out.concat(vlq(s.original.line))"),
    "N07 vlq drops the sign bit": _sub(
        "  if (value < 0) { rest = (0 - value) * 2 + 1 }",
        "  if (value < 0) { rest = (0 - value) * 2 }"),
    "N08 a value maps to the hole's END": _sub(
        "                     h, position_of(tpl, h.start))",
        "                     h, position_of(tpl, h.end))"),
    "N09 copied text records no per-line segment": _sub(
        '      if (i + 1 < to) {\n'
        '        segs = segs.push({ kind: "text", name: "", context: "",',
        '      if (false) {\n'
        '        segs = segs.push({ kind: "text", name: "", context: "",'),
    "N10 the map's JSON strings are not escaped": _sub(
        "  out = out.concat(escape_js(rendered.template))",
        "  out = out.concat(rendered.template)"),
    "N11 the name index field is dropped": _sub(
        '    if (s.kind == "value") {\n      let ni = name_index(names, s.name)',
        '    if (false) {\n      let ni = name_index(names, s.name)'),
    "N12 render_mapped reports someone else's template": _sub(
        "  return Ok({ template: tpl, text: cur.out, segments: cur.segs })",
        '  return Ok({ template: "", text: cur.out, segments: cur.segs })'),
    "N13 the line advance is dropped from mappings": _sub(
        '    while (gline < s.generated.line) {\n      out = out.concat(";")',
        '    while (gline < s.generated.line) {\n      out = out.concat("")'),
}


def _failed_checks(broken):
    failed = []
    for label, check in CHECKS:
        try:
            check(broken)
        except Exception:           # noqa: BLE001 - any failure counts
            failed.append(label)
    return failed


def test_the_proof_has_a_baseline_the_shipped_module_passes_every_check(ns):
    # half one: with NO mutation every check holds, so a mutation failing is
    # evidence about the mutation and not about the harness.
    assert _failed_checks(ns) == []


@pytest.mark.parametrize("label", sorted(MUTATIONS), ids=sorted(MUTATIONS))
def test_a_broken_map_is_caught(tmp_path, label):
    # half two: each deliberately broken module fails at least one check. A map
    # gate that no breakage can trip is the failure mode this repo has shipped.
    mutated = MUTATIONS[label](MODULE.read_text(encoding="utf-8"))
    broken = _build(tmp_path, CONSUMER, template_text=mutated)
    assert _failed_checks(broken), f"{label} was not caught by any check"
