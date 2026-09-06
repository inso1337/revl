"""Effect ownership modes, owned + borrowed v1 (roadmap item 308).

Exercises the three checks the design specifies (docs/design/308-effect-
ownership-modes.md):

  * R0 — an `acquire` return must be a NOMINAL OPAQUE HANDLE type (the taint
    base repair), with a `Unit` no-handle exemption for a lock-style acquire;
  * O1 — no hand-call of a declared inverse (a close) on a resource, in any
    body / undo / compensate position, with the mandatory own-undo exemption;
  * B1 — a borrowed resource does not escape its scope: the seven crossing
    clauses each have a refusing fixture, and the legitimate owner-holds-and-
    lends and pass-a-borrow-down patterns still admit.

The checks are additive: no new grammar, checker-only, and every fixture that
does NOT contain a genuine escape still admits.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# A nominal opaque handle `Sock`, its inverse `close_sock`, and the acquire.
_BASE = (
    "type Sock = { fd: Int }\n"
    "extern pure fn close_sock(h: Sock) = @py { return None }\n"
    "extern acquire fn open_sock() -> Sock undo close_sock(result)"
    ' = @py { return {"fd": 1} }\n'
)


def _refuse(src: str) -> str:
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    return str(ei.value)


def _admit(src: str) -> None:
    compile_source(src, "t.rvl")


# ---------------------------------------------------------------------------
# R0 — acquire returns are nominal opaque handle types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ret", ["Int", "Str", "Bool", "Float"])
def test_r0_primitive_acquire_return_refused(ret):
    msg = _refuse(
        f"extern pure fn drop(h: {ret}) = @py {{ return None }}\n"
        f"extern acquire fn grab() -> {ret} undo drop(result) = @py {{ return 1 }}\n")
    assert "nominal opaque handle type" in msg
    assert "308" in msg and "R0" in msg


@pytest.mark.parametrize("ret", ["Result[Sock, Str]", "Opt[Sock]", "List[Sock]"])
def test_r0_structural_acquire_return_refused(ret):
    msg = _refuse(
        _BASE + f"extern acquire fn grab() -> {ret} undo close_sock(result)"
        " = @py { return None }\n")
    assert "nominal opaque handle type" in msg


def test_r0_nominal_handle_admits():
    _admit(_BASE + "component C { let s = effect open_sock() undo close_sock(s) }\n")


def test_r0_unit_lock_acquire_admits():
    # a lock-style acquire yields no handle value; `Unit` is exempt and does
    # not poison the taint base
    _admit(
        "extern pure fn unlock() = @py { return None }\n"
        "extern acquire fn lock() -> Unit undo unlock() = @py { return None }\n"
        "component C { effect lock() undo unlock() }\n")


# ---------------------------------------------------------------------------
# O1 — no hand-call of a declared inverse
# ---------------------------------------------------------------------------

def test_o1_own_undo_exemption_admits():
    # the acquiring binding's own undo naming its own inverse is admitted
    _admit(_BASE + "component C { let s = effect open_sock() undo close_sock(s) }\n")


def test_o1_borrower_close_refused():
    msg = _refuse(
        _BASE + "service S { fn shut(c: Sock) -> Int }\n"
        "component P provides s: S {\n"
        "  provide s { fn shut(c) { let x = close_sock(c)  return 1 } }\n"
        "}\n")
    assert "declared inverse" in msg and "borrowed" in msg
    assert "O1" in msg


def test_o1_owner_hand_close_refused():
    # the owner racing its own accumulator: a mid-session close of its own
    # handle in a body position (not the acquiring binding's own undo)
    msg = _refuse(
        _BASE + "service S { fn ping() -> Int }\n"
        "component P provides s: S {\n"
        "  let sock = effect open_sock() undo close_sock(sock)\n"
        "  provide s { fn ping() { let x = close_sock(sock)  return 1 } }\n"
        "}\n")
    assert "declared inverse" in msg


def test_o1_close_smuggled_into_unrelated_undo_refused():
    msg = _refuse(
        _BASE + "extern pure fn noop() = @py { return None }\n"
        "service S { fn shut(c: Sock) -> Int }\n"
        "component P provides s: S {\n"
        "  provide s { fn shut(c) { effect noop() undo close_sock(c)  return 1 } }\n"
        "}\n")
    assert "declared inverse" in msg or "cannot be placed in an `undo`" in msg


def test_o1_close_smuggled_into_compensate_refused():
    msg = _refuse(
        _BASE + "extern emission fn ping() = @py { return None }\n"
        "service S { emission fn go(c: Sock) }\n"
        "component P provides s: S {\n"
        "  provide s { fn go(c) { emit ping() compensate close_sock(c) } }\n"
        "}\n")
    assert "declared inverse" in msg or "compensate" in msg


# ---------------------------------------------------------------------------
# B1 — a borrow does not escape (one refusing fixture per clause)
# ---------------------------------------------------------------------------

def test_b1_clause1_store_into_activation_state_refused():
    msg = _refuse(
        _BASE + "service S { emission fn put(c: Sock) }\n"
        "component P provides s: S {\n"
        "  let slots = effect Map.new() undo slots.drop()\n"
        "  provide s { fn put(c) { effect slots.insert(\"k\", c) undo slots.remove(\"k\") } }\n"
        "}\n")
    assert "activation-level state" in msg and "borrowed" in msg


def test_b1_clause2_closure_capture_refused_owner_too():
    # clause 2 binds the OWNER too: capturing its own handle in a closure
    msg = _refuse(
        _BASE + "extern pure fn apply(f: () -> Int) -> Int = @py { return 0 }\n"
        "service S { fn ping() -> Int }\n"
        "component P provides s: S {\n"
        "  let sock = effect open_sock() undo close_sock(sock)\n"
        "  provide s { fn ping() { return apply(() => sock.fd) } }\n"
        "}\n")
    assert "captured by a closure" in msg


def test_b1_clause3_non_owner_return_refused():
    msg = _refuse(
        _BASE + "service S { fn take(c: Sock) -> Sock }\n"
        "component P provides s: S {\n"
        "  provide s { fn take(c) { return c } }\n"
        "}\n")
    assert "returned across a signature" in msg and "borrowed" in msg


def test_b1_clause3_keys_on_tainted_carrier_type():
    # a `Session` wrapping a `Sock` must not walk out unrefused
    msg = _refuse(
        _BASE + "type Session = { conn: Sock }\n"
        "extern pure fn wrap(c: Sock) -> Session = @py { return {\"conn\": c} }\n"
        "service S { fn take(c: Sock) -> Session }\n"
        "component P provides s: S {\n"
        "  provide s { fn take(c) { return wrap(c) } }\n"
        "}\n")
    assert "Session" in msg


def test_b1_clause4_carrier_insert_refused():
    msg = _refuse(
        _BASE + "type Box = { c: Sock }\n"
        "service S { emission fn put(c: Sock) }\n"
        "component P provides s: S {\n"
        "  let slots = effect Map.new() undo slots.drop()\n"
        "  provide s { fn put(c) { effect slots.insert(\"k\", { c: c }) undo slots.remove(\"k\") } }\n"
        "}\n")
    assert "borrowed" in msg  # caught as a carrier / activation-state store


def test_b1_clause5_borrow_in_undo_refused():
    msg = _refuse(
        _BASE + "extern pure fn touch(c: Sock) = @py { return None }\n"
        "service S { emission fn go(c: Sock) }\n"
        "component P provides s: S {\n"
        "  provide s { fn go(c) { effect touch(c) undo touch(c) } }\n"
        "}\n")
    assert "undo" in msg and "borrowed" in msg


def test_b1_clause5_owned_in_compensate_refused():
    # the compensate half binds OWNED values too (Phase-2 use-after-close)
    msg = _refuse(
        _BASE + "extern pure fn note(c: Sock) = @py { return None }\n"
        "extern emission fn ping() = @py { return None }\n"
        "service S { emission fn go() }\n"
        "component P provides s: S {\n"
        "  let sock = effect open_sock() undo close_sock(sock)\n"
        "  provide s { fn go() { emit ping() compensate note(sock) } }\n"
        "}\n")
    assert "compensate" in msg and "owned" in msg


def test_b1_clause5_borrow_in_witnessed_args_refused():
    msg = _refuse(
        _BASE + "type Wit = { ok: Bool }\n"
        "extern pure fn unmark(w: Wit) = @py { return None }\n"
        "extern witnessed[fs] fn mark(c: Sock) -> Result[Wit, Str] undo unmark(result)"
        ' = @py { return {"ok": True} }\n'
        "service S { fn ping() -> Int }\n"
        "component P provides s: S {\n"
        "  let sock = effect open_sock() undo close_sock(sock)\n"
        "  let w = effect mark(sock)\n"
        "  provide s { fn ping() { return 1 } }\n"
        "}\n")
    assert "witnessed" in msg


def test_b1_clause6_spawn_config_refused():
    msg = _refuse(
        _BASE + "service S { fn ping() -> Int }\n"
        "component Child provides s: S {\n"
        "  config { c: Sock }\n"
        "  provide s { fn ping() { return 1 } }\n"
        "}\n"
        "component Parent provides s2: S {\n"
        "  let sock = effect open_sock() undo close_sock(sock)\n"
        "  let kid = effect spawn Child with { c: sock } undo kid.dispose()\n"
        "  provide s2 { fn ping() { return 1 } }\n"
        "}\n")
    assert "spawn" in msg and "config" in msg


def test_b1_clause7_handoff_type_refused():
    msg = _refuse(
        _BASE + "type Sess = { conn: Sock }\n"
        "service S { fn ping() -> Int }\n"
        "component P provides s: S {\n"
        "  handoff s: Sess\n"
        "  provide s { fn ping() { return 1 } }\n"
        "}\n")
    assert "handoff" in msg


# ---------------------------------------------------------------------------
# additivity — the legitimate patterns still admit
# ---------------------------------------------------------------------------

def test_owner_holds_and_lends_admits():
    _admit(
        _BASE + "service S { fn get() -> Sock }\n"
        "component P provides s: S {\n"
        "  let sock = effect open_sock() undo close_sock(sock)\n"
        "  provide s { fn get() { return sock } }\n"
        "}\n")


def test_pass_a_borrow_down_a_call_chain_admits():
    _admit(
        _BASE + "fn touchit(c: Sock) -> Int { return 1 }\n"
        "service S { fn touch(c: Sock) -> Int }\n"
        "component P provides s: S {\n"
        "  provide s { fn touch(c) { return touchit(c) } }\n"
        "}\n")


# ---------------------------------------------------------------------------
# F9 — method-scope early release (the decision, and what enforces it)
# ---------------------------------------------------------------------------
#
# The design left F9 open behind a corpus count: if any program acquires at
# method scope, either an explicit-release surface lands or the form is refused.
# The count is ZERO general method-scope acquires — the language already refuses
# them — so the surface that matters is the one method-scope acquisition that
# yields a live handle, `spawn`, and it already releases early: the instance is
# a child fiber with its own nested teardown scope and `w.dispose()` unloads it
# now, with no new grammar. These tests pin the decision's three legs: the
# general form stays refused, the early release stays admitted, and the
# request-scoped handle may not outlive the request that spawned it.

_WORKER = (
    "service T { fn run() -> Int }\n"
    "component Worker provides task: T {\n"
    "  provide task { fn run() = 7 }\n"
    "}\n"
)


def _sup(body: str, *, activation: str = "", returns: str = "Int") -> str:
    return (_WORKER + f"service S {{ fn go() -> {returns} }}\n"
            "component Sup provides s: S {\n" + activation
            + "  provide s {\n    fn go() {\n" + body + "    }\n  }\n}\n")


_SPAWN = "      let w = effect spawn Worker with { } undo w.dispose()\n"


def test_f9_general_method_scope_acquire_stays_refused():
    """The F9 decision's first leg: a general acquire at method scope is
    refused, with a diagnostic naming the restructure. This is what makes the
    corpus count zero, and what keeps `spawn` the only handle-yielding
    method-scope acquisition F9 has to reason about."""
    msg = _refuse(_BASE + _sup(
        "      let h = effect open_sock() undo close_sock(h)\n"
        "      return 1\n"))
    assert "only `spawn` may be acquired inside a provide-method body" in msg
    assert "activation body" in msg


def test_f9_method_scope_spawn_admits():
    """The baseline: a request-scoped instance used inside its own method."""
    _admit(_sup(_SPAWN + "      return w.task.run()\n"))


def test_f9_explicit_early_release_admits():
    """The early-release surface itself, spelled with the grammar revl already
    had: `effect w.dispose() undo w.dispose()` unloads the child fiber NOW.
    Nothing new is needed for it, which is the F9 decision."""
    _admit(_sup(_SPAWN
                + "      let r = w.task.run()\n"
                + "      effect w.dispose() undo w.dispose()\n"
                + "      return r\n"))


def test_f9_instance_handle_returned_refused():
    msg = _refuse(_sup(_SPAWN + "      return w\n", returns="Instance[Worker]"))
    assert "request-scoped instance handle `w`" in msg
    assert "308" in msg and "F9" in msg


def test_f9_instance_handle_stored_into_activation_state_refused():
    msg = _refuse(_sup(
        _SPAWN + '      effect cells.insert("k", w) undo cells.remove("k")\n'
                 "      return 1\n",
        activation="  let cells = effect Map.new() undo cells.drop()\n"))
    assert "request-scoped instance handle `w`" in msg


def test_f9_instance_handle_closure_capture_refused():
    """An arrow's type erases what it closes over, so a closure reading the
    instance carries it across any signature — B1 clause 2's reasoning, applied
    to the handle B1 cannot see."""
    msg = _refuse(_sup(_SPAWN + "      let f = () => w.task.run()\n"
                              "      return f()\n"))
    assert "request-scoped instance handle `w`" in msg


def test_f9_instance_handle_in_carrier_refused():
    msg = _refuse(_sup(_SPAWN + "      let rec = { inst: w }\n"
                              "      return 1\n"))
    assert "request-scoped instance handle `w`" in msg


def test_f9_instance_handle_aliased_to_another_local_refused():
    """A plain alias is the cheapest escape of all: `alias` outlives nothing on
    its own, but the walk is a whitelist, so anything that is not one of the two
    read-only slots is refused rather than reasoned about."""
    msg = _refuse(_sup(_SPAWN + "      let alias = w\n"
                              "      return alias.task.run()\n"))
    assert "request-scoped instance handle `w`" in msg


def test_f9_instance_handle_in_a_list_literal_refused():
    msg = _refuse(_sup(_SPAWN + "      let xs = [w]\n      return 1\n"))
    assert "request-scoped instance handle `w`" in msg


def test_f9_activation_scope_spawn_owner_pool_admits():
    """The owner carve-out is untouched: a spawn at ACTIVATION scope is the
    component's own instance for its whole life, stored and lent per call."""
    _admit(_WORKER + "service S { fn go() -> Int }\n"
           "component Sup provides s: S {\n"
           "  let w = effect spawn Worker with { } undo w.dispose()\n"
           "  provide s { fn go() = w.task.run() }\n"
           "}\n")


def test_f9_refusal_is_navigable_with_two_author_moves():
    """item 274: the refusal names fixes the author can enact where they stand
    — hand out a value, or hoist the spawn to activation scope."""
    with pytest.raises(RevlError) as ei:
        compile_source(_sup(_SPAWN + "      return w\n",
                            returns="Instance[Worker]"), "t.rvl")
    nav = ei.value.navigate
    assert nav["family"] == "ownership" and nav["refused"]["kind"] == "f9"
    actions = " ".join(alt["action"] for alt in nav["alternatives"])
    assert "VALUE" in actions and "ACTIVATION scope" in actions


# ---------------------------------------------------------------------------
# F10 — the retaining-extern audit (report-only)
# ---------------------------------------------------------------------------

from revl.audit_diff import audit_report  # noqa: E402
from revl.resources import retention_surface  # noqa: E402


def _surface(src: str) -> list:
    ir = compile_source(src, "t.rvl")
    return retention_surface(ir.get("externs"), ir.get("types"),
                             ir.get("services"))


_USE = "extern pure fn use_sock(h: Sock) -> Int = @py { return 1 }\n"


def test_f10_lists_a_resource_typed_parameter_of_a_non_inverse_extern():
    rows = _surface(_BASE + _USE)
    assert rows == [{"kind": "extern", "callee": "use_sock", "class": "pure",
                     "param": "h", "index": 0, "type": "Sock",
                     "resource": "Sock"}]


def test_f10_excludes_the_declared_inverse():
    """`close_sock` takes a `Sock` too, but teardown calling it with the handle
    exactly once is the contract working, not a retention hazard."""
    assert [r["callee"] for r in _surface(_BASE + _USE)] == ["use_sock"]


def test_f10_follows_the_taint_closure_through_a_carrier_type():
    rows = _surface(_BASE + "type Session = { conn: Sock, tag: Str }\n"
                    "extern emission[net] fn register(s: Session) -> Int"
                    " = @py { return 1 }\n")
    assert rows == [{"kind": "extern", "callee": "register", "class": "emission",
                     "param": "s", "index": 0, "type": "Session",
                     "resource": "Session"}]


def test_f10_lists_a_service_method_parameter():
    """A service implemented across a bridge runs host-side, so its declared
    parameters are the same frontier an extern's are."""
    rows = _surface(_BASE + "service Keep { fn hold(h: Sock) -> Int }\n")
    assert rows == [{"kind": "service-method", "callee": "Keep.hold",
                     "class": "plain", "param": "h", "index": 0,
                     "type": "Sock", "resource": "Sock"}]


def test_f10_absent_from_the_audit_of_a_handle_free_composition():
    """Conditional presence: a composition that declares no resource type has a
    byte-identical audit document."""
    ir = compile_source(
        "service S { fn go() -> Int }\n"
        "component C provides s: S { provide s { fn go() = 1 } }\n", "t.rvl")
    assert "retention" not in audit_report(ir)


def test_f10_present_in_the_audit_when_a_handle_reaches_a_non_inverse():
    ir = compile_source(
        _BASE + _USE + "service S { fn go() -> Int }\n"
        "component C provides s: S {\n"
        "  let h = effect open_sock() undo close_sock(h)\n"
        "  provide s { fn go() = use_sock(h) }\n"
        "}\n", "t.rvl")
    assert [r["callee"] for r in audit_report(ir)["retention"]] == ["use_sock"]


def test_f10_is_report_only_and_refuses_nothing():
    """The whole point: a retaining extern is NOT refusable — the declaration
    does not say "retains" — so every row above is a listed hazard, never a
    rejected program."""
    _admit(_BASE + _USE + "service S { fn go() -> Int }\n"
           "component C provides s: S {\n"
           "  let h = effect open_sock() undo close_sock(h)\n"
           "  provide s { fn go() = use_sock(h) }\n"
           "}\n")


# ---------------------------------------------------------------------------
# Slice 3a (issue #96) — `shared` / `transfer` ownership modes are RESERVED
# ---------------------------------------------------------------------------
#
# The roadmap deferred SHARED + TRANSFER to a later tier "[contextual keywords
# reserved]", but the markers were never actually reserved: `effect shared …` /
# `effect transfer …` fell through to a generic "expected a statement" parse
# error. Slice 3a reserves both markers at the acquire-binding position with a
# clear refusal. The modes themselves are NOT implemented here.


@pytest.mark.parametrize("mode", ["shared", "transfer"])
def test_slice3a_ownership_mode_marker_is_reserved(mode):
    msg = _refuse(
        _BASE
        + f"component C {{ let s = effect {mode} open_sock() undo close_sock(s) }}\n"
    )
    assert f"`{mode}` ownership mode is reserved for a later tier" in msg
    assert "not implemented in v1" in msg
    # the specific later-tier homes are named, not a generic parse error
    assert "item 294 leases" in msg
    assert "realm transfer" in msg
    assert "expected a statement" not in msg


@pytest.mark.parametrize("mode", ["shared", "transfer"])
def test_slice3a_marker_is_contextual_binding_name_still_admits(mode):
    """`shared` / `transfer` are CONTEXTUAL keywords: only the marker position
    (`effect <mode> <ident…>`) is reserved. A plain acquire with no marker still
    admits, so the reservation adds no new grammar."""
    _admit(
        _BASE
        + "component C { let s = effect open_sock() undo close_sock(s) }\n"
    )
