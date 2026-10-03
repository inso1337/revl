"""A provided method named after a TypeScript keyword is emitted on the ts tier
with one spelling at every site (issue #1512).

WHAT WAS WRONG. `_provide_impl` renamed a provided method with `_ident`
(`delete` -> `delete_`) and then looked the renamed spelling up in the service,
so the emitter refused 25 TypeScript keywords with "method 'delete_' is not
declared by service 'S'". A call through a required service renamed the same
way (`s.delete_(x)`), and the service interface did not rename at all. Names on
the append-`_` ladder (`delete_` -> `delete__`) were refused too.

WHAT IS EMITTED NOW. `_method_ident` is the one spelling, used at the
interface, the definition, a call through a required service, a lifecycle
`call` and a replayed compensation: the contract name, verbatim, because a
keyword is a legal property name. The interface member is quoted for a
reserved word, since a bare `new(..)` in an interface is a construct signature.

The runtime proof, every admitted keyword called by its contract name and
through a required service, is `tests/reserved_method_names.test.ts` (vitest),
and `scripts/typecheck-generated.mjs` typechecks the same module.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EMIT = _load("revl_ts_emit_1512", BACKEND / "emit.py")
GEN = _load("revl_gen_1512", BACKEND / "tests" / "fixtures" / "_gen_reserved_method_names.py")


def _consumer(name: str) -> str:
    return (f"service S {{ fn {name}(x: Int) -> Int }}\n"
            f"component P provides s: S {{ provide s {{ fn {name}(x) {{ return x + 1 }} }} }}\n"
            "service Ops { fn run(x: Int) -> Int }\n"
            "component C requires s: S provides ops: Ops {\n"
            f"  provide ops {{ fn run(x) {{ return s.{name}(x) * 10 }} }}\n}}\n")


def test_the_fixture_is_what_its_generator_writes():
    fixture = BACKEND / "tests" / "fixtures" / "reserved_method_names.ir.json"
    assert json.loads(json.dumps(GEN.build())) == json.loads(fixture.read_text(encoding="utf-8"))


def test_the_admitted_set_is_every_keyword_the_frontend_does_not_refuse():
    assert len(GEN.KEYWORDS) == 84
    assert set(GEN.ADMITTED) == (set(GEN.KEYWORDS) - set(GEN.FRONTEND_KEYWORDS)) | set(GEN.LADDER)


@pytest.mark.parametrize("name", GEN.FRONTEND_KEYWORDS)
def test_a_revl_keyword_is_refused_by_the_frontend_not_the_emitter(name):
    with pytest.raises(RevlError):
        compile_source(_consumer(name), "kw.rvl")


@pytest.mark.parametrize("name", GEN.ADMITTED)
def test_every_admitted_keyword_is_emitted_with_one_spelling(name):
    code = EMIT.emit(compile_source(_consumer(name), "kw.rvl"))
    assert f"        {name}(x: bigint) {{" in code          # definition
    assert f"ctx.s.{name}(x)" in code                        # call site
    member = json.dumps(name) if name in EMIT.JS_RESERVED else name
    assert f"  {member}(x: bigint): bigint" in code          # interface


def test_the_three_sites_for_delete():
    code = EMIT.emit(compile_source(_consumer("delete"), "kw.rvl"))
    assert '  "delete"(x: bigint): bigint' in code
    assert "        delete(x: bigint) {" in code
    assert "ctx.s.delete(x)" in code
    assert "delete_" not in code


def test_a_name_that_needs_no_rename_is_emitted_as_before():
    code = EMIT.emit(compile_source(_consumer("bump"), "plain.rvl"))
    assert "  bump(x: bigint): bigint" in code
    assert "        bump(x: bigint) {" in code
    assert "ctx.s.bump(x)" in code


@pytest.mark.skipif(
    shutil.which("node") is None
    or not (BACKEND / "node_modules" / ".bin" / "vitest").exists(),
    reason="the ts tier's vitest is not installed (cd backends/typescript && npm ci)")
def test_a_lifecycle_test_calls_it_on_the_ts_tier(tmp_path):
    """The lifecycle `call` site, run end to end by `revl test --backend ts`."""
    source = tmp_path / "kw_lifecycle.rvl"
    source.write_text(_consumer("class") + (
        'lifecycle test "a keyword method is called" {\n'
        "  load P\n  load C\n  let n = call s.class(41)\n  assert n == 42\n"
        "  let m = call ops.run(41)\n  assert m == 420\n"
        "  unload C\n  unload P\n  assert no_residue\n}\n"), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "ts", str(source)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "1 passed" in out or "1 test(s) passed" in out, out


#: Names #1552's keyword table does not list but a JS object answers to: the
#: Object.prototype and Function.prototype members, the thenable `then` (a
#: provider object with a `then` member is awaited as a promise) and the two
#: sloppy-mode globals. Re-measured after #1552 with `revl test --backend ts`:
#: each one crosses, called directly and through a required service.
PROTOTYPE_NAMES = ["then", "toString", "valueOf", "hasOwnProperty", "isPrototypeOf",
                   "propertyIsEnumerable", "toLocaleString", "prototype", "length",
                   "name", "call", "apply", "bind", "arguments", "eval"]


@pytest.mark.skipif(
    shutil.which("node") is None
    or not (BACKEND / "node_modules" / ".bin" / "vitest").exists(),
    reason="the ts tier's vitest is not installed (cd backends/typescript && npm ci)")
def test_a_prototype_name_crosses_on_the_ts_tier(tmp_path):
    names = PROTOTYPE_NAMES
    svc = "\n".join(f"  fn {n}(k: Int) -> Int" for n in names)
    impl = "\n".join(f"    fn {n}(k) = k + {i + 1}" for i, n in enumerate(names))
    ops = "\n".join(f"  fn go{i}(k: Int) -> Int" for i in range(len(names)))
    calls = "\n".join(f"    fn go{i}(k) = s.{n}(k)" for i, n in enumerate(names))
    steps = "".join(f"  let d{i} = call s.{n}(10)\n  assert d{i} == {11 + i}\n"
                    f"  let t{i} = call ops.go{i}(10)\n  assert t{i} == {11 + i}\n"
                    for i, n in enumerate(names))
    source = tmp_path / "proto_lifecycle.rvl"
    source.write_text(
        f"service S {{\n{svc}\n}}\n"
        f"component P provides s: S {{\n  provide s {{\n{impl}\n  }}\n}}\n"
        f"service Ops {{\n{ops}\n}}\n"
        f"component C requires s: S provides ops: Ops {{\n  provide ops {{\n{calls}\n  }}\n}}\n"
        'lifecycle test "every prototype name crosses" {\n  load P\n  load C\n'
        f"{steps}  unload C\n  unload P\n  assert no_residue\n}}\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "ts", str(source)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "1 passed" in out or "1 test(s) passed" in out, out
