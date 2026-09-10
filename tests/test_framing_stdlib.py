"""The stdlib request-framing module (issue #867, stdlib/framing.rvl).

ONE canonical answer to "how many body bytes follow this header block?", beside
`stdlib/http.rvl` instead of inside every component. The motivating case is
measured, not hypothetical: in `inso1337/revl-harness`
(`src/components/transport.rvl`) FOUR readers on ONE server each parse
`Content-Length`, and the same byte-identical header

    Content-Length: 5
    Content-Length: 7

is refused (400, connection closed) by two of them and ACCEPTED by the other two,
which honour the first value and parse the leftover bytes as the head of a second
pipelined request: one server answered `['400 Bad Request', '200 OK']` and
`['404 Not Found', '200 OK']` to it. The same readers also accepted a
`Content-Length` of 4300 and more digits, which passed a validator and then
defeated the integer conversion on the next line, so the request got NO response
at all. That is the class of defect `resolve_within` was added to the stdlib to
close for path confinement, and this module is the framing equivalent: a typed
refusal that names the rule, so no component derives framing and no two
components disagree about it.

What this file pins:

  * one test per rule of the module's contract, with the rule's own refusal case
    (`duplicate_content_length` / `malformed_content_length` /
    `unsupported_transfer_encoding` / `over_ceiling`) and its wire status;
  * the positives (a single field, no field, OWS around the value, the exact
    ceiling, a case-insensitive field name);
  * the doubled `Content-Length: 5` / `Content-Length: 7` shape from the issue,
    and the IDENTICAL duplicate `5` / `5`, which this module refuses too: the
    conservative half of what RFC 7230 3.3.2 and RFC 9110 8.6 allow, taken
    because "the two values are identical" is a judgement a reader has to make
    anyway (and one an intermediary can invalidate);
  * the caller's ceiling as its OWN case, so 400-class and 413-class refusals are
    distinguishable without re-deriving anything (`status_for` / `is_too_large`);
  * the 4300-digit value that got no response at all before: refused on its first
    digits, no conversion attempted, and no ceiling a caller can name (Int's own
    maximum included) lets the bound overflow.

Like stdlib/http.rvl and stdlib/render.rvl this is PURE revl (records, variants,
`match`, loops: no `@py`, no `@ts`), so there is nothing to defer per tier and the
module lowers on every backend. The py tier is executed here.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "framing.rvl"

#: a component-shaped consumer that asks the stdlib instead of parsing headers
#: itself: `frame` is the length read or the NEGATIVE status of the refusal (so a
#: refused request is visibly not a length), and the rest read one part of the
#: typed refusal each. `Header` reaches this consumer transitively through
#: stdlib/framing.rvl, exactly as a component that imports only this module sees
#: it.
CONSUMER = """\
use "stdlib/framing.rvl" {
  body_length, status_for, reason_of, message_of, is_too_large,
  header_count, header_values
}

// the canonical decision: the body length to read, or the NEGATIVE status of the
// refusal. A caller reads a length or a 4xx here and never re-derives either.
fn frame(hs: List[Header], ceiling: Int) -> Int {
  return match body_length(hs, ceiling) {
    Ok(n) => n,
    Err(r) => 0 - status_for(r),
  }
}

// WHICH rule refused it, as the machine token.
fn refusal(hs: List[Header], ceiling: Int) -> Str {
  return match body_length(hs, ceiling) {
    Ok(n) => "accepted",
    Err(r) => reason_of(r),
  }
}

// the sentence to ship with the refusal.
fn sentence(hs: List[Header], ceiling: Int) -> Str {
  return match body_length(hs, ceiling) {
    Ok(n) => "",
    Err(r) => message_of(r),
  }
}

// the O(1) 413 test, without matching.
fn too_large(hs: List[Header], ceiling: Int) -> Bool {
  return match body_length(hs, ceiling) {
    Ok(n) => false,
    Err(r) => is_too_large(r),
  }
}

fn count(hs: List[Header], name: Str) -> Int {
  return header_count(hs, name)
}

fn all_values(hs: List[Header], name: Str) -> Str {
  return header_values(hs, name).join("|")
}
"""


def _compile_consumer(tmp_path_factory):
    # ONLY the module under test is copied beside the consumer, as
    # tests/test_http_stdlib.py does for http.rvl. Its `use "stdlib/http.rvl"`
    # then resolves through the search path to the checkout's own file, which is
    # the file a real component reaches too. Copying http.rvl here as well would
    # put a SECOND `Header`/`Method` in the program under a different path (the
    # import inside the copy resolves back to the checkout, since
    # `tmp/stdlib/stdlib/http.rvl` does not exist), and two modules declaring the
    # same pub type is a genuine duplicate-pub-type refusal, not a fixture this
    # module should paper over. See
    # test_a_component_that_imports_http_rvl_too_compiles for the shape a real
    # component has.
    d = tmp_path_factory.mktemp("framing_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "framing.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                              encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    return _compile_consumer(tmp_path_factory)


# ---------------------------------------------------------------- the module

def test_module_imports_and_types_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"frame", "refusal", "sentence", "too_large", "count", "all_values"} <= names
    # PURE revl: the module introduces no @py externs
    assert consumer_ir.get("externs", []) == []


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    # the decision and its typed refusal: the surface a component depends on
    assert "pub type FramingRefusal =" in text
    for case in ("DuplicateContentLength", "MalformedContentLength",
                 "UnsupportedTransferEncoding", "OverCeiling"):
        assert case in text, case
    assert ("pub fn body_length(headers: List[Header], ceiling: Int) "
            "-> Result[Int, FramingRefusal]") in text
    assert "pub fn status_for(r: FramingRefusal) -> Int" in text
    assert "pub fn reason_of(r: FramingRefusal) -> Str" in text
    assert "pub fn is_too_large(r: FramingRefusal) -> Bool" in text
    assert "pub fn message_of(r: FramingRefusal) -> Str" in text
    assert "pub fn header_values(headers: List[Header], name: Str) -> List[Str]" in text
    assert "pub fn header_count(headers: List[Header], name: Str) -> Int" in text
    # the header set it reads is the one stdlib/http.rvl already models, kept as a
    # LIST precisely so a repeated name stays visible
    assert 'use "stdlib/http.rvl" { Header }' in text


def test_the_module_doc_names_the_evidence_and_the_rfc_rules():
    # the module has to say out loud why it exists and what it refuses, or the
    # next reader re-derives a different answer: the measured harness instance,
    # and the RFC clauses each rule is the conservative reading of.
    text = STDLIB.read_text(encoding="utf-8")
    assert "inso1337/revl-harness" in text
    assert "src/components/transport.rvl" in text
    assert "RFC 7230 3.3.2" in text
    assert "RFC 9110 8.6" in text
    assert "RFC 9110 5.5" in text
    assert "RFC 9112 6.1" in text
    assert "RFC 9112 6.3" in text


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_framing", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "framing.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


# ---- helpers: a header set is a LIST of {name, value}, duplicates and all -----

CELL = 1024 * 1024


def H(name, value):
    return {"name": name, "value": value}


def CL(*values):
    return [H("content-length", v) for v in values]


# ---- the positives ----------------------------------------------------------

def test_a_single_content_length_is_the_body_length(ns):
    assert ns["frame"](CL("5"), 100) == 5
    assert ns["frame"](CL("0"), 100) == 0
    assert ns["frame"](CL("100"), 100) == 100   # exactly the ceiling is accepted
    assert ns["refusal"](CL("5"), 100) == "accepted"


def test_no_content_length_and_no_transfer_encoding_is_no_body(ns):
    # RFC 9112 6.3 item 7: neither field means no body, which is 0, not a refusal
    assert ns["frame"]([H("host", "example.test")], CELL) == 0
    assert ns["refusal"]([H("host", "example.test")], CELL) == "accepted"


def test_field_names_compare_case_insensitively(ns):
    # RFC 9110 5.1
    assert ns["frame"]([H("Content-Length", "5")], 100) == 5
    assert ns["frame"]([H("CONTENT-LENGTH", "5")], 100) == 5
    assert ns["count"](CL("5"), "Content-Length") == 1


def test_ows_around_the_value_is_stripped(ns):
    # RFC 9110 5.5 (a field's value is stripped of surrounding OWS), and OWS is
    # SP and HTAB only (RFC 9110 5.6.3)
    assert ns["frame"](CL(" 5 "), 100) == 5
    assert ns["frame"](CL("\t5\t"), 100) == 5


# ---- rule 2: a repeated Content-Length, the issue's own shape ---------------

def test_the_doubled_content_length_from_the_issue_is_refused(ns):
    # THE case: two readers on the harness server refused this with 400 and
    # closed, two others honoured the first value and parsed `7` bytes of
    # leftover as a second request (`['400 Bad Request', '200 OK']`). Framing has
    # one answer: refuse and close (RFC 7230 3.3.3).
    hs = CL("5", "7")
    assert ns["frame"](hs, 100) == -400
    assert ns["refusal"](hs, 100) == "duplicate_content_length"
    assert ns["too_large"](hs, 100) is False


def test_an_identical_duplicate_content_length_is_refused_too(ns):
    # THE DECISION: refused even when byte-identical. RFC 7230 3.3.2 and RFC 9110
    # 8.6 permit a recipient to reject a repeated same-value list and RFC 9112 6.3
    # item 5 keeps that exception for an all-identical list; this module takes the
    # conservative half and rejects in every case, because "the values are
    # identical" is a judgement the reader has to make anyway, on values an
    # intermediary may have rewritten, and a reader that makes it differently from
    # its sibling on the same server is the defect this module exists to remove.
    hs = CL("5", "5")
    assert ns["frame"](hs, 100) == -400
    assert ns["refusal"](hs, 100) == "duplicate_content_length"


def test_a_third_field_and_a_comma_list_earn_the_same_refusal(ns):
    # three fields, and the same defect spelled as a list in ONE field: the value
    # grammar has no comma (RFC 9110 8.6: `Content-Length = 1*DIGIT`)
    assert ns["refusal"](CL("5", "5", "5"), 100) == "duplicate_content_length"
    assert ns["refusal"](CL("5, 5"), 100) == "duplicate_content_length"
    assert ns["refusal"](CL("5,7"), 100) == "duplicate_content_length"


# ---- rule 3: the value grammar ---------------------------------------------

@pytest.mark.parametrize("value", ["+5", "-5", "", "0000000001", "5 5", "0x5",
                                   "5.0", "\x0b5", "\n5", "5\n", "five"])
def test_a_value_that_is_not_one_plain_decimal_integer_is_refused(ns, value):
    # RFC 9112 6.3 item 5: an invalid Content-Length is refused and the connection
    # closed. `0000000001` is legal ABNF and is still refused: accepting two
    # spellings of one length forces every reader to compare them as text.
    # `\x0b` (VT) is NOT OWS, so it is malformed rather than stripped.
    assert ns["frame"](CL(value), 100) == -400, value
    assert ns["refusal"](CL(value), 100) == "malformed_content_length", value


# ---- rule 1: Transfer-Encoding is refused, not ignored ----------------------

@pytest.mark.parametrize("coding", ["chunked", "gzip", "identity", "chunked, gzip"])
def test_a_transfer_encoding_is_refused_not_ignored(ns, coding):
    # The other half of the smuggling family. revl's stdlib has no transfer-coding
    # decoder, so there is no length this module could hand back, and returning a
    # Content-Length beside a Transfer-Encoding is how a smuggled request is
    # built. `chunked` earns no exception (RFC 9112 6.1).
    hs = [H("transfer-encoding", coding)]
    assert ns["frame"](hs, CELL) == -400, coding
    assert ns["refusal"](hs, CELL) == "unsupported_transfer_encoding", coding


def test_the_rule_order_is_part_of_the_contract(ns):
    # Two components must agree on WHICH refusal a header earns, not only that it
    # is refused, so the order is fixed and documented: transfer encoding first,
    # then repetition, then the value grammar, then the ceiling.
    assert ns["refusal"]([H("transfer-encoding", "chunked")] + CL("5", "7"),
                         100) == "unsupported_transfer_encoding"
    assert ns["refusal"](CL("5", "nope"), 100) == "duplicate_content_length"
    assert ns["refusal"](CL("nope"), 1) == "malformed_content_length"


# ---- rule 4: the caller's ceiling, its own case -----------------------------

def test_over_the_ceiling_is_its_own_refusal_and_a_413(ns):
    hs = CL("5000")
    assert ns["frame"](hs, 100) == -413
    assert ns["refusal"](hs, 100) == "over_ceiling"
    assert ns["too_large"](hs, 100) is True


def test_the_caller_can_tell_the_400_class_from_the_413_class(ns):
    # the secondary ask of the issue: a caller maps framing-refused to 400 and
    # over-ceiling to 413 WITHOUT re-deriving which is which
    refused = CL("5", "7")
    too_big = CL("5000")
    assert ns["frame"](refused, 100) == -400
    assert ns["frame"](too_big, 100) == -413
    assert ns["too_large"](refused, 100) is False
    assert ns["too_large"](too_big, 100) is True
    assert ns["refusal"](refused, 100) != ns["refusal"](too_big, 100)
    assert ns["sentence"](refused, 100) != ns["sentence"](too_big, 100)


def test_a_negative_ceiling_refuses_every_request(ns):
    # a ceiling passed by mistake fails closed, zero-length bodies included
    assert ns["frame"](CL("0"), -1) == -413
    assert ns["refusal"](CL("0"), -1) == "over_ceiling"


# ---- the bound: no value length defeats it, no ceiling overflows it ---------

def test_a_4300_digit_value_is_refused_on_its_first_digits(ns):
    # the fourth comment on issue #867: the harness readers accepted a
    # Content-Length of 4300 and more digits, which passed their validator and
    # then defeated the integer conversion on the very next line, so the request
    # got NO response at all. Here it is a typed refusal, decision included, and
    # nothing is converted.
    assert ns["frame"](CL("9" * 4300), CELL) == -413
    assert ns["refusal"](CL("9" * 4300), CELL) == "over_ceiling"
    assert ns["frame"](CL("9" * 4301), CELL) == -413
    # 4300 zeros are not over the ceiling; they are malformed (leading zeros)
    assert ns["refusal"](CL("0" * 4300), CELL) == "malformed_content_length"


def test_the_bound_holds_at_int_max_as_the_ceiling(ns):
    # a caller may name Int's own maximum as its ceiling, and the value one digit
    # past that must still be a refusal rather than an overflow of the conversion
    imax = 9223372036854775807
    assert ns["frame"](CL(str(imax)), imax) == imax
    assert ns["frame"](CL(str(imax + 1)), imax) == -413
    assert ns["refusal"](CL("9" * 19), 100) == "over_ceiling"


# ---- the refusal sentence ---------------------------------------------------

def test_the_refusal_sentence_does_not_echo_the_offending_value(ns):
    # the value reaching a refusal can be thousands of digits long; reflecting it
    # into a response body or a log is a defect of its own, so the sentence is
    # static per rule
    text = ns["sentence"](CL("9" * 4300), 100)
    assert text != ""
    assert "9999" not in text
    assert "Content-Length must be one plain non-negative decimal integer" in \
        ns["sentence"](CL("+5"), 100)
    # one sentence per rule: the duplicate refusal cannot borrow the ceiling text
    assert ns["sentence"](CL("5", "7"), 100) != text


# ---- the duplicate-visible header readers ----------------------------------

def test_header_readers_see_a_repeated_name(ns):
    # stdlib/http.rvl's `header_value` answers with the FIRST match only, which is
    # the wrong question for framing: these keep every value
    hs = CL("5", "7")
    assert ns["count"](hs, "content-length") == 2
    assert ns["count"](hs, "Content-Length") == 2
    assert ns["all_values"](hs, "content-length") == "5|7"
    assert ns["count"](hs, "transfer-encoding") == 0
    assert ns["all_values"]([H("host", "x")], "content-length") == ""


# ---- usable as a component imports it --------------------------------------

def test_a_component_that_imports_http_rvl_too_compiles(tmp_path):
    # The primitive is not usable in practice if importing it breaks an ordinary
    # component. A component that wants the `Request` record AND the framing
    # decision writes BOTH `use` lines, and both resolve through the search path
    # to the checkout's own stdlib, so there is one `Header` in the program and
    # one `body_length`: this is the shape a real component has, and it lowers
    # with no externs.
    main = tmp_path / "transport.rvl"
    main.write_text("""\
use "stdlib/framing.rvl" { body_length, status_for, message_of }
use "stdlib/http.rvl" { Header, Request, header }

fn body_to_read(hs: List[Header], ceiling: Int) -> Int {
  return match body_length(hs, ceiling) {
    Ok(n) => n,
    Err(r) => 0 - status_for(r),
  }
}

fn refusal_for(hs: List[Header], ceiling: Int) -> Str {
  return match body_length(hs, ceiling) {
    Ok(n) => "",
    Err(r) => message_of(r),
  }
}

// the shape a real request handler has: the header set comes off the Request
// record and the framing decision is the stdlib's, not the component's
fn frames_request(req: Request, ceiling: Int) -> Int {
  return body_to_read(req.headers, ceiling)
}

fn doubled() -> Int {
  return body_to_read([header("content-length", "5"),
                       header("content-length", "7")], 100)
}
""", encoding="utf-8")
    ir = compile_files([str(main)])
    names = {f["name"] for f in ir["functions"]}
    assert {"body_to_read", "refusal_for", "frames_request", "doubled"} <= names
    assert ir.get("externs", []) == []
