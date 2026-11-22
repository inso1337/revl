"""Issue #3597: a host that already built a document's module hands it to the
session (`Session.offer_render`) instead of the session rendering it again, and
SLO discovery parses only a file that can declare a contract.

The render tests count calls to the py emitter's `emit` and to the `compile`
the driver runs, never wall-clock time, and pin what must not change: every
gate still runs, a pair for another document or emitter is never used, and each
generation gets its own module. The discovery tests count the parses
(`Session._slo_bind`).
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util

import pytest

import revl.run as run_mod
from revl._paths import python_backend_emitter
from revl.compiler import compile_source
from revl.mcp.session import Session, SessionError, _declares_slo

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a load boots a live cordis-py composition")

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

GATES = ("_enforce_sandbox", "_enforce_evidence", "_enforce_activation_gate",
         "_check_cache_applicability")


def _doc(answer: int) -> str:
    return DOC.replace("= 7", f"= {answer}")


class _Counts:
    def __init__(self) -> None:
        self.render = 0
        self.compile = 0
        self.gates: dict[str, int] = {}


@pytest.fixture
def counts(monkeypatch):
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
    for name in GATES:
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


def _host_build(text: str):
    """What a host that compiled and emitted `text` itself holds: the IR, the
    module text, and the code object it compiled from it."""
    ir = compile_source(text)
    source = python_backend_emitter().emit(ir)
    return ir, source, builtins.compile(source, "<host>", "exec")


# --------------------------------------------------------- offer_render


@needs_runtime
def test_control_a_load_with_no_offer_renders_and_compiles_once(
        counts, session):
    """Without this the `(0, 0)` assertions below could pass because the
    counters never fire."""
    session.load(compile_source(DOC), origin={"source": DOC})
    assert (counts.render, counts.compile) == (1, 1)
    assert "renderOffered" not in session._history[-1]


@needs_runtime
def test_an_offered_pair_is_loaded_without_a_render_or_a_compile(
        counts, session):
    ir, source, code = _host_build(DOC)
    counts.render = 0
    session.offer_render(ir, source, code)
    session.load(compile_source(DOC), origin={"source": DOC})
    assert (counts.render, counts.compile) == (0, 0)
    assert session.call("counter", "next")["result"] == 7
    # and every gate still ran on that load
    assert {name: counts.gates.get(name) for name in GATES} == {
        name: 1 for name in GATES}


@needs_runtime
def test_an_offer_without_code_is_compiled_by_the_session_not_the_load(
        counts, session):
    ir, source, _ = _host_build(DOC)
    counts.render = 0
    session.offer_render(ir, source)
    session.load(ir, origin={"source": DOC})
    assert (counts.render, counts.compile) == (0, 0)
    assert session.call("counter", "next")["result"] == 7
    # the code runs under THIS generation's file name
    assert session._driver.emitted_code.co_filename == "<revl-run gen1>"


@needs_runtime
def test_the_next_swap_to_the_offered_document_uses_it(counts, session):
    session.load(compile_source(_doc(1)), origin={"source": _doc(1)})
    counts.render = counts.compile = 0
    ir, source, code = _host_build(_doc(2))
    counts.render = 0
    session.offer_render(ir, source, code)
    session.swap(compile_source(_doc(2)), origin={"source": _doc(2)},
                 agent_authored=False)
    assert (counts.render, counts.compile) == (0, 0)
    assert session.call("counter", "next")["result"] == 2
    assert "renderOffered" in session._history[-1]
    # an undo and a swap back onto the offered document use it again, and
    # the generation that ran it says so
    session.swap(compile_source(_doc(1)), origin={"source": _doc(1)},
                 agent_authored=False)
    assert "renderOffered" not in session._history[-1]
    session.swap(compile_source(_doc(2)), origin={"source": _doc(2)},
                 agent_authored=False)
    assert counts.render == 0       # _doc(1) was rendered by the load
    assert "renderOffered" in session._history[-1]


@needs_runtime
def test_the_generation_record_carries_the_digest_of_the_offered_source(
        counts, session):
    ir, source, code = _host_build(DOC)
    reply = session.offer_render(ir, source, code)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert reply["renderOffered"] == digest
    session.load(compile_source(DOC), origin={"source": DOC})
    assert session._history[-1]["renderOffered"] == digest
    # consumed by the record, so a generation recorded without a render of its
    # own (an apply) cannot carry it
    assert session._render_offered is None
    session._record_generation(readmittable=False)
    assert "renderOffered" not in session._history[-1]
    session._history.pop()
    doc = session.history_document()
    assert doc["generations"][-1]["renderOffered"] == digest
    # a generation that rendered its own text carries no such member
    session.swap(compile_source(_doc(9)), origin={"source": _doc(9)},
                 agent_authored=False)
    assert "renderOffered" not in session._history[-1]
    assert "renderOffered" not in session.history_document()[
        "generations"][-1]


@needs_runtime
def test_a_pair_offered_for_another_document_is_never_used(counts, session):
    ir, source, code = _host_build(_doc(5))
    session.offer_render(ir, source, code)
    counts.render = 0
    session.load(compile_source(DOC), origin={"source": DOC})
    assert counts.render == 1
    assert session.call("counter", "next")["result"] == 7
    assert "renderOffered" not in session._history[-1]


@needs_runtime
def test_a_pair_is_never_used_by_another_emitter(counts, session, monkeypatch):
    ir, source, code = _host_build(DOC)
    session.offer_render(ir, source, code)
    emitter = python_backend_emitter()
    previous = emitter.emit
    replaced = []

    def other_emit(doc):
        replaced.append(1)
        return previous(doc)

    monkeypatch.setattr(emitter, "emit", other_emit)
    session.load(compile_source(DOC), origin={"source": DOC})
    assert replaced == [1]


@needs_runtime
def test_a_source_that_does_not_compile_is_refused_and_kept_nowhere(
        counts, session):
    ir, _, _ = _host_build(DOC)
    with pytest.raises(SessionError, match="does not compile"):
        session.offer_render(ir, "def (:")
    assert len(session._renders) == 0
    with pytest.raises(SessionError, match="module text"):
        session.offer_render(ir, b"x = 1")
    counts.render = 0
    session.load(compile_source(DOC), origin={"source": DOC})
    assert counts.render == 1


@needs_runtime
def test_a_document_with_no_key_is_not_kept(counts, session):
    ir, source, code = _host_build(DOC)
    reply = session.offer_render(dict(ir, extra=object()), source, code)
    assert reply["kept"] is False
    assert len(session._renders) == 0


@needs_runtime
def test_code_that_is_not_a_code_object_is_refused(session):
    ir, source, _ = _host_build(DOC)
    with pytest.raises(SessionError, match="code object"):
        session.offer_render(ir, source, "not code")
    assert len(session._renders) == 0


# ------------------------------------------------------- SLO discovery


def test_a_file_that_only_names_slow_or_slot_is_not_a_candidate():
    assert not _declares_slo("// a slow path\nlet slot = 1\n")
    assert not _declares_slo("composition c {\n  row @a from \"a.rvl\"\n}\n"
                             "let slot = slow\n")
    assert not _declares_slo("slo { p95_latency: 250ms }\n")   # no composition
    assert not _declares_slo("composition c {\n}\n")           # no slo clause
    assert not _declares_slo("composition c {\n  slow { }\n}\n")


def test_the_clause_head_the_parser_reads_is_a_candidate():
    assert _declares_slo("composition c {\n  slo { p95_latency: 250ms }\n}\n")
    assert _declares_slo("composition c {\n  slo {\n    availability: 99.9%\n"
                         "  }\n}\n")
    assert _declares_slo("composition c {\n\tslo\n\t{\n  }\n}\n")
    assert _declares_slo("composition c {\r\n  slo {\r\n  }\r\n}\r\n")


@pytest.fixture
def parses(monkeypatch):
    seen: list[str] = []
    real = Session._slo_bind

    def counted(self, document, root=None):
        seen.append(document)
        return real(self, document, root)

    monkeypatch.setattr(Session, "_slo_bind", counted)
    return seen


def _files(tmp_path):
    (tmp_path / "slow.rvl").write_text(
        "// the slow path, a slot for later\nlet slot = 1\n")
    (tmp_path / "plain.rvl").write_text(
        'composition plain {\n  row @a from "slow.rvl" provides a\n}\n')
    (tmp_path / "slo.rvl").write_text(
        'composition guarded {\n  slo { p95_latency: 250ms }\n'
        '  row @a from "slow.rvl" provides a\n}\n')
    return {n: str(tmp_path / f"{n}.rvl") for n in ("slow", "plain", "slo")}


@needs_runtime
def test_discovery_parses_only_the_file_that_declares_a_contract(
        session, parses, tmp_path):
    files = _files(tmp_path)
    ir = compile_source(DOC)
    session.load(ir, origin={"files": [files["slow"], files["plain"],
                                       files["slo"]]})
    assert parses == [files["slo"]]
    assert session._slo_composition == files["slo"]
    assert list(session._slo_contract()) == ["p95_latency_ms"]
    assert session._slo_contract()["p95_latency_ms"]["target"] == 250


@needs_runtime
def test_no_file_is_parsed_when_none_can_declare_a_contract(
        session, parses, tmp_path):
    files = _files(tmp_path)
    session.load(compile_source(DOC),
                 origin={"files": [files["slow"], files["plain"]]})
    assert parses == []
    assert session._slo_composition is None
    assert session._slo_contract() == {}


@needs_runtime
def test_a_contract_bound_with_configure_slo_skips_discovery(
        session, parses, tmp_path):
    files = _files(tmp_path)
    session.configure_slo(composition=files["slo"])
    assert parses == [files["slo"]]
    session.load(compile_source(DOC),
                 origin={"files": [files["slo"], files["plain"]]})
    assert parses == [files["slo"]]          # the load parsed nothing more
    assert session._slo_composition == files["slo"]


@needs_runtime
def test_a_parse_that_raises_leaves_the_session_with_no_contract(
        session, parses, tmp_path):
    broken = tmp_path / "broken.rvl"
    broken.write_text("composition broken {\n  slo { nonsense_datum: 1 }\n}\n")
    session.load(compile_source(DOC), origin={"files": [str(broken)]})
    assert parses == [str(broken)]
    assert session._slo_composition is None
    assert session._slo_contract() == {}


def _slo_file(tmp_path, name, p95):
    doc = tmp_path / f"{name}.rvl"
    doc.write_text(f'composition {name} {{\n  slo {{ p95_latency: {p95}ms }}\n'
                   f'  row @a from "slow.rvl" provides a\n}}\n')
    return str(doc)


def _target(session):
    return session._slo_contract()["p95_latency_ms"]["target"]


@needs_runtime
def test_a_swap_rediscovers_the_contract_of_its_own_files(
        session, parses, tmp_path):
    first = _slo_file(tmp_path, "first", 250)
    second = _slo_file(tmp_path, "second", 900)
    session.load(compile_source(_doc(1)), origin={"files": [first]})
    assert _target(session) == 250
    session.swap(compile_source(_doc(2)), origin={"files": [second]},
                 agent_authored=False)
    assert session._slo_composition == second
    assert _target(session) == 900


@needs_runtime
def test_a_contract_named_by_configure_slo_survives_a_swap(
        session, parses, tmp_path):
    named = _slo_file(tmp_path, "named", 250)
    other = _slo_file(tmp_path, "other", 900)
    session.configure_slo(composition=named)
    session.load(compile_source(_doc(1)), origin={"files": [other]})
    session.swap(compile_source(_doc(2)), origin={"files": [other]},
                 agent_authored=False)
    assert session._slo_composition == named
    assert _target(session) == 250
    # unloading forgets the explicit binding, so the next load discovers
    session.unload()
    session.load(compile_source(_doc(1)), origin={"files": [other]})
    assert session._slo_composition == other
    assert _target(session) == 900


@needs_runtime
def test_a_failed_load_leaves_nothing_for_the_next_load_to_inherit(
        session, parses, tmp_path):
    failed = _slo_file(tmp_path, "failed", 250)
    good = _slo_file(tmp_path, "good", 900)
    broken = compile_source(DOC)
    broken["components"] = "not a list"        # refused after discovery ran
    with pytest.raises(Exception):  # noqa: B017 - any refusal will do
        session.load(broken, origin={"files": [failed]})
    assert not session.loaded
    session.load(compile_source(DOC), origin={"files": [good]})
    assert session._slo_composition == good
    assert _target(session) == 900


@needs_runtime
def test_a_file_with_a_byte_order_mark_is_still_found(
        session, parses, tmp_path):
    doc = tmp_path / "bom.rvl"
    doc.write_bytes(b"\xef\xbb\xbf" + (
        'composition bom {\n  slo { p95_latency: 250ms }\n'
        '  row @a from "slow.rvl" provides a\n}\n').encode())
    session.load(compile_source(DOC), origin={"files": [str(doc)]})
    assert session._slo_composition == str(doc)
    assert _target(session) == 250
