"""Issue #3383: a swap onto a document the session has already run reuses the
rendered text and the compiled code instead of rendering and compiling again.

Undo and redo across generations, a reverted edit and a swap onto the running
code (#3304) all return to a document the session rendered a moment before.
These tests count calls to the py emitter's `emit` and to the `compile` the
driver runs, never wall-clock time. They also pin what must not change: every
gate still runs on every swap, every generation gets its own module, and the
reused code reports the NEW generation's file name, which is what a traceback
and the replay recorder quote.
"""

from __future__ import annotations

import builtins
import importlib.util
import types

import pytest

import revl.run as run_mod
from revl._paths import python_backend_emitter
from revl.compiler import compile_source
from revl.mcp.session import Session

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap boots a live cordis-py composition")

DOC = """
service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 7
  }
}
"""


def _doc(answer: int) -> str:
    return DOC.replace("= 7", f"= {answer}")


#: the gates `Session._plan_swap` runs on every swap, before the render
GATES = ("_enforce_sandbox", "_enforce_evidence", "_enforce_activation_gate",
         "_check_cache_applicability")


class _Counts:
    def __init__(self) -> None:
        self.render = 0
        self.compile = 0
        self.gates: dict[str, int] = {}


@pytest.fixture
def counts(monkeypatch):
    """Count the renders (the py emitter's `emit`), the compiles the driver
    runs (`compile` as `revl.run` resolves it) and each gate."""
    seen = _Counts()
    emitter = python_backend_emitter()
    real_emit = emitter.emit

    def emit(ir):
        seen.render += 1
        return real_emit(ir)

    def counted_compile(*args, **kwargs):
        seen.compile += 1
        return builtins.compile(*args, **kwargs)

    monkeypatch.setattr(emitter, "emit", emit)
    monkeypatch.setattr(run_mod, "compile", counted_compile, raising=False)
    for name in GATES + ("_enforce_declared_disclosure",):
        real = getattr(Session, name)

        def gate(self, *args, _real=real, _name=name, **kwargs):
            seen.gates[_name] = seen.gates.get(_name, 0) + 1
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Session, name, gate)
    return seen


@pytest.fixture
def session():
    s = Session()
    yield s
    if s.loaded:
        s.unload()


def _swap(s: Session, text: str, *, agent_authored: bool = False) -> None:
    s.swap(compile_source(text), origin={"source": text},
           agent_authored=agent_authored)


def test_a_second_swap_onto_the_same_document_skips_render_and_compile(
        counts, session):
    session.load(compile_source(DOC), origin={"source": DOC})
    assert (counts.render, counts.compile) == (1, 1)
    counts.gates.clear()  # the load ran them too
    # a freshly compiled IR of the same text, as an undo or a revert brings back
    _swap(session, DOC)
    _swap(session, DOC, agent_authored=True)
    assert (counts.render, counts.compile) == (1, 1)
    # and every gate still ran on both swaps
    assert {name: counts.gates.get(name) for name in GATES} == {
        name: 2 for name in GATES}
    assert counts.gates.get("_enforce_declared_disclosure") == 1
    assert session.call("counter", "next")["result"] == 7


def test_undo_and_redo_across_generations_hit_the_table(counts, session):
    session.load(compile_source(_doc(1)), origin={"source": _doc(1)})
    _swap(session, _doc(2))
    assert (counts.render, counts.compile) == (2, 2)
    _swap(session, _doc(1))
    assert session.call("counter", "next")["result"] == 1
    _swap(session, _doc(2))
    assert session.call("counter", "next")["result"] == 2
    assert (counts.render, counts.compile) == (2, 2)


def _code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _module_functions(module: types.ModuleType):
    for value in vars(module).values():
        if isinstance(value, types.FunctionType):
            yield value
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for member in vars(value).values():
                if isinstance(member, types.FunctionType):
                    yield member


def test_the_reused_code_reports_the_new_generation(counts, session):
    session.load(compile_source(DOC), origin={"source": DOC}, record=True)
    _swap(session, DOC)
    _swap(session, DOC)
    driver = session._driver
    assert counts.compile == 1, "the third generation must reuse the code"
    name = f"<revl-run gen{driver.generation}>"
    assert driver.generation == 3
    assert driver.emitted[0] == name
    assert {code.co_filename
            for code in _code_objects(driver.emitted_code)} == {name}
    # the generation still has its own module, and every function it defines
    # names the new generation, so a traceback through it does too, and the
    # replay recorder (which quotes `co_filename:co_firstlineno`) finds the line
    modules = [gen.module for gen in driver._gen_modules.values()]
    module = modules[-1]
    assert len({id(m) for m in modules}) == len(modules)
    functions = list(_module_functions(module))
    assert functions
    for fn in functions:
        assert fn.__code__.co_filename == name, fn.__qualname__
        assert session.recorder.sources.line(
            name, fn.__code__.co_firstlineno) is not None, fn.__qualname__


def test_a_change_in_any_keyed_input_misses(counts, session, monkeypatch):
    session.load(compile_source(DOC), origin={"source": DOC})
    _swap(session, DOC)
    assert counts.render == 1
    # the document
    _swap(session, _doc(8))
    assert counts.render == 2
    assert session.call("counter", "next")["result"] == 8
    # the emitter: a different `emit` (a reload, another backend) never reuses
    # text the old one rendered
    emitter = python_backend_emitter()
    previous = emitter.emit
    replaced = []

    def other_emit(ir):
        replaced.append(1)
        return previous(ir)

    monkeypatch.setattr(emitter, "emit", other_emit)
    _swap(session, DOC)
    assert replaced == [1]


def test_the_key_tells_apart_what_the_emitter_can():
    from revl.mcp.session import _RenderCache

    emitter = python_backend_emitter()
    ir = compile_source(DOC)
    key = _RenderCache.key(emitter, ir)
    assert key == _RenderCache.key(emitter, compile_source(DOC))
    # key order: the emitter iterates dicts in order
    reordered = dict(reversed(list(ir.items())))
    assert _RenderCache.key(emitter, reordered)[0] != key[0]
    # a tuple where the document has a list
    retyped = dict(ir, components=tuple(ir["components"]))
    assert _RenderCache.key(emitter, retyped)[0] != key[0]
    # anything that is not plain data has no key, so it is never cached
    assert _RenderCache.key(emitter, dict(ir, extra=object())) is None


def test_the_table_stays_at_its_fixed_size(counts, session):
    from revl.mcp.session import RENDER_CACHE_SIZE

    session.load(compile_source(_doc(0)), origin={"source": _doc(0)})
    for answer in range(1, RENDER_CACHE_SIZE + 3):
        _swap(session, _doc(answer))
        assert len(session._renders) == min(answer + 1, RENDER_CACHE_SIZE)
    renders = counts.render
    # the newest few are held, the oldest were dropped
    _swap(session, _doc(RENDER_CACHE_SIZE + 1))
    assert counts.render == renders
    _swap(session, _doc(0))
    assert counts.render == renders + 1
    assert len(session._renders) == RENDER_CACHE_SIZE
