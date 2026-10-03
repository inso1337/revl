"""Extern-declared compensations on the java tier (issue #1511).

An extern may declare its own compensation, the item-254 shape:

    extern emission fn put(k: Str) -> Int compensate undo_put() = @java { ... }

Every crossing of it owes that compensation, whatever position the call is
written in. The java emitter registered it nowhere before #1511, so an abort
after the crossing ran nothing, in any position, in a provide method or in the
activation body. The py tier's fix is tests/test_declared_compensate_positions.py;
this is the java mirror.

The fix is one path: `_expr`'s `fn` arm renders every call, so a crossing in any
position becomes `RevlDeclared.crossed(fx, frame, .., put(..), () -> undo())`.
Java evaluates `put(..)` first, and the helper tracks the declared compensation
through `RevlFrame.compensation`, the entry a site-spelled compensation makes.

Every runtime test here compiles the emitted unit and a small harness against
the in-repo cordis4j stubs, runs it on a JVM, and reads the host trace the
`@java` bodies print. The abort tests fail on the base: its abort runs only the
activation's site-spelled compensation. Without a JDK they skip, and a skip is
not a pass: CI's `backend-java` job provisions one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (ROOT / "src", HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import javac_gate  # noqa: E402
from emit import emit  # noqa: E402

from revl.compiler import compile_source  # noqa: E402

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)


def _host(line: str, ret: str | None = None, fail: bool = False) -> str:
    body = f'    System.out.println("TRACE {line}");\n'
    if fail:
        body += f'    if (true) {{ throw new RuntimeException("{line} failed"); }}\n'
    body += "    return;\n" if ret is None else f"    return {ret};\n"
    return "@java {\n" + body + "}"


def _declared(tag: str, fail: bool = False, unit: bool = False) -> str:
    ret = "Unit" if unit else "Int"
    return (f"extern pure fn undo_{tag}() -> Unit = {_host('comp:' + tag)}\n"
            f"extern emission fn put_{tag}(k: Str) -> {ret} compensate undo_{tag}()\n"
            f"  = {_host('put:' + tag, None if unit else '1L', fail=fail)}\n")


PRELUDE = (
    "fn double(n: Int) -> Int { return n * 2 }\n"
    f"extern emission fn plain(k: Str) -> Int = {_host('put:site', '1L')}\n"
    f"extern pure fn undo_site() -> Unit = {_host('comp:site')}\n"
)

#: One provide-method body per position. Each crosses `put_<tag>` once.
POSITIONS = {
    "statement": 'emit put_statement("k")\n      return 0',
    "let": 'let a = emit put_let("k")\n      return a',
    "return": 'return emit put_return("k")',
    "argument": 'let s = double(emit put_argument("k"))\n      return s',
    "ifarm": 'if (true) { emit put_ifarm("k") }\n      return 0',
    "nested": 'let b = emit put_nested("k") + 1\n      return b',
    "unit": 'emit put_unit("k")\n      return 0',
}

#: A site-spelled compensation in the activation body. It gives the base the
#: same teardown frame and abort seam, so the per-position tests fail on the
#: base by what the abort RUNS (`["site"]`), not by a missing class.
SITE = 'emit plain("k") compensate undo_site()'


def _decls(tags) -> str:
    return "".join(_declared(tag, unit=tag == "unit") for tag in tags)


def _program(decls: str, body: str, activation: str = "") -> str:
    return (PRELUDE + decls
            + "service Ops { emission fn run() -> Int }\n"
            + "component Agent provides ops: Ops {\n"
            + (f"  {activation}\n" if activation else "")
            + "  provide ops {\n    fn run() {\n      " + body
            + "\n    }\n  }\n}\n")


_HARNESS = """\
import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;

public final class RunDeclared {
    public static void main(String[] args) {
        Context root = new Context();
        Disposable activation = new revl.Components.AgentPlugin().apply(root);
        revl.Components.Ops ops = root.get(revl.Components.Ops.class);
        try {
            ops.run();
        } catch (RuntimeException failure) {
            System.out.println("THREW " + failure.getMessage());
        }
        System.out.println("CALLED");
        if (args[0].equals("abort")) {
            ((revl.Components.RevlActivation) activation).abort();
        }
        activation.dispose();
        System.out.println("DISPOSED");
    }
}
"""


def _run(tmp_path: Path, source: str, mode: str) -> list[str]:
    """Emit `source`, run one call then an abort or a clean unload, and return
    the host trace (`put:..`, `comp:..`) with the `CALLED`/`DISPOSED` marks."""
    classes = javac_gate.compile_unit(
        tmp_path, emit(compile_source(source, "declared_compensate.rvl")))
    harness = tmp_path / "RunDeclared.java"
    harness.write_text(_HARNESS, encoding="utf-8")
    built = subprocess.run(
        [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-cp", str(classes),
         "-d", str(classes), str(harness)],
        capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run(
        [javac_gate.JAVA, "-cp", str(classes), "RunDeclared", mode],
        capture_output=True, text=True, timeout=600)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return [line.removeprefix("TRACE ") for line in ran.stdout.splitlines()
            if line.startswith("TRACE ") or line in ("CALLED", "DISPOSED")
            or line.startswith("THREW ")]


def _compensations(trace: list[str]) -> list[str]:
    return [line.split(":", 1)[1] for line in trace if line.startswith("comp:")]


@needs_jdk
@pytest.mark.parametrize("position", sorted(POSITIONS))
def test_an_abort_runs_the_declared_compensation_in_every_position(tmp_path, position):
    trace = _run(tmp_path, _program(_decls([position]), POSITIONS[position],
                                    activation=SITE), "abort")
    called = trace.index("CALLED")
    assert _compensations(trace[:called]) == []   # owed, not run: nothing failed
    assert f"put:{position}" in trace[:called]
    assert _compensations(trace) == [position, "site"]


@needs_jdk
@pytest.mark.parametrize("position", sorted(POSITIONS))
def test_a_clean_commit_discharges_the_declared_compensation(tmp_path, position):
    trace = _run(tmp_path, _program(_decls([position]), POSITIONS[position],
                                    activation=SITE), "commit")
    assert f"put:{position}" in trace
    assert _compensations(trace) == []


EVERY_POSITION = (
    'emit put_statement("k")\n'
    '      let a = emit put_let("k")\n'
    '      let s = double(emit put_argument("k"))\n'
    '      if (true) { emit put_ifarm("k") }\n'
    '      emit plain("k") compensate undo_site()\n'
    '      let b = emit put_nested("k") + 1\n'
    '      return emit put_return("k")'
)


@needs_jdk
def test_the_abort_runs_every_compensation_newest_first(tmp_path):
    """Every position in one method, a site-spelled compensation among them,
    and a declared crossing in the activation body: one LIFO order, whichever
    way each was registered."""
    tags = ["statement", "let", "argument", "ifarm", "nested", "return", "act"]
    trace = _run(tmp_path, _program(_decls(tags), EVERY_POSITION,
                                    activation='emit put_act("k")'), "abort")
    assert _compensations(trace) == [
        "return", "nested", "site", "ifarm", "argument", "let", "statement", "act"]


@needs_jdk
def test_a_crossing_that_throws_registers_nothing(tmp_path):
    """A throwing `put_boom` crossed nothing, so it owes nothing; the earlier
    crossing still owes its compensation and the abort runs it, once."""
    decls = _declared("first") + _declared("boom", fail=True)
    trace = _run(tmp_path, _program(
        decls, 'emit put_first("k")\n      return emit put_boom("k")',
        activation=SITE), "abort")
    assert "THREW put:boom failed" in trace
    assert _compensations(trace) == ["first", "site"]


def test_the_emitted_crossing_goes_through_one_registration():
    """Every position renders through `RevlDeclared`, and a document with no
    compensate-declaring extern carries none of it."""
    code = emit(compile_source(
        _program(_decls(POSITIONS), "return 0",
                 activation='emit put_statement("k")'), "d.rvl"))
    assert ('RevlDeclared.crossed(fx, frame, "put_statement", "undo_statement", '
            'put_statement("k"), () -> undo_statement());') in code
    assert "private static final class RevlDeclared {" in code
    plain = emit(compile_source(
        _program("", 'emit plain("k") compensate undo_site()\n      return 0'), "d.rvl"))
    assert "RevlDeclared" not in plain
