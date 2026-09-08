"""The stdlib HTTP module (roadmap item 456, stdlib/http.rvl).

First-class typed HTTP contracts: `Request`/`Response`/`Header` records, a typed
`Body` (`Empty`/`Text`/`Json`) and an explicit routing `Outcome`
(`Handled(Response)`/`NotHandled`). This replaces the harness convention where a
route hands back a bare `Str` and the caller reconstructs status and outcome from
body prose — `""` for "nothing", `"::empty::"` for "handled, empty body", the
status parsed out of the text.

The point of the module is that the two things the old convention read out of a
string are now read out of the type:

  * WHETHER a route handled the request — `Handled` vs the typed `NotHandled`,
    read with `is_handled` / `status_of` (`None` when nothing handled it), never
    from `text == ""`;
  * the STATUS and BODY of a handled response — fields on `Response`, with body
    emptiness carried by the `Empty` variant and read with `is_empty_body`, never
    from `text == "" || text == "::empty::"`.

Like stdlib/render.rvl these are PURE revl (records, variants, `match`, no `@py`),
so there is nothing to defer per tier: the module runs on every backend. The py
tier is executed here. The exit criterion this pins (item 462): a route sets and
reads status/outcome through the typed contract with ZERO `""`/`"::empty::"`
sentinels, and a mishandled route is a typed `NotHandled`, not an empty string.
"""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "http.rvl"

#: a route-shaped consumer built ONLY on the typed contract: a router that maps a
#: `Request` to an `Outcome`, and readers that recover status / handled-ness /
#: body / emptiness / a header THROUGH the type, with no string sentinel anywhere.
CONSUMER = """\
use "stdlib/http.rvl" {
  Request, Response, Outcome, Body, Method,
  request, ok_text, ok_json, not_found, handled, not_handled,
  is_handled, status_of, status_text_of, response_of, is_ok,
  is_empty_body, body_text, header_value, method_str
}

// The route table. /hello -> 200 text; /data -> 200 json; /gone -> a HANDLED
// 404 response (the route ran and answered "not found"); anything else ->
// NotHandled (no route matched at all) — a distinction the empty-string
// convention could not make.
fn route(req: Request) -> Outcome {
  if (req.path == "/hello") { return handled(ok_text("hi")) }
  if (req.path == "/data") { return handled(ok_json("[1,2,3]")) }
  if (req.path == "/gone") { return handled(not_found()) }
  return not_handled()
}

// STATUS read through the type; -1 stands for "no route handled it" (there is no
// status to read), itself derived from the typed None, not from prose.
fn status_for(path: Str) -> Int {
  return match status_of(route(request(Get, path))) {
    Some(s) => s,
    None => -1,
  }
}

fn handled_for(path: Str) -> Bool {
  return is_handled(route(request(Get, path)))
}

fn body_for(path: Str) -> Str {
  return match response_of(route(request(Get, path))) {
    Some(r) => body_text(r.body),
    None => "<unrouted>",
  }
}

fn empty_body_for(path: Str) -> Bool {
  return match response_of(route(request(Get, path))) {
    Some(r) => is_empty_body(r.body),
    None => false,
  }
}

fn ctype_for(path: Str) -> Str {
  return match response_of(route(request(Get, path))) {
    Some(r) => match header_value(r.headers, "content-type") {
      Some(v) => v,
      None => "",
    },
    None => "",
  }
}

// REASON PHRASE (Cordis `statusText`) read through the type; "<unrouted>" only
// via the typed None, never from prose.
fn reason_for(path: Str) -> Str {
  return match status_text_of(route(request(Get, path))) {
    Some(t) => t,
    None => "<unrouted>",
  }
}

// 2xx-ness read through the typed response (mirrors Cordis `Response.ok`);
// false for the unrouted case, reached only via the typed None.
fn ok_for(path: Str) -> Bool {
  return match response_of(route(request(Get, path))) {
    Some(r) => is_ok(r),
    None => false,
  }
}

// the method survives the round trip through `method_str` on the typed record.
fn method_for(path: Str) -> Str {
  return method_str(request(Post, path).method)
}
"""


def _compile_consumer(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer (its repo content is pinned by
    # test_module_file_is_the_documented_surface)
    d = tmp_path_factory.mktemp("http_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "http.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                           encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


import pytest  # noqa: E402


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    return _compile_consumer(tmp_path_factory)


# ---------------------------------------------------------------- the module

def test_module_imports_and_types_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"route", "status_for", "handled_for"} <= names
    # PURE revl: the module introduces no @py externs
    assert consumer_ir.get("externs", []) == []


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    # the contract's shape is the public surface downstream depends on
    assert "pub type Header = { name: Str, value: Str }" in text
    assert "pub type Request = {" in text
    assert "pub type Response = {" in text
    assert "pub type Outcome = Handled(Response) | NotHandled" in text
    assert "pub fn status_of(o: Outcome) -> Opt[Int]" in text
    # aligned to the Cordis vocabulary: a typed Method set (mirrors
    # @cordisjs/server's method set) and Cordis's statusText on the Response.
    assert "pub type Method =" in text
    assert "status_text: Str" in text


def test_the_module_carries_no_status_or_empty_sentinels():
    # the whole point of 456: the CONTRACT replaces the string conventions, so
    # the module must not smuggle one back into its CODE. The sentinel may only
    # appear in explanatory prose (comment lines), never on a code line.
    for line in STDLIB.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("//"):
            continue
        assert "::empty::" not in line, f"sentinel on a code line: {line!r}"


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_http", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "http.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


# ---- status is read THROUGH the typed contract, not from body prose ---------

def test_status_comes_from_the_type(ns):
    assert ns["status_for"]("/hello") == 200
    assert ns["status_for"]("/data") == 200
    assert ns["status_for"]("/gone") == 404


def test_a_mishandled_route_is_typed_NotHandled_not_an_empty_string(ns):
    # the exit criterion: an unrouted path is NotHandled — is_handled is False and
    # status_of is None (rendered here as -1), NOT an empty string that a caller
    # has to recognise.
    assert ns["handled_for"]("/nope") is False
    assert ns["status_for"]("/nope") == -1
    # and a HANDLED route reports handled, distinct from the above
    assert ns["handled_for"]("/hello") is True


# ---- body emptiness is a variant, not a "" / "::empty::" sentinel -----------

def test_empty_body_is_the_typed_Empty_variant(ns):
    # /gone is a handled 404 with an Empty body: is_empty_body is True even though
    # body_text is "" — the emptiness lives in the type, not in the string.
    assert ns["empty_body_for"]("/gone") is True
    assert ns["body_for"]("/gone") == ""
    # a text route is NOT empty, and carries its payload
    assert ns["empty_body_for"]("/hello") is False
    assert ns["body_for"]("/hello") == "hi"


def test_typed_body_variants_carry_their_payload(ns):
    assert ns["body_for"]("/data") == "[1,2,3]"
    # the unrouted case is the reader's own default, reachable only because
    # response_of returned None — never a body sentinel
    assert ns["body_for"]("/nope") == "<unrouted>"


# ---- headers travel on the typed record ------------------------------------

def test_headers_are_read_from_the_record(ns):
    assert ns["ctype_for"]("/hello") == "text/plain; charset=utf-8"
    assert ns["ctype_for"]("/data") == "application/json"
    # a 404 with no headers -> the header lookup is None -> "" by the reader
    assert ns["ctype_for"]("/gone") == ""


# ---- Cordis-aligned status_text / ok / Method read through the type ----------

def test_status_text_mirrors_cordis_statustext(ns):
    # the reason phrase (Cordis `statusText`) travels on the typed Response and is
    # read through the outcome, distinct from the numeric status.
    assert ns["reason_for"]("/hello") == "OK"
    assert ns["reason_for"]("/gone") == "Not Found"
    # the unrouted case is the reader's typed-None default, never a body sentinel
    assert ns["reason_for"]("/nope") == "<unrouted>"


def test_is_ok_mirrors_cordis_response_ok(ns):
    assert ns["ok_for"]("/hello") is True   # 200
    assert ns["ok_for"]("/gone") is False   # handled 404
    assert ns["ok_for"]("/nope") is False   # unrouted


def test_method_is_a_typed_variant_not_a_free_string(ns):
    # the request carries a typed `Method`; its canonical wire spelling comes back
    # through `method_str`, mirroring Cordis's method vocabulary.
    assert ns["method_for"]("/hello") == "POST"
