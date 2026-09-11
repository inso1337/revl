"""Routed inputs are untrusted sources — roadmap item 457, Slice 1
(docs/design/457-endpoint-one-definition.md, "The five derived artifacts, exactly").

A `route` clause binds the operation's WHOLE parameter list from the request: the
path placeholders, the query scalars, the one body record, the `Authorization`
header, and the `Request` itself. Every one of those is a value an anonymous
caller chose, so the compiler declares `Untrusted[...]` on the author's behalf —
the inbound twin of the `emission[web]` return the outbound half mints (D-424c.9).
Without that stamp a routed handler could hand a raw request value straight to a
G9 sink (`emit run(...)`) with no `endorse` anywhere, which is the one thing the
taint lattice exists to make impossible.

The origin class is `input` — the class `_origin_of` gives an inbound crossing
that declares no capability scope — and it is keyed by the operation name, so the
provide method implementing the operation sees it in its own body.

This module pins the stamp and, just as importantly, its non-vacuity: a program
that declares no qualifier but does declare a route is NOT a vacuous program, and
a routed value that reaches only an ordinary (non-sink) emission still pays
nothing. The stamp is route-gated, so an unrouted operation is untouched, and a
declared qualifier keeps its own label — the route only fills what the author
left unlabelled.
"""

import pytest

from revl import RevlError
from revl.compiler import compile_source
from revl.diagnostics import classify


# a web fetch (the pre-existing untrusted origin), a shell sink (authority), an
# ordinary emission that grants nothing, and the two record shapes a route binds.
_PRELUDE = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
    "extern emission[db] fn save(v: Str) = @py { return }\n"
    "type NewNote = { title: Str, body: Str }\n"
    "type Request = { method: Str, path: Str }\n"
)


def _app(operation: str, handler: str) -> str:
    """A one-operation `NotesApi` and the component that provides it.

    `operation` is the service-side declaration (route clause plus signature);
    `handler` is the provide-method body, whose signature mirrors the operation.
    """
    return (
        _PRELUDE
        + "service NotesApi {\n"
        + operation
        + "}\n"
        + "component NotesHttp provides notes_api: NotesApi {\n"
        + "  provide notes_api {\n"
        + handler
        + "  }\n"
        + "}\n"
    )


def _refusal(src: str) -> str:
    """Compile `src`, require a refusal, and return its diagnostic code."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "route_input_taint.rvl")
    return classify(excinfo.value)["code"]


# the four bind kinds, each reaching a sink through the value the route bound.
# (label, service-side operation, provide-method signature, the request value)
_BIND_KINDS = [
    ("path", '  route get "/notes/{id}"\n'
             "  emission fn get_note(id: Str) -> Str\n",
     "get_note(id)", "id"),
    ("query", '  route get "/notes"\n'
              "  emission fn get_note(limit: Str) -> Str\n",
     "get_note(limit)", "limit"),
    ("body", '  route post "/notes"\n'
             "  emission fn get_note(note: NewNote) -> Str\n",
     "get_note(note)", "note.title"),
    ("request", '  route get "/notes/{id}"\n'
                "  emission fn get_note(req: Request) -> Str\n",
     "get_note(req)", "req.path"),
]


# --- 1. the stamp: every bind kind is an untrusted source ----------------------

@pytest.mark.parametrize("label, operation, signature, value",
                         _BIND_KINDS, ids=[k[0] for k in _BIND_KINDS])
def test_a_routed_input_reaching_a_sink_is_refused_with_G9(label, operation,
                                                          signature, value):
    """The route clause alone — no `Untrusted[T]` anywhere in the source —
    engages the walk and refuses the request value at the sink."""
    src = _app(
        operation,
        f"    fn {signature} {{\n"
        f"      emit run({value})\n"
        f"      return \"ok\"\n"
        "    }\n",
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "route_input_taint.rvl")
    err = excinfo.value
    assert classify(err)["code"] == "G9", f"{label}: got {classify(err)['code']}"
    message = err.message.lower()
    assert "untrusted" in message
    assert "shell command" in message      # the sink is named
    assert "(input)" in message            # the origin class is named


def test_the_route_clause_alone_is_what_flips_the_verdict():
    """The same handler, routed and unrouted: the clause is the whole difference."""
    handler = (
        "    fn get_note(id) {\n"
        "      emit run(id)\n"
        "      return id\n"
        "    }\n"
    )
    routed = _app('  route get "/notes/{id}"\n'
                  "  emission fn get_note(id: Str) -> Str\n", handler)
    unrouted = _app("  emission fn get_note(id: Str) -> Str\n", handler)

    assert _refusal(routed) == "G9"
    # no route clause -> the parameter is not a request value -> no refusal
    compile_source(unrouted, "unrouted.rvl")


def test_the_pre_existing_web_rule_still_refuses():
    """Non-vacuity of the rule the stamp sits beside: `emission[web]` -> sink
    still refuses, and names its own origin."""
    src = _app(
        '  route get "/notes"\n'
        "  emission fn get_note(url: Str) -> Str\n",
        "    fn get_note(url) {\n"
        "      let page = emit fetch(url)\n"
        "      emit run(page)\n"
        "      return url\n"
        "    }\n",
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "route_input_taint.rvl")
    err = excinfo.value
    assert classify(err)["code"] == "G9"
    assert "(web)" in err.message.lower()


# --- 2. non-vacuity: ordinary CRUD pays nothing -------------------------------

def test_a_routed_input_reaching_an_ordinary_emission_is_admitted():
    """A request value that reaches only an emission which grants nothing is
    admitted — the stamp does not make every routed handler refuse."""
    src = _app(
        '  route get "/notes"\n'
        "  emission fn list_notes(q: Str) -> Str\n",
        "    fn list_notes(q) {\n"
        "      emit save(q)\n"
        "      return q\n"
        "    }\n",
    )
    compile_source(src, "ordinary_crud.rvl")


# --- 3. the audited escape hatch ---------------------------------------------

def test_endorse_input_declassifies_a_routed_input():
    """The one way out is the audited `endorse[input]` expression, declared on
    the operation and applied to the value."""
    src = _app(
        '  route get "/notes/{id}"\n'
        "  endorse[input] emission fn get_note(id: Str) -> Str\n",
        "    fn get_note(id) {\n"
        '      let safe = endorse[input](id, reason = "operator-reviewed")\n'
        "      emit run(safe)\n"
        "      return id\n"
        "    }\n",
    )
    compile_source(src, "endorsed.rvl")


# --- 4. a declared qualifier keeps its own label ------------------------------

def test_a_declared_qualifier_on_a_routed_parameter_survives_the_stamp():
    """The stamp fills only what the author left unlabelled. A `Secret[Str]`
    routed parameter is still a confidential value — if the stamp overwrote the
    label, its origin would become `input` and the disclosure refusal would not
    fire."""
    src = _app(
        '  route get "/notes/{id}"\n'
        "  emission fn get_note(key: Secret[Str]) -> Str\n",
        "    fn get_note(key) {\n"
        "      emit save(key)\n"
        '      return "ok"\n'
        "    }\n",
    )
    assert _refusal(src) == "G-SECRET-FLOW"
