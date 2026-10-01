"""fillSpec version 2: the crossing a hole MAY contain, and how it is written.

Version 1 carried `capability.mayEmit`, a permission whose name reads as an
obligation: "this hole may emit" was taken as "this hole is an emission
position, so the fill must emit". It never is. A declared `emission`
operation bounds what its provider may do, and a provider may always be purer
than declared (G4). Version 2 keeps every version-1 field, and adds:

  * `version: 2`;
  * `capability.permitsCrossing`, the same bool under a name that cannot be
    read as an obligation;
  * `crossing`: `permitted`, `required` (always false), the call-site `form`,
    and `calls`, every crossing available at this position already written
    in its call-site form;
  * `reachableServices[].callableHere`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import fillspec  # noqa: E402

SOURCE = """extern emission fn audit(line: Str) -> Int = @py { return 1 }
extern emission[mail] fn notify(to: Str) -> Int = @py { return 1 }
service Db { fn read(k: Str) -> Str
             emission fn put(k: Str, v: Str) -> Int }
service Log { emission fn write(msg: Str) -> Int }
service Audit { fn digest(text: Str) -> Str
                emission[db] fn record(entry: Str) -> Int
                emission fn any(entry: Str) -> Int }
component A requires db: Db, log: Log provides audit: Audit {
  provide audit {
    fn digest(text) = hole[Str] "digest the text"
    fn record(entry) = hole[Int] "store the entry"
    fn any(entry) = hole[Int] "do anything"
  }
}
"""


def _spec(message: str) -> dict:
    for ob in fillspec.enrich(compile_source(SOURCE)):
        if ob["message"] == message:
            return ob["fillSpec"]
    raise AssertionError(f"no hole {message!r}")


def test_the_spec_is_versioned_and_keeps_the_version_one_fields():
    spec = _spec("store the entry")
    assert spec["version"] == fillspec.FILL_SPEC_VERSION == 3
    cap = spec["capability"]
    assert cap["mayEmit"] is cap["permitsCrossing"] is True
    assert cap["bound"] == ["db"]
    assert {"expected", "capability", "bindings",
            "reachableServices"} <= set(spec)


def test_a_crossing_is_permitted_never_required():
    for message in ("digest the text", "store the entry", "do anything"):
        crossing = _spec(message)["crossing"]
        assert crossing["required"] is False
        assert "never an obligation" in crossing["rule"]


def test_a_pure_position_permits_no_crossing_and_gives_no_form():
    spec = _spec("digest the text")
    assert spec["crossing"]["permitted"] is False
    assert spec["crossing"]["form"] is None
    assert spec["crossing"]["calls"] == []
    callable_here = {(e["instance"], e["method"]): e["callableHere"]
                     for e in spec["reachableServices"]}
    # a plain operation is callable anywhere; an emission one is not here
    assert callable_here == {("db", "put"): False, ("db", "read"): True,
                             ("log", "write"): False}


def test_a_scoped_position_lists_only_the_crossings_inside_its_bound():
    spec = _spec("store the entry")
    crossing = spec["crossing"]
    assert crossing["permitted"] is True
    assert crossing["form"].startswith("emit <key>.<operation>(<args>)")
    assert crossing["calls"] == [{
        "write": "emit db.put(<k: Str>, <v: Str>)", "returns": "Int",
        "carrier": "service", "capabilities": ["db"]}]
    callable_here = {(e["instance"], e["method"]): e["callableHere"]
                     for e in spec["reachableServices"]}
    assert callable_here == {("db", "put"): True, ("db", "read"): True,
                             ("log", "write"): False}


def test_a_bare_emission_position_lists_every_crossing():
    writes = [c["write"] for c in _spec("do anything")["crossing"]["calls"]]
    assert writes == ["emit db.put(<k: Str>, <v: Str>)",
                      "emit log.write(<msg: Str>)",
                      "emit audit(<line: Str>)",
                      "emit notify(<to: Str>)"]


def _bind(write: str, value: str) -> str:
    """A listed call with every `<name: Type>` placeholder bound to `value`."""
    import re
    return re.sub(r"<[^>]*>", value, write)


def test_a_listed_crossing_written_as_the_fill_compiles():
    """The form is the one the checker accepts: filling the scoped hole with
    the listed call, its placeholders bound, compiles."""
    (call,) = _spec("store the entry")["crossing"]["calls"]
    filled = SOURCE.replace('hole[Int] "store the entry"',
                            _bind(call["write"], "entry"))
    ir = compile_source(filled)
    assert [h["message"] for h in ir.get("holes") or []] == [
        "digest the text", "do anything"]


def test_a_crossing_outside_the_bound_written_as_the_fill_is_refused():
    """...and one the spec does not list is the G4 refusal it predicts."""
    from revl.errors import RevlError
    listed = {c["write"] for c in _spec("store the entry")["crossing"]["calls"]}
    unlisted = "emit log.write(<msg: Str>)"
    assert unlisted not in listed
    filled = SOURCE.replace('hole[Int] "store the entry"',
                            _bind(unlisted, "entry"))
    try:
        compile_source(filled)
    except RevlError as error:
        assert "emission[db]" in error.message
    else:
        raise AssertionError("a crossing outside the bound compiled")
