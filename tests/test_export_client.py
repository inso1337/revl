"""`revl export client --lang ts` — a revl service becomes a typed remote client.

Item 424 gap (c) slice C1 (docs/design/424-dsh-language-gaps.md, D-424c.6/.7):
pure IR codegen of a typed client for a NON-revl consumer, over the canonical
value encoding the four bridges already speak (docs/interop-bridge.md). No
runtime, no emission, no language change.

Three claims are under test:

  * The client's TS TYPES ARE the canonical wire encoding — a record is a plain
    object, `Opt[T]` is `T | null`, and a user ADT or `Result` is the
    adjacently-tagged `{$kind, $value}` object — so a value round-trips to the
    placement bridge by construction.
  * D-424c.7/.8: the client carries the gate FRONTIER (item 338) and makes NO
    safety claim about the callee (no "verified remote").
  * A method the projection cannot express (a resource handle; a non-`Str`-key
    `Map`) is REFUSED at generation, naming the method.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.export_client import export_client  # noqa: E402
from revl.gate import gate_version  # noqa: E402


def _compile(source: str, tmp_path: Path, name: str = "x.rvl") -> dict:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return compile_files([str(path)])


_INVENTORY = """
type Item = { sku: Str, qty: Int }
type Outcome = Found(Item) | Missing

service Inventory {
  fn lookup(sku: Str) -> Outcome
  fn tally(items: List[Item]) -> Result[Int, Str]
  fn maybe(sku: Str) -> Opt[Item]
}

extern pure fn wire(sku: Str) -> Outcome = @py { return None }

component Store provides inv: Inventory {
  provide inv {
    fn lookup(sku) = wire(sku)
    fn tally(items) = Ok(0)
    fn maybe(sku) = None
  }
}
"""


def test_types_are_the_canonical_wire_encoding(tmp_path):
    ir = _compile(_INVENTORY, tmp_path)
    ts = export_client(ir, lang="ts", service="Inventory")

    # a record -> a plain TS interface (no $kind marker; records never carry one)
    assert "export interface Item {" in ts
    assert "sku: string;" in ts
    assert "qty: number;" in ts
    assert '"$kind"' not in ts.split("export interface Item")[1].split("}")[0]

    # a user ADT -> an adjacently-tagged discriminated union, nullary case has
    # no $value (exactly the canonical encoding)
    assert 'export type Outcome =' in ts
    assert '{ "$kind": "Found", "$value": Item }' in ts
    assert '{ "$kind": "Missing" }' in ts

    # Opt[T] -> the bare value or null, never tagged
    assert "async maybe(sku: string): Promise<Item | null>" in ts

    # Result -> the tagged Ok/Err object shape
    assert '{ "$kind": "Ok", "$value": number }' in ts
    assert '{ "$kind": "Err", "$value": string }' in ts

    # List[T] -> Array<T>
    assert "items: Array<Item>" in ts

    # one client class, one async method per operation, over a Transport seam
    assert "export class InventoryClient {" in ts
    assert "constructor(private readonly transport: Transport) {}" in ts
    assert 'await this.transport.call("lookup", [sku])' in ts


def test_carries_the_frontier_and_makes_no_safety_claim(tmp_path):
    ir = _compile(_INVENTORY, tmp_path)
    ts = export_client(ir, lang="ts", service="Inventory")

    # item 338: the gate frontier is a first-class field on the artifact
    frontier = gate_version()["frontier"]
    assert f'export const REVL_GATE_FRONTIER = "{frontier}";' in ts
    assert frontier  # non-empty

    # D-424c.8: a client is bounded LOCALLY and claims nothing about the callee.
    # No verified-remote badge, anywhere.
    lowered = ts.lower()
    assert "local contract only" in lowered
    assert "verified" not in lowered or "verified-remote" in lowered
    assert "no claim" in lowered


def test_emits_an_http_transport_onto_the_serve_http_face(tmp_path):
    # D-424c.6: the client ships a ready Transport onto `revl serve --http`, so
    # the two C1 halves connect without the consumer hand-writing a fetch. It
    # POSTs the positional args as a JSON array and reads `{ok, value}`.
    ir = _compile(_INVENTORY, tmp_path)
    ts = export_client(ir, lang="ts", service="Inventory")
    assert "export function httpTransport(base: string): Transport {" in ts
    assert "method: \"POST\"" in ts
    assert "JSON.stringify(args)" in ts
    assert "return reply.value;" in ts
    # transport only — it still makes no claim about the remote
    assert "no claim about the remote" in ts.lower()


def test_composition_exports_every_provided_service(tmp_path):
    ir = _compile(_INVENTORY, tmp_path)
    ts = export_client(ir, lang="ts", composition=True)
    assert "export class InventoryClient {" in ts


def test_resource_method_is_refused_naming_the_method(tmp_path):
    source = """
    extern pure fn close_ledger(h: LedgerHandle) = @py { pass }
    extern acquire fn open_ledger(path: Str) -> LedgerHandle undo close_ledger(result)
      = @py { return 1 }
    service Pool {
      fn grab(path: Str) -> LedgerHandle
    }
    component P provides p: Pool {
      provide p { fn grab(path) = open_ledger(path) }
    }
    """
    ir = _compile(source, tmp_path)
    with pytest.raises(RevlError) as exc:
        export_client(ir, lang="ts", service="Pool")
    message = str(exc.value)
    assert "Pool.grab" in message
    assert "resource" in message


def test_non_str_map_key_is_refused_naming_the_method(tmp_path):
    source = """
    service Counter {
      fn count(m: Map[Int, Str]) -> Int
    }
    component C provides c: Counter {
      provide c { fn count(m) = 0 }
    }
    """
    ir = _compile(source, tmp_path)
    with pytest.raises(RevlError) as exc:
        export_client(ir, lang="ts", service="Counter")
    message = str(exc.value)
    assert "Counter.count" in message
    assert "key type `Int`" in message


def test_unknown_service_is_refused(tmp_path):
    ir = _compile(_INVENTORY, tmp_path)
    with pytest.raises(RevlError) as exc:
        export_client(ir, lang="ts", service="Nope")
    assert "no service named `Nope`" in str(exc.value)


def test_unknown_language_is_refused(tmp_path):
    ir = _compile(_INVENTORY, tmp_path)
    with pytest.raises(RevlError) as exc:
        export_client(ir, lang="go", service="Inventory")
    assert "unknown client language `go`" in str(exc.value)


def test_cli_writes_the_client_to_output(tmp_path, capsys):
    src = tmp_path / "inv.rvl"
    src.write_text(_INVENTORY, encoding="utf-8")
    out = tmp_path / "client.ts"
    code = main(["export", "client", str(src), "--lang", "ts",
                 "--service", "Inventory", "-o", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "export class InventoryClient {" in text
    assert "REVL_GATE_FRONTIER" in text


def test_cli_stdout_and_refusal_exit(tmp_path, capsys):
    # a good stdout run
    src = tmp_path / "inv.rvl"
    src.write_text(_INVENTORY, encoding="utf-8")
    assert main(["export", "client", str(src), "--service", "Inventory"]) == 0
    assert "InventoryClient" in capsys.readouterr().out

    # a refusal exits nonzero and names the method on stderr
    bad = tmp_path / "counter.rvl"
    bad.write_text(
        "service Counter { fn count(m: Map[Int, Str]) -> Int }\n"
        "component C provides c: Counter { provide c { fn count(m) = 0 } }\n",
        encoding="utf-8")
    assert main(["export", "client", str(bad), "--service", "Counter"]) == 1
    assert "Counter.count" in capsys.readouterr().err


# -------------------------------------------- `--face webui` (item 457 slice S4)

#: a component contributing a frontend through the ambient WebUI coeffect, with
#: the typed reactive channel: `add_entry`'s `data` parameter is the declared
#: state record, and the component's provision is the RPC surface
#: (docs/frontend-assets.md; design notes 526 and 530 Decision B).
_CONSOLE = """
type Panel = { label: Str, hits: Int }

service PanelRpc {
  fn open(id: Str) -> Bool
  fn touch(id: Str)
}

service WebUI {
  emission fn add_entry(
    dev_source: Str,
    prod_manifest: Str,
    routes: List[Str],
    data: Panel,
  ) -> Str
}

component Console requires webui: WebUI provides panel: PanelRpc {
  emit webui.add_entry("./f/entry.client.ts", "./f/dist/.vite/manifest.json",
                       ["/panel"], { label: "panel", hits: 0 })

  provide panel {
    fn open(id) = true
    fn touch(id) { }
  }
}
"""


def test_webui_face_projects_state_from_the_data_parameter(tmp_path):
    """The reactive state is the record type of `add_entry`'s `data` parameter,
    rendered `readonly`: the browser observes the synced state and changes it
    through the RPC half, never by assignment."""
    ir = _compile(_CONSOLE, tmp_path)
    face = export_client(ir, lang="ts", face="webui", component="Console")
    assert "export interface ConsoleState {" in face
    assert "  readonly label: string;" in face
    assert "  readonly hits: number;" in face
    # the state record is not ALSO emitted as a bare interface under its revl name
    assert "export interface Panel {" not in face


def test_webui_face_projects_rpc_from_the_declared_provisions(tmp_path):
    """The RPC method set is exactly the component's declared provisions (526),
    every call asynchronous because it crosses the WebSocket seam. A `Unit` result
    is `Promise<void>`, not `Promise<null>`."""
    ir = _compile(_CONSOLE, tmp_path)
    face = export_client(ir, lang="ts", face="webui", component="Console")
    assert "export interface ConsoleRpc {" in face
    assert "  open(id: string): Promise<boolean>;" in face
    assert "  touch(id: string): Promise<void>;" in face
    assert "export type ConsoleChannel = ConsoleState & ConsoleRpc;" in face


def test_webui_face_names_its_own_sources_in_the_header(tmp_path):
    """The generated file says which declarations it projects, so a reader can get
    back to the one definition, and marks itself DO NOT EDIT."""
    ir = _compile(_CONSOLE, tmp_path)
    face = export_client(ir, lang="ts", face="webui", component="Console")
    assert "--face webui" in face
    assert "// Component: Console" in face
    assert "Panel (the `data` parameter of `WebUI.add_entry`)" in face
    assert "panel: PanelRpc (the component's declared provisions)" in face
    assert "DO NOT EDIT" in face


def test_webui_face_refuses_a_component_with_no_data_parameter(tmp_path):
    """An `add_entry` with no `data` is the pre-projection surface: there is no
    typed reactive channel to render, and emitting an empty one would claim a
    contract that does not exist. Refused, naming what to add."""
    ir = _compile(_CONSOLE.replace("    data: Panel,\n", "")
                  .replace(', { label: "panel", hits: 0 }', ""), tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="webui", component="Console")
    assert "no `data` parameter" in str(excinfo.value)


def test_webui_face_refuses_an_untyped_data_parameter(tmp_path):
    """`data` must be a declared RECORD: the reactive state's fields are what the
    browser reads, so a scalar has nothing to project. (The record LITERAL itself
    is already checked against the declared parameter type by the compiler, which
    is why the published value and the projected type cannot drift.)"""
    ir = _compile(_CONSOLE.replace("    data: Panel,", "    data: Str,")
                  .replace('{ label: "panel", hits: 0 }', '"panel"'), tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="webui", component="Console")
    assert "not a declared record type" in str(excinfo.value)


def test_the_published_record_is_checked_against_the_declared_data_type(tmp_path):
    """The one-declaration property, from the other side: a published field the
    declared state record does not have is a COMPILE error, so the projected
    `useRpc<T>()` type cannot describe a surface the server does not publish."""
    source = _CONSOLE.replace('{ label: "panel", hits: 0 }',
                              '{ label: "panel", hits: 0, extra: true }')
    with pytest.raises((RevlError, Exception)) as excinfo:
        _compile(source, tmp_path, name="drift.rvl")
    assert "data" in str(excinfo.value)


def test_webui_face_refuses_a_component_that_provides_nothing(tmp_path):
    """With no provision there is no RPC surface, so the channel is half a
    contract. Refused naming `provides`, rather than emitting an empty interface."""
    source = _CONSOLE.replace(
        "component Console requires webui: WebUI provides panel: PanelRpc {",
        "component Console requires webui: WebUI {").replace(
        """
  provide panel {
    fn open(id) = true
    fn touch(id) { }
  }
""", "")
    ir = _compile(source, tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="webui", component="Console")
    assert "provides nothing" in str(excinfo.value)


def test_webui_face_refuses_a_component_with_no_webui_requirement(tmp_path):
    """A component that contributes no frontend has no channel to project, and the
    diagnostic names the coeffect it would need."""
    ir = _compile(_INVENTORY, tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="webui", component="Store")
    assert "no webui channel to project" in str(excinfo.value)


def test_webui_face_refuses_an_unknown_component(tmp_path):
    ir = _compile(_CONSOLE, tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="webui", component="Nope")
    assert "no component named `Nope`" in str(excinfo.value)


def test_the_two_faces_do_not_accept_each_other_s_selector(tmp_path):
    """`--component` names a webui channel and `--service` names a rest client;
    crossing them is refused rather than silently ignoring a flag."""
    ir = _compile(_CONSOLE, tmp_path)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="webui", service="PanelRpc")
    assert "pass `--component NAME`" in str(excinfo.value)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", component="Console")
    assert "`--component` selects a `--face webui` channel" in str(excinfo.value)
    with pytest.raises(RevlError) as excinfo:
        export_client(ir, lang="ts", face="soap", component="Console")
    assert "unknown client face `soap`" in str(excinfo.value)


def test_the_webui_face_is_reachable_from_the_cli(tmp_path, capsys):
    """`revl export client --face webui --component NAME` writes the projection,
    the verb the design note named (459 F7)."""
    path = tmp_path / "console.rvl"
    path.write_text(_CONSOLE, encoding="utf-8")
    out = tmp_path / "contract.ts"
    assert main(["export", "client", "--lang", "ts", "--face", "webui",
                 "--component", "Console", "-o", str(out), str(path)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "export type ConsoleChannel = ConsoleState & ConsoleRpc;" in text
