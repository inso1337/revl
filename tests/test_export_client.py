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
