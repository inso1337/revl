"""`revl import cordis` — a Cordis (TS) plugin's surface -> revl source.

Three claims are under test, the same shape as `test_import_wit.py` and
`test_import_openapi.py`.

The first is mechanical: **the generated source compiles**. Every fixture is
round-tripped through `compile_source`/`compile_files` and its IR inspected.

The second is the family's emission rule: a Cordis plugin is untyped TS and
vouches for nothing, so every operation is `emission` unless the plugin's JSDoc
(`@revl:pure`) or `--pure` at import time explicitly weakens it — and a service
declaration is a G4 upper bound, so `test_a_plain_op_backed_by_emission_is_rejected`
shows the weakened form is still sound: the compiler itself refuses a plain
operation that reaches an irreversible call.

The third is unique to this member and the honest hard part: a signature that
**cannot** be recovered from the untyped source is refused loudly (or left as a
`// UNRECOVERED` marker under `--mark-unrecovered`), never guessed.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.import_cordis import import_cordis, import_cordis_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "cordis"


def _methods(ir: dict, service: str) -> dict:
    return ir["services"][service]["methods"]


def _extern_classes(ir: dict) -> dict:
    return {extern["name"]: extern["class"] for extern in ir["externs"]}


# ------------------------------------- fixture 1: a Service class is recovered

def test_service_class_becomes_a_service():
    source = import_cordis_file(str(FIXTURES / "cache.ts"))
    # the provided key `store` (from `super(ctx, 'store')`) names the service
    assert "service Store {" in source
    assert "component StoreProvider provides store: Store {" in source
    # the header states the trust direction rather than implying safety
    assert "TRUSTED, NOT CHECKED (G8)" in source
    assert "Source plugin:" in source and "cache.ts" in source
    # the plugin's own coeffect dependencies are surfaced
    assert "required: database" in source
    assert "optional: logger" in source


def test_service_class_compiles_with_recovered_signatures():
    ir = compile_source(import_cordis_file(str(FIXTURES / "cache.ts")), "cache.rvl")
    methods = _methods(ir, "Store")
    # TS types crossed into revl surface types
    assert methods["get"]["params"] == [{"name": "key", "type": "Str"}]
    assert methods["get"]["returns"] == "Opt[Str]"          # `string | undefined`
    assert methods["set"]["returns"] is None                # `void`
    assert methods["size"]["returns"] == "Int"              # `bigint`
    assert methods["warm"]["params"] == [{"name": "keys", "type": "List[Str]"}]
    assert methods["warm"]["returns"] == "Float"            # `Promise<number>`
    assert [c["name"] for c in ir["components"]] == ["StoreProvider"]
    assert ir["components"][0]["provides"] == {"store": "Store"}


def test_lifecycle_methods_are_not_operations():
    """`stop()` is a Cordis teardown hook, not part of the callable surface —
    it is detected as teardown evidence and left out of the service."""
    source = import_cordis_file(str(FIXTURES / "cache.ts"))
    ir = compile_source(source, "cache.rvl")
    assert "stop" not in _methods(ir, "Store")
    assert "Teardown detected but NOT paired to any operation" in source
    assert "stop() teardown" in source


def test_object_literal_provide_shape_is_recovered():
    """The functional `ctx.provide('greeter'); ctx.greeter = { ... }` shape,
    not just the `Service` class."""
    ir = compile_source(import_cordis_file(str(FIXTURES / "greeter.ts")), "g.rvl")
    methods = _methods(ir, "Greeter")
    assert methods["greet"]["returns"] == "Str"
    assert methods["record"]["params"] == [{"name": "name", "type": "Str"},
                                           {"name": "at", "type": "Float"}]


# ------------------------------ DSH's real plugin shapes (roadmap item 116)

def test_service_subclass_via_a_non_service_base_is_recovered():
    """(a) A `Service` subclass reached through a project-local base class
    (`class Sessions extends BaseStore`, `BaseStore extends Service`) is
    recovered through the real base chain, not just a literal `extends Service`."""
    source = import_cordis_file(str(FIXTURES / "subclassed.ts"))
    assert "class Sessions extends BaseStore (a Service subclass via BaseStore)" in source
    ir = compile_source(source, "subclassed.rvl")
    methods = _methods(ir, "Sessions")
    # the subclass's own operation is present ...
    assert methods["lookup"]["params"] == [{"name": "id", "type": "Str"}]
    assert methods["lookup"]["returns"] == "Opt[Str]"
    # ... and a public method inherited from the local base class is merged in
    assert methods["size"]["returns"] == "Int"
    assert ir["components"][0]["provides"] == {"sessions": "Sessions"}


def test_decorated_methods_are_recognized_as_operations():
    """(b) A method carrying decorators (`@cache`, `@throttle(100)`, `@audit.log`)
    is still recognized: the decorator is skipped to reach the underlying `def`,
    and no phantom operation is minted from the decorator name."""
    source = import_cordis_file(str(FIXTURES / "decorated.ts"))
    ir = compile_source(source, "decorated.rvl")
    methods = _methods(ir, "Metrics")
    assert set(methods) == {"read", "record"}          # not `cache`/`throttle`/…
    assert methods["record"]["params"] == [{"name": "key", "type": "Str"},
                                           {"name": "value", "type": "Int"}]
    # a `@revl:pure` JSDoc above a decorated method still reaches the method
    assert "plain by assertion: `@revl:pure`" in source
    assert methods["read"]["emission"] is False
    assert _extern_classes(ir)["cordis_metrics_read"] == "pure"


def test_named_record_from_a_local_import_is_transcribed():
    """(c) A record/interface type defined in another local module and imported
    is followed and transcribed as a revl `type` — including a record it nests
    from a further module — instead of the nominal type being refused."""
    source = import_cordis_file(str(FIXTURES / "records.ts"))
    # both the directly-referenced record and the one it nests are declared,
    # dependency-first so the file compiles as-is
    assert "type GeoPoint = { lat: Float, lng: Float }" in source
    assert source.index("type GeoPoint") < source.index("type UserRecord")
    ir = compile_source(source, "records.rvl")
    assert set(ir["types"]) == {"GeoPoint", "UserRecord"}
    fields = ir["types"]["UserRecord"]["fields"]
    assert fields["display_name"] == "Str"             # camelCase -> snake_case
    assert fields["home"] == "Opt[GeoPoint]"           # optional nested record
    assert fields["tags"] == "List[Str]"
    methods = _methods(ir, "Directory")
    assert methods["register"]["params"] == [{"name": "user", "type": "UserRecord"}]
    # the `UserId = string` alias resolves inline (revl has no type alias)
    assert methods["lookup"]["params"] == [{"name": "id", "type": "Str"}]
    assert methods["lookup"]["returns"] == "Opt[UserRecord]"
    assert "record type `UserRecord` transcribed from" in source


def test_a_nominal_type_with_no_local_definition_is_still_refused():
    """The record-following of (c) does not weaken the refusal: a nominal type
    that is neither defined in the file nor reachable through a *local* import
    (here a bare name, and a name imported from a package) is still refused."""
    plugin = """
    import { Widget } from 'some-package'
    export const provide = 'x'
    export class X extends Service {
      constructor(ctx) { super(ctx, 'x') }
      draw(w: Widget): void {}
    }
    """
    with pytest.raises(RevlError) as excinfo:
        import_cordis(plugin, filename="x.ts")
    assert "nominal type `Widget` is not defined in this file" in str(excinfo.value)


# --------------------------------------------------------- the emission rule

def test_untyped_ts_defaults_every_operation_to_emission():
    source = import_cordis_file(str(FIXTURES / "greeter.ts"))
    # neither `greet` nor `record` carries a `@revl:pure` claim, so both emit
    assert "emission fn greet(name: Str) -> Str" in source
    assert "untyped TS carries no reversibility claim" in source
    ir = compile_source(source, "g.rvl")
    assert _methods(ir, "Greeter")["greet"]["emission"] is True
    assert _extern_classes(ir)["cordis_greeter_greet"] == "emission"


def test_a_jsdoc_pure_tag_weakens_to_plain():
    """`@revl:pure` in the method's JSDoc is the plugin *author's* claim — the
    analogue of WIT's `/// @revl:pure`."""
    source = import_cordis_file(str(FIXTURES / "cache.ts"))
    assert "plain by assertion: `@revl:pure`" in source
    ir = compile_source(source, "cache.rvl")
    assert _methods(ir, "Store")["get"]["emission"] is False
    assert _extern_classes(ir)["cordis_store_get"] == "pure"
    # everything without the tag stays emission
    assert _methods(ir, "Store")["set"]["emission"] is True


PLAIN_PLUGIN = """
export const provide = 'probe'
export class Probe extends Service {
  constructor(ctx) { super(ctx, 'probe') }
  readOnlyLooking(key: string): string { return key }
}
"""


def test_the_importing_engineer_can_assert_purity():
    source = import_cordis(PLAIN_PLUGIN, filename="probe.ts",
                           pure=["Probe.readOnlyLooking"])
    assert "plain by assertion: `--pure Probe.readOnlyLooking`" in source
    ir = compile_source(source, "probe.rvl")
    assert _methods(ir, "Probe")["read_only_looking"]["emission"] is False
    assert _extern_classes(ir)["cordis_probe_read_only_looking"] == "pure"


def test_a_bare_method_name_also_matches():
    source = import_cordis(PLAIN_PLUGIN, filename="probe.ts",
                           pure=["readOnlyLooking"])
    assert "\n  fn read_only_looking(key: Str) -> Str" in source


def test_a_pure_assertion_that_matches_nothing_is_an_error():
    """Silently leaving it `emission` would be safe but dishonest: the engineer
    believes they narrowed something."""
    with pytest.raises(RevlError) as excinfo:
        import_cordis(PLAIN_PLUGIN, filename="probe.ts", pure=["Probe.reedOnly"])
    assert "--pure named 'Probe.reedOnly', which is not a method" in str(excinfo.value)


def test_a_plain_op_backed_by_emission_is_rejected():
    """Why the generated plain `fn` is trustworthy: G4 makes a service
    declaration an upper bound, so the compiler refuses the mismatch the
    importer would have to make in order to under-declare."""
    source = import_cordis(PLAIN_PLUGIN, filename="probe.ts",
                           pure=["Probe.readOnlyLooking"])
    broken = source.replace("extern pure fn", "extern emission fn")
    with pytest.raises(RevlError) as excinfo:
        compile_source(broken, "broken.rvl")
    assert "declared plain, but this implementation reaches" in str(excinfo.value)


# -------------------------------- the honest hard part: unrecovered signatures

def test_an_unrecoverable_signature_is_refused_loudly():
    """A parameter with no TypeScript type and no JSDoc type cannot be
    recovered — and is refused, not guessed."""
    with pytest.raises(RevlError) as excinfo:
        import_cordis_file(str(FIXTURES / "untyped.ts"))
    message = str(excinfo.value)
    assert "untyped.ts:" in message                        # a real line
    assert "unrecoverable signature on `forecast`" in message
    assert "parameter `city` has no TypeScript type" in message
    assert "--mark-unrecovered" in message                 # names the way forward


def test_mark_unrecovered_emits_a_loud_marker_and_still_compiles():
    """`--mark-unrecovered` keeps the recoverable surface and replaces the
    unrecoverable method with a `// UNRECOVERED` marker — which is a comment,
    so the file still compiles."""
    source = import_cordis_file(str(FIXTURES / "mixed.ts"), mark_unrecovered=True)
    assert "// UNRECOVERED: Registry.install" in source
    # the good operation is still there and callable
    ir = compile_source(source, "mixed.rvl")
    assert list(_methods(ir, "Registry")) == ["version"]
    assert _methods(ir, "Registry")["version"]["returns"] == "Str"
    # the marker never emits a guessed signature
    assert "fn install(" not in "".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//"))


def test_a_service_of_only_unrecovered_ops_is_refused_even_when_marking():
    """`untyped.ts`'s single method is unrecoverable, so even with the marker
    flag there is no callable surface to generate."""
    with pytest.raises(RevlError) as excinfo:
        import_cordis_file(str(FIXTURES / "untyped.ts"), mark_unrecovered=True)
    assert "no callable surface remains" in str(excinfo.value)


def test_an_inline_object_type_is_refused_not_flattened():
    plugin = """
    export const provide = 'x'
    export class X extends Service {
      constructor(ctx) { super(ctx, 'x') }
      shape(v: { a: string }): void {}
    }
    """
    with pytest.raises(RevlError) as excinfo:
        import_cordis(plugin, filename="x.ts")
    assert "inline object/tuple type" in str(excinfo.value)


def test_an_unknown_nominal_type_is_refused():
    plugin = """
    export const provide = 'x'
    export class X extends Service {
      constructor(ctx) { super(ctx, 'x') }
      find(u: UserRecord): void {}
    }
    """
    with pytest.raises(RevlError) as excinfo:
        import_cordis(plugin, filename="x.ts")
    assert "nominal type `UserRecord` is not defined in this file" in str(excinfo.value)


# ------------------------------------------------------------ refusals: shape

def test_a_plugin_with_no_provided_service_is_refused():
    with pytest.raises(RevlError) as excinfo:
        import_cordis("export const name = 'noop'\n", filename="noop.ts")
    assert "provides no service this importer can find" in str(excinfo.value)


def test_a_provided_service_with_no_readable_surface_is_refused():
    """A provide with no class and no readable object literal — a dynamically
    assembled service the importer will not fake."""
    plugin = "export const provide = 'dyn'\nexport function apply(ctx) { build(ctx) }\n"
    with pytest.raises(RevlError) as excinfo:
        import_cordis(plugin, filename="dyn.ts")
    assert "exposes no method surface this importer can read" in str(excinfo.value)


# ----------------------------------------------------------------- name mapping

def test_a_type_shadowing_a_revl_builtin_is_renamed_or_the_key_is_safe():
    """A provide key that would name a revl built-in service gets a safe name."""
    plugin = """
    export const provide = 'list'
    export class L extends Service {
      constructor(ctx) { super(ctx, 'list') }
      at(i: bigint): string { return '' }
    }
    """
    source = import_cordis(plugin, filename="l.ts")
    assert "service ListService {" in source
    assert compile_source(source, "l.rvl")["services"]["ListService"]


# ------------------------------------------------------------------- the CLI

def test_cli_writes_generated_source(tmp_path):
    out = tmp_path / "cache.rvl"
    assert main(["import", "cordis", str(FIXTURES / "cache.ts"), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "service Store {" in text
    assert compile_source(text, str(out))["services"]["Store"]


def test_cli_backend_choice_reaches_the_host_bodies(tmp_path):
    out = tmp_path / "cache.rvl"
    assert main(["import", "cordis", str(FIXTURES / "cache.ts"),
                 "--backend", "py", "-o", str(out)]) == 0
    source = out.read_text(encoding="utf-8")
    assert "= @py { #" in source


def test_cli_mark_unrecovered_flag(tmp_path):
    out = tmp_path / "mixed.rvl"
    assert main(["import", "cordis", str(FIXTURES / "mixed.ts"),
                 "--mark-unrecovered", "-o", str(out)]) == 0
    assert "// UNRECOVERED" in out.read_text(encoding="utf-8")


def test_cli_reports_a_refusal_and_exits_nonzero(capsys):
    assert main(["import", "cordis", str(FIXTURES / "untyped.ts")]) == 1
    assert "unrecoverable signature on `forecast`" in capsys.readouterr().err


def test_cli_missing_file(tmp_path, capsys):
    assert main(["import", "cordis", str(tmp_path / "absent.ts")]) == 1
    assert "cannot read" in capsys.readouterr().err
