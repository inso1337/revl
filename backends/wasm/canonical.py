"""revl -> standard WASI Preview 2 component (canonical ABI) — item 41 slice-3.

Slices 1-2 emitted the WIT *interface* (``revl export wit``) and WIT resources.
This slice makes a revl program's pure ``Str``-boundary functions loadable by a
STANDARD component-model host (wasmtime, jco, Spin, wasmCloud) over the
CANONICAL ABI — not only by cordis-wasm's custom exported-``memory`` convention.

Scope (honest, per item 41 slice-3): the pure top-level functions whose
parameters are all ``Str`` and whose result is ``Str``. That is the smallest
coherent canonical-ABI slice — one ``Str``-taking, ``Str``-returning function
that a real component-model host loads and runs. Records / lists / variants
lift-lower at the canonical boundary is a follow-on slice; functions carrying
those types simply do not appear on the component interface here (they stay in
the core module for intra-module calls), so the gap is explicit, not silent.

How it works
------------
``_V3Emitter`` already lowers a pure ``Str`` function to a core function
``$f (export "f")`` of signature ``(param i32 …) (result i32)``, where every
value is an INTERNAL revl string pointer: ``[u32 byte_len][utf8 bytes]``. That
is the tier's custom in-memory ABI (a cordis-wasm host reads it through the
exported ``memory``). The canonical ABI a component host speaks is different:

  * a ``string`` crosses as a bare ``(ptr, len)`` pair — ``ptr`` points at the
    first byte, there is NO length prefix, ``len`` is the utf-8 byte count;
  * the host places an incoming string into guest memory by calling an exported
    ``cabi_realloc``; and
  * a ``string``-returning export returns a single ``i32`` pointing at an
    8-byte *return area* holding ``[ptr: i32, len: i32]``.

So this module keeps the ``_V3Emitter`` core body verbatim and adds, at the
export edge, the canonical boundary:

  * ``cabi_realloc(old, old_size, align, new_size) -> ptr`` — the standard
    allocator the host calls, backed by the same bump heap (``$__hp`` /
    ``$alloc``) the module already uses;
  * ``$__canon_lift_str(ptr, len) -> internal`` — LOWERS an incoming bare
    ``(ptr, len)`` into an internal ``[u32 len][bytes]`` string; and
  * one canonical export per boundary function, named
    ``<package>/<iface>#<op>`` (the core name ``wit-component`` expects for a
    component exporting that interface). It lifts each bare string param in,
    calls the internal ``$f``, then lowers the internal result back out into a
    fresh return area.

``build_component`` wraps the core module into a real component with
``wasm-tools component embed`` + ``component new``. The emitted WIT world
exports the interface ``revl export wit`` (slice-1) produces — reused verbatim
here — so the binary and the WIT agree by construction.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

# emit.py lives next to this file; import it as a sibling regardless of how the
# package is rooted (the wasm backend is loaded by path in several harnesses).
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from emit import EmitError, emit as _emit_core  # noqa: E402

# export_wit (slice-1) is the single source of the WIT interface shape; reuse it
# read-only so the component's exported interface is byte-identical to what
# `revl export wit` prints. It lives under src/revl.
_SRC = _HERE.parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from revl.export_wit import _kebab_name, _kebab_type, export_wit  # noqa: E402

_DEFAULT_PACKAGE = "revl:exported"


def _is_str(ty: object) -> bool:
    return ty == "Str"


def _boundary_functions(functions: list[dict]) -> list[dict]:
    """The pure functions whose whole signature is ``Str`` — the ones this slice
    can present over the canonical ABI. A function with a non-``Str`` parameter
    or result is left off the interface (it stays in the core module so a
    boundary function may still call it), never silently mis-lowered."""
    out = []
    for fn in functions:
        params = fn.get("params") or []
        if not _is_str(fn.get("returns")):
            continue
        if any(not _is_str(p.get("type")) for p in params):
            continue
        out.append(fn)
    return out


def _canon_helpers() -> str:
    """`cabi_realloc` (the host's allocator) + the bare->internal string lift,
    both backed by the module's existing `$alloc` / `$alloc_str` bump heap."""
    return (
        '  ;; --- canonical ABI boundary (item 41 slice-3) ---\n'
        '  ;; cabi_realloc: the standard allocator a component host calls to place\n'
        '  ;; an incoming string/list into this module\'s memory. Backed by the same\n'
        '  ;; bump heap ($__hp) the internal code uses; old/align are unused (a bump\n'
        '  ;; allocator never frees or re-aligns — every block is already 8-aligned).\n'
        '  (func (export "cabi_realloc")\n'
        '      (param $old i32) (param $old_size i32) (param $align i32) (param $new_size i32)\n'
        '      (result i32)\n'
        '    (call $alloc (local.get $new_size)))\n'
        '  ;; $__canon_lift_str: bare (ptr,len) canonical string -> internal\n'
        '  ;; [u32 len][bytes] revl string. Allocates via $alloc_str (len+4, len at\n'
        '  ;; offset 0) and copies the len utf-8 bytes in after the prefix.\n'
        '  (func $__canon_lift_str (param $ptr i32) (param $len i32) (result i32)\n'
        '    (local $s i32) (local $i i32)\n'
        '    (local.set $s (call $alloc_str (local.get $len)))\n'
        '    (block (loop\n'
        '      (br_if 1 (i32.ge_u (local.get $i) (local.get $len)))\n'
        '      (i32.store8\n'
        '        (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))\n'
        '        (i32.load8_u (i32.add (local.get $ptr) (local.get $i))))\n'
        '      (local.set $i (i32.add (local.get $i) (i32.const 1)))\n'
        '      (br 0)))\n'
        '    (local.get $s))'
    )


def _canon_export(fn: dict, package: str, iface: str) -> str:
    """One canonical-ABI export wrapping the internal `$<name>`.

    Lifts every bare-string parameter into an internal string, calls the
    internal function, then lowers the internal result string back out: the
    return value is a pointer to a fresh 8-byte return area holding
    ``[ptr -> first byte, len]`` (an internal string is ``[u32 len][bytes]`` so
    the byte pointer is ``q + 4`` and the length is the u32 at ``q``)."""
    name = fn.get("name")
    export_name = f"{package}/{iface}#{_kebab_name(name)}"
    params = fn.get("params") or []
    n = len(params)
    param_decls = " ".join(
        f"(param $c_p{i} i32) (param $c_p{i}_len i32)" for i in range(n))
    lift_locals = " ".join(f"(local $c_s{i} i32)" for i in range(n))
    lifts = "\n".join(
        f"    (local.set $c_s{i} "
        f"(call $__canon_lift_str (local.get $c_p{i}) (local.get $c_p{i}_len)))"
        for i in range(n))
    call_args = " ".join(f"(local.get $c_s{i})" for i in range(n))
    lift_block = (lifts + "\n") if lifts else ""
    return (
        f'  ;; canonical export of pure fn `{name}` -> WIT `{iface}#{_kebab_name(name)}`\n'
        f'  (func (export "{export_name}") {param_decls} (result i32)\n'
        f'    {lift_locals} (local $c_q i32) (local $c_ret i32)\n'
        f'{lift_block}'
        f'    (local.set $c_q (call ${name} {call_args}))\n'
        f'    (local.set $c_ret (call $alloc (i32.const 8)))\n'
        f'    (i32.store (local.get $c_ret) (i32.add (local.get $c_q) (i32.const 4)))\n'
        f'    (i32.store offset=4 (local.get $c_ret) (i32.load (local.get $c_q)))\n'
        f'    (local.get $c_ret))'
    )


def _core_with_canonical(ir: dict, boundary: list[dict], package: str,
                         iface: str) -> str:
    """The `_V3Emitter` core module for this IR, with the canonical boundary
    (cabi_realloc + lift helper + one export per boundary function) spliced in
    just before the module's closing paren."""
    modules = _emit_core({
        "ir_version": 3,
        "types": ir.get("types") or {},
        "functions": ir.get("functions") or [],
        "externs": ir.get("externs") or [],
        "tests": [],
    })
    core = modules.get("functions")
    if core is None:
        raise EmitError("no `functions` module was emitted for the canonical component")
    body = core.rstrip()
    if not body.endswith(")"):
        raise EmitError("unexpected core module shape (no closing paren to splice into)")
    trunk = body[:-1].rstrip("\n")
    additions = [_canon_helpers()] + [
        _canon_export(fn, package, iface) for fn in boundary]
    return trunk + "\n" + "\n".join(additions) + "\n)\n"


def emit_component(ir: dict, *, service: str,
                   package: str = _DEFAULT_PACKAGE) -> dict:
    """Lower a revl IR to a standard canonical-ABI component's inputs.

    Returns ``{"core_wat", "wit", "package", "interface", "world", "functions"}``:
    the core WAT (canonical exports + cabi_realloc), the WIT document (the
    slice-1 interface plus a world that exports it), and the names needed to
    wrap the two into a component with ``build_component``.

    ``service`` names the WIT interface the boundary functions are grouped under
    (the component's exported interface). Raises ``EmitError`` if the IR has no
    ``Str``-only pure function to present.
    """
    functions = ir.get("functions") or []
    boundary = _boundary_functions(functions)
    if not boundary:
        raise EmitError(
            "no canonical-ABI-emittable function: item 41 slice-3 exports the "
            "pure functions whose whole signature is `Str` (one Str-taking, "
            "Str-returning function is the minimal component). This IR has none "
            "— records/lists/variants at the canonical boundary are a follow-on "
            "slice."
        )
    iface = _kebab_type(service)

    # The WIT interface, produced by slice-1's exporter over a service view of
    # the boundary functions, so the component's interface is byte-identical to
    # `revl export wit`. A `world` that exports it is appended so wit-component
    # has an export target.
    synthetic = {
        "services": {service: {"methods": {
            fn["name"]: {"params": fn.get("params") or [],
                         "returns": fn.get("returns")}
            for fn in boundary}}},
        "types": ir.get("types") or {},
        "externs": [],
    }
    interface_wit = export_wit(synthetic, service=service, package=package)
    world_name = f"{iface}-component"
    wit = (
        interface_wit.rstrip()
        + "\n\n"
        + "/// The world wit-component wraps the core module against: it exports\n"
        + f"/// the `{iface}` interface above over the canonical ABI (item 41 slice-3).\n"
        + f"world {world_name} {{\n  export {iface};\n}}\n"
    )

    core_wat = _core_with_canonical(ir, boundary, package, iface)
    return {
        "core_wat": core_wat,
        "wit": wit,
        "package": package,
        "interface": iface,
        "world": world_name,
        "functions": [fn["name"] for fn in boundary],
    }


# --------------------------------------------------------------------------- #
# Component wrapping + execution — the standard toolchain, used by tests and by
# `tools/conformance.py`. Both tools degrade gracefully when the binaries are
# absent (the same policy the wasm tier's wasmtime gate already uses).
# --------------------------------------------------------------------------- #

def wasm_tools_binary() -> str | None:
    return shutil.which("wasm-tools")


def build_component(core_wat: str, wit: str, out_dir: pathlib.Path,
                    world: str, *, name: str = "component") -> pathlib.Path:
    """Wrap a canonical core module + WIT into a real WASI-P2 component.

    Runs ``wasm-tools parse`` -> ``component embed --world`` -> ``component
    new``. Returns the path to the ``.wasm`` component. Raises ``EmitError`` if
    ``wasm-tools`` is missing or any step fails (the caller decides skip vs
    fail)."""
    binary = wasm_tools_binary()
    if binary is None:
        raise EmitError("wasm-tools not found (needed to wrap the core module into a component)")
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    core_wat_path = out_dir / f"{name}.core.wat"
    core_wasm_path = out_dir / f"{name}.core.wasm"
    embedded_path = out_dir / f"{name}.embedded.wasm"
    component_path = out_dir / f"{name}.wasm"
    wit_dir = out_dir / "wit"
    wit_dir.mkdir(exist_ok=True)
    core_wat_path.write_text(core_wat, encoding="utf-8")
    (wit_dir / f"{name}.wit").write_text(wit, encoding="utf-8")

    def _run(args: list[str]) -> None:
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise EmitError(
                f"wasm-tools {args[0]} failed: "
                f"{result.stderr.strip() or result.stdout.strip()}")

    _run(["parse", str(core_wat_path), "-o", str(core_wasm_path)])
    _run(["component", "embed", str(wit_dir), "--world", world,
          str(core_wasm_path), "-o", str(embedded_path)])
    _run(["component", "new", str(embedded_path), "-o", str(component_path)])
    return component_path


def validate_component(component_path: pathlib.Path) -> None:
    """`wasm-tools validate --features all` — proves the binary is a valid
    component (component-model features on). Raises ``EmitError`` on failure."""
    binary = wasm_tools_binary()
    if binary is None:
        raise EmitError("wasm-tools not found")
    result = subprocess.run(
        [binary, "validate", "--features", "all", str(component_path)],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise EmitError(f"component failed validation: {result.stderr.strip()}")


def wasmtime_binary() -> str | None:
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = pathlib.Path.home() / ".wasmtime" / "bin" / "wasmtime"
    return str(fallback) if fallback.is_file() else None


def run_component_str(component_path: pathlib.Path, func: str, arg: str) -> str:
    """Invoke ``func(arg)`` on the component under wasmtime's COMPONENT MODEL
    (``wasmtime run --invoke``, which only accepts a component here) and return
    the string the canonical ABI lifted back. Proves the round trip end to end.
    """
    binary = wasmtime_binary()
    if binary is None:
        raise EmitError("wasmtime not found")
    result = subprocess.run(
        [binary, "run", "--invoke", f'{func}("{arg}")', str(component_path)],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise EmitError(f"component invocation failed: {result.stderr.strip()}")
    out = result.stdout.strip()
    # wasmtime prints the returned string as a quoted literal, e.g. "hi world".
    if len(out) >= 2 and out[0] == '"' and out[-1] == '"':
        return out[1:-1]
    return out
