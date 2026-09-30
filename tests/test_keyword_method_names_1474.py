"""A provided method named after a Python keyword, soft keyword or `self` is
emitted and called on the python tier (issue #1474).

WHAT WAS WRONG. The py emitter renamed a provided method's `def` (`class` ->
`class_`) and then looked the renamed spelling up in the service it provides,
so `fn class()` was refused with "method 'class_' is not part of the provided
service". `self` was refused outright as emitter scaffolding. Measured on
main under Python 3.12 over every Python keyword and soft keyword plus `self`:
of the 40 names, the frontend refuses 15 (they are revl keywords too), and the
py emitter refused all 25 it was handed.

WHAT IS EMITTED NOW. One function, `_method_def_name`, decides the rename at
the definition, at the registration (the provided class gets the contract
name back as an alias) and at every static call site (`getattr(target,
'class')`, because `target.class` is not Python). The contract name stays
the attribute every dispatcher looks up.

The end-to-end tests drive a live cordis-py composition. Without the pinned
`cordis` fork they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import keyword
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the calls run against a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`",
)

NAMES = sorted(set(keyword.kwlist) | set(keyword.softkwlist) | {"self"})

#: The names the revl frontend itself refuses as an identifier: revl keywords.
#: They never reach an emitter. Pinned so the admitted set below is exactly
#: the rest, and a frontend change that admits one of them fails here.
FRONTEND_KEYWORDS = {
    "as", "assert", "async", "await", "break", "continue", "else", "for",
    "if", "in", "match", "return", "type", "while", "with",
}

ADMITTED = [n for n in NAMES if n not in FRONTEND_KEYWORDS]


def _provider(name: str) -> str:
    return (f"service S {{ fn {name}(x: Int) -> Int }}\n"
            f"component P provides s: S {{ provide s {{ fn {name}(x) {{ return x + 1 }} }} }}\n")


def _consumer(name: str) -> str:
    return _provider(name) + (
        "service Ops { fn run(x: Int) -> Int }\n"
        f"component C requires s: S provides ops: Ops {{\n"
        f"  provide ops {{ fn run(x) {{ return s.{name}(x) * 10 }} }}\n}}\n")


def test_the_sweep_covers_every_keyword_and_soft_keyword_and_self():
    # the keyword lists differ by Python version (`type` is a soft keyword
    # from 3.12), so the sweep is the running interpreter's own list
    assert {"self", "_", "class", "case"} <= set(ADMITTED)
    assert set(NAMES) == set(ADMITTED) | (FRONTEND_KEYWORDS & set(NAMES))


@pytest.mark.parametrize("name", sorted(FRONTEND_KEYWORDS))
def test_a_revl_keyword_is_refused_by_the_frontend_not_the_emitter(name):
    with pytest.raises(RevlError, match="expected ident"):
        compile_source(_provider(name), "kw.rvl")


@needs_cordis
@pytest.mark.parametrize("name", ADMITTED)
def test_a_keyword_method_is_provided_and_called_by_its_contract_name(name):
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(_provider(name), "kw.rvl"))
    assert session.call("s", name, [41])["result"] == 42
    session.unload()


@needs_cordis
@pytest.mark.parametrize("name", ADMITTED)
def test_a_keyword_method_is_called_through_a_required_service(name):
    """The static call site: `s.class(x)` in another component's body."""
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(_consumer(name), "kw_consumer.rvl"))
    assert session.call("ops", "run", [41])["result"] == 420
    session.unload()


def test_a_name_that_needs_no_rename_is_emitted_as_before():
    from _backend_import import backend_emitter
    code = backend_emitter("python").emit(
        compile_source(_consumer("bump"), "plain.rvl"))
    assert "def bump(self, x):" in code
    assert "_revl_ctx.s.bump(x)" in code
    assert "setattr(" not in code and "getattr(_revl_ctx" not in code


def test_the_rename_is_one_function_at_all_three_sites():
    from _backend_import import backend_emitter
    code = backend_emitter("python").emit(
        compile_source(_consumer("class"), "kw.rvl"))
    assert "def class_(self, x):" in code                  # definition
    assert "setattr(_S, 'class', _S.class_)" in code       # registration
    assert "getattr(_revl_ctx.s, 'class')(x)" in code      # call


@needs_cordis
@pytest.mark.parametrize("name", ["class", "self", "_", "case"])
def test_a_recorded_session_reaches_it_through_the_recorder(name):
    """Under `record=True` the required service is the recorder's proxy, which
    looks the method up by the name the call site hands it."""
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(_consumer(name), "kw_rec.rvl"), record=True)
    assert session.call("ops", "run", [41])["result"] == 420
    session.unload()


@needs_cordis
def test_a_lifecycle_test_calls_it(tmp_path):
    import subprocess
    source = tmp_path / "kw_lifecycle.rvl"
    source.write_text(_provider("class") + (
        'lifecycle test "a keyword method is called" {\n'
        "  load P\n  let n = call s.class(41)\n  assert n == 42\n"
        "  unload P\n  assert no_residue\n}\n"), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "py", str(source)],
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 test(s) passed" in proc.stdout + proc.stderr
