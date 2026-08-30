"""Taint and provenance — static information-flow (roadmap item 249, Slice A).

The security property: untrusted input cannot DIRECTLY create authority. A value
that returns across an untrusted-origin boundary is `Untrusted[T]`; a sink that
grants authority declares its parameter `Trusted[T]`; the checker refuses an
`Untrusted[T]` reaching a `Trusted[T]` sink unless a declassifier intervenes.

The refusal is a compile error tagged G9 (docs/design/249-taint-provenance.md).
This is Slice A (the static half); the runtime tag is Slice B, queued behind 243
Slice 2. See the design doc for the full lattice, sinks and declassifiers.
"""

import pytest

from revl import RevlError
from revl.compiler import compile_source
from revl.diagnostics import classify, explain
from revl.taint import strip_qualifiers, top_qualifier


# a web fetch (untrusted origin) and a shell sink (authority), reused below
_PRELUDE = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
)


def _agent(body: str) -> str:
    return (
        _PRELUDE
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + f"    fn go(url) {{\n{body}\n    }}\n"
        + "  }\n"
        + "}\n"
    )


# --- 1. the core rule: a tainted value reaching a sink is refused (G9) ---------

def test_tainted_value_reaching_a_sink_is_refused_with_G9():
    src = _agent("      let page = emit fetch(url)\n      emit run(page)")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "taint_sink.rvl")
    err = excinfo.value
    record = classify(err)
    assert record["code"] == "G9", f"expected G9, got {record['code']}: {err.message}"
    assert "untrusted" in err.message.lower()
    assert "shell command" in err.message  # the sink is named
    assert "web" in err.message             # the origin is named


def test_G9_is_a_registered_guarantee_with_a_fix():
    record = explain("G9")
    assert record["ok"] and record["guarantee"]
    assert record["fix"]


# --- 2. a declassified value flows clean --------------------------------------

def test_verified_parser_declassifies_and_flows_clean():
    """The strongest declassifier (Decision 3.1): a `verified fn` returning
    `Trusted[T]` is total (G7), so the untrusted bytes cannot slip through — its
    result flows into the sink with no refusal."""
    src = (
        _PRELUDE
        + "verified fn sanitize(s: Untrusted[Str]) -> Trusted[Str] { return s }\n"
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let page = emit fetch(url)\n"
        + "      let safe = sanitize(page)\n"
        + "      emit run(safe)\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )
    compile_source(src, "declassified.rvl")  # must not raise


def test_endorse_declassifies_and_flows_clean():
    """The audited escape hatch (Decision 3.2): `endorse(<value>)` produces a
    `Trusted[T]` and flows clean."""
    src = _agent(
        "      let page = emit fetch(url)\n"
        "      let safe = endorse(page)\n"
        "      emit run(safe)")
    compile_source(src, "endorsed.rvl")  # must not raise


# --- 3. propagation soundness: taint never silently disappears (no false-clean) -

def test_concatenation_does_not_launder_a_trusted_prefix():
    """taint(a + b) = taint(a) ∪ taint(b): a trusted prefix does not launder an
    untrusted suffix (Decision 2, concatenation)."""
    src = _agent(
        "      let page = emit fetch(url)\n"
        "      let cmd = \"echo \" + page\n"
        "      emit run(cmd)")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "concat.rvl")
    assert classify(excinfo.value)["code"] == "G9"


def test_taint_propagates_through_a_pure_call():
    """A pure function returning a value derived from an untrusted argument
    returns untrusted (Decision 2, function calls)."""
    src = (
        _PRELUDE
        + "extern pure fn upper(s: Str) -> Str = @py { return s }\n"
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let page = emit fetch(url)\n"
        + "      let x = upper(page)\n"
        + "      emit run(x)\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "callprop.rvl")
    assert classify(excinfo.value)["code"] == "G9"


def test_a_plain_fn_returning_trusted_does_not_launder():
    """Only a `verified fn` (total, G7) may declassify by construction. A plain
    `fn` that declares a `Trusted[T]` return while taking untrusted input is not
    a checked parser — the checker must still see the flow as tainted."""
    src = (
        _PRELUDE
        + "fn sanitize(s: Untrusted[Str]) -> Trusted[Str] { return s }\n"
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let page = emit fetch(url)\n"
        + "      let safe = sanitize(page)\n"
        + "      emit run(safe)\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "fake_sanitizer.rvl")
    assert classify(excinfo.value)["code"] == "G9"


# --- 4. byte-identity: the qualifier is stripped, no runtime feature -----------

def test_program_without_qualifiers_is_untouched():
    """A program using no `Untrusted`/`Trusted` qualifier engages nothing: the
    ordinary web-fetch-into-shell flow compiles exactly as before item 249."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Str) = @py { return }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let page = emit fetch(url)\n"
        "      emit run(page)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    compile_source(src, "plain.rvl")  # must not raise — no taint surface


def test_qualifier_is_stripped_from_the_emitted_ir():
    """Orthogonality: the base type the IR carries is the bare type — the
    qualifier lives only in the checker, never in the emitted artifact."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "verified fn sanitize(s: Untrusted[Str]) -> Trusted[Str] { return s }\n"
    )
    ir = compile_source(src, "strip.rvl")
    externs = {e["name"]: e for e in ir.get("externs") or []}
    assert externs["fetch"]["returns"] == "Str"
    fns = {f["name"]: f for f in ir.get("functions") or []}
    assert fns["sanitize"]["returns"] == "Str"
    assert fns["sanitize"]["params"][0]["type"] == "Str"


# --- 5. provenance on the G8 audit surface (Decision 5) -----------------------

def test_taint_and_declassify_tokens_reach_the_audit_surface():
    """A web-tainted value reaching a (non-refusing) emission, and a declassified
    one, both leave stable tokens on the audit surface — so `revl audit --diff`
    treats a newly-routed exfiltration edge or a newly-added `endorse` as a
    widening, the same way it already treats one more emission."""
    from revl.audit_diff import audit_report, crossings
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[net] fn send(body: Str) = @py { return }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let page = emit fetch(url)\n"
        "      emit send(page)\n"          # web taint reaches a send: recorded
        "      let safe = endorse(page)\n"  # a declassification: recorded
        "      emit run(safe)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    ir = compile_source(src, "audit.rvl")
    tokens = crossings(audit_report(ir))
    assert "taint:Agent:web" in tokens
    assert "declassify:Agent:web" in tokens


def test_no_taint_tokens_without_qualifiers():
    """Byte-identity for the audit surface: a program with no qualifier carries
    no `taint:`/`declassify:` token at all."""
    from revl.audit_diff import audit_report, crossings
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "extern emission[net] fn send(body: Str) = @py { return }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) { let page = emit fetch(url)  emit send(page) }\n"
        "  }\n"
        "}\n"
    )
    ir = compile_source(src, "plain_audit.rvl")
    assert "taint" not in ir["components"][0]
    tokens = crossings(audit_report(ir))
    assert not any(t.startswith(("taint:", "declassify:")) for t in tokens)


# --- the type-surgery helpers (unit) ------------------------------------------

def test_strip_qualifiers_is_recursive_and_idempotent():
    assert strip_qualifiers("Untrusted[Str]") == "Str"
    assert strip_qualifiers("Trusted[Int]") == "Int"
    assert strip_qualifiers("List[Untrusted[Str]]") == "List[Str]"
    assert strip_qualifiers("Result[Trusted[Int], Str]") == "Result[Int, Str]"
    assert strip_qualifiers("Str") == "Str"
    assert strip_qualifiers(strip_qualifiers("Untrusted[Str]")) == "Str"


def test_top_qualifier():
    assert top_qualifier("Untrusted[Str]") == "Untrusted"
    assert top_qualifier("Trusted[Int]") == "Trusted"
    assert top_qualifier("Str") is None
    assert top_qualifier("List[Untrusted[Str]]") is None
