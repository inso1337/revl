"""An extern-declared `compensate` registers at a provide-method site (#1592).

An emission extern can declare its own reversal (item 254):

    extern emission fn put_row(body: Str) -> Int compensate restore_row() = ...

Every `emit put_row(..)` registers `restore_row()` as a compensation: discharged
on a clean commit, run in Phase 2 of an abort. On py the activation body did
that, and the timer site since #1590, but a provide-method `emit put_row(m)`
rendered only the forward call: a call then an abort left `put m` and never
ran `restore`, while the site-spelled control (`emit put_row(m) compensate
plain_restore()`) restored. The method site now registers the declared
compensation through `Frame.compensation_method`, unless the site spells its
own, which replaces it (issue #1902); a value-position `return emit put_row(m)`
(#1603) registers it too. Issue #2688 moved the direct host-extern registration
into the seam: the fire carries `compensate=lambda: <registration>`, so the
runtime runs it when the host body returns and before a `time` refusal is
decided, rather than on a next line the refusal would skip.
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="registration and abort are proven against a live cordis-py "
           "composition; install it with `sh backends/python/setup.sh`")


def _log(line: str, ret: str) -> str:
    return ("    import os\n"
            "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


_SOURCE = (
    "extern emission fn put_row(body: Str) -> Int compensate restore_row() = @py {\n"
    + _log("'put ' + body", "1") + "}\n"
    "extern emission fn restore_row() -> Int = @py {\n"
    + _log("'restore'", "0") + "}\n"
    "extern emission fn plain_restore() -> Int = @py {\n"
    + _log("'site-restore'", "0") + "}\n"
    "service Rows {\n"
    "  emission fn put(m: Str)\n"
    "  emission fn both(m: Str)\n"
    "  emission fn give(m: Str) -> Int\n"
    "  emission fn keep(m: Str) -> Int\n"
    "}\n"
    "component Store provides rows: Rows {\n"
    "  provide rows {\n"
    "    fn put(m) { emit put_row(m) }\n"
    "    fn both(m) { emit put_row(m) compensate plain_restore() }\n"
    "    fn give(m) { return emit put_row(m) }\n"
    "    fn keep(m) {\n"
    "      let n = emit put_row(m)\n"
    "      return n\n"
    "    }\n"
    "  }\n"
    "}\n"
)

_IR = compile_source(_SOURCE, "method_declared_compensate.rvl")


def _emitted() -> str:
    from revl._paths import python_backend_emitter  # noqa: PLC0415
    return python_backend_emitter().emit(copy.deepcopy(_IR))


def _method(src: str, name: str) -> list:
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip().startswith(f"def {name}(self"))
    body = []
    for ln in lines[start + 1:]:
        if not ln.strip():
            break
        body.append(ln.strip())
    return body


FIRE = "_revl_extern_emit(_revl_ctx, 'put_row', put_row, (m,))"
# the named call rides beside the thunk so a fresh process can re-issue it
# (issue #1369's WAL descriptor); the crossing is named because the
# registration now shares the fire's line (issue #2688), so the recorder
# cannot pair them by source adjacency
DECLARED = ("_revl_frame.compensation_method(lambda: restore_row(), "
            "call={'receiver': None, 'method': 'restore_row', 'args': []}, "
            "crossing='put_row')")


def _seam_fire(registration: str) -> str:
    """The fire with the registration handed to the seam (issue #2688). The
    factory registers and returns None: the entry is already on the frame, so
    the seam must not enqueue a second copy for Phase 2."""
    return FIRE[:-1] + f", compensate=lambda: ({registration}, None)[1])"


def test_an_emit_statement_registers_the_declared_compensation():
    assert _method(_emitted(), "put") == [_seam_fire(DECLARED)]


def test_a_site_spelled_compensation_replaces_the_declared_one():
    """Issue #1902: one compensation per crossing. The site spells its own, so
    the extern's declared one is not registered beside it."""
    site = ("_revl_frame.compensation_method(lambda: plain_restore(), "
            "call={'receiver': None, 'method': 'plain_restore', 'args': []}, "
            "crossing='put_row')")
    assert _method(_emitted(), "both") == [_seam_fire(site)]


def test_a_value_emission_registers_the_declared_compensation_before_returning():
    src = _emitted()
    declared_fire = _seam_fire(DECLARED)
    assert _method(src, "give") == [f"return {declared_fire}"]
    assert _method(src, "keep") == [f"n = {declared_fire}", "return n"]


# ---------------------------------------------------------------------------
# live: call, then abort or commit


@pytest.fixture
def order(tmp_path, monkeypatch):
    log = tmp_path / "order.log"
    monkeypatch.setenv("REVL_ORDER_LOG", str(log))
    return log


def _lines(log: Path) -> list:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _session():
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(copy.deepcopy(_IR))
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return session, driver.runtime._frame_for_ctx(fiber.ctx)


@needs_cordis
@pytest.mark.parametrize("method, value", [("put", None), ("give", 1), ("keep", 1)])
def test_a_call_then_an_abort_runs_the_declared_compensation(order, method, value):
    session, frame = _session()

    assert session.call("rows", method, ["m"])["result"] == value
    frame.abort()
    session.unload()

    assert _lines(order) == ["put m", "restore"]
    assert frame.compensation_residue == []


@needs_cordis
def test_a_call_then_an_abort_runs_only_the_site_spelled_one(order):
    session, frame = _session()

    session.call("rows", "both", ["m"])
    frame.abort()
    session.unload()

    # issue #1902: the site-spelled compensation replaces the declared one
    assert _lines(order) == ["put m", "site-restore"]


@needs_cordis
def test_a_clean_unload_discharges_the_declared_compensation(order):
    session, frame = _session()

    session.call("rows", "put", ["m"])
    session.unload()

    assert _lines(order) == ["put m"]
