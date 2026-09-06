"""The self-host O1/B1 ownership-parity gap, named executably (roadmap item 308).

The design (docs/design/308-effect-ownership-modes.md, "Self-host note" and
open question 3) left ONE thing to decide at slice 2: does the O1/B1 resource
ownership flow walk have to be ported into `selfhost/checker.rvl`, or can the
port be deferred with the gap named so it is not "discovered by a red oracle"?

This module records the DECISION and pins the EVIDENCE.

Decision: DEFERRED, gap named here. The self-host checker's second half already
covers component/provide bodies for G4 emission-declaration and call-site
typing, message-for-message with the reference (see test_selfhost_checker.py's
slice two). It does NOT implement the reference's O1 (no hand-call of a declared
inverse) or B1 (a borrow does not escape its scope) ownership flow walk, which
lives in `src/revl/lower.py` over the resource-taint base. Porting it is a
separate, larger slice, sequenced whenever the ownership dialect is self-hosted.

Why deferral is safe TODAY, and why this test is the guard that keeps it safe:

* On a POSITIVE ownership program (the owner holds a handle and lends it, closes
  it in its own teardown), the reference admits and the self-host admits: they
  AGREE, so such a program may sit in the self-host corpus with no red oracle.
  `test_positive_ownership_program_agrees` pins that.

* On an O1/B1 REJECTION the two DIVERGE: the reference refuses with the item-308
  message, the self-host accepts (its checker has no ownership flow walk). An
  ownership REJECTION fixture added to the self-host corpus (REJECTED_PROGRAMS in
  test_selfhost_checker.py, or a rejection fixture swept by the differential
  survey) would therefore red the oracle for a KNOWN, DESIGNED gap — the exact
  "discovered by a red oracle" outcome the design forbids. The two divergence
  pins below own that divergence explicitly instead.

When the O1/B1 flow walk is ported to the self-host checker, the two
`_gap` assertions here will START FAILING (the self-host will begin to refuse).
That is the signal to: (1) flip these pins to assert agreement, (2) move the
O1/B1 rejection fixtures into the shared corpus, and (3) strike the deferral
from the design doc's self-host note and open question 3. Until then, this file
is the whole record that the gap is intended, not a latent bug.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _exec_emitted() -> dict:
    """Emit and load the self-host checker exactly as test_selfhost_checker
    does, exposing its slice-two entry point `check_service_src`."""
    ir = compile_files([str(ROOT / "selfhost" / "checker.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_ownership_gap", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "selfhost_ownership_gap.py", "exec"),
             namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def check_src():
    """"" if the self-host checker accepts a program, else its refusal
    message (spelled as the reference spells it)."""
    return _exec_emitted()["check_service_src"]


def _ref_check(src: str) -> str:
    """"" if the reference compiler accepts, else its diagnostic message."""
    try:
        compile_source(src, "gap.rvl")
        return ""
    except RevlError as e:
        return e.message


# A nominal opaque handle `Sock`, its declared inverse, and the acquire — the
# same base the reference ownership tests use (test_ownership_modes_308.py).
_BASE = (
    "type Sock = { fd: Int }\n"
    "extern pure fn close_sock(h: Sock) = @py { return None }\n"
    "extern acquire fn open_sock() -> Sock undo close_sock(result)"
    ' = @py { return {"fd": 1} }\n'
)

# O1: a borrower (a provide method receiving a `Sock`) hand-calls the declared
# inverse. The reference refuses (double-close); the self-host has no O1 walk.
_O1_BORROWER_CLOSE = (
    _BASE + "service S { fn shut(c: Sock) -> Int }\n"
    "component P provides s: S {\n"
    "  provide s { fn shut(c) { let x = close_sock(c)  return 1 } }\n"
    "}\n"
)

# B1: a provide method returns a borrowed handle across its signature. The
# reference refuses (a borrow may not outlive its owner); the self-host has no
# B1 walk.
_B1_NON_OWNER_RETURN = (
    _BASE + "service S { fn take(c: Sock) -> Sock }\n"
    "component P provides s: S {\n"
    "  provide s { fn take(c) { return c } }\n"
    "}\n"
)

# A legitimate owner-holds-and-lends program: acquire at activation scope, close
# in the component's own teardown, provide a method that touches no handle. The
# reference ADMITS this; so does the self-host. Their agreement is what makes a
# POSITIVE ownership program safe to carry in the self-host corpus.
_OWNER_HOLDS = (
    _BASE + "service S { fn ping() -> Int }\n"
    "component P provides s: S {\n"
    "  let sock = effect open_sock() undo close_sock(sock)\n"
    "  provide s { fn ping() { return 1 } }\n"
    "}\n"
)


def test_o1_rejection_is_a_named_selfhost_gap(check_src):
    """Reference refuses O1; the self-host checker does not yet. Pinned so the
    divergence is owned, not discovered by a red oracle."""
    ref = _ref_check(_O1_BORROWER_CLOSE)
    assert "O1" in ref and "item 308" in ref, (
        f"reference no longer refuses this O1 program as expected: {ref!r}")
    got = check_src(_O1_BORROWER_CLOSE)
    assert got == "", (
        "the self-host checker now refuses an O1 program: the ownership flow "
        "walk appears to have been ported. Flip this pin to assert agreement, "
        "move the O1 rejection fixtures into the shared self-host corpus, and "
        "strike the deferral from docs/design/308-effect-ownership-modes.md "
        f"(self-host note / open question 3). selfhost returned {got!r}")


def test_b1_rejection_is_a_named_selfhost_gap(check_src):
    """Reference refuses B1 (a borrow does not escape); the self-host checker
    does not yet. Same deferral, same guard."""
    ref = _ref_check(_B1_NON_OWNER_RETURN)
    assert "B1" in ref and "item 308" in ref, (
        f"reference no longer refuses this B1 program as expected: {ref!r}")
    got = check_src(_B1_NON_OWNER_RETURN)
    assert got == "", (
        "the self-host checker now refuses a B1 program: the ownership flow "
        "walk appears to have been ported. Flip this pin to assert agreement, "
        "move the B1 rejection fixtures into the shared self-host corpus, and "
        "strike the deferral from docs/design/308-effect-ownership-modes.md "
        f"(self-host note / open question 3). selfhost returned {got!r}")


def test_positive_ownership_program_agrees(check_src):
    """The owner-holds-and-lends program admits on BOTH sides. This is why the
    deferral is safe: a POSITIVE ownership program carries no divergence, so it
    may live in the self-host corpus today; only the O1/B1 REJECTIONS above must
    stay out until the flow walk is ported."""
    assert _ref_check(_OWNER_HOLDS) == ""
    assert check_src(_OWNER_HOLDS) == ""
