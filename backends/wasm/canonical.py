"""revl -> standard WASI Preview 2 component (canonical ABI) — item 41.

Slices 1-2 emitted the WIT *interface* (``revl export wit``) and WIT resources.
Slice-3 made a revl program's pure ``Str``-boundary functions loadable by a
STANDARD component-model host (wasmtime, jco, Spin, wasmCloud) over the
CANONICAL ABI. This module is the aggregate follow-on: it extends that
canonical boundary from ``Str`` alone to the full value surface the wasm tier
lowers internally — non-``Str`` scalars (``Int`` -> ``s64``, ``Bool``),
**records**, **lists**, and **variants** (user variants, ``Opt``, ``Result``) —
so a component-model host can call a revl ``fn`` that takes or returns any of
them, not just strings.

Two ABIs, and the bridge between them
-------------------------------------
``_V3Emitter`` (``emit.py``) lowers each pure ``fn`` to a core function whose
values use the tier's INTERNAL, uniform in-memory ABI:

  * ``Int``  — an ``i64`` value; ``Bool`` — an ``i32`` (0/1) value;
  * ``Str``  — an ``i32`` pointer to ``[u32 byte_len][utf8 bytes]``;
  * record   — an ``i32`` pointer to ``[slot0][slot1]…`` (one 8-byte slot per
    field, in declaration order; an ``Int`` sits native, a pointer/``Bool`` is
    zero-extended into the slot);
  * ``List[T]`` — an ``i32`` pointer to ``[u32 count][pad][slot0]…``;
  * variant/``Opt``/``Result`` — an ``i32`` pointer to
    ``[u32 tag][pad][payload slot]``.

The CANONICAL ABI a component host speaks is the Component Model's: values are
*flattened* into core params, aggregates are laid out in linear memory with
canonical size/alignment, ``string``/``list`` cross as a bare ``(ptr, len)``
pair, and a result that flattens to more than one core value is returned
INDIRECTLY as a single ``i32`` pointer to a canonically-laid-out return area.
The host places incoming buffers into guest memory through an exported
``cabi_realloc``.

This module keeps every ``_V3Emitter`` body verbatim and, at the export edge,
emits a canonical wrapper per boundary function plus a small library of
lift/lower helpers (``$__canon_*``) that translate between the two ABIs over
the SAME bump heap (``$__hp`` / ``$alloc``) slice-3 established:

  * ``cabi_realloc`` — the host's allocator, backed by ``$alloc``;
  * ``$__canon_lift_str`` / ``$__canon_lower_str`` — bare ``(ptr,len)`` <->
    internal ``[u32 len][bytes]``;
  * ``$__canon_lift_rec_*`` / ``$__canon_lower_rec_*``, ``…_list_*``,
    ``…_var_*`` — one memoized pair per aggregate type reached, recursing
    through ``load_canon`` / ``store_canon``.

``build_component`` wraps the core module into a real component with
``wasm-tools component embed`` + ``component new``; the WIT world it embeds is
``revl export wit``'s output (slice-1) verbatim, so the binary and the
interface agree by construction, proven under ``wasmtime run --invoke``.

Boundary scope (explicit, not silent): a function is presented over the
canonical ABI iff every parameter and its result is canonically lowerable. A
function carrying a type this module cannot lower (``Float``, ``Map``, a
resource handle, a function value) — or a *parameter* that is a variant whose
payload is itself an aggregate (the direct-flattened join of an aggregate
payload is the remaining gap; such a variant is fine as a *result*, which
crosses through memory) — is left off the interface and stays in the core
module for intra-module calls, never mis-lowered.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

# emit.py lives next to this file. Every backend ships its own `emit.py`, so a
# bare `import emit` binds the CANONICAL name `emit` in `sys.modules` to
# whichever backend won the race — in a combined pytest process a `tests/`
# suite that does `import emit` for another backend (e.g. the python emitter,
# whose `emit()` returns a *str*) poisons that name, and this module would then
# bind `_emit_core` to the wrong renderer and blow up with `'str' object has no
# attribute 'get'` (items 98/150). Load our sibling by PATH under a unique,
# per-path-cached module name so the binding is order-independent regardless of
# what `import emit` did elsewhere. This is the same discipline
# `tests/_backend_import.py` uses; sharing the `revl_wasm_emit` name means a
# combined run still executes the wasm emitter exactly once.
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _load_wasm_emit():
    name = "revl_wasm_emit"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, _HERE / "emit.py")
        assert spec is not None and spec.loader is not None, _HERE / "emit.py"
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_emit_mod = _load_wasm_emit()
EmitError = _emit_mod.EmitError
_emit_core = _emit_mod.emit

# export_wit (slice-1) is the single source of the WIT interface shape; reuse it
# read-only so the component's exported interface is byte-identical to what
# `revl export wit` prints. It lives under src/revl.
_SRC = _HERE.parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from revl.export_wit import (  # noqa: E402
    _kebab_name, _kebab_type, _split_generic, export_wit)

_DEFAULT_PACKAGE = "revl:exported"

# The Component Model's canonical-ABI flattening limits (CABI constants).
_MAX_FLAT_PARAMS = 16
_MAX_FLAT_RESULTS = 1

# Bytes `cabi_realloc` reserves BELOW every buffer it hands the host (item
# 432a). The internal string layout puts a u32 length in front of the bytes, so
# 4 would do; 8 keeps the returned pointer on `$alloc`'s 8-byte slot grid.
_STR_HEADROOM = 8


def _align_to(offset: int, align: int) -> int:
    return (offset + align - 1) & ~(align - 1)


def _san(ty: str) -> str:
    """A revl type string -> a wat-identifier-safe suffix (`List[Person]` ->
    `List_Person_`). Only used to name the per-type helper functions."""
    return "".join(c if c.isalnum() else "_" for c in ty)


def _internal_wasm(ty: str | None) -> str:
    """The wasm value type the INTERNAL (`_V3Emitter`) ABI carries `ty` in: an
    `Int` is an i64 value, everything else an i32 (a `Bool` value or a
    linear-memory pointer). Mirrors `emit._wasm_ty`."""
    return "i64" if ty == "Int" else "i32"


def _slot_load(address: str, ty: str | None) -> str:
    """Read one internal 8-byte slot as `ty` (mirrors `emit._V3Emitter`)."""
    load = f"(i64.load {address})"
    return load if ty == "Int" else f"(i32.wrap_i64 {load})"


def _slot_store(address: str, value: str, ty: str | None) -> str:
    """Write `value` into one internal 8-byte slot as `ty`."""
    if ty != "Int":
        value = f"(i64.extend_i32_u {value})"
    return f"(i64.store {address} {value})"


def _join(a: str, b: str) -> str:
    """Canonical `join` of two flattened core types (CABI `flatten_variant`)."""
    if a == b:
        return a
    if {a, b} == {"i32", "f32"}:
        return "i32"
    return "i64"


class _Canon:
    """The canonical-ABI codec for one IR's type table.

    Computes flatten / size / alignment for the lowerable value surface, and
    generates (memoized) the lift/lower helper functions plus one canonical
    export wrapper per boundary function.
    """

    def __init__(self, types: dict) -> None:
        self.types = types or {}
        # helper registry: key -> name, and name -> wat body (insertion order)
        self._names: dict[tuple, str] = {}
        self.helpers: dict[str, str] = {}
        self._fresh = 0
        # widest canonical return area any export in this module needs (item
        # 432f). One module-level cell serves them all; see `_reserve_ret_area`.
        self.ret_area = 0

    # -- type table views (mirror emit._V3Emitter) -----------------------
    def record_fields(self, ty: str | None) -> list[tuple[str, str]] | None:
        spec = self.types.get(ty or "")
        if spec is None or spec.get("kind") != "record":
            return None
        return list((spec.get("fields") or {}).items())

    def tagged_layout(self, ty: str | None) -> list[tuple[str, str | None]] | None:
        if not ty:
            return None
        spec = self.types.get(ty)
        if spec is not None and spec.get("kind") == "variant":
            return [(c["name"], c.get("payload")) for c in spec.get("cases") or []]
        head, args = _split_generic(ty)
        if head == "Opt" and len(args) == 1:
            return [("None", None), ("Some", args[0])]
        if head == "Result" and len(args) == 2:
            return [("Ok", args[0]), ("Err", args[1])]
        return None

    def _list_elem(self, ty: str) -> str | None:
        head, args = _split_generic(ty)
        if head == "List" and len(args) == 1:
            return args[0]
        return None

    # -- flatten / size / align ------------------------------------------
    def flatten(self, ty: str | None) -> list[str]:
        if ty == "Bool":
            return ["i32"]
        if ty == "Int":
            return ["i64"]
        if ty == "Str":
            return ["i32", "i32"]
        if self._list_elem(ty or "") is not None:
            return ["i32", "i32"]
        fields = self.record_fields(ty)
        if fields is not None:
            out: list[str] = []
            for _name, fty in fields:
                out += self.flatten(fty)
            return out
        layout = self.tagged_layout(ty)
        if layout is not None:
            joined: list[str] = []
            for _case, payload in layout:
                if payload is None:
                    continue
                for i, ct in enumerate(self.flatten(payload)):
                    if i < len(joined):
                        joined[i] = _join(joined[i], ct)
                    else:
                        joined.append(ct)
            return ["i32"] + joined
        raise EmitError(f"cannot flatten canonical type {ty!r}")

    def align(self, ty: str | None) -> int:
        if ty == "Bool":
            return 1
        if ty == "Int":
            return 8
        if ty == "Str":
            return 4
        if self._list_elem(ty or "") is not None:
            return 4
        fields = self.record_fields(ty)
        if fields is not None:
            return max([1] + [self.align(fty) for _n, fty in fields])
        layout = self.tagged_layout(ty)
        if layout is not None:
            return max([1] + [self.align(p) for _c, p in layout if p is not None])
        raise EmitError(f"cannot align canonical type {ty!r}")

    def size(self, ty: str | None) -> int:
        if ty == "Bool":
            return 1
        if ty == "Int":
            return 8
        if ty == "Str":
            return 8
        if self._list_elem(ty or "") is not None:
            return 8
        fields = self.record_fields(ty)
        if fields is not None:
            off = 0
            for _n, fty in fields:
                off = _align_to(off, self.align(fty)) + self.size(fty)
            return _align_to(off, self.align(ty)) if fields else 0
        layout = self.tagged_layout(ty)
        if layout is not None:
            payloads = [p for _c, p in layout if p is not None]
            max_pa = max([1] + [self.align(p) for p in payloads])
            max_ps = max([0] + [self.size(p) for p in payloads])
            size = _align_to(1, max_pa) + max_ps
            return _align_to(size, self.align(ty))
        raise EmitError(f"cannot size canonical type {ty!r}")

    def field_offsets(self, ty: str) -> list[tuple[str, str, int]]:
        """`(field_name, field_type, canonical_offset)` in declaration order."""
        out = []
        off = 0
        for name, fty in self.record_fields(ty) or []:
            off = _align_to(off, self.align(fty))
            out.append((name, fty, off))
            off += self.size(fty)
        return out

    def payload_offset(self, ty: str) -> int:
        layout = self.tagged_layout(ty) or []
        payloads = [p for _c, p in layout if p is not None]
        max_pa = max([1] + [self.align(p) for p in payloads])
        return _align_to(1, max_pa)

    # -- lowerability gates ----------------------------------------------
    def can_lower_result(self, ty: str | None) -> bool:
        if ty in ("Bool", "Int", "Str"):
            return True
        elem = self._list_elem(ty or "")
        if elem is not None:
            return self.can_lower_result(elem)
        fields = self.record_fields(ty)
        if fields is not None:
            return all(self.can_lower_result(f) for _n, f in fields)
        layout = self.tagged_layout(ty)
        if layout is not None:
            return all(p is None or self.can_lower_result(p) for _c, p in layout)
        return False

    def can_lower_param(self, ty: str | None) -> bool:
        if ty in ("Bool", "Int", "Str"):
            return True
        elem = self._list_elem(ty or "")
        if elem is not None:
            return self.can_lower_param(elem)
        fields = self.record_fields(ty)
        if fields is not None:
            return all(self.can_lower_param(f) for _n, f in fields)
        layout = self.tagged_layout(ty)
        if layout is not None:
            # a variant PARAM is decoded from flattened+joined core values; only
            # scalar / Str / empty payloads are reconstructed here. An aggregate
            # payload in param position is the remaining gap (fine as a result).
            return all(p is None or p in ("Bool", "Int", "Str")
                       for _c, p in layout)
        return False

    # -- helper registry --------------------------------------------------
    def _reg(self, key: tuple, name: str, body) -> str:
        if key in self._names:
            return self._names[key]
        self._names[key] = name
        self.helpers[name] = ""      # reserve (recursive types)
        self.helpers[name] = body()
        return name

    # -- load_canon / store_canon: canonical memory <-> internal value ----
    def load_canon(self, ty: str, addr: str) -> str:
        """WAT expr producing an internal value from a canonical value at `addr`."""
        if ty == "Bool":
            return f"(i32.load8_u {addr})"
        if ty == "Int":
            return f"(i64.load {addr})"
        if ty == "Str":
            return (f"(call $__canon_lift_str (i32.load {addr}) "
                    f"(i32.load offset=4 {addr}))")
        elem = self._list_elem(ty)
        if elem is not None:
            return (f"(call ${self._lift_list(elem)} (i32.load {addr}) "
                    f"(i32.load offset=4 {addr}))")
        if self.record_fields(ty) is not None:
            return f"(call ${self._lift_rec(ty)} {addr})"
        if self.tagged_layout(ty) is not None:
            return f"(call ${self._lift_var(ty)} {addr})"
        raise EmitError(f"cannot load canonical type {ty!r}")

    def store_canon(self, ty: str, val: str, addr: str) -> str:
        """WAT stmt writing an internal value `val` canonically at `addr`."""
        if ty == "Bool":
            return f"(i32.store8 {addr} {val})"
        if ty == "Int":
            return f"(i64.store {addr} {val})"
        if ty == "Str":
            return f"(call $__canon_lower_str {val} {addr})"
        elem = self._list_elem(ty)
        if elem is not None:
            return f"(call ${self._lower_list(elem)} {val} {addr})"
        if self.record_fields(ty) is not None:
            return f"(call ${self._lower_rec(ty)} {val} {addr})"
        if self.tagged_layout(ty) is not None:
            return f"(call ${self._lower_var(ty)} {val} {addr})"
        raise EmitError(f"cannot store canonical type {ty!r}")

    # -- per-type helpers -------------------------------------------------
    def _lift_rec(self, ty: str) -> str:
        name = f"__canon_lift_rec_{_san(ty)}"

        def body() -> str:
            fields = self.field_offsets(ty)
            lines = [f"  (func ${name} (param $c i32) (result i32)",
                     "    (local $r i32)",
                     f"    (local.set $r (call $alloc (i32.const {8 * len(fields)})))"]
            for i, (_fn, fty, coff) in enumerate(fields):
                caddr = (f"(i32.add (local.get $c) (i32.const {coff}))"
                         if coff else "(local.get $c)")
                iaddr = (f"(i32.add (local.get $r) (i32.const {8 * i}))"
                         if i else "(local.get $r)")
                lines.append("    " + _slot_store(iaddr, self.load_canon(fty, caddr), fty))
            lines.append("    (local.get $r))")
            return "\n".join(lines)

        return self._reg(("lift_rec", ty), name, body)

    def _lower_rec(self, ty: str) -> str:
        name = f"__canon_lower_rec_{_san(ty)}"

        def body() -> str:
            lines = [f"  (func ${name} (param $r i32) (param $c i32)"]
            for i, (_fn, fty, coff) in enumerate(self.field_offsets(ty)):
                caddr = (f"(i32.add (local.get $c) (i32.const {coff}))"
                         if coff else "(local.get $c)")
                iaddr = (f"(i32.add (local.get $r) (i32.const {8 * i}))"
                         if i else "(local.get $r)")
                lines.append("    " + self.store_canon(fty, _slot_load(iaddr, fty), caddr))
            lines[-1] += ")"
            return "\n".join(lines)

        return self._reg(("lower_rec", ty), name, body)

    def bulk_copyable(self, elem: str) -> bool:
        """True iff a `List[elem]` body is BYTE-IDENTICAL between the canonical
        array and the internal `[u32 count][pad][slot0]...` body, so the
        crossing is one `memory.copy` in and zero copies out (item 432c).

        Decided from the LAYOUT, never from the element's name, so a later
        element type cannot silently inherit a wrong copy:

        * an aggregate is carried internally as a POINTER to its own block and
          canonically as the DATA itself, so the two layouts are never the same
          bytes -- a bulk copy would copy pointers, not values;
        * the canonical element must be 8 bytes at 8-byte alignment, which is
          exactly the internal slot's size and stride;
        * and moving one element must reduce to a plain 64-bit move, with no
          widening, narrowing or per-element helper call. That is checked by
          generating the very expressions the element-at-a-time loop would use
          and requiring both to be a bare `i64.store` of a bare `i64.load`.

        `Int` is the only element type that satisfies all three today; every
        other one keeps the loop. The aggregate test runs first so the probe
        never reaches `load_canon`/`store_canon` for a type whose helpers those
        would register.
        """
        if (self._list_elem(elem) is not None
                or self.record_fields(elem) is not None
                or self.tagged_layout(elem) is not None):
            return False        # aggregate: internally a pointer, not the data
        try:
            if self.size(elem) != 8 or self.align(elem) != 8:
                return False    # canonical stride is not the internal slot
            src, dst = "$SRC", "$DST"
            lift = _slot_store(dst, self.load_canon(elem, src), elem)
            lower = self.store_canon(elem, _slot_load(dst, elem), src)
        except EmitError:
            return False        # not canonically representable at all
        return (lift == f"(i64.store {dst} (i64.load {src}))"
                and lower == f"(i64.store {src} (i64.load {dst}))")

    def _lift_list(self, elem: str) -> str:
        name = f"__canon_lift_list_{_san(elem)}"

        def body() -> str:
            if self.bulk_copyable(elem):
                return "\n".join([
                    f"  ;; item 432(c): `{elem}` lays out identically canonical"
                    " and internal, so the",
                    "  ;; whole body crosses in one bulk copy, not a load/store"
                    " per element.",
                    f"  (func ${name} (param $p i32) (param $len i32) (result i32)",
                    "    (local $r i32)",
                    "    (local.set $r (call $alloc (i32.add (i32.const 8) "
                    "(i32.mul (local.get $len) (i32.const 8)))))",
                    "    (i32.store (local.get $r) (local.get $len))",
                    "    (memory.copy (i32.add (local.get $r) (i32.const 8)) "
                    "(local.get $p) (i32.mul (local.get $len) (i32.const 8)))",
                    "    (local.get $r))",
                ])
            esz = self.size(elem)
            caddr = (f"(i32.add (local.get $p) (i32.mul (local.get $i) "
                     f"(i32.const {esz})))")
            iaddr = ("(i32.add (local.get $r) (i32.add (i32.const 8) "
                     "(i32.mul (local.get $i) (i32.const 8))))")
            return "\n".join([
                f"  (func ${name} (param $p i32) (param $len i32) (result i32)",
                "    (local $r i32) (local $i i32)",
                "    (local.set $r (call $alloc (i32.add (i32.const 8) "
                "(i32.mul (local.get $len) (i32.const 8)))))",
                "    (i32.store (local.get $r) (local.get $len))",
                "    (block (loop",
                "      (br_if 1 (i32.ge_u (local.get $i) (local.get $len)))",
                "      " + _slot_store(iaddr, self.load_canon(elem, caddr), elem),
                "      (local.set $i (i32.add (local.get $i) (i32.const 1)))",
                "      (br 0)))",
                "    (local.get $r))",
            ])

        return self._reg(("lift_list", elem), name, body)

    def _lower_list(self, elem: str) -> str:
        name = f"__canon_lower_list_{_san(elem)}"

        def body() -> str:
            if self.bulk_copyable(elem):
                # item 432(c): the internal body at `r+8` ALREADY IS the
                # canonical array, so the lowered side points the host at it
                # instead of allocating a scratch buffer and refilling it. The
                # body is 8-aligned ($alloc hands out 8-byte slots), which is
                # the canonical alignment such an element demands, and the host
                # reads the result before it can re-enter the instance, exactly
                # as it already does for $__canon_lower_str's aliased bytes.
                return "\n".join([
                    f"  ;; item 432(c): `{elem}` lays out identically canonical"
                    " and internal, so the",
                    "  ;; internal body IS the canonical array: point at it,"
                    " copy nothing.",
                    f"  (func ${name} (param $r i32) (param $dst i32)",
                    "    (i32.store (local.get $dst) (i32.add (local.get $r) "
                    "(i32.const 8)))",
                    "    (i32.store offset=4 (local.get $dst) "
                    "(i32.load (local.get $r))))",
                ])
            esz = self.size(elem)
            caddr = (f"(i32.add (local.get $buf) (i32.mul (local.get $i) "
                     f"(i32.const {esz})))")
            iaddr = ("(i32.add (local.get $r) (i32.add (i32.const 8) "
                     "(i32.mul (local.get $i) (i32.const 8))))")
            return "\n".join([
                f"  (func ${name} (param $r i32) (param $dst i32)",
                "    (local $count i32) (local $i i32) (local $buf i32)",
                "    (local.set $count (i32.load (local.get $r)))",
                "    (local.set $buf (call $alloc (i32.mul (local.get $count) "
                f"(i32.const {esz}))))",
                "    (block (loop",
                "      (br_if 1 (i32.ge_u (local.get $i) (local.get $count)))",
                "      " + self.store_canon(elem, _slot_load(iaddr, elem), caddr),
                "      (local.set $i (i32.add (local.get $i) (i32.const 1)))",
                "      (br 0)))",
                "    (i32.store (local.get $dst) (local.get $buf))",
                "    (i32.store offset=4 (local.get $dst) (local.get $count)))",
            ])

        return self._reg(("lower_list", elem), name, body)

    def _lift_var(self, ty: str) -> str:
        name = f"__canon_lift_var_{_san(ty)}"

        def body() -> str:
            layout = self.tagged_layout(ty) or []
            poff = self.payload_offset(ty)
            lines = [f"  (func ${name} (param $c i32) (result i32)",
                     "    (local $r i32)",
                     "    (local.set $r (call $alloc (i32.const 16)))",
                     "    (i32.store (local.get $r) (i32.load8_u (local.get $c)))",
                     "    (i64.store offset=8 (local.get $r) (i64.const 0))"]
            caddr = (f"(i32.add (local.get $c) (i32.const {poff}))"
                     if poff else "(local.get $c)")
            iaddr = "(i32.add (local.get $r) (i32.const 8))"
            for k, (_case, payload) in enumerate(layout):
                if payload is None:
                    continue
                store = _slot_store(iaddr, self.load_canon(payload, caddr), payload)
                lines.append(
                    f"    (if (i32.eq (i32.load (local.get $r)) (i32.const {k})) "
                    f"(then {store}))")
            lines.append("    (local.get $r))")
            return "\n".join(lines)

        return self._reg(("lift_var", ty), name, body)

    def _lower_var(self, ty: str) -> str:
        name = f"__canon_lower_var_{_san(ty)}"

        def body() -> str:
            layout = self.tagged_layout(ty) or []
            poff = self.payload_offset(ty)
            lines = [f"  (func ${name} (param $r i32) (param $c i32)",
                     "    (i32.store8 (local.get $c) (i32.load (local.get $r)))"]
            caddr = (f"(i32.add (local.get $c) (i32.const {poff}))"
                     if poff else "(local.get $c)")
            iaddr = "(i32.add (local.get $r) (i32.const 8))"
            for k, (_case, payload) in enumerate(layout):
                if payload is None:
                    continue
                store = self.store_canon(payload, _slot_load(iaddr, payload), caddr)
                lines.append(
                    f"    (if (i32.eq (i32.load (local.get $r)) (i32.const {k})) "
                    f"(then {store}))")
            lines[-1] += ")"
            return "\n".join(lines)

        return self._reg(("lower_var", ty), name, body)

    # -- flattened-param lift (top-level + record/variant params) ---------
    def _new_local(self) -> str:
        self._fresh += 1
        return f"$t{self._fresh}"

    def lift_flat(self, ty: str, cursor: "_Cursor", stmts: list[str],
                  locals_: list[tuple[str, str]]) -> str:
        """Consume the flattened core params for `ty` from `cursor`, appending
        any construction statements, and return a WAT expr for the internal
        value. `locals_` collects `(name, wasm_type)` scratch declarations."""
        if ty in ("Bool", "Int"):
            nm, _ct = cursor.take()
            return f"(local.get {nm})"
        if ty == "Str":
            p, _ = cursor.take()
            ln, _ = cursor.take()
            return f"(call $__canon_lift_str (local.get {p}) (local.get {ln}))"
        elem = self._list_elem(ty)
        if elem is not None:
            p, _ = cursor.take()
            ln, _ = cursor.take()
            return (f"(call ${self._lift_list(elem)} (local.get {p}) "
                    f"(local.get {ln}))")
        fields = self.record_fields(ty)
        if fields is not None:
            t = self._new_local()
            locals_.append((t, "i32"))
            stmts.append(f"(local.set {t} (call $alloc (i32.const {8 * len(fields)})))")
            for i, (_fn, fty) in enumerate(fields):
                val = self.lift_flat(fty, cursor, stmts, locals_)
                iaddr = (f"(i32.add (local.get {t}) (i32.const {8 * i}))"
                         if i else f"(local.get {t})")
                stmts.append(_slot_store(iaddr, val, fty))
            return f"(local.get {t})"
        layout = self.tagged_layout(ty)
        if layout is not None:
            disc, _ = cursor.take()
            joined = [cursor.take() for _ in self.flatten(ty)[1:]]
            t = self._new_local()
            locals_.append((t, "i32"))
            stmts.append(f"(local.set {t} (call $alloc (i32.const 16)))")
            stmts.append(f"(i32.store (local.get {t}) (local.get {disc}))")
            stmts.append(f"(i64.store offset=8 (local.get {t}) (i64.const 0))")
            iaddr = f"(i32.add (local.get {t}) (i32.const 8))"
            for k, (_case, payload) in enumerate(layout):
                if payload is None:
                    continue
                val = self._reinterpret(payload, joined)
                store = _slot_store(iaddr, val, payload)
                stmts.append(
                    f"(if (i32.eq (local.get {disc}) (i32.const {k})) (then {store}))")
            return f"(local.get {t})"
        raise EmitError(f"cannot lift canonical param type {ty!r}")

    def _reinterpret(self, payload: str, joined: list[tuple[str, str]]) -> str:
        """Rebuild a scalar/Str payload from the variant's joined core slots,
        narrowing a slot the join widened to i64 back to i32 where the payload
        wants it."""
        flat = self.flatten(payload)

        def slot(i: int) -> str:
            nm, ct = joined[i]
            if flat[i] == ct:
                return f"(local.get {nm})"
            if flat[i] == "i32" and ct == "i64":
                return f"(i32.wrap_i64 (local.get {nm}))"
            return f"(local.get {nm})"

        if payload in ("Int", "Bool"):
            return slot(0)
        if payload == "Str":
            return f"(call $__canon_lift_str {slot(0)} {slot(1)})"
        raise EmitError(f"cannot reinterpret variant payload {payload!r}")

    # -- the canonical export wrapper ------------------------------------
    def canon_export(self, fn: dict, package: str, iface: str,
                     call_symbol: str | None = None) -> str:
        """A canonical-ABI wrapper for one boundary function/method.

        `fn` carries `name`/`params`/`returns` in the SAME shape whether it is a
        top-level pure `fn` or a service method. `call_symbol` names the core
        function the wrapper delegates to: for a pure `fn` it is `$<name>` (the
        default); for a service method it is the named provide-method function
        (`$__prov_<key>_<method>`), which carries the very same internal ABI a
        pure `fn` does, so exactly one wrapper shape serves both.
        """
        name = fn["name"]
        params = fn.get("params") or []
        ret = fn.get("returns")
        callee = call_symbol or f"${name}"
        export_name = f"{package}/{iface}#{_kebab_name(name)}"

        # flatten each param into named core params
        idx = 0
        grouped: list[tuple[str, list[tuple[str, str]]]] = []
        for p in params:
            names = []
            for ct in self.flatten(p.get("type")):
                names.append((f"$c{idx}", ct))
                idx += 1
            grouped.append((p.get("type"), names))
        if idx > _MAX_FLAT_PARAMS:
            raise EmitError(
                f"{name}: {idx} flattened params exceed the canonical "
                f"MAX_FLAT_PARAMS ({_MAX_FLAT_PARAMS}); indirect params are a "
                "remaining gap")
        param_decl = " ".join(
            f"(param {nm} {ct})" for _t, names in grouped for nm, ct in names)

        stmts: list[str] = []
        locals_: list[tuple[str, str]] = []
        arg_gets: list[str] = []
        for i, (pty, names) in enumerate(grouped):
            cur = _Cursor(names)
            val = self.lift_flat(pty, cur, stmts, locals_)
            a = f"$a{i}"
            locals_.append((a, _internal_wasm(pty)))
            stmts.append(f"(local.set {a} {val})")
            arg_gets.append(f"(local.get {a})")

        call = f"(call {callee} {' '.join(arg_gets)})".replace("  ", " ")

        # result: 0 -> void, 1 -> direct core value, >1 -> indirect return area
        flat = self.flatten(ret) if ret and ret != "Unit" else []
        if not flat:
            result_decl = ""
            stmts.append(f"(drop {call})")
            ret_expr = ""
        elif len(flat) <= _MAX_FLAT_RESULTS:
            result_decl = f" (result {flat[0]})"
            rv = "$rv"
            locals_.append((rv, _internal_wasm(ret)))
            stmts.append(f"(local.set {rv} {call})")
            ret_expr = self._direct_result(ret, f"(local.get {rv})")
        else:
            result_decl = " (result i32)"
            rv = "$rv"
            locals_.append((rv, _internal_wasm(ret)))
            locals_.append(("$area", "i32"))
            stmts.append(f"(local.set {rv} {call})")
            # item 432(f): the canonical return area is CONSTANT for the
            # module. The component host reads it before it can re-enter the
            # instance, so one module-level cell sized to the widest result
            # serves every call on a single-threaded instance -- no bump per
            # call, and no 8 bytes of unreclaimed heap growth per call.
            self.ret_area = max(self.ret_area, self.size(ret))
            stmts.append("(local.set $area (global.get $__canon_ret_area))")
            stmts.append(self.store_canon(ret, f"(local.get {rv})", "(local.get $area)"))
            ret_expr = "(local.get $area)"

        local_decl = " ".join(f"(local {nm} {wt})" for nm, wt in locals_)
        body = "\n    ".join(stmts)
        tail = ("\n    " + ret_expr) if ret_expr else ""
        return (
            f'  ;; canonical export of `{name}` -> WIT `{iface}#{_kebab_name(name)}`\n'
            f'  (func (export "{export_name}") {param_decl}{result_decl}\n'
            f'    {local_decl}\n'
            f'    {body}{tail})'
        )

    def _direct_result(self, ret: str, rv: str) -> str:
        """The single core value a `MAX_FLAT_RESULTS`-fitting result returns,
        read out of the internal value `rv`."""
        if ret in ("Int", "Bool"):
            return rv                                   # already the value
        fields = self.record_fields(ret)
        if fields is not None:                          # single-scalar record
            (_name, fty), = fields
            return _slot_load(rv, fty)
        if self.tagged_layout(ret) is not None:         # enum (all-empty cases)
            return f"(i32.load {rv})"
        raise EmitError(f"cannot direct-return canonical type {ret!r}")

    # -- the shared canonical library (always emitted) -------------------
    def base_helpers(self, alloc_floor: int) -> str:
        """The always-emitted canonical library.

        `alloc_floor` is the lowest address `$alloc` can ever hand out in this
        module, which is what makes the item-432(a) zero-copy `Str` lift a
        CHECKED optimisation rather than an assumption; see below.
        """
        # item 432(a). A canonical `string` param reaches a linear-memory
        # callee in memory the callee itself handed out through
        # `cabi_realloc`, so if `cabi_realloc` reserves _STR_HEADROOM bytes
        # below every buffer, the incoming bytes ALREADY sit exactly where the
        # internal `[u32 len][bytes]` layout wants them: lifting is one store
        # of the length, O(1) in the string rather than a copy of it.
        #
        # That the buffer came from this module's `cabi_realloc` is the
        # canonical ABI's contract for an indirect param, but it is CHECKED
        # here rather than assumed: the fast path is gated on `$ptr` lying in
        # this module's own heap, the only region `cabi_realloc` hands out.
        # Anything else -- a zero-length buffer, which a host may place at any
        # aligned address including 0, or a pointer a non-conforming host
        # invented -- takes a bulk `memory.copy` instead, which is correct for
        # any pointer and still 1 fuel/byte. The fix therefore cannot write
        # below a buffer it was not given room under.
        #
        # 8 bytes of headroom, not the 4 the length needs, so the pointer
        # `cabi_realloc` returns stays 8-aligned for `$alloc`'s slot grid.
        return (
            '  ;; --- canonical ABI boundary (item 41) ---\n'
            '  ;; cabi_realloc: the standard allocator a component host calls to\n'
            '  ;; place an incoming string/list into this module\'s memory. Backed\n'
            '  ;; by the same bump heap ($__hp / $alloc); old/align are unused (a\n'
            '  ;; bump allocator never frees, and $alloc already 8-aligns).\n'
            '  ;; item 432(a): every buffer is handed out with '
            f'{_STR_HEADROOM} bytes of headroom\n'
            '  ;; below it, so lifting a canonical string into the internal\n'
            '  ;; [u32 len][bytes] layout is one store instead of a copy.\n'
            '  (func (export "cabi_realloc")\n'
            '      (param $old i32) (param $old_size i32) (param $align i32) (param $new_size i32)\n'
            '      (result i32)\n'
            '    (i32.add\n'
            '      (call $alloc (i32.add (local.get $new_size) '
            f'(i32.const {_STR_HEADROOM})))\n'
            f'      (i32.const {_STR_HEADROOM})))\n'
            '  ;; bare (ptr,len) canonical string <-> internal [u32 len][bytes].\n'
            '  ;; The lift is zero-copy for a buffer that came from the\n'
            '  ;; cabi_realloc above (every conforming host\'s string param), and\n'
            '  ;; falls back to a bulk copy for any pointer outside this heap.\n'
            '  (func $__canon_lift_str (param $ptr i32) (param $len i32) (result i32)\n'
            '    (local $s i32)\n'
            f'    (if (i32.ge_u (local.get $ptr) (i32.const {alloc_floor + _STR_HEADROOM}))\n'
            '      (then\n'
            '        (i32.store (i32.sub (local.get $ptr) (i32.const 4)) (local.get $len))\n'
            '        (return (i32.sub (local.get $ptr) (i32.const 4)))))\n'
            '    (local.set $s (call $alloc_str (local.get $len)))\n'
            '    (memory.copy\n'
            '      (i32.add (local.get $s) (i32.const 4))\n'
            '      (local.get $ptr)\n'
            '      (local.get $len))\n'
            '    (local.get $s))\n'
            '  (func $__canon_lower_str (param $s i32) (param $dst i32)\n'
            '    (i32.store (local.get $dst) (i32.add (local.get $s) (i32.const 4)))\n'
            '    (i32.store offset=4 (local.get $dst) (i32.load (local.get $s))))'
        )


import re as _re


class _Cursor:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self.items = items
        self.i = 0

    def take(self) -> tuple[str, str]:
        item = self.items[self.i]
        self.i += 1
        return item


def _boundary_functions(functions: list[dict], canon: _Canon) -> list[dict]:
    """The pure functions this module can present over the canonical ABI: every
    parameter is canonically lowerable as a param and the result as a result. A
    function carrying anything else is left off the interface (it stays in the
    core module so a boundary function may still call it), never mis-lowered."""
    out = []
    for fn in functions:
        ret = fn.get("returns")
        if ret and ret != "Unit" and not canon.can_lower_result(ret):
            continue
        params = fn.get("params") or []
        if any(not canon.can_lower_param(p.get("type")) for p in params):
            continue
        out.append(fn)
    return out


def _boundary_methods(methods: dict, canon: _Canon) -> list[tuple[str, dict]]:
    """The service methods presentable over the canonical ABI, in declaration
    order: every parameter is canonically lowerable as a param and the result as
    a result — the SAME gate the pure-`fn` path uses. A method carrying anything
    else is left off the interface; it stays a `provide:` export in the core
    module so an intra-module caller may still reach it, never mis-lowered."""
    out: list[tuple[str, dict]] = []
    for mname, spec in (methods or {}).items():
        ret = spec.get("returns")
        if ret and ret != "Unit" and not canon.can_lower_result(ret):
            continue
        params = spec.get("params") or []
        if any(not canon.can_lower_param(p.get("type")) for p in params):
            continue
        out.append((mname, spec))
    return out


def _provider_of(components: list[dict], service: str) -> tuple[dict, str] | None:
    """The `(component, provide-key)` that provides `service`, or None. A single
    service is the shippable target, so the FIRST provider wins if several do."""
    for component in components or []:
        for key, provided in (component.get("provides") or {}).items():
            if provided == service:
                return component, key
    return None


# The provide-method exports `_ComponentEmitter` emits are anonymous core funcs
# (`(func (export "provide:<key>.<method>") …)`); a canonical wrapper in the same
# module can only `call` a *named* function. We name them here — purely in the
# component text this module wraps, so `_ComponentEmitter` and every existing
# component golden stay untouched — and hand the wrapper the symbol to call.
_PROVIDE_EXPORT = _re.compile(r'\(func \(export "(provide:[^"]+)"\)')


def _provide_symbol(export_name: str) -> str:
    # `provide:realm/kv.get` -> `$__prov_realm_kv_get`
    return "$__prov_" + _san(export_name[len("provide:"):])


def _name_provide_funcs(module: str) -> tuple[str, dict[str, str]]:
    """Give every `provide:` export in `module` a callable `$__prov_*` symbol.
    Returns the rewritten module and a `{export_name: symbol}` map."""
    symbols: dict[str, str] = {}

    def repl(m: "_re.Match") -> str:
        export_name = m.group(1)
        sym = _provide_symbol(export_name)
        symbols[export_name] = sym
        return f'(func {sym} (export "{export_name}")'

    return _PROVIDE_EXPORT.sub(repl, module), symbols


# `_V3Emitter` / `_ComponentEmitter` anchor the bump heap with this global; the
# canonical boundary needs both its value (the floor every `$alloc` result is
# at or above, item 432a) and the room just under it (the shared canonical
# return area, item 432f).
_HEAP_GLOBAL = _re.compile(r"\(global \$__hp \(mut i32\) \(i32\.const (\d+)\)\)")


def _canonical_module(core: str, canon: _Canon, exports: list[str]) -> str:
    """`core` with the canonical boundary spliced in: the shared return area
    carved off the bottom of the bump heap, then cabi_realloc + the lift/lower
    library + one wrapper per boundary function.

    Must run AFTER every `canon.canon_export` call, since those are what size
    the return area and populate `canon.helpers`.
    """
    m = _HEAP_GLOBAL.search(core)
    if m is None:
        raise EmitError(
            "core module has no `$__hp` heap global; the canonical boundary "
            "anchors its return area and its lift bounds check against it")
    heap_start = int(m.group(1))
    area = _align_to(canon.ret_area, 8)
    if area:
        # item 432(f): one module-level cell instead of a bump per call. Taken
        # off the BOTTOM of the heap and the heap started above it, so it can
        # never be handed out again; `$alloc`'s first result moves up by
        # `area`, which is the floor the lift bounds check uses.
        core = core.replace(
            m.group(0),
            f"(global $__hp (mut i32) (i32.const {heap_start + area}))\n"
            "  ;; item 432(f): the canonical return area is constant for the\n"
            "  ;; module (the host reads it before it can re-enter), so one\n"
            "  ;; cell serves every call instead of one bump allocation each.\n"
            f"  (global $__canon_ret_area i32 (i32.const {heap_start}))",
            1)
    additions = ([canon.base_helpers(heap_start + area)]
                 + list(canon.helpers.values()) + exports)
    return _splice_canonical(core, additions)


def _splice_canonical(core: str, additions: list[str]) -> str:
    """Splice `additions` (WAT funcs) in just before a core module's closing
    paren. Shared by the pure-`fn` and service-method paths."""
    body = core.rstrip()
    if not body.endswith(")"):
        raise EmitError("unexpected core module shape (no closing paren to splice into)")
    trunk = body[:-1].rstrip("\n")
    return trunk + "\n" + "\n".join(additions) + "\n)\n"


def _core_with_canonical(ir: dict, canon: _Canon, boundary: list[dict],
                         package: str, iface: str) -> str:
    """The `_V3Emitter` core module for this IR, with the canonical boundary
    (cabi_realloc + lift/lower library + one export per boundary function)
    spliced in just before the module's closing paren."""
    # generate the exports first so `canon.helpers` is fully populated
    exports = [canon.canon_export(fn, package, iface) for fn in boundary]
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
    return _canonical_module(core, canon, exports)


def _service_core_with_canonical(ir: dict, canon: _Canon, component: dict,
                                 boundary: list[tuple[str, dict]],
                                 package: str, iface: str) -> str:
    """The `_ComponentEmitter` module for `component`, with the canonical
    boundary spliced in: each service method's `provide:` export is named, and a
    canonical wrapper delegates to it. The provide method already lowers to the
    tier's INTERNAL ABI (`_ComponentEmitter._boundary_wty` — `Int` an i64 value,
    every compound an i32 pointer), which is exactly what the pure-`fn` wrapper
    consumes, so the SAME lift/lower library and wrapper serve both."""
    modules = _emit_core(ir)
    name = component.get("name")
    core = modules.get(name)
    if core is None:
        raise EmitError(
            f"no module emitted for component {name!r} (the canonical service "
            "boundary lowers a single provider component)")
    named_core, symbols = _name_provide_funcs(core)
    exports = []
    for mname, spec in boundary:
        # resolve the provide export by its `.<method>` suffix so a realm-scoped
        # key (`isolate`) resolves without re-deriving the scope here.
        sym = _resolve_provide_symbol(symbols, mname)
        fn = {"name": mname, "params": spec.get("params") or [],
              "returns": spec.get("returns")}
        exports.append(canon.canon_export(fn, package, iface, call_symbol=sym))
    return _canonical_module(named_core, canon, exports)


def _resolve_provide_symbol(symbols: dict[str, str], method: str) -> str:
    matches = [sym for exp, sym in symbols.items() if exp.endswith(f".{method}")]
    if not matches:
        raise EmitError(
            f"no `provide:` export found for method {method!r} in the component "
            "module (the provider must actually provide the presented method)")
    if len(matches) > 1:
        raise EmitError(
            f"method {method!r} is provided under more than one key; name the "
            "single provided service to present it unambiguously")
    return matches[0]


def _assemble(*, service: str, methods: dict, types: dict, core_wat: str,
              presented: list[str], package: str) -> dict:
    """The WIT document + result dict shared by the pure-`fn` and service paths.

    `methods` is the `{name: {params, returns}}` view of exactly what the core
    module presents, so the embedded WIT (built from it via slice-1's exporter)
    and the binary agree by construction, and a `world` exporting the interface
    gives wit-component an export target."""
    iface = _kebab_type(service)
    synthetic = {"services": {service: {"methods": methods}},
                 "types": types, "externs": []}
    # `export_wit` (slice-1) already emits any referenced named type
    # (`record`/`variant`/`enum`) INSIDE the interface body — valid WIT — since
    # item 97 (ec97d07). The component embeds its output directly; no
    # relocation is needed.
    interface_wit = export_wit(synthetic, service=service, package=package)
    world_name = f"{iface}-component"
    wit = (
        interface_wit.rstrip()
        + "\n\n"
        + "/// The world wit-component wraps the core module against: it exports\n"
        + f"/// the `{iface}` interface above over the canonical ABI (item 41).\n"
        + f"world {world_name} {{\n  export {iface};\n}}\n"
    )
    return {
        "core_wat": core_wat,
        "wit": wit,
        "package": package,
        "interface": iface,
        "world": world_name,
        "functions": presented,
    }


def emit_component(ir: dict, *, service: str,
                   package: str = _DEFAULT_PACKAGE) -> dict:
    """Lower a revl IR to a standard canonical-ABI component's inputs.

    Returns ``{"core_wat", "wit", "package", "interface", "world", "functions"}``:
    the core WAT (canonical exports + the lift/lower library + cabi_realloc), the
    WIT document (the slice-1 interface plus a world that exports it), and the
    names needed to wrap the two into a component with ``build_component``.

    ``service`` names the WIT interface. Two IR shapes cross:

      * a component that **provides** a service named ``service`` — the service's
        ``provide`` methods are lowered by ``_ComponentEmitter`` and presented
        over the canonical ABI (the service-level boundary, item 41 slice-3);
      * otherwise, the top-level pure ``fn``s, grouped under ``service`` as the
        interface name (the original pure-`fn` boundary).

    Raises ``EmitError`` if the selected shape has nothing canonically lowerable.
    """
    canon = _Canon(ir.get("types") or {})
    provider = _provider_of(ir.get("components") or [], service) \
        if service in (ir.get("services") or {}) else None
    if provider is not None:
        return _emit_service_component(ir, canon, service, provider, package)

    functions = ir.get("functions") or []
    boundary = _boundary_functions(functions, canon)
    if not boundary:
        raise EmitError(
            "no canonical-ABI-emittable function: this component presents the "
            "pure functions whose whole signature (params + result) is "
            "canonically lowerable — scalars (Int/Bool/Str), records, lists and "
            "variants/Opt/Result. This IR has none (a Float/Map/resource/"
            "function-typed signature, or a variant PARAM with an aggregate "
            "payload, stays off the interface)."
        )
    iface = _kebab_type(service)
    core_wat = _core_with_canonical(ir, canon, boundary, package, iface)
    methods = {fn["name"]: {"params": fn.get("params") or [],
                            "returns": fn.get("returns")} for fn in boundary}
    return _assemble(service=service, methods=methods, types=ir.get("types") or {},
                     core_wat=core_wat, presented=[fn["name"] for fn in boundary],
                     package=package)


def _emit_service_component(ir: dict, canon: _Canon, service: str,
                            provider: tuple[dict, str], package: str) -> dict:
    """Present a component-provided service's ``provide`` methods over the
    canonical ABI (item 41 slice-3, the service-level boundary)."""
    component, _key = provider
    iface = _kebab_type(service)
    declared = ((ir.get("services") or {}).get(service) or {}).get("methods") or {}
    boundary = _boundary_methods(declared, canon)
    if not boundary:
        raise EmitError(
            f"service {service!r} presents no canonical-ABI-emittable method: a "
            "method crosses when its whole signature (params + result) is "
            "canonically lowerable — scalars (Int/Bool/Str), records, lists and "
            "variants/Opt/Result. This service has none (a Float/Map/resource/"
            "function-typed signature, or a variant PARAM with an aggregate "
            "payload, stays off the interface)."
        )
    core_wat = _service_core_with_canonical(
        ir, canon, component, boundary, package, iface)
    methods = {mname: {"params": spec.get("params") or [],
                       "returns": spec.get("returns")} for mname, spec in boundary}
    return _assemble(service=service, methods=methods, types=ir.get("types") or {},
                     core_wat=core_wat, presented=[m for m, _ in boundary],
                     package=package)


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


def run_component(component_path: pathlib.Path, invoke: str) -> str:
    """Invoke `invoke` (a WAVE call expr, e.g. ``make("x", 5)``) on the
    component under wasmtime's COMPONENT MODEL (``wasmtime run --invoke``, which
    only accepts a component here) and return wasmtime's WAVE-formatted result
    line. Proves the canonical round trip end to end."""
    binary = wasmtime_binary()
    if binary is None:
        raise EmitError("wasmtime not found")
    result = subprocess.run(
        [binary, "run", "--invoke", invoke, str(component_path)],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise EmitError(f"component invocation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def run_component_str(component_path: pathlib.Path, func: str, arg: str) -> str:
    """`func(arg)` for a ``string``-returning export — unquotes the WAVE result."""
    out = run_component(component_path, f'{func}("{arg}")')
    if len(out) >= 2 and out[0] == '"' and out[-1] == '"':
        return out[1:-1]
    return out
