"""`revl import wit` — WIT world/interface -> revl source.

Two claims are under test.

The first is mechanical: **the generated source compiles**. A codegen that
emits plausible-looking text is worthless; every fixture here is round-tripped
through `compile_files` and the resulting IR is inspected for the services and
types it should carry.

The second is the interesting one. WIT describes *shape* and says nothing
about reversibility, while revl's `emission` is a semantic claim that a
service declaration imposes as an **upper bound** on every provider (G4). So
the importer defaults every operation to `emission` and requires an explicit,
recorded human assertion (`/// @revl:pure`, or `--pure` at import time) to
weaken it — the same trust direction `revl mcp import` takes with
`readOnlyHint`. `test_a_plain_operation_backed_by_an_emission_is_rejected`
is why the weakened form is still safe: the compiler itself refuses a plain
operation whose implementation reaches an irreversible call, so a generated
plain `fn` is genuinely plain.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.import_wit import import_wit, import_wit_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "wit"


def _compile(source: str, tmp_path: Path, name: str = "imported.rvl") -> dict:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return compile_files([str(path)])


def _methods(ir: dict, service: str) -> dict:
    return ir["services"][service]["methods"]


def _extern_classes(ir: dict) -> dict:
    return {extern["name"]: extern["class"] for extern in ir["externs"]}


# ------------------------------------------------- fixture 1: a plain interface

def test_simple_interface_becomes_a_service():
    source = import_wit_file(str(FIXTURES / "greeter.wit"))
    assert "service Greeter {" in source
    # kebab-case crosses to revl's own conventions
    assert "emission fn log_greeting(name: Str, at_millis: Int)" in source
    # the one operation the WIT author asserted is reversible
    assert "\n  fn greet(name: Str) -> Str" in source
    # the header states the trust direction rather than implying safety
    assert "TRUSTED, NOT CHECKED (G8)" in source
    assert "Source WIT:" in source and "greeter.wit" in source


def test_simple_interface_compiles(tmp_path):
    ir = _compile(import_wit_file(str(FIXTURES / "greeter.wit")), tmp_path)
    methods = _methods(ir, "Greeter")
    assert methods["greet"]["emission"] is False
    assert methods["log_greeting"]["emission"] is True
    assert _extern_classes(ir) == {"wit_greeter_greet": "pure",
                                   "wit_greeter_log_greeting": "emission"}
    assert [c["name"] for c in ir["components"]] == ["GreeterProvider"]
    assert ir["components"][0]["provides"] == {"greeter": "Greeter"}


# ------------------- fixture 2: records + variants + enums + option/result/list

def test_catalog_maps_the_whole_type_family(tmp_path):
    ir = _compile(import_wit_file(str(FIXTURES / "catalog.wit")), tmp_path)

    assert ir["types"]["Item"] == {
        "params": [], "kind": "record",
        "fields": {"id": "Int", "name": "Str", "price": "Float", "tags": "List[Str]"},
    }
    assert ir["types"]["Availability"]["kind"] == "variant"
    assert [c["name"] for c in ir["types"]["Availability"]["cases"]] == \
        ["InStock", "BackOrder", "Discontinued"]
    assert ir["types"]["Lookup"]["cases"] == [
        {"name": "Found", "payload": "Item"},
        {"name": "Ambiguous", "payload": "List[Str]"},
        {"name": "Missing", "payload": None},
    ]

    catalog = _methods(ir, "Catalog")
    assert catalog["stock"]["returns"] == "Opt[Availability]"
    assert catalog["reserve"]["returns"] == "Result[Item, Str]"
    assert catalog["reserve"]["params"] == [{"name": "id", "type": "Str"},
                                           {"name": "quantity", "type": "Int"}]
    assert catalog["clear"]["returns"] is None
    # `find` is the only operation the WIT asserts is reversible
    assert [name for name, op in catalog.items() if not op["emission"]] == ["find"]


def test_a_type_alias_is_inlined_not_declared(tmp_path):
    """revl has no type alias — `type Sku = Str` would declare a one-case
    *variant* named `Str`, which is a different (and wrong) thing."""
    source = import_wit_file(str(FIXTURES / "catalog.wit"))
    assert "type Sku" not in source
    assert "fn find(id: Str)" in source
    assert "note: WIT `type sku = ...` is inlined as `Str`" in source
    assert "Sku" not in _compile(source, tmp_path)["types"]


def test_world_exports_are_provided_and_imports_are_declaration_only(tmp_path):
    source = import_wit_file(str(FIXTURES / "catalog.wit"))
    ir = _compile(source, tmp_path)

    # an inline world export is callable: service + extern + provider
    assert _methods(ir, "Store")["restock"]["returns"] == "Result[Unit, Str]"
    assert "wit_store_restock" in _extern_classes(ir)
    assert {c["name"] for c in ir["components"]} == {"CatalogProvider", "StoreProvider"}

    # an inline world import is what the *host* expects to be given: revl
    # gets the declaration, and no extern-backed provider is invented for it
    assert _methods(ir, "StoreImports")["audit"]["emission"] is True
    assert not any(name.startswith("wit_store_audit") for name in _extern_classes(ir))

    # interface references are reported, not silently dropped
    assert "world `store` exports `catalog`: defined in this file" in source
    assert ("world `store` imports `wasi:clocks/monotonic-clock@0.2.0`: "
            "defined in another package") in source


# -------------------------------------------------- resources (Slice 2)

def test_resource_maps_to_the_acquire_returned_handle_model(tmp_path):
    """A WIT resource is a live handle with identity — exactly revl's
    acquire-returned resource type (distribute.py): it crosses a seam by
    proxy, and its WIT destructor is the `undo` of an `extern acquire`."""
    source = import_wit_file(str(FIXTURES / "resource.wit"))
    # the handle: an opaque proxy record, identity host-side
    assert "type Descriptor = { handle: Int }" in source
    # construction + destructor as a tracked inverse (G4); `result` is the
    # undo slot's one implicit binding — the acquired handle
    assert ("extern acquire fn descriptor_new(path: Str) -> Descriptor"
            " undo descriptor_drop(result)") in source
    # the destructor is a declared extern, so the undo's callee is checked
    assert "extern emission fn descriptor_drop(self: Descriptor)" in source
    assert "crosses a seam by proxy not by copy (distribute.py)" in source

    ir = _compile(source, tmp_path)
    methods = _methods(ir, "Filesystem")
    # a method takes the handle as its first parameter (`self`)
    assert methods["descriptor_read"]["params"][0] == {"name": "self", "type": "Descriptor"}
    # `@revl:pure` on the WIT method survives; the rest default to emission
    assert methods["descriptor_tell"]["emission"] is False
    assert methods["descriptor_read"]["emission"] is True
    # distribute.py sees Descriptor as a resource type: it is an acquire return
    from revl.distribute import distributability
    assert "Descriptor" in {e["returns"] for e in ir["externs"]
                            if e.get("class") == "acquire"}
    verdict = distributability(ir)["Filesystem"]
    assert verdict["verdict"] == "address-space-bound"
    assert any("Descriptor" in reason for reason in verdict["reasons"])


def test_borrow_and_own_map_to_the_handle_type(tmp_path):
    """`borrow<r>` / `own<r>` are references to a resource; the reference IS
    the handle type — copy-vs-move is the host concern the acquire/undo pair
    already tracks."""
    source = import_wit("""
package a:b;
interface i {
  resource conn {
    constructor();
  }
  send: func(c: borrow<conn>, bytes: list<u8>);
  take: func(c: own<conn>);
}
""", filename="borrow.wit")
    ir = _compile(source, tmp_path)
    methods = _methods(ir, "I")
    assert methods["send"]["params"][0] == {"name": "c", "type": "Conn"}
    assert methods["take"]["params"][0] == {"name": "c", "type": "Conn"}


def test_a_resource_with_no_constructor_still_declares_its_destructor(tmp_path):
    source = import_wit("""
package a:b;
interface i {
  resource pool { }
  open: func() -> pool;
}
""", filename="pool.wit")
    assert "extern acquire fn pool_new() -> Pool undo pool_drop(result)" in source
    assert "declares no constructor" in source
    ir = _compile(source, tmp_path)
    assert "Pool" in {e["returns"] for e in ir["externs"] if e.get("class") == "acquire"}


# --------------------------------------------------------- the emission rule

NOTHING_ASSERTED = """
package a:b;
interface probe {
  read-only-looking: func(key: string) -> string;
}
"""


def test_wit_carries_no_claim_so_everything_is_an_emission(tmp_path):
    source = import_wit(NOTHING_ASSERTED, filename="probe.wit")
    assert "emission fn read_only_looking" in source
    assert "WIT carries no reversibility claim" in source
    ir = _compile(source, tmp_path)
    assert _methods(ir, "Probe")["read_only_looking"]["emission"] is True
    assert _extern_classes(ir)["wit_probe_read_only_looking"] == "emission"


def test_the_importing_engineer_can_assert_purity(tmp_path):
    source = import_wit(NOTHING_ASSERTED, filename="probe.wit",
                        pure=["probe.read-only-looking"])
    assert "plain by assertion: `--pure probe.read-only-looking`" in source
    ir = _compile(source, tmp_path)
    assert _methods(ir, "Probe")["read_only_looking"]["emission"] is False
    assert _extern_classes(ir)["wit_probe_read_only_looking"] == "pure"


def test_a_bare_function_name_also_matches():
    source = import_wit(NOTHING_ASSERTED, filename="probe.wit",
                        pure=["read-only-looking"])
    assert "\n  fn read_only_looking(key: Str) -> Str" in source


def test_a_pure_assertion_that_matches_nothing_is_an_error():
    """Silently leaving it `emission` would be safe but dishonest: the
    engineer believes they narrowed something."""
    with pytest.raises(RevlError) as excinfo:
        import_wit(NOTHING_ASSERTED, filename="probe.wit", pure=["probe.reed_only"])
    assert "--pure named 'probe.reed_only', which is not a function" in str(excinfo.value)


def test_a_plain_operation_backed_by_an_emission_is_rejected(tmp_path):
    """Why the generated plain `fn` is trustworthy: G4 makes a service
    declaration an upper bound, so the compiler refuses the mismatch the
    importer would have to make in order to under-declare."""
    source = import_wit(NOTHING_ASSERTED, filename="probe.wit",
                        pure=["probe.read-only-looking"])
    broken = source.replace("extern pure fn", "extern emission fn")
    with pytest.raises(RevlError) as excinfo:
        _compile(broken, tmp_path, "broken.rvl")
    assert "declared plain, but this implementation reaches" in str(excinfo.value)


# ------------------------------------------------------------ name handling

def test_names_that_would_collide_with_revl_are_renamed(tmp_path):
    source = import_wit("""
package a:b;
record %result { code: s32 }
interface odd {
  %type: func(kind: string) -> %result;
}
""", filename="odd.wit")
    # `Result` is a revl built-in, `type` is a revl keyword
    assert "type ResultTy = { code: Int }" in source
    assert "note: type `result` -> `ResultTy`" in source
    # the `%` escape keeps `%result` a user type rather than WIT's builtin
    assert "Result[" not in source
    assert "fn type_(kind: Str) -> ResultTy" in source
    ir = _compile(source, tmp_path)
    assert "ResultTy" in ir["types"]
    assert "type_" in _methods(ir, "Odd")


def test_a_case_shadowing_a_builtin_constructor_is_reported(tmp_path):
    source = import_wit("package a:b;\nvariant v { ok(u32), missing }\n"
                        "interface i { f: func(p: v); }\n", filename="v.wit")
    assert "`V.Ok` shadows revl's built-in `Ok` constructor" in source
    assert _compile(source, tmp_path)["types"]["V"]["cases"][0]["name"] == "Ok"


def test_doc_comments_are_carried_across():
    source = import_wit_file(str(FIXTURES / "greeter.wit"))
    assert "// Build a greeting for a name." in source


# ----------------------------------------------------- refusals (the messages)

def _refusal(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        import_wit(source, filename="bad.wit")
    return str(excinfo.value)


@pytest.mark.parametrize("wit,expected", [
    ("interface i { f: func() -> stream<u8>; }", "stream<T>"),
    ("interface i { f: func() -> future<u8>; }", "future<T>"),
    ("interface i { f: func(p: tuple<u32, string>); }", "tuple<...>"),
    ("interface i { f: func(cb: func); }", "a `func` type used as a value"),
    ("interface i { flags perms { read, write } }", "`flags perms`"),
    ("interface i { f: async func(); }", "`async func` (`f`)"),
    ("interface i { f: func() -> (a: u32, b: u32); }",
     "a named/multiple result list on `f`"),
    ("interface i { f: func(p: map<string, u32>); }", "a generic WIT type `map<...>`"),
    ("world w { include other; }", "`include` in world `w`"),
    ("world w { export inner: interface { f: func(); } }",
     "an inline `interface` in world `w`"),
    ("interface i { use wasi:io/streams.{input}; f: func(); }",
     "`use` from another package (`wasi...`)"),
    ("interface i { enum e { a(u32) } }", "a payload on enum case `e.a`"),
])
def test_unsupported_constructs_name_themselves(wit, expected):
    message = _refusal(f"package a:b;\n{wit}\n")
    assert "unsupported WIT construct" in message
    assert expected in message
    assert message.startswith("bad.wit:")          # every refusal carries a line


def test_an_unknown_type_says_where_to_look():
    message = _refusal("package a:b;\ninterface i { f: func(p: nowhere); }\n")
    assert "unknown WIT type `nowhere`" in message
    assert "resolves names inside one file only" in message


def test_nested_options_are_refused_because_opt_does_not_nest():
    message = _refusal(
        "package a:b;\ninterface i { f: func() -> option<option<string>>; }\n")
    assert "`option<option<T>>`" in message
    assert "does not distinguish" in message or "cannot distinguish" in message


def test_a_circular_alias_is_reported_as_a_cycle():
    message = _refusal("package a:b;\ntype x = y;\ntype y = x;\n"
                       "interface i { f: func(p: x); }\n")
    assert "circular WIT type alias: x -> y -> x" in message


def test_two_definitions_of_one_type_name_collide_loudly():
    message = _refusal("""
package a:b;
interface one { record point { x: u32 } f: func(); }
interface two { record point { x: string } g: func(); }
""")
    assert "`point` is defined twice with different shapes" in message
    assert "revl types share one module namespace" in message


def test_an_identical_repeat_of_a_type_is_harmless(tmp_path):
    source = import_wit("""
package a:b;
interface one { record point { x: u32 } f: func(p: point); }
interface two { record point { x: u32 } g: func(p: point); }
""", filename="dup.wit")
    assert source.count("type Point =") == 1
    assert "Point" in _compile(source, tmp_path)["types"]


def test_two_items_that_generate_one_service_name_collide():
    message = _refusal("package a:b;\ninterface store { f: func(); }\n"
                       "world store { export g: func(); }\n")
    assert "both generate the revl service `Store`" in message


def test_a_file_with_no_operations_is_refused():
    message = _refusal("package a:b;\nrecord point { x: u32 }\n")
    assert "declares no functions to import" in message


# ------------------------------------------------------------------- the CLI

def test_cli_writes_generated_source(tmp_path, capsys):
    out = tmp_path / "greeter.rvl"
    assert main(["import", "wit", str(FIXTURES / "greeter.wit"), "-o", str(out)]) == 0
    assert "service Greeter {" in out.read_text(encoding="utf-8")
    assert compile_files([str(out)])["services"]["Greeter"]


def test_cli_backend_choice_reaches_the_host_bodies(tmp_path):
    out = tmp_path / "greeter.rvl"
    assert main(["import", "wit", str(FIXTURES / "greeter.wit"),
                 "--backend", "py", "-o", str(out)]) == 0
    source = out.read_text(encoding="utf-8")
    assert "= @py { #" in source
    assert compile_files([str(out)])["externs"][0]["bodies"].keys() == {"py"}


def test_cli_reports_a_refusal_and_exits_nonzero(tmp_path, capsys):
    bad = tmp_path / "bad.wit"
    bad.write_text("package a:b;\ninterface i { f: func(p: tuple<u32, string>); }\n",
                   encoding="utf-8")
    assert main(["import", "wit", str(bad)]) == 1
    assert "unsupported WIT construct: tuple<...>" in capsys.readouterr().err


def test_cli_missing_file(tmp_path, capsys):
    assert main(["import", "wit", str(tmp_path / "absent.wit")]) == 1
    assert "cannot read" in capsys.readouterr().err
