"""The ts emitter registers an extern-declared `compensate` through one path,
and refuses by name a position it cannot register (issue #1511).

The runtime proof, an abort after each position running exactly the declared
compensations newest first, is `tests/declared_compensate.test.ts` (vitest).
These are toolchain-free checks on the emitted text.
"""

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

FIXTURE = BACKEND / "tests" / "fixtures" / "declared_compensate.ir.json"


def _load():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_1511", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EMIT = _load()


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_is_what_its_generator_writes():
    spec = importlib.util.spec_from_file_location(
        "revl_gen_1511", BACKEND / "tests" / "fixtures" / "_gen_declared_compensate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert json.loads(json.dumps(gen.build())) == _fixture()


def test_every_method_position_registers_exactly_once():
    """An `emit` statement (`statement`, and the one in the `if` arm) registers
    through the statement path every emit site shares (issue #1592,
    `compensationMethod`). Every expression position goes through
    `Frame.declared`. Each crossing registers once, never twice."""
    code = EMIT.emit(_fixture())
    for tag in ("statement", "let", "return", "argument", "ifarm", "nested"):
        calls = re.findall(rf"\bput_{tag}\(\"k\"\)", code)
        wrapped = re.findall(rf"\$revl_frame\.declared\(put_{tag}\(\"k\"\), ", code)
        stated = re.findall(
            rf'\$revl_frame\.compensationMethod\(\{{ key: "undo_{tag}"', code)
        offsets = re.findall(rf"=> undo_{tag}\(\)", code)
        # crossed once in `Positions` and once in `Everything.run`
        assert len(calls) == 2, (tag, calls)
        expected = (0, 2) if tag in ("statement", "ifarm") else (2, 0)
        assert (len(wrapped), len(stated)) == expected, (tag, wrapped, stated)
        assert len(offsets) == 2, (tag, offsets)


def test_the_activation_statement_registers_with_the_step():
    code = EMIT.emit(_fixture())
    assert '      put_act("k")\n' in code
    assert re.search(r'yield \$revl_frame\.compensation\(\{ key: "undo_act", '
                     r'method: "undo_act", args: \[\], site: "Everything\.body#\d+" \}, '
                     r'"undo_act", \[\], \(\) => undo_act\(\)\)', code)
    assert "declared(put_act" not in code


def test_a_program_without_one_emits_as_before():
    """No declared compensation, no `Frame`: the gate stays tight."""
    code = EMIT.emit(compile_source(
        "extern emission fn put(k: Str) -> Int = @ts { return 1n }\n"
        "service S { emission fn run() -> Int }\n"
        "component C provides s: S {\n  provide s {\n"
        '    fn run() { return emit put("k") }\n  }\n}\n', "plain.rvl"))
    assert "Frame" not in code and ".declared(" not in code


def _activation_argument_ir() -> dict:
    """`emit log(put("k"))` in an activation body: the declared crossing is an
    argument, which an activation body has no frame to register on. Written by
    hand from compiled IR, since the frontend does not produce it."""
    ir = compile_source(
        "extern pure fn undo_put() -> Unit = @ts { return }\n"
        "extern emission fn put(k: Str) -> Int compensate undo_put() = @ts { return 1n }\n"
        "extern emission fn log(n: Int) -> Int = @ts { return n }\n"
        'component C {\n  emit put("k")\n}\n', "act.rvl")
    ir = copy.deepcopy(ir)
    step = ir["components"][0]["body"][0]
    step["expr"] = {"kind": "fn", "name": "log", "args": [step["expr"]]}
    return ir


def test_a_position_with_no_frame_to_register_on_is_refused_by_name():
    with pytest.raises(EMIT.EmitError, match=r"extern `put` declares a `compensate`"):
        EMIT.emit(_activation_argument_ir())
