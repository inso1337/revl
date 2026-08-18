"""The self-hosted lexer (selfhost/lexer.rvl, syntax-2.0 §11): compiled by
revl, emitted through the python backend, executed, and cross-checked
token-for-token against the reference lexer (src/revl/lexer.py) over the
real example corpus — including its own source (self-application)."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.lexer import lex as reference_lex  # noqa: E402

CORPUS = [
    "examples/migrator.rvl",
    "examples/pulse.rvl",
    "examples/user_cache.rvl",
    "examples/beacon.rvl",
    "examples/tenants.rvl",
    "backends/rust/scenarios/probe.rvl",
    "selfhost/lexer.rvl",  # the money shot: the lexer lexes itself
]


def _exec_emitted(tag: str) -> dict:
    """Compile selfhost/lexer.rvl, emit python, exec. The component in the
    file makes the emitted module import the cordis-py `runtime` adapter;
    the pure functions under test don't touch it, so a lazy stub suffices."""
    ir = compile_files([str(ROOT / "selfhost" / "lexer.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        f"pyemit_selfhost_{tag}", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_lexer.py", "exec"), namespace)
    finally:
        # never leak the stub into other tests' real runtime imports
        if had_runtime:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def _emitted_lex_src():
    return _exec_emitted("x")["lex_src"]


def _canon_reference(tokens):
    """Reference tokens -> the (kind, text, line) shape the revl lexer
    produces (ints as digits, template parts serialized, eof text "")."""
    out = []
    for t in tokens:
        if t.kind == "eof":
            out.append(("eof", "", t.line))
        elif t.kind == "int":
            out.append(("int", str(t.value), t.line))
        elif t.kind == "template":
            parts = "|".join(
                ("t:" + s) if k == "text" else ("v:" + s) for k, s in t.value)
            out.append(("template", parts, t.line))
        elif t.kind == "hostbody":
            pytest.skip("corpus file contains a host body (not yet lexed)")
        else:
            out.append((t.kind, str(t.value), t.line))
    return out


def _canon_emitted(tokens):
    return [(t["kind"], t["text"], t["line"]) for t in tokens]


@pytest.fixture(scope="module")
def lex_src():
    return _emitted_lex_src()


@pytest.mark.parametrize("rel", CORPUS)
def test_selfhosted_lexer_matches_reference(lex_src, rel):
    source = (ROOT / rel).read_text(encoding="utf-8")
    want = _canon_reference(reference_lex(source, rel))
    got = _canon_emitted(lex_src(source))
    assert "error" not in {k for k, _, _ in got}, [t for t in got if t[0] == "error"]
    assert got == want


def test_selfhosted_lexer_in_file_tests_pass():
    """The .rvl file's own `test` blocks run under the python backend."""
    namespace = _exec_emitted("t")
    tests = namespace.get("REVL_TESTS")
    assert tests and len(tests) >= 3, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()


def test_selfhosted_keyword_set_matches_reference():
    """Close the class, not just the instance.

    `hole` was in the reference KEYWORDS and absent from the revl lexer's
    `keywords()`, and the corpus test above did not catch it — no corpus
    file uses `hole`, so the two lexers agreed on every input they were
    ever asked about. A differential oracle only covers what the corpus
    exercises; a set that must stay equal should be compared as a set.
    """
    from revl.lexer import KEYWORDS

    emitted = set(_exec_emitted("kw")["keywords"]())
    assert emitted == set(KEYWORDS), {
        "missing from selfhost": sorted(set(KEYWORDS) - emitted),
        "extra in selfhost": sorted(emitted - set(KEYWORDS)),
    }


# Interpolation forms the corpus never exercises. The revl lexer used to
# accept only `${ident}` and returned an `error` token for everything else,
# while the reference captures the body as raw brace-balanced source for the
# parser to re-parse (§3.2). Same class of miss as `hole` above: legal input
# nobody happened to lex.
TEMPLATE_CASES = [
    "`sum is ${a + b}!`",
    "`hi ${name}`",
    "`n=${r.count}`",
    "`rec ${ {a: 1} }`",
    "`multi\n${x}\nline`",
    "`plain`",
    "`$ bare`",
    "`${f(1, 2)}`",
]


@pytest.mark.parametrize("src", TEMPLATE_CASES)
def test_selfhosted_lexer_templates_match_reference(lex_src, src):
    got = _canon_emitted(lex_src(src))
    assert "error" not in {k for k, _, _ in got}, got
    assert got == _canon_reference(reference_lex(src, "template_case.rvl"))
