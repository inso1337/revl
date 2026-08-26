"""Witnessed-inverse externs — roadmap item 243, SLICE 1 (frontend + IR core).

Design: docs/design/243-witnessed-externs.md (design locked, Fable-reviewed).

The one thing to get right: a `witnessed` effect is a TRANSACTION, not an
`acquire` bracket. Its declared inverse replays on ABORT ONLY and is discharged
(+ its witness GC'd) on commit; a bracket also replays on a clean unload. So the
teardown accumulator gains a second entry kind, `transactional`, distinct from
the bracket — landed here as the IR descriptor the Slice-2 runtime seam reads.

Slice 1 is additive: no existing program uses `witnessed`, so the backends stay
green and are untouched. These tests exercise parse/check/IR only.
"""

import pytest

from revl.parser import Parser
from revl.compiler import compile_source
from revl.emission_analysis import _emitting_fns
from revl.errors import RevlError


# -- helpers ----------------------------------------------------------------

_TYPES = (
    "type FsWitness = { path: Str, backup: Str }\n"
    "type FsError = { code: Str }\n"
)
_RESTORE = "extern pure fn restore(w: FsWitness) -> Unit = @ts { return }\n"
_RM = (
    "extern witnessed[fs] fn rm(path: Str) -> Result[FsWitness, FsError]"
    " undo restore(result) = @ts { return {} }\n"
)


def _witnessed_program(extra: str = "") -> str:
    return _TYPES + _RESTORE + _RM + extra


def _rm_node(ir: dict) -> dict:
    return next(e for e in ir["externs"] if e["name"] == "rm")


# -- classification parses + checks -----------------------------------------

def test_parser_accepts_witnessed_classification_and_scope():
    prog = Parser(_witnessed_program(), "t.rvl").parse()
    rm = next(e for e in prog.externs if e.name == "rm")
    assert rm.classification == "witnessed"
    assert rm.capabilities == ("fs",)


def test_witnessed_is_a_contextual_keyword_not_reserved():
    # `witnessed` is recognised only in the classification slot, so it remains a
    # legal ordinary identifier everywhere else (no self-host KEYWORDS sync).
    ir = compile_source("fn f(witnessed: Int) -> Int { return witnessed }", "t.rvl")
    assert ir["functions"][0]["name"] == "f"


def test_witnessed_extern_checks_and_lowers():
    ir = compile_source(_witnessed_program(), "t.rvl")
    rm = _rm_node(ir)
    assert rm["class"] == "witnessed"
    assert rm["capabilities"] == ["fs"]
    assert "undo" in rm


# -- the transactional entry kind (the one thing to get right) --------------

def test_lowered_ir_carries_a_transactional_entry():
    rm = _rm_node(compile_source(_witnessed_program(), "t.rvl"))
    assert rm["entry_kind"] == "transactional"
    assert rm["revertible"] is True
    assert rm["witness"] == "FsWitness"


def test_registration_is_ok_conditional():
    # the inverse auto-registers on the Result's `Ok` branch only — a failed
    # mutation touched nothing and must not schedule a rollback.
    rm = _rm_node(compile_source(_witnessed_program(), "t.rvl"))
    assert rm["ok_conditional"] is True


def test_transactional_entry_differs_from_an_acquire_bracket():
    # An acquire is a BRACKET: it carries `undo` but no `entry_kind` (its effect
    # step replays on clean unload AND abort). A witnessed extern is a
    # TRANSACTION: `entry_kind == "transactional"`, abort-only + commit
    # discharge. The two must be distinguishable in the IR.
    witnessed = _rm_node(compile_source(_witnessed_program(), "t.rvl"))
    acq_src = (
        _TYPES + _RESTORE
        + "extern acquire fn open(p: Str) -> FsWitness undo restore(result)"
          " = @ts { return {} }\n"
    )
    acquire = next(e for e in compile_source(acq_src, "t.rvl")["externs"]
                   if e["name"] == "open")
    assert acquire.get("entry_kind") is None
    assert witnessed["entry_kind"] == "transactional"
    assert witnessed["entry_kind"] != acquire.get("entry_kind")
    # both carry an inverse — the difference is the entry kind, not the undo
    assert "undo" in witnessed and "undo" in acquire


# -- refusal: witnessed outside effect position -----------------------------

def test_witnessed_refused_in_fn_body():
    with pytest.raises(RevlError) as ei:
        compile_source(
            _witnessed_program(
                "fn f(p: Str) -> Result[FsWitness, FsError] { return rm(p) }"),
            "t.rvl")
    assert "cannot be called in the body of fn `f`" in str(ei.value)


def test_witnessed_refused_in_test_body():
    with pytest.raises(RevlError) as ei:
        compile_source(_witnessed_program('test "deletes" { let w = rm("x") }'),
                       "t.rvl")
    assert "cannot be called in the body of test `deletes`" in str(ei.value)


# -- refusal: the declared inverse's classification -------------------------

def test_emission_inverse_refused():
    src = (
        _TYPES
        + "extern emission fn log(w: FsWitness) = @ts { return }\n"
        + "extern witnessed[fs] fn rm(path: Str) -> Result[FsWitness, FsError]"
          " undo log(result) = @ts { return {} }\n"
    )
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    assert "which is an emission" in str(ei.value)


def test_witnessed_inverse_refused():
    src = (
        _TYPES + _RESTORE
        + "extern witnessed[fs] fn wr(w: FsWitness) -> Result[FsWitness, FsError]"
          " undo restore(result) = @ts { return {} }\n"
        + "extern witnessed[fs] fn rm(path: Str) -> Result[FsWitness, FsError]"
          " undo wr(result) = @ts { return {} }\n"
    )
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    assert "which is itself witnessed" in str(ei.value)


# -- refusal: the witness must be WAL-serializable data ---------------------

def test_host_object_witness_refused():
    # `Pool` is a host handle (dies with the process); after a crash only the
    # WAL survives, so the witness must be durable data the inverse rebuilds from.
    src = (
        _TYPES
        + "extern pure fn rel(m: Pool) -> Unit = @ts { return }\n"
        + "extern witnessed[fs] fn rm(path: Str) -> Result[Pool, FsError]"
          " undo rel(result) = @ts { return {} }\n"
    )
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    assert "is a host object" in str(ei.value)


def test_non_result_return_refused():
    src = (
        _TYPES + _RESTORE
        + "extern witnessed[fs] fn rm(path: Str) -> FsWitness"
          " undo restore(result) = @ts { return {} }\n"
    )
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    assert "must return `Result[Witness, Error]`" in str(ei.value)


def test_missing_undo_refused():
    src = (
        _TYPES
        + "extern witnessed[fs] fn rm(path: Str) -> Result[FsWitness, FsError]"
          " = @ts { return {} }\n"
    )
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    assert "must declare `undo`" in str(ei.value)


# -- parser scope guards ----------------------------------------------------

def test_non_witnessed_extern_rejects_capability_scope():
    with pytest.raises(RevlError) as ei:
        Parser("extern pure[x] fn f() = @ts { return }", "t.rvl").parse()
    assert "takes no capability scope" in str(ei.value)


def test_empty_witnessed_scope_refused():
    with pytest.raises(RevlError) as ei:
        Parser(
            "extern witnessed[] fn f(p: Str) -> Result[Int, Int]"
            " undo g(result) = @ts { return {} }", "t.rvl").parse()
    assert "names no capability" in str(ei.value)


# -- emission analysis no longer marks a witnessed call revertible ----------

def test_emission_analysis_treats_witnessed_as_boundary_crossing():
    # Rule 2: reversibility ties to registration, not to a declared inverse. A
    # witnessed extern seeds the emitting fixed point exactly like an emission,
    # so an unregistered witnessed reach is never mistaken for revertible/invisible
    # to the item-33 policy gate.
    ir = compile_source(_witnessed_program(), "t.rvl")
    emitting = _emitting_fns([], ir["externs"])
    assert "rm" in emitting
    # its pure inverse is not a boundary crossing
    assert "restore" not in emitting


def test_witnessed_capability_is_the_declared_scope():
    from revl.emission_analysis import _emitting_capabilities
    ir = compile_source(_witnessed_program(), "t.rvl")
    caps = _emitting_capabilities([], ir["externs"])
    assert caps["rm"] == {"fs"}
