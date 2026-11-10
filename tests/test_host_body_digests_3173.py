"""Issue #3173: the host-body disclosure record carries a body digest.

Two generations whose extern has the same name, classification and backends
but a different `@py` body used to produce identical records, so a consumer
comparing a running generation to a candidate could not tell a replaced body
from an unchanged one. `host_body_entry` now adds `digests`: one lowercase hex
sha256 per backend over the verbatim body string, so a one-byte change moves
that backend's digest and nothing else on the record has to.
"""

from __future__ import annotations

import hashlib

from revl import compile_source
from revl.mcp.session import host_body_entry
from revl.plan import plan

_SERVICE = """
service Greeter {
  fn greet(x: Str) -> Str
}

component GreeterSvc provides greeter: Greeter {
  provide greeter {
    fn greet(x) = host_greet(x)
  }
}
"""


def _py_source(body: str) -> str:
    return ("extern pure fn host_greet(x: Str) -> Str = @py {\n"
            f"    return {body}\n"
            "}\n" + _SERVICE)


def _two_backend_source(py_body: str, ts_body: str) -> str:
    return ("extern pure fn host_greet(x: Str) -> Str\n"
            f"  = @py {{ {py_body} }}\n"
            f"  = @ts {{ {ts_body} }}\n" + _SERVICE)


def _only_entry(result: dict) -> dict:
    assert result["admissible"] is True, result
    entries = result["candidateHostBodies"]
    assert len(entries) == 1, entries
    return entries[0]


def _without_digests(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "digests"}


def _digest(extern: dict, backend: str) -> str:
    body = extern["bodies"][backend]
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_a_replaced_body_changes_only_the_digest():
    """The record's other fields are identical, so `digests` is the only
    evidence of the replacement, and it is the digest of the verbatim body."""
    one = _only_entry(plan(source=_py_source('"one:é:" + x'), include_ir=True))
    two = _only_entry(plan(source=_py_source('"two:" + x'), include_ir=True))

    assert set(one) == {"extern", "classification", "backends", "digests"}
    assert _without_digests(one) == _without_digests(two)
    assert one["digests"] != two["digests"]
    assert list(one["digests"]) == one["backends"] == ["py"]

    extern = next(e for e in compile_source(
        _py_source('"one:é:" + x'), "one.rvl")["externs"]
        if e["name"] == "host_greet")
    assert one["digests"]["py"] == _digest(extern, "py")

    same = _only_entry(plan(source=_py_source('"one:é:" + x'), include_ir=True))
    assert same == one


def test_a_candidate_that_replaces_one_backend_moves_only_that_digest():
    """A two-backend extern whose `@py` body changed and whose `@ts` body did
    not: the candidate record keeps the unchanged backend's digest."""
    running = compile_source(
        _two_backend_source('return "py:" + x', 'return "ts:" + x'),
        "running.rvl")
    before = host_body_entry(next(e for e in running["externs"]
                                  if e["name"] == "host_greet"))
    after = _only_entry(plan(
        source=_two_backend_source('return "py:" + x + "!"',
                                   'return "ts:" + x'),
        manifest=running, include_ir=True))

    assert _without_digests(after) == _without_digests(before)
    assert list(after["digests"]) == after["backends"] == ["py", "ts"]
    assert after["digests"]["ts"] == before["digests"]["ts"]
    assert after["digests"]["py"] != before["digests"]["py"]
    moved = {backend for backend in before["digests"]
             if before["digests"][backend] != after["digests"][backend]}
    assert moved == {"py"}
