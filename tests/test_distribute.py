"""Distributability verdict tests (docs/interop-bridge.md §4).

A service crosses a process seam cleanly (transport-safe) only when every
operation is `async fn` with value-typed parameters and returns; a sync
method, an emission, or a resource type in a signature pins it to one address
space. These assert the verdict the `revl audit` bridge report is built on.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.distribute import distributability  # noqa: E402


def _dist(tmp_path, source):
    src = tmp_path / "svc.rvl"
    src.write_text(source, encoding="utf-8")
    return distributability(compile_files([str(src)]))


def test_all_async_value_typed_is_transport_safe(tmp_path):
    verdicts = _dist(tmp_path, """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
""")
    assert verdicts["Cache"]["verdict"] == "transport-safe"


def test_sync_and_emission_are_address_space_bound(tmp_path):
    verdicts = _dist(tmp_path, """
type Row = { id: Int, name: Str }
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
""")
    database = verdicts["Database"]
    assert database["verdict"] == "address-space-bound"
    assert "query: not async fn" in database["reasons"]
    assert "execute: emission (sync)" in database["reasons"]


def test_resource_return_crosses_by_handle(tmp_path):
    verdicts = _dist(tmp_path, """
type Socket = { fd: Int }
extern acquire fn open_sock(port: Int) -> Socket undo close_sock(sock)
  = @py { return {"fd": port} }
service Net {
  async fn accept() -> Socket
}
""")
    net = verdicts["Net"]
    assert net["verdict"] == "address-space-bound"
    assert net["reasons"] == ["accept: resource type Socket crosses"]
