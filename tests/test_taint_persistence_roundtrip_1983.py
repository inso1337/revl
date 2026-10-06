"""The host boundary is not a launder edge (issue #1983).

`docs/design/249-taint-provenance.md` scopes the no-false-clean invariant
WITHIN a body: the turn's own fns, records, collections, state and relay are
tracked, and the host round-trip through a persistence resource is the one
boundary the walk did not model. So

    let page = emit fetch(url)          # page : Untrusted[Str]
    emit write(path, page)              # the value leaves the turn
    let draft = emit read(path)         # read declares `-> Str`, so it returns CLEAN
    emit announce(draft, "...")         # ... and the sink accepts it

was ADMITTED, while the direct `emit announce(emit fetch(url), ...)` is refused
(G9). `write`'s parameter is a SINK and `read`'s return is a plain `Str`, so the
value is laundered by the very extern declarations that describe the boundary.

The fix (Slice B, writer-side): the taint walk keeps a table keyed by
`(scope, extern)` — the persistence scope of the extern that wrote, and the
extern's name — and a call to a *sibling* extern of the same scope MINTS the
recorded origins into its own result. `read` is then dirty exactly when
something tainted was written, the existing G9 refusal fires at `announce` with
no new raise site, and the chain names every hop.

Two properties this file pins:

* **Flow-insensitive per resource.** The table is joined per resource, not per
  execution order, so reordering the write and the read inside a body cannot
  defeat it. That is deliberate — it is why the refusal cannot be dodged by
  moving the write after a guard.
* **No false dirty.** The table is only ever filled by a write that carries a
  CONCRETE origin, and a reload only mints for a *sibling* extern of the scope
  (never for the writer's own name). A program that writes clean values, or
  reads a resource it never wrote, or writes a tainted value nothing ever
  reads, stays admitted — including the corpus's `db` shape, where `execute`
  and `put` are both sinks of `db` and no reader exists.
"""

import pytest

from revl import RevlError
from revl.compiler import compile_source
from revl.diagnostics import classify


_PRELUDE = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "extern emission[fs] fn write(path: Str, value: Str) = @py { return }\n"
    "extern emission[fs] fn read(path: Str) -> Str = @py { return \"\" }\n"
    "extern emission fn announce(to: Trusted[Str], body: Str) = @py { return }\n"
)


def _agent(body: str, prelude: str = _PRELUDE) -> str:
    return (
        prelude
        + "service Memory { emission fn store(path: Str) }\n"
        + "component A provides mem: Memory {\n"
        + "  provide mem {\n"
        + f"    fn store(path) {{\n{body}\n    }}\n"
        + "  }\n"
        + "}\n"
    )


def _hidden_write(body: str, prelude: str = _PRELUDE) -> str:
    """The issue's round-trip shape with the write in a sibling provider."""
    return (
        prelude
        + "service Store { emission fn save(path: Str)\n"
        + "                emission fn load(path: Str) -> Str }\n"
        + "service Ops { emission fn resume(path: Str) }\n"
        + "component S provides store: Store {\n"
        + "  provide store {\n"
        + "    fn save(path) {\n"
        + "      let page = emit fetch(path)\n"
        + "      emit write(path, page)\n"
        + "    }\n"
        + "    fn load(path) { return emit read(path) }\n"
        + "  }\n"
        + "}\n"
        + "component A provides ops: Ops requires store: Store {\n"
        + "  provide ops {\n"
        + f"    fn resume(path) {{\n{body}\n    }}\n"
        + "  }\n"
        + "}\n"
    )


def _refuse(src: str, filename: str = "roundtrip.rvl") -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, filename)
    return excinfo.value


# --- 1. the defect: the host round-trip no longer launders the origin ---------

_ROUNDTRIP = (
    "      let page = emit fetch(path)\n"
    "      emit write(path, page)\n"
    "      let draft = emit read(path)\n"
    "      emit announce(draft, \"hi\")"
)


def test_a_host_round_trip_no_longer_launders_an_untrusted_origin():
    """The issue's repro: today this was ADMITTED under the trusted-author
    profile while the direct form is refused."""
    err = _refuse(_agent(_ROUNDTRIP))
    assert classify(err)["code"] == "G9", err.message
    assert "untrusted" in err.message.lower()
    assert "`announce`" in err.message       # the sink is named
    assert "(web)" in err.message            # the origin survives the boundary


def test_the_refusal_names_the_whole_chain_through_the_host():
    """The diagnostic must name the hops, not just the sink: the reader has to
    see that the value came back out of `read`."""
    err = _refuse(_agent(_ROUNDTRIP))
    assert "fetch() -> write -> read -> announce" in str(err), str(err)


def test_the_read_is_dirty_because_something_tainted_was_written():
    """The same shape without the write is admitted — the read is not blanket
    dirty, and the reload mints exactly what was written."""
    clean = _agent(
        "      let page = emit fetch(path)\n"
        "      emit write(path, \"static page\")\n"
        "      let draft = emit read(path)\n"
        "      emit announce(draft, \"hi\")"
    )
    compile_source(clean, "clean_roundtrip.rvl")


def test_reordering_the_read_before_the_write_cannot_defeat_it():
    """Flow-insensitive per resource: the table is joined per (scope, extern),
    so the refusal does not depend on the order inside the body."""
    err = _refuse(_agent(
        "      let draft = emit read(path)\n"
        "      let page = emit fetch(path)\n"
        "      emit write(path, page)\n"
        "      emit announce(draft, \"hi\")"
    ))
    assert classify(err)["code"] == "G9", err.message


def test_a_read_of_a_resource_the_body_never_wrote_is_clean():
    compile_source(
        _agent(
            "      let draft = emit read(path)\n"
            "      emit announce(draft, \"hi\")"
        ),
        "read_only.rvl",
    )


def test_a_tainted_write_nothing_reads_reaches_no_sink():
    """Persistence is not authority: storing an untrusted value is not a G9
    flow, and with no reader the body reaches no trusted sink at all."""
    compile_source(
        _agent(
            "      let page = emit fetch(path)\n"
            "      emit write(path, page)"
        ),
        "write_only.rvl",
    )


# --- 2. the seam: a wrapper that only reloads is not a launder edge ----------

def test_a_wrapper_operation_that_only_reloads_is_refused_at_its_caller():
    """A provider method that only does `return emit read(p)` must carry the
    origin in its own signature, so a caller that never names the sink (or the
    resource) is judged on it."""
    err = _refuse(_hidden_write(
        "      let draft = emit store.load(path)\n"
        "      emit announce(draft, \"hi\")"
    ))
    assert classify(err)["code"] == "G9", err.message
    assert "`announce`" in err.message


def test_a_wrapper_that_reloads_a_clean_resource_is_admitted():
    """The same seam with a clean write stays admitted — the signature mint
    follows the table, not the mere existence of a reader."""
    src = _hidden_write(
        "      let draft = emit store.load(path)\n"
        "      emit announce(draft, \"hi\")"
    ).replace("let page = emit fetch(path)", "let page = \"static page\"")
    compile_source(src, "wrapper_clean.rvl")


# --- 3. no false dirty: the corpus's sibling-sink shape ----------------------

_SIBLING_SINKS = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "extern emission[db] fn execute(sql: Str) -> Int = @py { return 0 }\n"
    "extern emission[db] fn put(key: Str, value: Str) = @py { return }\n"
)


def test_two_sinks_of_one_scope_with_no_reader_stay_admitted():
    """`db` has several writer externs (`execute` for statements, `put` for a
    key, both in the corpus's memory store) and no reader here. Recording the
    tainted write must not make the scope's sibling sinks dirty."""
    compile_source(
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[db] fn execute(sql: Str) -> Int = @py { return 0 }\n"
        "extern emission[db] fn put(key: Str, value: Str) = @py { return }\n"
        "service Memory { emission fn store(url: Str) }\n"
        "component A provides mem: Memory {\n"
        "  provide mem {\n"
        "    fn store(url) {\n"
        "      let page = emit fetch(url)\n"
        "      emit put(url, page)\n"
        "      emit execute(\"INSERT INTO pages VALUES ('\")\n"
        "    }\n"
        "  }\n"
        "}\n",
        "sibling_sinks.rvl",
    )


def test_a_program_with_no_persistence_scope_is_untouched():
    """Vacuity: with no persistence sink declared the table stays empty, so the
    walk adds no refusal the pre-#1983 one did not have. Here a fetch is read and
    never sunk at all."""
    compile_source(
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component A provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let page = emit fetch(url)\n"
        "    }\n"
        "  }\n"
        "}\n",
        "no_scope.rvl",
    )


def test_the_direct_form_is_still_refused():
    """The pre-existing G9 rule is untouched: the round-trip fix ADDS a refusal,
    it does not replace this one."""
    err = _refuse(_agent(
        "      let page = emit fetch(path)\n"
        "      emit announce(page, \"hi\")"
    ))
    assert classify(err)["code"] == "G9", err.message
    assert "fetch() -> announce" in str(err), str(err)


def test_the_join_is_per_scope_not_per_path():
    """A documented conservatism, pinned so a later slice refines it
    deliberately: `write(p, ...)` then `read(q)` names two different paths, and
    the table joins them at the scope. The refusal is sound (something tainted
    did reach the scope) but can refuse a program that never reloads the written
    path. Keying the table by path is H16 (provenance-carrying memory)."""
    err = _refuse(_agent(
        "      let page = emit fetch(path)\n"
        "      emit write(path, page)\n"
        "      let draft = emit read(\"other/page\")\n"
        "      emit announce(draft, \"hi\")"
    ))
    assert classify(err)["code"] == "G9", err.message
