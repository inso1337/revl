"""Generated wasm components are confined by default (issue #2729).

Three behaviours, each with a test that fails before this slice:

  * an UNCONFINED component — one whose produced bytes carry an import the
    component never declared, or an import outside the expected namespaces —
    is refused by the byte-level import check, not by the emitter's bookkeeping;
  * a CONFINED component with limits emits, validates and runs, and
    ``run_component`` applies fuel, a memory ceiling and a wall-clock ceiling
    even when the caller passes none;
  * a policy that asks for wasm confinement but declares no limits is refused
    with a named error instead of running unbounded.

The memory and fuel traps are distinct reasons on the same contained-trap path.
The emitter-level check (``emit._assert_imports_within_requires``) reads the
emitter's own import list, so a mutated component's bytes pass it; these tests
mutate the produced component's embedded core module to prove the new check
reads what actually runs.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backends" / "wasm"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402
from revl.policy import parse_policy  # noqa: E402
from revl.wasm_confinement import (  # noqa: E402
    ComponentLimits,
    ConfinementError,
    check_component_imports,
    limits_from_policy,
)

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")


def _canonical():
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_canonical_2729", BACKEND / "canonical.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toolchain() -> bool:
    have = shutil.which("wasm-tools") and (
        shutil.which("wasmtime")
        or os.path.exists(os.path.expanduser("~/.wasmtime/bin/wasmtime")))
    return bool(have)


def _need_toolchain():
    if not _toolchain():
        if _REQUIRE:
            pytest.fail("wasm-tools/wasmtime absent but REVL_REQUIRE_WASMTIME "
                        "is set", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")


# --------------------------------------------------------------------------- #
# helpers: build the canonical echoer component and edit its produced bytes.
# --------------------------------------------------------------------------- #

_ECHO = (BACKEND / "golden" / "canonical_echoer.revl").read_text(encoding="utf-8")


def _component(tmp_path: pathlib.Path) -> pathlib.Path:
    canonical = _canonical()
    emitted = canonical.emit_component(compile_source(_ECHO), service="Echoer")
    return canonical.build_component(
        emitted["core_wat"], emitted["wit"], tmp_path,
        emitted["world"], name="echoer")


def _read_uleb(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _uleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _replace_first_core_module(component: bytes, core: bytes) -> bytes:
    """Rewrite a component's embedded core module IN THE BYTES.

    The section framing stays valid (the size is re-encoded), so the result is
    still a component; only the core module's import section differs. This is
    the mutation the emitter's bookkeeping cannot see."""
    assert component[:8] == b"\0asm\x0d\x00\x01\x00", "not a component"
    out = bytearray(component[:8])
    pos = 8
    replaced = False
    while pos < len(component):
        section_id = component[pos]
        size, start = _read_uleb(component, pos + 1)
        payload = component[start:start + size]
        if section_id == 1 and not replaced:
            payload = core
            replaced = True
        out.append(section_id)
        out += _uleb(len(payload))
        out += payload
        pos = start + size
    assert replaced, "no embedded core module"
    return bytes(out)


def _core_bytes(wat: str, tmp_path: pathlib.Path) -> bytes:
    source = tmp_path / "mutated.wat"
    source.write_text(wat, encoding="utf-8")
    target = tmp_path / "mutated.wasm"
    result = subprocess.run(
        ["wasm-tools", "parse", str(source), "-o", str(target)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return target.read_bytes()


def _component_with_import(tmp_path: pathlib.Path, module: str,
                           field: str) -> pathlib.Path:
    canonical = _canonical()
    emitted = canonical.emit_component(compile_source(_ECHO), service="Echoer")
    component = _component(tmp_path)
    injected = emitted["core_wat"].replace(
        "(module\n",
        f'(module\n  (import "{module}" "{field}" (func $injected))\n', 1)
    core = _core_bytes(injected, tmp_path)
    mutated = tmp_path / "mutated.wasm"
    mutated.write_bytes(_replace_first_core_module(component.read_bytes(), core))
    return mutated


# --------------------------------------------------------------------------- #
# 1. an unconfined component refuses — the byte-level import check.
# --------------------------------------------------------------------------- #

def test_undeclared_capability_import_in_produced_bytes_is_refused(tmp_path):
    _need_toolchain()
    component = _component_with_import(tmp_path, "coeffect:kv", "put")
    with pytest.raises(ConfinementError, match="kv"):
        check_component_imports(component.read_bytes(), declared=frozenset())
    # the runtime refuses the same bytes by default; nothing executes.
    canonical = _canonical()
    with pytest.raises(ConfinementError, match="kv"):
        canonical.run_component(component, 'echo("x")')


def test_import_outside_the_expected_namespaces_is_refused(tmp_path):
    _need_toolchain()
    component = _component_with_import(tmp_path, "env", "secret")
    with pytest.raises(ConfinementError, match="expected namespaces"):
        check_component_imports(component.read_bytes(), declared=frozenset())


def test_a_declared_capability_import_is_confined(tmp_path):
    _need_toolchain()
    component = _component_with_import(tmp_path, "coeffect:kv", "put")
    # declaring `kv` is what the emitter would have required; the byte check
    # reads the produced import section rather than trusting the bookkeeping.
    check_component_imports(component.read_bytes(), declared=frozenset({"kv"}))


def test_the_durable_wal_channel_is_structural(tmp_path):
    # `coeffect:revl:wal` is a first-party framing channel, not a declared
    # capability (emit.py and least_authority exempt it too). A record-mode
    # component must not need `revl:wal` in its declared set.
    _need_toolchain()
    component = _component_with_import(tmp_path, "coeffect:revl:wal", "record")
    check_component_imports(component.read_bytes(), declared=frozenset())


def test_an_instance_import_to_an_unknown_component_is_refused(tmp_path):
    # `instance:<component>` reaches another component; only a component that
    # is part of this composition may be named.
    _need_toolchain()
    component = _component_with_import(tmp_path, "instance:Admin", "secret.read")
    with pytest.raises(ConfinementError, match="Admin"):
        check_component_imports(component.read_bytes(), declared=frozenset(),
                                declared_targets=frozenset({"Store"}))
    check_component_imports(component.read_bytes(), declared=frozenset(),
                            declared_targets=frozenset({"Admin"}))


def test_aggregate_component_component_level_imports_are_confined(tmp_path):
    # the aggregate wrapper emits component-level `import-type-*` plumbing;
    # it is read (not skipped) and accepted as the wrapper's own lift plumbing.
    _need_toolchain()
    canonical = _canonical()
    source = (BACKEND / "golden" / "canonical_aggregates.revl").read_text(
        encoding="utf-8")
    emitted = canonical.emit_component(compile_source(source), service="Registry")
    component = canonical.build_component(
        emitted["core_wat"], emitted["wit"], tmp_path, emitted["world"],
        name="registry")
    check_component_imports(component.read_bytes(), declared=frozenset())


# --------------------------------------------------------------------------- #
# 2. a confined component with limits emits and runs; limits are on by default.
# --------------------------------------------------------------------------- #

def test_confined_component_with_limits_runs(tmp_path):
    _need_toolchain()
    canonical = _canonical()
    component = _component(tmp_path)
    check_component_imports(component.read_bytes(), declared=frozenset())
    limits = ComponentLimits(fuel=1_000_000_000, memory_bytes=64 * 1024 * 1024,
                             wall_seconds=30)
    assert canonical.run_component(component, 'echo("world")',
                                   limits=limits) == '"world"'


def test_run_component_applies_limits_even_when_the_caller_passes_none(
        monkeypatch, tmp_path):
    canonical = _canonical()
    seen: dict[str, list[str]] = {}

    class _Done:
        returncode = 0
        stdout = '"ok"'
        stderr = ""

    def _run(argv, **kwargs):
        seen["argv"] = argv
        return _Done()

    component = tmp_path / "unused.wasm"
    component.write_bytes(b"\0asm\x0d\x00\x01\x00")
    monkeypatch.setattr(canonical, "wasmtime_binary", lambda: "wasmtime")
    monkeypatch.setattr(canonical.subprocess, "run", _run)
    monkeypatch.setattr(canonical, "check_component_imports",
                        lambda *_a, **_k: ())
    assert canonical.run_component(component, "go()") == '"ok"'
    argv = " ".join(seen["argv"])
    # the VALUES matter: a caller that passed nothing must still get the
    # documented default ceilings, not an unbounded invocation.
    assert "fuel=1000000000" in argv
    assert "max-memory-size=67108864" in argv
    assert "trap-on-grow-failure=y" in argv
    assert "timeout=120000ms" in argv


def test_run_component_uses_the_limits_it_is_given(monkeypatch, tmp_path):
    canonical = _canonical()
    seen: dict[str, list[str]] = {}

    class _Done:
        returncode = 0
        stdout = '"ok"'
        stderr = ""

    def _run(argv, **kwargs):
        seen["argv"] = argv
        return _Done()

    component = tmp_path / "unused.wasm"
    component.write_bytes(b"\0asm\x0d\x00\x01\x00")
    monkeypatch.setattr(canonical, "wasmtime_binary", lambda: "wasmtime")
    monkeypatch.setattr(canonical.subprocess, "run", _run)
    monkeypatch.setattr(canonical, "check_component_imports",
                        lambda *_a, **_k: ())
    limits = ComponentLimits(fuel=1234, memory_bytes=131_072,
                             wall_seconds=0.5)
    assert canonical.run_component(component, "go()", limits=limits) == '"ok"'
    argv = " ".join(seen["argv"])
    assert "fuel=1234" in argv
    assert "max-memory-size=131072" in argv
    assert "timeout=500ms" in argv


def test_fuel_exhaustion_traps_with_a_fuel_reason(tmp_path):
    _need_toolchain()
    canonical = _canonical()
    component = _spin_component(tmp_path)
    limits = ComponentLimits(fuel=100_000, memory_bytes=64 * 1024 * 1024,
                             wall_seconds=30)
    with pytest.raises(canonical.ComponentTrap) as caught:
        canonical.run_component(component, "spin()", limits=limits)
    assert caught.value.reason == "fuel"
    assert "fuel" in str(caught.value)


def test_memory_ceiling_traps_with_a_memory_reason(tmp_path):
    _need_toolchain()
    canonical = _canonical()
    component = _grow_component(tmp_path)
    limits = ComponentLimits(fuel=10_000_000_000, memory_bytes=131_072,
                             wall_seconds=30)
    with pytest.raises(canonical.ComponentTrap) as caught:
        canonical.run_component(component, "grow()", limits=limits)
    assert caught.value.reason == "memory"
    assert "memory" in str(caught.value)
    # the trap names the ceiling that was passed, not the default: the grow
    # loop asks for a third page past a two-page ceiling.
    assert "196608" in caught.value.detail


def test_wall_clock_ceiling_traps_with_a_wall_reason(tmp_path):
    _need_toolchain()
    canonical = _canonical()
    component = _spin_component(tmp_path)
    limits = ComponentLimits(fuel=10_000_000_000_000,
                             memory_bytes=64 * 1024 * 1024, wall_seconds=0.2)
    with pytest.raises(canonical.ComponentTrap) as caught:
        canonical.run_component(component, "spin()", limits=limits)
    assert caught.value.reason == "wall"
    assert "wall" in str(caught.value)


# a guest that never returns and a guest that allocates past the ceiling,
# both as standard components (component WAT parsed straight to a binary).
_SPIN_WAT = """(component
  (core module $m
    (memory (export "memory") 1)
    (func (export "spin") (loop $l br $l))
  )
  (core instance $i (instantiate $m))
  (func (export "spin") (canon lift (core func $i "spin")))
)
"""

_GROW_WAT = """(component
  (core module $m
    (memory (export "memory") 1)
    (func (export "grow") (result i32)
      (loop $l
        (i32.const 1) (memory.grow) (i32.const -1) (i32.eq)
        (if (then (unreachable)))
        (br $l))
      (i32.const 0))
  )
  (core instance $i (instantiate $m))
  (func (export "grow") (result u32) (canon lift (core func $i "grow")))
)
"""


def _guest(tmp_path: pathlib.Path, wat: str) -> pathlib.Path:
    source = tmp_path / "guest.wat"
    source.write_text(wat, encoding="utf-8")
    target = tmp_path / "guest.wasm"
    result = subprocess.run(
        ["wasm-tools", "parse", str(source), "-o", str(target)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return target


def _spin_component(tmp_path: pathlib.Path) -> pathlib.Path:
    return _guest(tmp_path, _SPIN_WAT)


def _grow_component(tmp_path: pathlib.Path) -> pathlib.Path:
    return _guest(tmp_path, _GROW_WAT)


# --------------------------------------------------------------------------- #
# 3. a policy that declares confinement but no limits refuses.
# --------------------------------------------------------------------------- #

def test_missing_limits_policy_refuses_with_a_named_error():
    policy = parse_policy("wasm components confined")
    assert policy.wasm_confined is True
    with pytest.raises(ConfinementError, match="limits"):
        limits_from_policy(policy)


def test_policy_limits_parse_in_dsl_and_json():
    dsl = parse_policy(
        "wasm components confined\n"
        "wasm component limits fuel = 500000, memory = 8MiB, wall = 5s")
    limits = limits_from_policy(dsl)
    assert limits == ComponentLimits(fuel=500_000,
                                     memory_bytes=8 * 1024 * 1024,
                                     wall_seconds=5.0)
    assert dsl.wasm_confined is True
    js = parse_policy(
        '{"wasm": {"confined": true, "limits": '
        '{"fuel": 500000, "memory": "8MiB", "wall": "5s"}}}')
    assert limits_from_policy(js) == limits
    assert js.wasm_confined is True
    # naming the ceilings IS the confinement switch in both spellings
    limits_only = parse_policy(
        "wasm component limits fuel = 500000, memory = 8MiB, wall = 5s")
    assert limits_only.wasm_confined is True


def test_an_invalid_limit_set_is_a_parse_error():
    from revl.policy import PolicyError

    with pytest.raises(PolicyError, match="missing"):
        parse_policy("wasm component limits fuel = 500000, memory = 8MiB")
    with pytest.raises(PolicyError, match="fuel"):
        parse_policy("wasm component limits fuel = 0, memory = 8MiB, wall = 5s")
    with pytest.raises(PolicyError, match="64 KiB"):
        parse_policy("wasm component limits fuel = 10, memory = 1000, wall = 5s")


def test_the_cli_render_names_a_refused_verdict():
    from revl.cli.change import _render_quarantine

    rendered = _render_quarantine({"verdict": "refused",
                                   "note": "unconfined"})
    assert "REFUSE" in rendered
    assert "refused" in rendered


def test_policy_absent_means_the_default_confinement():
    # no policy: a generated component still runs confined, never unbounded.
    limits = limits_from_policy(None)
    assert limits.fuel > 0
    assert limits.memory_bytes > 0
    assert limits.wall_seconds > 0


def test_quarantine_refuses_under_a_missing_limits_policy():
    # the live path: quarantine reads the policy's limits before building or
    # running anything, so the unnamed-ceiling policy is refused, not graded.
    from revl.mcp import quarantine as Q
    from revl.mcp.session import Session

    session = Session()
    session.sandbox = parse_policy("wasm components confined")
    report = Q.run(session, {"source": 'fn tag(s: Str) -> Str { return `[${s}]` }'})
    assert report["verdict"] == "refused", report
    assert "limits" in report["substrate"]["reason"]


_SUB_RESOURCE_WAT = """(component
  (import "import-type-t" (type (sub resource)))
  (core module $m (func (export "f")))
  (core instance $i (instantiate $m))
  (func (export "x") (canon lift (core func $i "f")))
)
"""


def test_a_component_level_sub_resource_import_is_decoded(tmp_path):
    # a wrapper's component-level type import can be `(sub resource)` (one
    # descriptor byte, `03 01`), not only an equality (`03 00 <idx>`); the
    # reader must consume both without misaligning the section.
    _need_toolchain()
    component = _guest(tmp_path, _SUB_RESOURCE_WAT)
    check_component_imports(component.read_bytes(), declared=frozenset())


def test_quarantine_grades_a_wall_trap_as_timeout():
    # issue #2729: the wall-clock ceiling is a `timeout` verdict, distinct from
    # the fuel trap. A huge fuel budget makes the 200 ms wall the first ceiling
    # the spinning candidate hits.
    _need_toolchain()
    from revl.mcp import quarantine as Q
    from revl.mcp.session import Session

    session = Session()
    session.sandbox = parse_policy(
        "wasm component limits fuel = 1000000000000, memory = 64MiB, wall = 200ms")
    report = Q.run(session, {
        "source": 'fn spin(s: Str) -> Str { while (1 == 1) { } return s }'})
    assert report["verdict"] == "timeout", report["substrate"]
    assert report["substrate"]["counts"]["timeout"] >= 1
