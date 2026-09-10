"""The stdlib TEMPLATE module (roadmap item 459, issue #722; stdlib/template.rvl).

Item 459 asks for "reusable templates with contextual escaping". stdlib/escape.rvl
(also 459) names the three per-context escapers, but it leaves the choice of WHICH
one applies at each site to whoever writes the markup, and that choice is the
security-relevant one: the same bytes are inert in a text node and a
`</script>` breakout inside a script element. stdlib/template.rvl is the consumer
that takes the choice away from the caller. A template writes the context of
every insertion site next to the site (`{{script:token}}`), the escaper is chosen
BY THE HOLE, and there is no raw/no-escape hole form to forget to avoid.

This suite pins the module and, above all, pins the REFUSALS and the
wrongly-contextual failure modes, not just the happy path:

  * the module imports through `use`, its public names reach the IR, and it
    introduces NO externs (pure revl, every tier);
  * the module file is the documented public surface;
  * `contexts`/`context_ok` are the closed context set;
  * `escape_for` picks a DIFFERENT escaper per context, and refuses an unknown
    or misspelled context instead of falling through to a default;
  * `scan` returns holes left to right with their offsets, and refuses a
    malformed template before any value is bound;
  * `render`/`render_one` escape each value for its own hole's context, copy the
    template text through, and refuse an unbound hole and a duplicated name;
  * the SECURITY assertions: a `</script>` payload through a `script` hole emits
    no raw `<`, `>`, `&` or `'`; the raw-insertion forms (`{{&x}}`, `{{x}}`,
    `{{{x}}}`, `{{x|raw}}`) are refused; and a test demonstrates that the
    WRONGLY-contextual escaper (escape_js in an HTML attribute) is exploitable
    while the context-scoped path is not.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

MODULE = ROOT / "stdlib" / "template.rvl"
ESCAPE = ROOT / "stdlib" / "escape.rvl"

#: a consumer that reaches every public name through one named `use`.
CONSUMER = """\
use "stdlib/template.rvl" {
  Hole, Binding, TemplateError,
  contexts, context_ok, escape_for, scan, render, render_one,
}

fn tpl_contexts() -> List[Str] { return contexts() }

fn tpl_context_ok(c: Str) -> Bool { return context_ok(c) }

fn tpl_escape_for(c: Str, v: Str) -> Result[Str, TemplateError] { return escape_for(c, v) }

fn tpl_holes(t: Str) -> Result[List[Hole], TemplateError] { return scan(t) }

fn tpl_render(t: Str, vs: List[Binding]) -> Result[Str, TemplateError] {
  return render(t, vs)
}

fn tpl_render_one(t: Str, n: Str, v: Str) -> Result[Str, TemplateError] {
  return render_one(t, n, v)
}

fn bind(n: Str, v: Str) -> Binding { return { name: n, value: v } }
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so both stdlib files
    # sit beside the consumer fixture (their repo content is pinned by
    # test_module_file_is_the_documented_surface).
    d = tmp_path_factory.mktemp("template_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "template.rvl").write_text(MODULE.read_text(encoding="utf-8"),
                                               encoding="utf-8")
    (d / "stdlib" / "escape.rvl").write_text(ESCAPE.read_text(encoding="utf-8"),
                                             encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_functions_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"scan", "render", "render_one", "escape_for",
            "contexts", "context_ok"} <= names
    # PURE revl: no @py externs are introduced by the module
    assert consumer_ir.get("externs", []) == []


def test_module_file_is_the_documented_surface():
    text = MODULE.read_text(encoding="utf-8")
    assert "pub type Hole = { name: Str, context: Str, start: Int, end: Int }" in text
    assert "pub type Binding = { name: Str, value: Str }" in text
    assert "pub type TemplateError = { code: Str, message: Str }" in text
    assert "pub fn contexts() -> List[Str]" in text
    assert "pub fn context_ok(context: Str) -> Bool" in text
    assert "pub fn escape_for(context: Str, value: Str) -> Result[Str, TemplateError]" in text
    assert "pub fn scan(tpl: Str) -> Result[List[Hole], TemplateError]" in text
    assert "pub fn render(tpl: Str, values: List[Binding]) -> Result[Str, TemplateError]" in text
    assert "pub fn render_one(tpl: Str, name: Str, value: Str) -> Result[Str, TemplateError]" in text
    # every public fn is one of the documented doors, so a raw-insertion door
    # cannot be added without this list changing.
    public = [line for line in text.splitlines() if line.startswith("pub fn ")]
    assert len(public) == 6, public


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_template", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "template.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


def _ok(result):
    """The payload of an Ok, asserted to be one (Ok/Err are __slots__ tagged)."""
    assert type(result).__name__ == "Ok", f"expected Ok, got {result!r}"
    return result.value


def _err(result):
    """The TemplateError of an Err, asserted to be one."""
    assert type(result).__name__ == "Err", f"expected Err, got {result!r}"
    return result.value


# ---- the context set ---------------------------------------------------------

def test_contexts_is_the_closed_set(ns):
    assert list(ns["tpl_contexts"]()) == ["html", "script", "uri"]


def test_context_ok_accepts_only_the_closed_set(ns):
    for good in ("html", "script", "uri"):
        assert ns["tpl_context_ok"](good)
    for bad in ("", "Script", "HTML", "URI", "javascript", "html ", " text"):
        assert not ns["tpl_context_ok"](bad)


# ---- escape_for: the rule is chosen by the context ---------------------------

def test_escape_for_html_encodes_markup(ns):
    assert _ok(ns["tpl_escape_for"]("html", "</script>")) == "&lt;/script&gt;"
    assert _ok(ns["tpl_escape_for"]("html", 'a"b')) == "a&quot;b"


def test_escape_for_script_hex_closes_element_breakout(ns):
    out = _ok(ns["tpl_escape_for"]("script", "</script>"))
    assert out == "\\u003C/script\\u003E"
    assert "<" not in out and ">" not in out and "&" not in out


def test_escape_for_uri_percent_encodes_a_component(ns):
    assert _ok(ns["tpl_escape_for"]("uri", "a b/c?d")) == "a%20b%2Fc%3Fd"


def test_escape_for_picks_a_different_rule_per_context(ns):
    # the whole point: one value, three contexts, three different outputs. If
    # any two agreed here the "chosen by the insertion context" claim would be
    # untested for that pair.
    payload = "</script> ' a&b "
    got = {c: _ok(ns["tpl_escape_for"](c, payload)) for c in ("html", "script", "uri")}
    assert len(set(got.values())) == 3, got
    assert got["html"] != got["script"] and got["script"] != got["uri"]
    assert got["html"] != got["uri"]


def test_escape_for_refuses_an_unknown_context(ns):
    # no silent fall-through to a default escaper: an unknown or misspelled
    # context is an Err. This is the case that would otherwise be a silent
    # downgrade from the intended escaper.
    for bad in ("", "Script", "HTML", "javscript", "htm", " raw"):
        e = _err(ns["tpl_escape_for"](bad, "</script>"))
        assert e["code"] == "unknown-context", (bad, e)
        assert bad in e["message"]
        assert "html, script, uri" in e["message"]


# ---- scan --------------------------------------------------------------------

def test_scan_returns_holes_left_to_right_with_offsets(ns):
    tpl = '<a href="{{uri:u}}">{{html:t}}</a>'
    hs = _ok(ns["tpl_holes"](tpl))
    assert [(h["name"], h["context"]) for h in hs] == [("u", "uri"), ("t", "html")]
    assert (hs[0]["start"], hs[0]["end"]) == (9, 9 + len("{{uri:u}}"))
    assert (hs[1]["start"], hs[1]["end"]) == (20, 20 + len("{{html:t}}"))
    # the offsets address the template exactly
    assert tpl[hs[0]["start"]:hs[0]["end"]] == "{{uri:u}}"
    assert tpl[hs[1]["start"]:hs[1]["end"]] == "{{html:t}}"


def test_scan_accepts_a_template_with_no_holes(ns):
    assert _ok(ns["tpl_holes"]("plain <b>markup</b>")) == []


def test_scan_refuses_an_unterminated_hole(ns):
    e = _err(ns["tpl_holes"]('<p>{{html:x</p>'))
    assert e["code"] == "malformed-hole"
    assert "unterminated" in e["message"]


def test_scan_refuses_a_hole_with_no_context(ns):
    for tpl, body in (("{{ x }}", " x "), ("{{x}}", "x"), ("{{&x}}", "&x"),
                      ("{{{x}}}", "{x"), ("{{x|raw}}", "x|raw")):
        e = _err(ns["tpl_holes"](tpl))
        assert e["code"] == "malformed-hole", (tpl, e)
        assert body in e["message"]
        assert "no `context:name` separator" in e["message"]


def test_scan_refuses_an_unknown_context(ns):
    for tpl, ctx in (("{{Script:x}}", "Script"), ("{{htmlx:x}}", "htmlx"),
                     ("{{:x}}", "")):
        e = _err(ns["tpl_holes"](tpl))
        assert e["code"] == "unknown-context", (tpl, e)
        assert ctx in e["message"]


def test_scan_refuses_a_non_identifier_name(ns):
    # the slot can only ever name a bound value, so a filter, an expression or
    # a directive cannot be smuggled through it.
    for tpl in ("{{html:a b}}", "{{html:1abc}}", "{{html:a|upper}}",
                "{{html:}}", "{{html:a+b}}", "{{html:(x)}}", "{{html:&x}}",
                "{{html:a'b}}"):
        e = _err(ns["tpl_holes"](tpl))
        assert e["code"] == "malformed-hole", (tpl, e)
        assert "identifier" in e["message"]


def test_scan_accepts_dotted_and_underscored_names(ns):
    hs = _ok(ns["tpl_holes"]("{{html:a.b_c1}}"))
    assert [h["name"] for h in hs] == ["a.b_c1"]


# ---- render: happy path ------------------------------------------------------

def test_render_escapes_each_value_for_its_own_context(ns):
    tpl = '<h1>{{html:title}}</h1><script>boot("{{script:token}}");</script>'
    out = _ok(ns["tpl_render"](tpl, [ns["bind"]("title", "A <b> B"),
                                     ns["bind"]("token", "</script>")]))
    assert out == '<h1>A &lt;b&gt; B</h1><script>boot("\\u003C/script\\u003E");</script>'
    assert 'boot("</script>")' not in out
    # binding only ONE of the two holes is refused, not silently skipped
    e = _err(ns["tpl_render"](tpl, [ns["bind"]("token", "</script>")]))
    assert e["code"] == "unknown-binding" and "html:title" in e["message"]


def test_render_copies_non_hole_text_through_unchanged(ns):
    tpl = 'keep <b>&</b> {{html:x}} <!-- tail -->'
    out = _ok(ns["tpl_render"](tpl, [ns["bind"]("x", "<i>")]))
    assert out == 'keep <b>&</b> &lt;i&gt; <!-- tail -->'


def test_render_one_is_the_single_slot_convenience(ns):
    assert _ok(ns["tpl_render_one"]("{{uri:q}}", "q", "a b")) == "a%20b"


def test_render_accepts_an_unused_binding(ns):
    # an extra binding is not an error; a MISSING one is (below).
    assert _ok(ns["tpl_render"]("{{html:x}}", [ns["bind"]("x", "1"),
                                               ns["bind"]("y", "2")])) == "1"


# ---- render: refusals --------------------------------------------------------

def test_render_refuses_an_unbound_hole(ns):
    e = _err(ns["tpl_render"]('<p>{{html:x}}</p>', []))
    assert e["code"] == "unknown-binding"
    assert "html:x" in e["message"]
    assert "no empty default" in e["message"]


def test_render_refuses_a_duplicated_binding_name(ns):
    e = _err(ns["tpl_render"]("{{html:x}}", [ns["bind"]("x", "1"),
                                             ns["bind"]("x", "2")]))
    assert e["code"] == "duplicate-binding"
    assert "`x`" in e["message"]
    assert "ambiguous" in e["message"]


def test_render_refuses_a_malformed_template_before_binding(ns):
    e = _err(ns["tpl_render"]("{{&x}}", [ns["bind"]("x", "1")]))
    assert e["code"] == "malformed-hole"


def test_render_error_codes_are_a_stable_closed_set(ns):
    seen = set()
    for tpl, vs in (("<p>{{html:x}}</p>", []),
                    ("{{html:x}}", [ns["bind"]("x", "1"), ns["bind"]("x", "2")]),
                    ("{{&x}}", []),
                    ("{{Script:x}}", []),
                    ("{{html:x}}", [])):
        seen.add(_err(ns["tpl_render"](tpl, vs))["code"])
    assert seen == {"unknown-binding", "duplicate-binding", "malformed-hole",
                    "unknown-context"}


# ---- SECURITY: the escaped path is the safe path ----------------------------

SCRIPT_PAYLOAD = '</script><img src=x onerror=alert(1)><!--'


def test_script_context_payload_cannot_break_the_element(ns):
    # the raw bytes an HTML parser scans for the end of the element must not
    # appear; the JS value is byte-identical once the runtime unescapes it.
    out = _ok(ns["tpl_render_one"]("<script>x(\"{{script:v}}\")</script>", "v",
                                 SCRIPT_PAYLOAD))
    assert out == ('<script>x("\\u003C/script\\u003E\\u003Cimg src=x '
                   'onerror=alert(1)\\u003E\\u003C!--")</script>')
    inner = out[len('<script>x("'):-len('")</script>')]
    for raw in ("<", ">", "&", "'"):
        assert raw not in inner, (raw, inner)


def test_html_context_payload_cannot_break_a_quoted_attribute(ns):
    out = _ok(ns["tpl_render_one"]('<a title="{{html:t}}">x</a>', "t",
                                  '"><script>alert(1)</script>'))
    assert out == ('<a title="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;">'
                   'x</a>')
    attr = out[len('<a title="'):-len('">x</a>')]
    assert '"' not in attr and "<" not in attr and ">" not in attr


def test_uri_context_payload_cannot_inject_a_component(ns):
    out = _ok(ns["tpl_render_one"]('/n/{{uri:s}}', "s", "a/../../etc?x=1#f"))
    assert out == "/n/a%2F..%2F..%2Fetc%3Fx%3D1%23f"


def test_the_context_choice_is_load_bearing(ns):
    # The proof that the context choice is not cosmetic. escape_js leaves the
    # single quote alone (it targets a double-quoted JS string), so using it for
    # a single-quoted HTML attribute is a breakout, and using it for one is
    # exactly what declaring `script` at an attribute site does. The module
    # makes the escaper follow the declaration; it does not check the
    # declaration against the markup (see the limits test below).
    hostile = "' onload=alert(1) x='"
    wrong = _ok(ns["tpl_escape_for"]("script", hostile))
    assert "'" in wrong, wrong  # wrongly-contextual output is breakable
    right = _ok(ns["tpl_escape_for"]("html", hostile))
    assert "'" not in right, right  # the context-correct output is not
    assert right == "&#39; onload=alert(1) x=&#39;"


def test_a_wrong_declaration_is_an_author_error_the_module_documents_not_catches(ns):
    # Documented LIMIT, pinned so it cannot be mistaken for a guarantee: the
    # context is INTENT, declared by the template author. The module is not an
    # HTML parser and does not verify that a hole's declared context matches the
    # markup around it, so declaring `script` inside an attribute still breaks
    # out. What the module does guarantee is the other half: escaping is never
    # optional, and a context outside the closed set is refused.
    hostile = "' onmouseover=alert(1) x='"
    out = _ok(ns["tpl_render_one"]("<a title='{{script:v}}'>x</a>", "v", hostile))
    assert out == "<a title='' onmouseover=alert(1) x=''>x</a>"
    # ... and the same site declared `html` does NOT break out
    safe = _ok(ns["tpl_render_one"]('<a title="{{html:v}}">x</a>', "v", hostile))
    attr = safe[len('<a title="'):-len('">x</a>')]
    assert "'" not in attr and '"' not in attr  # the raw quote is gone entirely
    assert safe == '<a title="&#39; onmouseover=alert(1) x=&#39;">x</a>'


def test_no_raw_insertion_form_is_expressible(ns):
    # every "insert without escaping" spelling is a malformed hole, so the
    # mistake is inexpressible rather than merely undetected.
    for tpl in ("{{&x}}", "{{{x}}}", "{{x|raw}}", "{{raw:x}}", "{{!x}}",
                "{{html:x|safe}}", "{{=x}}"):
        result = ns["tpl_render"](tpl, [ns["bind"]("x", SCRIPT_PAYLOAD)])
        assert type(result).__name__ == "Err", (tpl, result)
        assert result.value["code"] in ("malformed-hole", "unknown-context"), (tpl, result)
