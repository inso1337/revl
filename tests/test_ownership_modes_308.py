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
