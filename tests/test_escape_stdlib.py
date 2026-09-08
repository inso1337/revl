"""The stdlib ESCAPE module (roadmap item 459, issue #722; stdlib/escape.rvl).

Item 459 wants first-class frontend asset integration, and names "reusable
templates with contextual escaping" as part of it. Design notes 526/530 move the
frontend itself onto Cordis WebUI's external assets (examples/webui-entry/), but
wherever server DATA crosses into a page — a bootstrap payload injected next to
the entry, a server-rendered shell, a URL built from a request field — the
crossing still needs escaping, and the escaper must match the CONTEXT the data
lands in. This module names those context escapers once so an asset-integration
boundary reaches for the right one instead of re-deriving (or misusing) escaping
per site. It is reusable and app-neutral: no console markup, no framework — the
safe-crossing primitive the asset story is built on (526 argues revl must NOT
grow a console framework).

Like stdlib/str.rvl and stdlib/render.rvl the module is PURE revl (built on the
base Str/Int surface, no `@py`), so it runs on every backend the day it lands.
The py tier is executed here.

Checked here:
  * the module imports through `use`, the three escapers reach the IR, and the
    kit introduces NO externs (pure revl, every tier);
  * the module file is the documented public surface;
  * escape_html: entity-encodes `& < > " '` (text and quoted-attribute safe),
    handles `&` first (no double-encoding), passes other text through;
  * escape_js: closes `</script>`/`<!--` element breakout by hex-encoding
    `< > &`, encodes `\\`/`"`/controls, and the JS line terminators U+2028/2029,
    while leaving the single quote (wrong context) alone;
  * escape_uri: RFC-3986 component encoding — unreserved passes, everything else
    is `%XX` uppercase over the UTF-8 bytes (multi-byte scalars included).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "escape.rvl"

#: a consumer that reaches every escaper through a named `use`, in PURE revl.
CONSUMER = """\
use "stdlib/escape.rvl" { escape_html, escape_js, escape_uri }

fn html(s: Str) -> Str { return escape_html(s) }
fn js(s: Str) -> Str { return escape_js(s) }
fn uri(s: Str) -> Str { return escape_uri(s) }
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface).
    d = tmp_path_factory.mktemp("escape_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "escape.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                             encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_functions_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"escape_html", "escape_js", "escape_uri"} <= names
    # PURE revl: no @py externs are introduced by the kit
    assert consumer_ir.get("externs", []) == []


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert "pub fn escape_html(s: Str) -> Str" in text
    assert "pub fn escape_js(s: Str) -> Str" in text
    assert "pub fn escape_uri(s: Str) -> Str" in text


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_escape", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "escape.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


# ---- escape_html: HTML text / quoted-attribute context ----------------------

def test_html_encodes_the_five_chars(ns):
    assert ns["html"]("<b>") == "&lt;b&gt;"
    assert ns["html"]('a"b') == "a&quot;b"
    assert ns["html"]("a'b") == "a&#39;b"
    assert ns["html"]("a&b") == "a&amp;b"


def test_html_ampersand_first_no_double_encoding(ns):
    # `&` must be rewritten before it can be re-seen inside a produced entity;
    # `<&>` becomes exactly three entities, not `&amp;lt;...`.
    assert ns["html"]("<&>") == "&lt;&amp;&gt;"


def test_html_passes_other_text_through(ns):
    assert ns["html"]("plain text 123 /?=#") == "plain text 123 /?=#"
    assert ns["html"]("") == ""


def test_html_neutralizes_an_attribute_breakout(ns):
    # a value landing in `title="..."` cannot close the attribute or add one.
    payload = '"><script>alert(1)</script>'
    out = ns["html"](payload)
    assert '"' not in out
    assert "<" not in out and ">" not in out
    assert out == "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"


# ---- escape_js: <script> string-literal context -----------------------------

def test_js_closes_script_element_breakout(ns):
    # the raw bytes an HTML parser scans must contain no `<`, `>` or `&`, so
    # `</script>` / `<!--` cannot terminate the element, yet the JS value is
    # byte-identical after the runtime unescapes the \uXXXX forms.
    out = ns["js"]("</script><!--&")
    assert "<" not in out and ">" not in out and "&" not in out
    assert out == "\\u003C/script\\u003E\\u003C!--\\u0026"


def test_js_escapes_quote_and_backslash(ns):
    assert ns["js"]('a"b') == 'a\\"b'
    assert ns["js"]("a\\b") == "a\\\\b"


def test_js_leaves_single_quote_alone(ns):
    # the target is a double-quoted literal; escaping `'` would be wrong-context
    # over-escaping.
    assert ns["js"]("it's") == "it's"


def test_js_encodes_controls_and_line_terminators(ns):
    assert ns["js"]("a\nb\rc\td") == "a\\nb\\rc\\td"
    # U+2028 / U+2029 are valid JSON but break a JS string literal.
    assert ns["js"]("\u2028") == "\\u2028"
    assert ns["js"]("\u2029") == "\\u2029"
    # another C0 control (NUL) -> \u0000
    assert ns["js"]("\x00") == "\\u0000"
    assert ns["js"]("\x1f") == "\\u001F"


def test_js_passes_ordinary_text(ns):
    assert ns["js"]("hello 123 world") == "hello 123 world"
    assert ns["js"]("") == ""


# ---- escape_uri: RFC-3986 single-component context --------------------------

def test_uri_unreserved_passes_through(ns):
    keep = "ABCabc012-._~"
    assert ns["uri"](keep) == keep


def test_uri_percent_encodes_reserved_and_delimiters(ns):
    assert ns["uri"]("a b") == "a%20b"
    assert ns["uri"]("a/b?c=d&e#f") == "a%2Fb%3Fc%3Dd%26e%23f"
    assert ns["uri"]("100%") == "100%25"


def test_uri_encodes_multibyte_utf8_by_byte(ns):
    # é = U+00E9 -> C3 A9 ; € = U+20AC -> E2 82 AC ; each byte its own %XX.
    assert ns["uri"]("é") == "%C3%A9"
    assert ns["uri"]("€") == "%E2%82%AC"


def test_uri_hex_is_uppercase(ns):
    out = ns["uri"]("~ ")  # ~ passes, space -> %20 ; force a hex letter case
    assert out == "~%20"
    # a byte with a letter nibble comes out uppercase (é -> %C3%A9, not %c3%a9)
    assert ns["uri"]("é") == ns["uri"]("é").upper()
