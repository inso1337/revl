"""An extern's DECLARED `compensate` is registered wherever a provide method
crosses it, on the python tier (item 254's shape; found under issue #1369).

WHAT WAS WRONG. An extern may declare its own compensation:

    extern emission fn put(k: Str) -> Int compensate undo_put() = @py { ... }

The activation body registered it at an `emit put(...)` statement. A provide
method registered it NOWHERE: the py emitter handled only a site-spelled
`emit put(...) compensate g()` in a method body. So after a tool call crossed
`put`, an abort ran no compensation for it, and the program had declared the
crossing undoable. Measured on the base with host bodies that log what they
run, one program per position, abort after the call:

    position in a provide method     compensation run on abort
    statement   emit put(..)          none
    let         let a = emit put(..)  none
    return      return emit put(..)   none
    argument    double(emit put(..))  none
    if arm      if (c) { emit put() } none
    nested      emit put(..) + 1      none
    site-spelled `compensate g()`     g           (the control)

In an activation body only the statement position is admissible (`let`, an
argument and an `if` arm are refused by the frontend), and it already
registered.

WHAT RUNS NOW. Every extern that declares `compensate` is decorated with
`declared_crossing`, and every provide method that can reach one runs in a
call scope. Inside the scope the decorator registers the declared compensation
through `Frame.compensation_method`, the call a site-spelled compensation
makes, after the host body returns. A plain call scope does not settle
anything on failure: the entries wait for the activation's verdict, as a
site-spelled compensation's always have.

Every test here fails on the base, and every one drives a live cordis-py
composition. Without the pinned `cordis` fork they SKIP, and a skip is not a
pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the abort runs against a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`",
)


def _host(line: str, ret: str = "1", fail: bool = False) -> str:
    body = ("@py {\n"
            "    import os\n"
            "    with open(os.environ['REVL_C_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line!r} + chr(10))\n")
    if fail:
        body += f"    raise RuntimeError({line!r} + ' failed')\n"
    return body + f"    return {ret}\n}}"


def _declared(tag: str, fail: bool = False) -> str:
    return (f"extern pure fn undo_{tag}() = {_host('comp:' + tag, 'None')}\n"
            f"extern emission fn put_{tag}(k: Str) -> Int compensate undo_{tag}()\n"
            f"  = {_host('put:' + tag, fail=fail)}\n")


PRELUDE = (
    "fn double(n: Int) -> Int { return n * 2 }\n"
    f"extern emission fn plain(k: Str) -> Int = {_host('put:site')}\n"
    f"extern pure fn undo_site() = {_host('comp:site', 'None')}\n"
)

#: One provide-method body per position. Each crosses `put_<tag>` once.
POSITIONS = {
    "statement": 'emit put_statement("k")\n      return 0',
    "let": 'let a = emit put_let("k")\n      return a',
    "return": 'return emit put_return("k")',
    "argument": 'let s = double(emit put_argument("k"))\n      return s',
    "ifarm": 'if (true) { emit put_ifarm("k") }\n      return 0',
    "nested": 'let b = emit put_nested("k") + 1\n      return b',
}


def _method_program(decls: str, body: str, activation: str = "") -> str:
    return (PRELUDE + decls
            + "service Ops { emission fn run() -> Int }\n"
            + "component Agent provides ops: Ops {\n"
            + (f"  {activation}\n" if activation else "")
            + "  provide ops {\n    fn run() {\n      " + body
            + "\n    }\n  }\n}\n")


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "c.log"
    monkeypatch.setenv("REVL_C_LOG", str(path))

    def lines() -> list[str]:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return lines


def _load(source: str):
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(source, "declared_compensate.rvl"))
    ((_name, fiber),) = session._driver.fibers.items()
    frame = session._driver.runtime._frame_for_ctx(fiber.ctx)
    assert frame is not None
    return session, frame


def _compensations(lines: list[str]) -> list[str]:
    return [line.split(":", 1)[1] for line in lines if line.startswith("comp:")]


@needs_cordis
@pytest.mark.parametrize("position", sorted(POSITIONS))
def test_an_abort_runs_the_declared_compensation_in_every_position(log, position):
    session, frame = _load(_method_program(_declared(position), POSITIONS[position]))
    session.call("ops", "run", [])
    assert _compensations(log()) == []          # owed, not run: nothing failed
    frame.abort()
    session.unload()
    assert _compensations(log()) == [position]


@needs_cordis
@pytest.mark.parametrize("position", sorted(POSITIONS))
def test_a_clean_commit_discharges_the_declared_compensation(log, position):
    """The entry exists and the commit discharges it: the emission was the
    deliverable. On the base there was no entry to discharge."""
    session, frame = _load(_method_program(_declared(position), POSITIONS[position]))
    session.call("ops", "run", [])
    assert [entry.method for entry in frame._compensations] == [f"undo_{position}"]
    session.unload()
    assert _compensations(log()) == []
    assert all(entry.discharged for entry in frame._compensations)


EVERY_POSITION = (
    'emit put_statement("k")\n'
    '      let a = emit put_let("k")\n'
    '      let s = double(emit put_argument("k"))\n'
    '      if (true) { emit put_ifarm("k") }\n'
    '      emit plain("k") compensate undo_site()\n'
    '      let b = emit put_nested("k") + 1\n'
    '      return emit put_return("k")'
)


@needs_cordis
def test_the_abort_runs_every_compensation_newest_first(log):
    """Every position in one method, a site-spelled compensation among them,
    and a declared crossing in the activation body. The abort runs the call's
    compensations newest first, the site-spelled one in its place, and then
    the activation's: one LIFO order, whichever way each was registered."""
    tags = ["statement", "let", "argument", "ifarm", "nested", "return", "act"]
    decls = "".join(_declared(tag) for tag in tags)
    session, frame = _load(_method_program(
        decls, EVERY_POSITION, activation='emit put_act("k")'))
    session.call("ops", "run", [])
    frame.abort()
    session.unload()
    assert _compensations(log()) == [
        "return", "nested", "site", "ifarm", "argument", "let", "statement",
        "act"]


@needs_cordis
def test_a_crossing_that_raises_registers_nothing_and_a_failed_call_waits(log):
    """Item 247's rule for a non-computer-use crossing: the compensation is
    owed for an emission that crossed, so a raising `put_boom` registers
    nothing. And a plain call scope does not settle on failure: the earlier
    crossing's compensation waits for the activation's verdict and runs on the
    abort, once."""
    decls = _declared("first") + _declared("boom", fail=True)
    session, frame = _load(_method_program(
        decls, 'emit put_first("k")\n      return emit put_boom("k")'))
    with pytest.raises(Exception, match="put:boom failed"):
        session.call("ops", "run", [])
    assert _compensations(log()) == []
    assert [entry.method for entry in frame._compensations] == ["undo_first"]
    frame.abort()
    session.unload()
    assert _compensations(log()) == ["first"]


@needs_cordis
def test_the_call_scope_is_reset_after_every_call(log):
    """The scope is a context variable. A leak would register the NEXT call's
    crossings, or an unrelated activation's, onto a finished call."""
    session, frame = _load(_method_program(_declared("let"), POSITIONS["let"]))
    runtime = session._driver.runtime   # the module the emitted code imports
    session.call("ops", "run", [])
    assert runtime._CALL_SCOPE.get() is None
    session.unload()


def test_a_non_ui_extern_registers_at_its_site_with_its_named_call():
    """One registrar per crossing (the #1591/#1616 merge): a compensate-
    declaring extern that is not a computer-use crossing is not decorated and
    its method runs in no call scope. It registers at the site, with the named
    call a fresh process re-issues."""
    from _backend_import import backend_emitter
    code = backend_emitter("python").emit(compile_source(
        _method_program(_declared("let"), POSITIONS["let"]), "d.rvl"))
    assert "call_scope(" not in code
    assert "_revl_declared_crossing" not in code
    assert ("_revl_frame.compensation_method(lambda: undo_let(), "
            "call={'receiver': None, 'method': 'undo_let', 'args': []})") in code


# ------------------------------------ the shape the examples/ax lane reported


AX = (
    "extern pure fn unfile() = " + _host("comp:unfile", "None") + "\n"
    "extern emission fn file_host(t: Str) -> Str compensate unfile()\n"
    "  = " + _host("put:file", "'id-1'") + "\n"
    "extern emission fn withdraw_host(t: Str) -> Unit = " + _host("put:withdraw", "None") + "\n"
    "service Tickets {\n  emission fn file(t: Str) -> Str\n"
    "  emission fn withdraw(t: Str)\n}\n"
    "service Ops { emission fn run(t: Str) -> Str }\n"
    "component Desk provides tickets: Tickets {\n  provide tickets {\n"
    "    fn file(t) { return emit file_host(t) }\n"
    "    fn withdraw(t) { emit withdraw_host(t) }\n  }\n}\n"
    "component Agent requires tickets: Tickets provides ops: Ops {\n"
    "  provide ops {\n    fn run(t) {\n"
    "      emit tickets.file(t) compensate tickets.withdraw(t)\n"
    "      return \"ok\"\n    }\n  }\n}\n"
)


@needs_cordis
def test_a_session_abort_runs_the_provider_side_declared_compensation(log):
    """The examples/ax report: after a completed call, `Session.abort` ran
    neither compensation. Measured here in-process, the site-spelled service
    compensation (`tickets.withdraw`) DID run on the base; the one the provider's
    own extern declares (`file_host ... compensate unfile()`) did not, because
    a provide method never registered it. Both run now, newest first: `unfile`
    registered inside the `file` call, before `withdraw` was registered after
    it returned, so `withdraw` runs first."""
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(AX, "ax.rvl"))
    session.call("ops", "run", ["T1"])
    verdict = session.abort()
    assert _compensations(log()) == ["unfile"]
    assert "put:withdraw" in log()
    assert log() == ["put:file", "put:withdraw", "comp:unfile"]
    assert verdict["noResidue"] is True and verdict["compensationResidue"] == []


@needs_cordis
def test_an_abort_verdict_is_not_clean_while_a_compensation_did_not_land(log):
    """`noResidue` was the in-process R4 check alone, so an abort whose
    compensation RAISED reported `noResidue: true` beside a non-empty
    `compensationResidue`: a clean verdict for an undo that did not happen.
    The owed compensation is now a fifth check, and the verdict is not clean."""
    from revl.mcp.session import Session
    source = _method_program(
        "extern pure fn undo_let() = @py { raise RuntimeError('offset boom') }\n"
        "extern emission fn put_let(k: Str) -> Int compensate undo_let()\n"
        "  = " + _host("put:let") + "\n",
        POSITIONS["let"])
    session = Session()
    session.load(compile_source(source, "boom.rvl"))
    session.call("ops", "run", [])
    verdict = session.abort()
    [residue] = verdict["compensationResidue"]
    assert residue["method"] == "undo_let" and residue["outcome"] == "failed"
    assert verdict["checks"]["compensations"] is False
    assert verdict["noResidue"] is False
    # the four in-process checks are still reported, and still pass
    assert all(verdict["checks"][k] for k in
               ("registry", "provisions", "effects", "listeners"))
