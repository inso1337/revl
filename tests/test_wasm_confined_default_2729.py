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
mutate the produced component's embedded core module (or synthesize the
component's framing directly) to prove the new check reads what actually runs.

The admission tests below build the component framing BY HAND. That is
deliberate: the check under test reads the import section, so a hand-built
component is the same input a mutated real one produces, needs no wasm-tools or
wasmtime, and therefore does not skip in any job that collects this file. The
toolchain-gated tests that remain are the ones that need a real component built
and executed; CI's `backend-wasm` job installs both tools and runs this file
with `REVL_REQUIRE_WASMTIME=1`, so even those never skip there.
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


def _encode_name(text: str) -> bytes:
    data = text.encode("utf-8")
    return _uleb(len(data)) + data


def _core_with_import(module: str, field: str) -> bytes:
    """A minimal core module whose import section names exactly one import."""
    payload = (_uleb(1) + _encode_name(module) + _encode_name(field)
               + b"\x00" + _uleb(0))          # func, type index 0
    return b"\0asm\x01\0\0\0" + b"\x02" + _uleb(len(payload)) + payload


def _component_with_core(core: bytes) -> bytes:
    return b"\0asm\x0d\x00\x01\x00" + b"\x01" + _uleb(len(core)) + core


def _import_component(module: str, field: str) -> bytes:
    """A component whose embedded core module carries one injected import.

    No toolchain: this is the same framing `wasm-tools component new` produces
    for the module, and the check reads the bytes rather than the emitter."""
    return _component_with_core(_core_with_import(module, field))


def _component_level(component: bytes, name: str, sort: int,
                     descriptor: bytes = b"\x01",
                     name_tag: int = 0x00) -> bytes:
    """Append a component-level import section to `component`'s bytes.

    `descriptor` is the sort-specific body that follows the tag; the default
    (`03 01`) is a type import with a `sub resource` bound. `name_tag` is the
    extern-name kind that precedes the name; the standard one is `0x00`. The
    parser under test reads framing, name kind, name and sort, so this is the
    exact shape it sees."""
    payload = (_uleb(1) + bytes([name_tag]) + _encode_name(name)
               + bytes([sort]) + descriptor)
    return component + b"\x0a" + _uleb(len(payload)) + payload


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


def test_a_capability_import_is_checked_by_its_composed_address():
    # The bug: `coeffect:tenantb/kv` was admitted whenever `kv` was declared,
    # because the realm was stripped before the comparison. A capability is
    # the full address; the realm is part of the authority.
    imported = _import_component("coeffect:tenantb/kv", "put")
    check_component_imports(imported, declared=frozenset({"kv"}),
                            isolate={"kv": "tenantb"})
    with pytest.raises(ConfinementError, match="composed address"):
        check_component_imports(imported, declared=frozenset({"kv"}),
                                isolate={"kv": "tenanta"})
    with pytest.raises(ConfinementError, match="composed address"):
        # no placement means the shared realm; a realm-qualified import is not
        # that address.
        check_component_imports(imported, declared=frozenset({"kv"}))


def test_an_isolated_component_may_not_import_the_shared_realm():
    # The converse mismatch: a component placed in `tenanta` must import
    # `coeffect:tenanta/kv`, never the shared `coeffect:kv`.
    imported = _import_component("coeffect:kv", "put")
    with pytest.raises(ConfinementError, match="composed address"):
        check_component_imports(imported, declared=frozenset({"kv"}),
                                isolate={"kv": "tenanta"})


def test_a_declared_star_does_not_make_the_key_check_vacuous():
    # `*` is the unnameable boundary: it cannot prove that a NAMED import is
    # inside it. Only a concrete declaration covers a concrete address.
    imported = _import_component("coeffect:kv", "put")
    with pytest.raises(ConfinementError, match="composed address"):
        check_component_imports(imported, declared=frozenset({"*"}))
    check_component_imports(imported, declared=frozenset({"*", "kv"}))


def test_a_route_import_is_checked_by_its_composed_address():
    imported = _import_component("route:kv", "live")
    check_component_imports(imported, declared=frozenset({"kv"}))
    with pytest.raises(ConfinementError, match="composed address"):
        check_component_imports(imported, declared=frozenset())
    # a routed require has no single-realm address: the emitter renders
    # `route:<key>`, so a realm-qualified route import is not that address.
    with pytest.raises(ConfinementError, match="composed address"):
        check_component_imports(_import_component("route:tenantb/kv", "live"),
                                declared=frozenset({"kv"}),
                                isolate={"kv": "tenantb"})


def test_the_policy_allow_list_bounds_a_declared_import():
    imported = _import_component("coeffect:kv", "put")
    check_component_imports(imported, declared=frozenset({"kv"}),
                            allowed=("kv",))
    with pytest.raises(ConfinementError, match="policy does not allow"):
        check_component_imports(imported, declared=frozenset({"kv"}),
                                allowed=("other",))
    with pytest.raises(ConfinementError, match="policy does not allow"):
        check_component_imports(imported, declared=frozenset({"kv"}),
                                allowed=())


def test_a_spawn_import_must_name_a_target_and_the_new_field():
    imported = _import_component("spawn:Admin", "new")
    check_component_imports(imported, declared=frozenset(),
                            declared_targets=frozenset({"Admin"}))
    with pytest.raises(ConfinementError, match="Admin"):
        check_component_imports(imported, declared=frozenset(),
                                declared_targets=frozenset({"Store"}))
    with pytest.raises(ConfinementError, match="new"):
        check_component_imports(_import_component("spawn:Admin", "evil"),
                                declared=frozenset(),
                                declared_targets=frozenset({"Admin"}))
    with pytest.raises(ConfinementError, match="sibling"):
        check_component_imports(_import_component("spawn:Admin/extra", "new"),
                                declared=frozenset(),
                                declared_targets=frozenset({"Admin", "extra"}))


def test_an_instance_import_must_name_a_target_and_a_key_dot_method_field():
    imported = _import_component("instance:Admin", "secret.read")
    check_component_imports(imported, declared=frozenset(),
                            declared_targets=frozenset({"Admin"}))
    with pytest.raises(ConfinementError, match="Admin"):
        check_component_imports(imported, declared=frozenset(),
                                declared_targets=frozenset({"Store"}))
    with pytest.raises(ConfinementError, match="key"):
        check_component_imports(_import_component("instance:Admin", "secret"),
                                declared=frozenset(),
                                declared_targets=frozenset({"Admin"}))
    with pytest.raises(ConfinementError, match="sibling"):
        check_component_imports(
            _import_component("instance:Admin/extra", "secret.read"),
            declared=frozenset(),
            declared_targets=frozenset({"Admin", "extra"}))


def test_structural_seam_fields_are_the_known_ones():
    for module, field in (("host", "job_run"), ("dispose", "instance"),
                          ("coeffect:revl:wal", "record")):
        check_component_imports(_import_component(module, field),
                                declared=frozenset())
    for module, field in (("host", "evil"), ("dispose", "evil"),
                          ("coeffect:revl:wal", "evil")):
        with pytest.raises(ConfinementError, match="structural"):
            check_component_imports(_import_component(module, field),
                                    declared=frozenset())
    # a config field is bound only when the caller declares it; an ordinary
    # component has no config surface, so the default is empty and refuses.
    with pytest.raises(ConfinementError, match="structural"):
        check_component_imports(_import_component("config", "admin"),
                                declared=frozenset())
    check_component_imports(_import_component("config", "admin"),
                            declared=frozenset(),
                            config_fields=("admin",))


def test_component_level_import_evil_is_refused():
    # `import-*` is not a prefix that makes an import plumbing: `import-evil`
    # is an import like any other and is refused.
    evil = _component_level(_import_component("host", "job_run"),
                            "import-evil", 0x03)
    with pytest.raises(ConfinementError, match="COMPONENT level"):
        check_component_imports(evil, declared=frozenset())


def test_component_level_plumbing_name_must_match_its_sort():
    # A type import called `import-func-*` is a spoof of the function family.
    spoof = _component_level(_import_component("host", "job_run"),
                             "import-func-t", 0x03)
    with pytest.raises(ConfinementError, match="import-type"):
        check_component_imports(spoof, declared=frozenset())


def test_component_level_lift_plumbing_is_accepted():
    # the wrapper's real plumbing, in both families, is accepted.
    check_component_imports(
        _component_level(_import_component("host", "job_run"),
                         "import-type-t", 0x03),
        declared=frozenset())
    check_component_imports(
        _component_level(_import_component("host", "job_run"),
                         "import-func-f", 0x01, descriptor=b"\x00"),
        declared=frozenset())


def test_a_nonstandard_component_level_name_kind_is_refused():
    # wasm-tools emits extern-name kind 0x00; any other kind is an import shape
    # this checker cannot read, so it is refused rather than skipped.
    weird = _component_level(_import_component("host", "job_run"),
                             "import-type-t", 0x03, name_tag=0x01)
    with pytest.raises(ConfinementError, match="extern-name"):
        check_component_imports(weird, declared=frozenset())


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
# 2b. trap classification: a ceiling, a real trap, an instantiation failure and
#     a plain non-zero exit are different reasons, decided from wasmtime's text.
# --------------------------------------------------------------------------- #

def test_trap_reasons_are_classified_separately():
    canonical = _canonical()
    classify = canonical._classify_failure
    assert classify("wasm trap: all fuel consumed by WebAssembly") == "fuel"
    assert classify("wasm trap: failed to grow memory from 1 to 2 pages") == "memory"
    assert classify("wasm trap: memory growth exceeds memory type's limits") == "memory"
    assert classify("wasm trap: Memory maximum size exceeded") == "memory"
    assert classify("wasm trap: interrupt") == "wall"
    assert classify("wasm trap: integer overflow") == "trap"
    assert classify("failed to instantiate: unknown import `env.secret`") == "instantiate"
    # a message that merely contains the word is NOT a wall-clock ceiling: the
    # old substring match made every "timeout" a wall trap.
    assert classify("error: /tmp/timeout-artifacts: no such file") == "exit"


def test_an_instantiation_failure_does_not_claim_a_trap_ceiling():
    canonical = _canonical()
    error = canonical.ComponentTrap("instantiate",
                                    "unknown import `host.secret`")
    assert "did not instantiate" in str(error)
    assert "trap ceiling" not in str(error)


# --------------------------------------------------------------------------- #
# 2c. the quarantine battery: traps are graded, refusals are not swallowed, and
#     the policy's allow-list and the candidate's realm placement are supplied.
# --------------------------------------------------------------------------- #

class _FakeEmitError(Exception):
    pass


class _FakeTrap(_FakeEmitError):
    def __init__(self, reason, detail):
        self.reason = reason
        super().__init__(detail)


class _FakeCanonical:
    """The slice of `backends/wasm/canonical.py` `_run_substrate` calls.

    The flow under test is the battery's accounting and its forwarding of the
    confinement inputs, not wasmtime; a stub lets it run with no toolchain."""

    EmitError = _FakeEmitError
    ComponentTrap = _FakeTrap

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def emit_component(self, ir, *, service):
        return {"core_wat": "", "wit": "", "world": "",
                "package": "revl:candidate", "interface": "go",
                "functions": ["go"]}

    def wasm_tools_binary(self):
        return "wasm-tools"

    def wasmtime_binary(self):
        return "wasmtime"

    def build_component(self, core_wat, wit, out_dir, world, name="x"):
        return pathlib.Path("component.wasm")

    def validate_component(self, component):
        return None

    def run_component_str(self, component, func, arg, **kwargs):
        self.calls.append(kwargs)
        if self.outcome == "trap":
            raise self.ComponentTrap("trap", "wasm trap: integer overflow")
        if self.outcome == "instantiate":
            raise self.ComponentTrap(
                "instantiate", "wasm trap: failed to instantiate component")
        if self.outcome == "refuse":
            raise ConfinementError("the composed address is not declared")
        return "ok"


_ISOLATED_SOURCE = """
service Kv { fn get(k: Str) -> Str }
component Store requires kv: Kv provides store: Kv {
  isolate kv in realm("tenantb")
  provide store { fn get(k) = "x" }
}
"""


def _isolated_ir():
    return compile_source(_ISOLATED_SOURCE), "Kv"


def test_the_battery_forwards_the_allow_list_and_the_realm_placement():
    from revl.mcp import quarantine as Q

    ir, service = _isolated_ir()
    policy = parse_policy("component Store may reach kv")
    fake = _FakeCanonical("return")
    substrate = Q._run_substrate(fake, ir, service, policy=policy)

    assert substrate["status"] == "passed", substrate
    assert fake.calls
    assert fake.calls[0]["allowed"] == ("kv",)
    assert fake.calls[0]["isolate"] == {"kv": "tenantb"}


def test_a_trap_in_a_probe_is_graded_not_raised():
    from revl.mcp import quarantine as Q

    ir, service = _isolated_ir()
    fake = _FakeCanonical("trap")
    substrate = Q._run_substrate(fake, ir, service)

    assert substrate["status"] == "trapped", substrate
    assert substrate["counts"]["trapped"] == len(fake.calls)


def test_an_instantiation_failure_is_not_graded_as_a_trap():
    # The old classifier called any non-zero exit a `trap`, so a component that
    # never instantiated was reported as a contained runtime escape. Nothing
    # ran, so the battery could not run it: unavailable, not trapped.
    from revl.mcp import quarantine as Q

    ir, service = _isolated_ir()
    fake = _FakeCanonical("instantiate")
    substrate = Q._run_substrate(fake, ir, service)

    assert substrate["status"] == "unavailable", substrate
    assert substrate["ran"] is False
    assert "did not instantiate" in substrate["reason"]
    assert substrate["counts"]["trapped"] == 0


def test_a_confinement_refusal_reaches_the_refused_verdict(monkeypatch):
    # A refusal is the byte check refusing the produced component before
    # anything ran. It must reach `run`, which maps it to `refused`; it must
    # never be graded as a guest outcome.
    from revl.mcp import quarantine as Q
    from revl.mcp.session import Session

    fake = _FakeCanonical("refuse")
    monkeypatch.setattr(Q, "_load_canonical", lambda: fake)
    report = Q.run(Session(), {"source": 'fn tag(s: Str) -> Str { return s }'})

    assert report["verdict"] == "refused", report
    assert report["substrate"]["reason"]


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
