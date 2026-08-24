"""`revl export wit` — a revl service/composition becomes a WIT interface.

Slice 1 of the Component Model bridge (docs/wit-bridge.md): the reverse of
`revl import wit`, pure IR codegen. Two claims are under test.

The mechanical one: the exported WIT is *structurally valid* and re-imports —
every case here round-trips import -> revl -> compile -> **export** -> revl ->
compile and asserts the services and types are stable. (No WIT toolchain is
assumed present; validity is checked structurally and by re-import, which is
the stronger check for this bridge.)

The interesting one: WIT carries **shape, not effects**. The exporter emits
`/// @revl:pure` for a plain operation (the one datum that round-trips into a
revl classification) and carries the rest of revl's *verified* lifecycle —
`emission`, capability scopes, `async`, the acquire/undo inverse — as
`/// @revl:*` doc comments ALONGSIDE the WIT, because a WIT type cannot hold
them. `test_wit_cannot_carry_the_effect_that_revl_verifies` is that boundary.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.import_wit import import_wit, import_wit_file  # noqa: E402
from revl.export_wit import export_wit  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "wit"

_WASM_TOOLS = shutil.which("wasm-tools")


def _assert_parses_as_wit(wit: str, tmp_path: Path) -> None:
    """Every user-defined `record`/`variant`/`enum` must live INSIDE an
    `interface` — a top-level type is invalid WIT. Prefer `wasm-tools` as the
    authority; fall back to a structural check (no type keyword appears at
    column 0) when the toolchain is absent."""
    # structural: no `record`/`variant`/`enum` declaration at the package top
    # level (they only ever appear indented, inside an interface).
    for line in wit.splitlines():
        if re.match(r"^(record|variant|enum)\s", line):
            raise AssertionError(f"top-level WIT type is invalid WIT: {line!r}")
    if _WASM_TOOLS is None:
        return
    path = tmp_path / "exported.wit"
    path.write_text(wit, encoding="utf-8")
    proc = subprocess.run(
        [_WASM_TOOLS, "component", "wit", str(path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"wasm-tools rejected the exported WIT:\n{proc.stderr}\n---\n{wit}")


def _compile(source: str, tmp_path: Path, name: str = "x.rvl") -> dict:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return compile_files([str(path)])


def _ir_from_wit(fixture: str, tmp_path: Path) -> dict:
    return _compile(import_wit_file(str(FIXTURES / fixture)), tmp_path)


def _shape(ir: dict) -> tuple:
    """The shape an export must preserve: service method signatures + the
    nominal type declarations. Extern *bodies* (stubs) are deliberately out."""
    services = {
        name: {
            op: (spec.get("emission"),
                 tuple((p["name"], p["type"]) for p in spec.get("params") or []),
                 spec.get("returns"))
            for op, spec in (svc.get("methods") or {}).items()
        }
        for name, svc in (ir.get("services") or {}).items()
    }
    types = {
        name: (t.get("kind"),
               tuple((t.get("fields") or {}).items()),
               tuple((c["name"], c.get("payload")) for c in t.get("cases") or []))
        for name, t in (ir.get("types") or {}).items()
    }
    resources = {e["returns"] for e in ir.get("externs") or []
                 if e.get("class") == "acquire"}
    return services, types, resources


# ------------------------------------------------------------- basic export

def test_a_service_exports_to_a_wit_interface(tmp_path):
    ir = _ir_from_wit("greeter.wit", tmp_path)
    wit = export_wit(ir, service="Greeter")
    assert "package revl:exported;" in wit
    assert "interface greeter {" in wit
    # kebab-case crosses back
    assert "greet: func(name: string) -> string;" in wit
    assert "log-greeting: func(name: string, at-millis: s64);" in wit
    # the pure assertion is the one datum that round-trips into a revl class
    assert "/// @revl:pure\n  greet:" in wit


def test_records_variants_and_enums_reverse(tmp_path):
    ir = _ir_from_wit("catalog.wit", tmp_path)
    wit = export_wit(ir, service="Catalog")
    assert "record item { id: s64, name: string, price: f64, tags: list<string> }" in wit
    # a payload-free variant round-trips as a WIT `enum`
    assert "enum availability { in-stock, back-order, discontinued }" in wit
    # one with payloads is a `variant`
    assert "variant lookup { found(item), ambiguous(list<string>), missing }" in wit
    # option / result reverse, including the `_` for a Unit ok-arm
    assert "stock: func(id: string) -> option<availability>;" in wit
    assert "reserve: func(id: string, quantity: s64) -> result<item, string>;" in wit


def test_result_unit_arms_reverse_to_underscore_and_bare(tmp_path):
    src = import_wit(
        "package a:b;\ninterface i {\n"
        "  a: func() -> result<_, string>;\n"
        "  b: func() -> result<u32>;\n"
        "  c: func() -> result;\n}\n", filename="r.wit")
    wit = export_wit(_compile(src, tmp_path), service="I")
    assert "a: func() -> result<_, string>;" in wit
    assert "b: func() -> result<s64>;" in wit
    assert "c: func() -> result;" in wit


# ------------------------------------------------- resources reverse (Slice 2)

def test_a_resource_exports_to_a_wit_resource_block(tmp_path):
    ir = _ir_from_wit("resource.wit", tmp_path)
    wit = export_wit(ir, service="Filesystem")
    assert "resource descriptor {" in wit
    assert "constructor(path: string);" in wit
    # a method drops the `self` handle and keeps the rest
    assert "read: func(len: s64) -> list<s64>;" in wit
    assert "tell: func() -> s64;" in wit
    # a free function returning the handle stays a free function
    assert "open: func(path: string) -> descriptor;" in wit


# --------------------------------------------------------------- round trips

@pytest.mark.parametrize("fixture,service", [
    ("greeter.wit", "Greeter"),
    ("catalog.wit", "Catalog"),
    ("resource.wit", "Filesystem"),
])
def test_import_export_round_trip_is_stable(fixture, service, tmp_path):
    ir1 = _ir_from_wit(fixture, tmp_path)
    wit = export_wit(ir1, service=service)
    ir2 = _compile(import_wit(wit, filename="round.wit"), tmp_path, "back.rvl")
    s1, t1, r1 = _shape(ir1)
    s2, t2, r2 = _shape(ir2)
    # the exported service round-trips exactly
    assert s1[service] == s2[service]
    # the resource set is stable
    assert r1 == r2
    # every type the round-trip reconstructed matches the original
    for name, decl in t2.items():
        assert t1[name] == decl


# ---------------------------------------------- valid-WIT top-level shape

def test_referenced_types_live_inside_the_interface(tmp_path):
    """A referenced `record`/`variant`/`enum` must be declared INSIDE the
    interface whose functions use it — a top-level type is invalid WIT."""
    ir = _ir_from_wit("catalog.wit", tmp_path)
    wit = export_wit(ir, service="Catalog")
    _assert_parses_as_wit(wit, tmp_path)
    # the type declarations sit inside `interface catalog { ... }`, indented
    iface = wit[wit.index("interface catalog {"):]
    assert "  record item {" in iface
    assert "  enum availability {" in iface
    assert "  variant lookup {" in iface
    # and nothing between `package` and the first `interface` declares a type
    preamble = wit[wit.index("package"):wit.index("interface catalog {")]
    assert "record" not in preamble and "variant" not in preamble


def test_composition_wit_parses_and_shares_a_type_via_use(tmp_path):
    """When two exported interfaces reference the same type, one interface
    owns the declaration and the other pulls it in with a `use` — the whole
    document parses as valid WIT."""
    ir = {
        "types": {"Widget": {"kind": "record",
                             "fields": {"id": "Int", "label": "Str"}}},
        "services": {
            "A": {"methods": {"make": {
                "params": [{"name": "label", "type": "Str"}],
                "returns": "Widget"}}},
            "B": {"methods": {"inspect": {
                "params": [{"name": "w", "type": "Widget"}],
                "returns": "Str"}}},
        },
        "components": [{"provides": {"a": "A", "b": "B"}}],
    }
    wit = export_wit(ir, composition=True)
    _assert_parses_as_wit(wit, tmp_path)
    # interface `a` owns the record; interface `b` brings it in with `use`
    iface_a = wit[wit.index("interface a {"):wit.index("interface b {")]
    assert "  record widget {" in iface_a
    iface_b = wit[wit.index("interface b {"):]
    assert "use a.{widget};" in iface_b
    assert "record widget" not in iface_b


# ------------------------------------------------- the composition surface

def test_composition_exports_every_provided_service(tmp_path):
    ir = _ir_from_wit("catalog.wit", tmp_path)  # provides Catalog and Store
    wit = export_wit(ir, composition=True)
    assert "interface catalog {" in wit
    assert "interface store {" in wit
    _assert_parses_as_wit(wit, tmp_path)


# ------------------------------------------------- the effects boundary

def test_wit_cannot_carry_the_effect_that_revl_verifies(tmp_path):
    """The claim of the bridge: WIT is shape; revl's verified lifecycle rides
    alongside as `/// @revl:*` comments, never in a WIT type."""
    ir = _ir_from_wit("greeter.wit", tmp_path)
    wit = export_wit(ir, service="Greeter")
    assert "WIT DESCRIBES SHAPE, NOT EFFECTS." in wit
    # log-greeting is an emission in revl; WIT's `func` cannot say so, so the
    # fact rides in a doc comment, not the signature
    assert "/// @revl:emission" in wit
    assert "log-greeting: func(name: string, at-millis: s64);" in wit
    # and re-importing does NOT recover the emission from the type — it recovers
    # it because the importer *defaults* to emission (WIT still said nothing)
    back = _compile(import_wit(wit, filename="b.wit"), tmp_path)
    assert back["services"]["Greeter"]["methods"]["log_greeting"]["emission"] is True


def test_capability_scope_is_carried_as_a_comment_not_a_type(tmp_path):
    ir = _compile("""
service Db {
  emission [store] fn write(key: Str, value: Str)
}
extern emission fn store(key: Str, value: Str) = @py { # ... }
component DbProvider provides db: Db {
  provide db {
    fn write(key, value) = store(key, value)
  }
}
""", tmp_path)
    wit = export_wit(ir, service="Db")
    assert "/// @revl:emission [store]" in wit
    # the WIT signature itself carries no scope
    assert "write: func(key: string, value: string);" in wit


# ------------------------------------------------------------------- errors

def test_unknown_service_lists_the_known_ones(tmp_path):
    ir = _ir_from_wit("greeter.wit", tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_wit(ir, service="Nope")
    assert "no service named `Nope`" in str(excinfo.value)
    assert "Greeter" in str(excinfo.value)


def test_a_map_type_cannot_be_exported(tmp_path):
    ir = _compile("""
service Cache {
  fn all() -> Map[Str, Int]
}
""", tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_wit(ir, service="Cache")
    assert "WIT has no map type" in str(excinfo.value)


def test_needs_a_selection(tmp_path):
    ir = _ir_from_wit("greeter.wit", tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_wit(ir)
    assert "select what to export" in str(excinfo.value)


# ------------------------------------------------------------------- the CLI

def test_cli_exports_a_service(tmp_path, capsys):
    rvl = tmp_path / "greeter.rvl"
    rvl.write_text(import_wit_file(str(FIXTURES / "greeter.wit")), encoding="utf-8")
    out = tmp_path / "greeter.wit"
    assert main(["export", "wit", str(rvl), "--service", "Greeter",
                 "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "interface greeter {" in text
    # the exported WIT re-imports and compiles
    back = tmp_path / "back.rvl"
    assert main(["import", "wit", str(out), "-o", str(back)]) == 0
    assert compile_files([str(back)])["services"]["Greeter"]


def test_cli_requires_a_selection(tmp_path):
    rvl = tmp_path / "greeter.rvl"
    rvl.write_text(import_wit_file(str(FIXTURES / "greeter.wit")), encoding="utf-8")
    # the mutually-exclusive group is required: argparse exits 2
    with pytest.raises(SystemExit) as excinfo:
        main(["export", "wit", str(rvl)])
    assert excinfo.value.code == 2
