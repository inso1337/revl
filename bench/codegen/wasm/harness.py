"""Codegen bench harness for the revl wasm backend.

Measures what the emitter PRODUCES, not how fast this machine is. Every
primary metric here is load-robust and reproducible on a busy box:

* ``bytes.core`` / ``bytes.component`` -- module size after ``wasm-tools``.
* ``funcs.defined`` / ``funcs.reachable`` / ``bytes.unreachable`` -- how much
  of the emitted helper prelude no export can ever call.
* ``fuel`` -- wasmtime execution fuel consumed by one ``--invoke`` call,
  found by bisection on ``-W fuel=N``. Fuel is a deterministic count of
  executed wasm operations, so it does not move with machine load. This is
  the number to quote, not wall clock.
* ``copies`` / ``allocs`` -- static counts of ``memory.copy`` and ``$alloc``
  call sites on the path a given export takes.

A/B against a hand-written alternative is done with VARIANTS: textual
rewrites of the emitted core WAT that stand in for a proposed emitter fix.
The emitter itself is never touched, so this stays an audit.

Usage:
    PYTHONPATH=<repo>/src python bench/codegen/wasm/run.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BACKEND = ROOT / "backends" / "wasm"
PROGRAMS = HERE / "programs"


def load_canonical():
    """Load ``backends/wasm/canonical.py`` under a private module name.

    The repo has several same-named ``emit.py`` modules; binding the bare
    name poisons ``sys.modules`` (roadmap item 419a). Load by path.
    """
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(
        "revl_bench_wasm_canonical", BACKEND / "canonical.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def have_toolchain() -> tuple[bool, str]:
    canon = load_canonical()
    if canon.wasm_tools_binary() is None:
        return False, "wasm-tools not on PATH"
    if canon.wasmtime_binary() is None:
        return False, "wasmtime not on PATH"
    return True, ""


# --------------------------------------------------------------------------
# emit
# --------------------------------------------------------------------------

def emit(program: pathlib.Path, service: str) -> dict:
    canon = load_canonical()
    sys.path.insert(0, str(ROOT / "src"))
    from revl import compile_source

    return canon.emit_component(
        compile_source(program.read_text(encoding="utf-8")), service=service)


# --------------------------------------------------------------------------
# static analysis of the emitted WAT
# --------------------------------------------------------------------------

_FUNC_DEF = re.compile(r'^\s*\(func\s+(?:(\$[\w:.#$-]+)\s*)?(?:\(export\s+"([^"]+)"\))?')
_CALL = re.compile(r'\(call\s+(\$[\w:.#$-]+)')


def _split_funcs(wat: str) -> list[tuple[str | None, str | None, str, int]]:
    """Split a WAT module into top-level ``(func ...)`` forms.

    Returns ``(name, export, text, byte_len)`` per function, by paren
    matching from each top-level ``(func`` at indent 2.
    """
    out: list[tuple[str | None, str | None, str, int]] = []
    i = 0
    n = len(wat)
    while True:
        j = wat.find("\n  (func", i)
        if j < 0:
            break
        start = j + 1
        depth = 0
        k = start
        in_str = False
        while k < n:
            ch = wat[k]
            if in_str:
                if ch == "\\":
                    k += 2
                    continue
                if ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == ";" and wat[k:k + 2] == ";;":
                k = wat.find("\n", k)
                if k < 0:
                    k = n
                continue
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        text = wat[start:k]
        head = _FUNC_DEF.match(text)
        name = head.group(1) if head else None
        export = head.group(2) if head else None
        if export is None:
            m = re.match(r'^\s*\(func\s+\$[\w:.#$-]+\s+\(export\s+"([^"]+)"\)', text)
            if m:
                export = m.group(1)
        out.append((name, export, text, len(text.encode("utf-8"))))
        i = k
    return out


def reachability(wat: str) -> dict:
    """Which emitted functions any export can actually reach."""
    funcs = _split_funcs(wat)
    by_name = {name: text for name, _e, text, _b in funcs if name}
    size = {name: b for name, _e, _t, b in funcs if name}
    roots = [name for name, exp, _t, _b in funcs if exp and name]
    # exported-but-unnamed funcs (cabi_realloc, the canonical exports) still
    # pull in callees, so scan them as roots too.
    anon_calls: set[str] = set()
    for name, exp, text, _b in funcs:
        if exp and not name:
            anon_calls |= set(_CALL.findall(text))
    seen: set[str] = set()
    stack = list(roots) + list(anon_calls)
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in by_name:
            continue
        seen.add(cur)
        stack.extend(_CALL.findall(by_name[cur]))
    defined = set(by_name)
    dead = sorted(defined - seen)
    return {
        "defined": len(defined),
        "reachable": len(seen),
        "dead": dead,
        "dead_bytes": sum(size[d] for d in dead),
        "total_func_bytes": sum(size.values()),
    }


def path_costs(wat: str, entry: str) -> dict:
    """Static ``memory.copy`` / ``$alloc`` / byte-loop counts on the call
    graph rooted at the export named ``entry``."""
    funcs = _split_funcs(wat)
    by_name = {name: text for name, _e, text, _b in funcs if name}
    root_text = None
    for name, exp, text, _b in funcs:
        if exp == entry:
            root_text = text
            break
    if root_text is None:
        raise KeyError(f"no export named {entry!r}")
    seen: set[str] = set()
    texts = [root_text]
    stack = list(_CALL.findall(root_text))
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in by_name:
            continue
        seen.add(cur)
        texts.append(by_name[cur])
        stack.extend(_CALL.findall(by_name[cur]))
    blob = "\n".join(texts)
    return {
        "memory_copy_sites": blob.count("memory.copy"),
        "alloc_calls": blob.count("(call $alloc"),
        "str_concat_calls": blob.count("(call $str_concat)"),
        "byte_loop_stores": blob.count("i32.store8"),
        "funcs_on_path": len(seen) + 1,
    }


# --------------------------------------------------------------------------
# build + run
# --------------------------------------------------------------------------

def build(core_wat: str, wit: str, world: str, out_dir: pathlib.Path,
          name: str) -> pathlib.Path:
    canon = load_canonical()
    return canon.build_component(core_wat, wit, out_dir, world, name=name)


def core_wasm_bytes(core_wat: str, out_dir: pathlib.Path, name: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    wat_path = out_dir / f"{name}.probe.wat"
    wasm_path = out_dir / f"{name}.probe.wasm"
    wat_path.write_text(core_wat, encoding="utf-8")
    subprocess.run(
        [shutil.which("wasm-tools"), "parse", str(wat_path), "-o", str(wasm_path)],
        check=True, capture_output=True, timeout=120)
    return wasm_path.stat().st_size


def _wasmtime() -> str:
    return load_canonical().wasmtime_binary()


def invoke(component: pathlib.Path, expr: str, fuel: int | None = None):
    """Run one component export. Returns ``(ok, out_of_fuel, stdout)``."""
    cmd = [_wasmtime(), "run"]
    if fuel is not None:
        cmd += ["-W", f"fuel={fuel}"]
    cmd += ["--invoke", expr, str(component)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode == 0:
        return True, False, res.stdout.strip()
    err = (res.stderr or "") + (res.stdout or "")
    return False, ("fuel" in err.lower()), err.strip()


def fuel_cost(component: pathlib.Path, expr: str, hi: int = 1 << 30) -> int:
    """Exact fuel consumed by one ``--invoke``, by bisection.

    Fuel is a deterministic operation count: the same module and the same
    argument consume the same fuel on any machine, under any load. That is
    what makes this the honest number to compare.

    CAVEAT, measured not assumed: ``wasmtime run --invoke`` meters
    INSTANTIATION as well as the call, and a module carrying `(data ...)`
    segments pays a fixed ~16385-fuel instantiation charge (probe: adding
    two 12-byte data segments to an otherwise identical module moved a
    16-byte `echo` from 380 to 16765 fuel). So a raw total is
    instantiation-inclusive. Compare variants with ``fuel_delta`` (exact:
    instantiation is identical across variants of one program) and scaling
    with ``fuel_slope`` (exact: instantiation is constant in n).
    """
    ok, _oof, out = invoke(component, expr)
    if not ok:
        raise RuntimeError(f"invoke failed without fuel metering: {out[:400]}")
    lo = 0
    ok_hi, oof_hi, _ = invoke(component, expr, fuel=hi)
    if not ok_hi:
        raise RuntimeError("even the fuel ceiling was not enough")
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        ok_m, oof_m, out_m = invoke(component, expr, fuel=mid)
        if ok_m:
            hi = mid
        elif oof_m:
            lo = mid
        else:
            raise RuntimeError(f"non-fuel failure at fuel={mid}: {out_m[:300]}")
    return hi


# --------------------------------------------------------------------------
# variants: stand-ins for proposed emitter fixes, applied to emitted WAT
# --------------------------------------------------------------------------

#: What the emitter writes today for the canonical string lift: a
#: byte-at-a-time copy loop.
LIFT_LOOP = """  (func $__canon_lift_str (param $ptr i32) (param $len i32) (result i32)
    (local $s i32) (local $i i32)
    (local.set $s (call $alloc_str (local.get $len)))
    (block (loop
      (br_if 1 (i32.ge_u (local.get $i) (local.get $len)))
      (i32.store8
        (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))
        (i32.load8_u (i32.add (local.get $ptr) (local.get $i))))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br 0)))
    (local.get $s))"""

#: What a competent wasm author writes instead. `memory.copy` is already in
#: this module's instruction budget -- `$str_concat`, `$str_slice`,
#: `$list_push` and friends all use it.
LIFT_MEMCOPY = """  (func $__canon_lift_str (param $ptr i32) (param $len i32) (result i32)
    (local $s i32)
    (local.set $s (call $alloc_str (local.get $len)))
    (memory.copy
      (i32.add (local.get $s) (i32.const 4))
      (local.get $ptr)
      (local.get $len))
    (local.get $s))"""


def variant_memcopy_lift(core_wat: str) -> str:
    if LIFT_LOOP not in core_wat:
        raise RuntimeError("lift loop not found -- emitter shape changed")
    return core_wat.replace(LIFT_LOOP, LIFT_MEMCOPY)


#: The ceiling: no copy at all. A canonical `string` param reaches a
#: linear-memory callee in memory the callee itself handed out through
#: `cabi_realloc`, so if `cabi_realloc` reserves 8 bytes of headroom and
#: returns `p+8`, the incoming bytes already sit exactly where the
#: internal `[u32 len][bytes]` layout wants them. Lifting is then one
#: store of the length -- O(1) in the string, not O(n).
ZEROCOPY_REALLOC = ("    (i32.add (call $alloc (i32.add (local.get $new_size) "
                    "(i32.const 8))) (i32.const 8)))")
ZEROCOPY_LIFT = """  (func $__canon_lift_str (param $ptr i32) (param $len i32) (result i32)
    (i32.store (i32.sub (local.get $ptr) (i32.const 4)) (local.get $len))
    (i32.sub (local.get $ptr) (i32.const 4)))"""


def variant_zerocopy_lift(core_wat: str) -> str:
    if "    (call $alloc (local.get $new_size)))" not in core_wat:
        raise RuntimeError("cabi_realloc not found -- emitter shape changed")
    out = core_wat.replace("    (call $alloc (local.get $new_size)))",
                           ZEROCOPY_REALLOC, 1)
    return _replace_func(out, "$__canon_lift_str", ZEROCOPY_LIFT)


def variant_prune_dead(core_wat: str) -> str:
    """Drop every emitted function no export can reach.

    Stands in for gating the helper prelude on use, the way ``$str_split`` /
    ``$str_join`` / ``$str_index_of`` are already gated.
    """
    info = reachability(core_wat)
    out = core_wat
    for name, _exp, text, _b in _split_funcs(core_wat):
        if name in set(info["dead"]):
            out = out.replace("\n" + text, "", 1)
    return out


def variant_static_return_area(core_wat: str) -> str:
    """Serve the 8-byte canonical return area from one module-level cell
    instead of bumping the heap on every call.

    The component host reads the return area before the next call into the
    instance, so one static cell is enough for a single-threaded instance.
    """
    m = re.search(r"\(global \$__hp \(mut i32\) \(i32\.const (\d+)\)\)", core_wat)
    if m is None:
        raise RuntimeError("heap global not found -- emitter shape changed")
    base = int(m.group(1))
    out = core_wat.replace(
        m.group(0),
        f"(global $__hp (mut i32) (i32.const {base + 16}))\n"
        f"  (global $__ret_area i32 (i32.const {base}))")
    out = out.replace(
        "(local.set $area (call $alloc (i32.const 8)))",
        "(local.set $area (global.get $__ret_area))")
    return out


def _concat_n_helper(k: int) -> str:
    """A hand-written k-ary concat: one length pass, one allocation, one
    ``memory.copy`` per part. What a competent author writes for a k-part
    template literal."""
    params = " ".join(f"(param $a{i} i32)" for i in range(k))
    locals_ = " ".join(f"(local $l{i} i32)" for i in range(k))
    lines = [f"  (func $str_concat{k} {params} (result i32)",
             f"    {locals_} (local $p i32) (local $o i32)"]
    for i in range(k):
        lines.append(f"    (local.set $l{i} (i32.load (local.get $a{i})))")
    total = "(local.get $l0)"
    for i in range(1, k):
        total = f"(i32.add {total} (local.get $l{i}))"
    lines.append(f"    (local.set $p (call $alloc_str {total}))")
    lines.append("    (local.set $o (i32.add (local.get $p) (i32.const 4)))")
    for i in range(k):
        lines.append(
            f"    (memory.copy (local.get $o) "
            f"(i32.add (local.get $a{i}) (i32.const 4)) (local.get $l{i}))")
        if i < k - 1:
            lines.append(
                f"    (local.set $o (i32.add (local.get $o) (local.get $l{i})))")
    lines.append("    (local.get $p))")
    return "\n".join(lines)


_CHAIN_STEP = re.compile(r"\n      (\(.*?\))\n      \(call \$str_concat\)")


def variant_fused_concat(core_wat: str) -> str:
    """Collapse every emitted left-fold `$str_concat` chain into one k-ary
    concat: one allocation and one copy per part, instead of k-1
    allocations that recopy the growing prefix each step.
    """
    out = core_wat
    arities: set[int] = set()
    for name, _exp, text, _b in _split_funcs(core_wat):
        if "(call $str_concat)" not in text:
            continue
        head, _sep, body = text.partition(")\n")
        steps = _CHAIN_STEP.findall(body)
        if not steps:
            continue
        first = body.split("\n", 1)[0].strip()
        parts = [first, *steps]
        arities.add(len(parts))
        rebuilt = (head + ")\n    " + parts[0] + "\n"
                   + "\n".join(f"      {p}" for p in parts[1:])
                   + f"\n      (call $str_concat{len(parts)})\n    return)")
        out = out.replace(text, rebuilt, 1)
    if not arities:
        raise RuntimeError("no concat chain found -- emitter shape changed")
    helpers = "\n".join(_concat_n_helper(k) for k in sorted(arities)) + "\n"
    return out.replace("  (func $str_concat ", helpers + "  (func $str_concat ", 1)


#: For an element type whose CANONICAL layout is byte-identical to the
#: internal 8-byte slot -- `Int` is the case that exists today -- the
#: emitted element-at-a-time lift/lower loops are provably one
#: `memory.copy`. The lowered body needs no scratch buffer at all: the
#: internal list body at `r+8` is already exactly the canonical array.
LIST_INT_MEMCOPY = """  (func $__canon_lift_list_Int (param $p i32) (param $len i32) (result i32)
    (local $r i32)
    (local.set $r (call $alloc (i32.add (i32.const 8) (i32.mul (local.get $len) (i32.const 8)))))
    (i32.store (local.get $r) (local.get $len))
    (memory.copy (i32.add (local.get $r) (i32.const 8)) (local.get $p)
                 (i32.mul (local.get $len) (i32.const 8)))
    (local.get $r))"""

LIST_INT_ALIAS_LOWER = """  (func $__canon_lower_list_Int (param $r i32) (param $dst i32)
    (i32.store (local.get $dst) (i32.add (local.get $r) (i32.const 8)))
    (i32.store offset=4 (local.get $dst) (i32.load (local.get $r))))"""


def _replace_func(core_wat: str, name: str, replacement: str) -> str:
    for fname, _exp, text, _b in _split_funcs(core_wat):
        if fname == name:
            return core_wat.replace(text, replacement.lstrip(), 1)
    raise RuntimeError(f"{name} not found -- emitter shape changed")


def variant_list_bulk(core_wat: str) -> str:
    """`List[Int]` across the boundary with bulk memory instead of a
    per-element loop, and with the lowered side aliasing the already-
    canonical internal body instead of allocating and refilling a copy."""
    out = _replace_func(core_wat, "$__canon_lift_list_Int", LIST_INT_MEMCOPY)
    return _replace_func(out, "$__canon_lower_list_Int", LIST_INT_ALIAS_LOWER)


def variant_combined(core_wat: str) -> str:
    """Every proposed fix at once, which is how they would actually ship."""
    out = variant_zerocopy_lift(core_wat)
    out = variant_static_return_area(out)
    for extra in (variant_fused_concat, variant_list_bulk):
        try:
            out = extra(out)
        except RuntimeError:
            pass  # that shape is not in this program
    return variant_prune_dead(out)


VARIANTS = {
    "memcopy_lift": variant_memcopy_lift,
    "zerocopy_lift": variant_zerocopy_lift,
    "prune_dead": variant_prune_dead,
    "static_return_area": variant_static_return_area,
    "fused_concat": variant_fused_concat,
    "list_bulk": variant_list_bulk,
    "combined": variant_combined,
}
