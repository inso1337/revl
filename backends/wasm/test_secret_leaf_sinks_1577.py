"""Can a wasm host sink print one leaf of a declared `Secret[T]`? (issue #1577)

The other five tiers scrub a declared secret at run time: each registers the
value (and, for a container, every leaf) at the door it enters through, and
each funnels host text through a redaction step. This tier has neither a
registry nor a funnel, so the question had to be measured sink by sink.

**The sinks.** A value the module holds can reach text a person or a file can
read only where the host reads the module's memory and writes what it read.
On this tier that is ONE place: the record-mode durable-WAL channel
(`coeffect:revl:wal.record`). `run_harness.py` reads the framed witness Str
back out of the calling fiber's memory and relays it as a `[wal]` frame, and
`revl.run_wasm` writes it into `$REVL_WAL`. Every other line the runner prints
carries a name the compiler fixed (component, provision key, fiber state) or
a count, and the runtime's own `rt.log` (trap text, `trace`) is never printed
by either harness. `test_the_host_reads_module_memory_in_one_place` and
`test_the_host_import_namespaces_are_the_known_set` pin that inventory, so a
new sink cannot appear without this file being revisited.

**The doors, measured on the live runtime** (`test_no_door_puts_a_leaf_on_a_sink`):

* config field: refused for every shape. A statically composed component has
  no config channel, and a spawn target's config crosses only Int/Bool (the
  `Secret[...]` qualifier is stripped before the emitter, so `Secret[Str]` is
  refused as `Str`).
* provide-method parameter: `Secret[List[Str]]` and `Secret[Str]` emit, and no
  sink can carry them. A method-body witnessed effect parks its witness in the
  runtime accumulator (`$__mw_head`) and is never framed, the once-mode
  harness never calls a method, and the lifecycle harness skips any call that
  crosses a non-scalar boundary.
* extern return: a leaf REACHED THE WAL, and so did the scalar control. The
  shape is a witnessed extern that takes the value through a `Secret[...]`
  parameter and hands it back as an Ok witness the author did not declare
  confidential. The checker allows the call (the parameter is declared
  confidential), the witness carries no `secret_witness` stamp, and record mode
  framed the bytes into the WAL file verbatim.
* `Secret[Map[Str, Str]]` is refused at every door: `Map` has no value
  representation on this tier.

**The fix is a refusal, at the framing site, by name.** An activation-registered
witnessed extern that takes a declared `Secret[...]` parameter and whose Ok
witness is not declared confidential is an `EmitError` naming the extern and
the parameter. It is refused whether or not the emission is in record mode,
because the shape belongs to the program and `REVL_WAL` to one run of it.
A scrub was not chosen. The only scrub this tier can perform is the emit-time
placeholder, and applying it to a witness the author did not declare
confidential would silently make the descriptor unreplayable (`revl recover`
refuses a `<redacted:secret>` referent). The author can already opt in to that
by declaring the witness position `Secret[...]`, and the control below proves
that path stays open. A value-keyed scrub needs a registry in module memory and
a host-side matcher that would have to hold the secret bytes outside the
sandbox to match them. That is a larger change than the leak warrants.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import run_wasm as run_wasm_mod  # noqa: E402

CANARY = "s3cr3tleaf1577"
REDACTED_SECRET = "<redacted:secret>"

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")


def _wat_str(addr: int, text: str) -> str:
    """WAT that writes a canonical Str (`[u32 len][utf8 bytes]`) at `addr`."""
    data = text.encode()
    out = [f"(i32.store (i32.const {addr}) (i32.const {len(data)}))"]
    out += [f"(i32.store8 (i32.const {addr + 4 + i}) (i32.const {b}))"
            for i, b in enumerate(data)]
    return " ".join(out)


# A one-element `List[Str]` at 6000 (`[u32 count][pad][8-byte slot]`) whose
# element is the canary Str at 6100.
_LIST_BODY = (_wat_str(6100, CANARY)
              + " (i32.store (i32.const 6000) (i32.const 1))"
              + " (i64.store (i32.const 6008) (i64.extend_i32_u (i32.const 6100)))"
              + " (i32.const 6000)")
_STR_BODY = _wat_str(6100, CANARY) + " (i32.const 6100)"
# `Ok(v)`: a tagged cell `[u32 tag=0][pad][slot payload]` at 6200.
_OK_OF_PARAM = ("(i32.store (i32.const 6200) (i32.const 0)) "
                "(i64.store (i32.const 6208) (i64.extend_i32_u (local.get $p_v))) "
                "(i32.const 6200)")

# shape -> (declared type, @wasm origin body, how to read one leaf of `src`)
_SHAPES = {
    "list": ("Secret[List[Str]]", _LIST_BODY, "src[0]"),
    "map": ("Secret[Map[Str, Str]]", "(i32.const 0)", 'src["key"]'),
    "scalar": ("Secret[Str]", _STR_BODY, "src"),
}


def _sink(witness: str = "Str") -> str:
    """The sink: a witnessed extern that takes a confidential parameter and
    hands it back as its Ok witness, which record mode frames into the WAL."""
    return f'''
extern witnessed[t] fn stash(v: Secret[Str]) -> Result[{witness}, Str]
    undo unstash(result)
    = @wasm {{ {_OK_OF_PARAM} }}

extern pure fn unstash(v: Str) -> Unit = @wasm {{ }}
'''


def _program(door: str, shape: str) -> str:
    ty, body, leaf = _SHAPES[shape]
    origin = f"extern pure fn origin() -> {ty} = @wasm {{ {body} }}\n"
    if door == "extern":
        # `origin()[0]` does not lower (the type of an extern call inside an
        # index is not inferred on this tier), so the leaf is read in a fn.
        return origin + _sink() + f'''
fn leaf(src: {ty}) -> Secret[Str] {{
  return {leaf}
}}
component Holder {{
  effect stash(leaf(origin()))
}}
'''
    if door == "config":
        return _sink() + f'''
component Holder {{
  config {{ src: {ty} }}
  effect stash(config.src{leaf[3:]})
}}
'''
    if door == "spawn-config":
        return origin + _sink() + f'''
service Ping {{ fn ping() -> Int }}
component Worker provides ping: Ping {{
  config {{ src: {ty} }}
  effect stash(config.src{leaf[3:]})
  provide ping {{ fn ping() = 1 }}
}}
component Boss {{
  let w = effect spawn Worker with {{ src: origin() }} undo w.dispose()
}}
'''
    if door == "param":
        return origin + _sink() + f'''
service Front {{
  emission fn put(src: {ty}) -> Int
}}
component Portal provides front: Front {{
  provide front {{
    fn put(src) {{
      effect stash({leaf})
      return 1
    }}
  }}
}}
component Caller requires front: Front {{
  emit front.put(origin())
}}
'''
    raise KeyError(door)


def _emitter():
    # one module object per call site: `EmitError` is a class on the module
    # OBJECT, so the refusal is caught against the instance that raised it.
    return run_wasm_mod._wasm_emitter()


def _ir(door: str, shape: str) -> dict:
    return compile_source(_program(door, shape), f"leaf_{door}_{shape}.rvl")


# ---------------------------------------------------------------------------
# the refusal, at the emitter (runs everywhere: no runtime needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record", [True, False])
@pytest.mark.parametrize("shape", ["list", "scalar"])
def test_a_confidential_parameter_framed_as_a_plain_witness_is_refused(shape, record):
    """The shape that leaked. In record mode the module would frame the
    witness verbatim, and this tier has nothing that could scrub it. The
    refusal does not depend on the mode: the shape is a property of the
    program, and `REVL_WAL` is a property of one run of it."""
    emit = _emitter()
    with pytest.raises(emit.EmitError) as caught:
        emit.emit(_ir("extern", shape), record=record)
    message = str(caught.value)
    assert "`stash`" in message, message
    assert "`v`" in message, message
    assert "issue #1577" in message, message


def test_the_refusal_fixture_is_this_shape():
    """`tests/fixtures/emit_wasm_refusals/` drives the line-coverage gate over
    the refusal; it has to be refused for THIS reason, not another."""
    path = _ROOT / "tests" / "fixtures" / "emit_wasm_refusals" / "secret_fed_plain_witness.rvl"
    emit = _emitter()
    with pytest.raises(emit.EmitError) as caught:
        emit.emit(compile_source(path.read_text(encoding="utf-8"), str(path)))
    assert "issue #1577" in str(caught.value)


def test_a_declared_confidential_witness_still_frames_the_placeholder():
    """The way out the refusal names stays open: an author who declares the
    witness position confidential gets the placeholder in the record."""
    src = _program("extern", "scalar").replace(_sink(), _sink("Secret[Str]"))
    modules = _emitter().emit(compile_source(src, "leaf_declared.rvl"), record=True)
    wat = modules["Holder"]
    assert "(call $revl_wal_record " in wat
    assert REDACTED_SECRET in wat


def test_an_ordinary_witnessed_extern_is_not_refused():
    """The false-positive control: no confidential parameter, no refusal, and
    the referent is still framed verbatim for recovery."""
    src = '''
extern witnessed[t] fn stash(v: Str) -> Result[Str, Str]
    undo unstash(result)
    = @wasm { (i32.const 0) }
extern pure fn unstash(v: Str) -> Unit = @wasm { }
component Holder { effect stash("row") }
'''
    wat = _emitter().emit(compile_source(src, "leaf_plain.rvl"), record=True)["Holder"]
    assert "(call $revl_wal_record " in wat
    assert REDACTED_SECRET not in wat


# ---------------------------------------------------------------------------
# the sink inventory, pinned
# ---------------------------------------------------------------------------


def _functions_reading_memory(path: Path) -> set[str]:
    """Names of the functions in a harness that read a module's memory."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    readers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "read"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "memory"):
                    readers.add(node.name)
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "_read_wasm_str"):
                    readers.add(node.name)
    return readers


def test_the_host_reads_module_memory_in_one_place():
    """The once-mode harness reads module memory only to relay the WAL frame,
    and the lifecycle harness never reads it. A new reader is a new sink."""
    # `record` is the closure `_install_wal_channel` binds as the host half of
    # `coeffect:revl:wal.record`, so the enclosing function counts too.
    assert _functions_reading_memory(_HERE / "run_harness.py") == {
        "_read_wasm_str", "_install_wal_channel", "record"}
    assert _functions_reading_memory(_HERE / "lifecycle_harness.py") == set()


def test_the_host_import_namespaces_are_the_known_set():
    """Every import namespace the emitter can give a module. Only `revl:wal`
    hands the host a pointer it reads back as text; the rest carry Int/Bool
    values, a static job id, or a call into another module."""
    source = (_HERE / "emit.py").read_text(encoding="utf-8")
    spelled = set(re.findall(r'\(import "([^"]*)"', source))
    assert spelled == {
        "{_wat_string(self._import_module(key))}",  # coeffect:<key>, another module
        "route:{key}",            # Int realm index, another module
        "{_WAL_IMPORT_MODULE}",   # coeffect:revl:wal, the one text sink
        "host",                   # job_run(i32): a compile-time job id
        "config",                 # inbound only, Int/Bool
        "spawn:{target}",         # Int/Bool config values, returns a handle
        "dispose",                # an i32 handle
        "instance:{component}",   # a call into another instance
    }, spelled
    assert '_WAL_IMPORT_MODULE = "coeffect:revl:wal"' in source
    assert 'return f"coeffect:{self._scoped_key(key)}"' in source


# ---------------------------------------------------------------------------
# the measurement, on the live runtime
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wasm_runtime():
    reason = run_wasm_mod.wasm_runtime_reason()
    if reason is not None:
        if _REQUIRE:
            pytest.fail(f"cordis-wasm runtime not available and "
                        f"REVL_REQUIRE_WASMTIME is set: {reason}")
        pytest.skip(f"cordis-wasm runtime not available: {reason}")


def _run_recorded(ir: dict, wal: Path) -> tuple[int, str, str]:
    """`revl run --backend wasm --once` with `$REVL_WAL` set: what the runner
    printed, and what reached the durable WAL file."""
    buffer = io.StringIO()
    previous = os.environ.get("REVL_WAL")
    os.environ["REVL_WAL"] = str(wal)
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            rc = run_wasm_mod.run_wasm(ir, {}, [], once=True)
    finally:
        if previous is None:
            del os.environ["REVL_WAL"]
        else:
            os.environ["REVL_WAL"] = previous
    return rc, buffer.getvalue(), (wal.read_text() if wal.exists() else "")


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("door", ["extern", "param", "config", "spawn-config"])
def test_no_door_puts_a_leaf_on_a_sink(door, shape, wasm_runtime, tmp_path):
    """Every door and shape, run for real with the WAL on: the canary reaches
    neither the runner's output nor the WAL file, whether the program was
    refused or ran."""
    rc, out, wal = _run_recorded(_ir(door, shape), tmp_path / "run.wal")
    assert CANARY not in out, f"{door}/{shape}: a leaf reached the runner output\n{out}"
    assert CANARY not in wal, f"{door}/{shape}: a leaf reached the WAL\n{wal}"


def test_the_wal_sink_is_live(wasm_runtime, tmp_path):
    """Non-vacuity: the same sink, fed the same bytes through an ordinary
    witness, does write them. So the test above measures a working sink."""
    src = f'''
extern pure fn origin() -> Str = @wasm {{ {_STR_BODY} }}
extern witnessed[t] fn stash(v: Str) -> Result[Str, Str]
    undo unstash(result)
    = @wasm {{ {_OK_OF_PARAM} }}
extern pure fn unstash(v: Str) -> Unit = @wasm {{ }}
component Holder {{ effect stash(origin()) }}
'''
    rc, out, wal = _run_recorded(compile_source(src, "leaf_live.rvl"),
                                 tmp_path / "run.wal")
    assert rc == 0, out
    assert CANARY in wal, wal
