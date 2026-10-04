"""A service imported on its own carries the record types its operations name.

Issue #1899. Importing only `Store` from a module did not make that module a
pure dependency, so none of its types entered the merged program, and a
structural record literal passed to `store.put(row: Item)` was checked against
an `Item` the calling module could not resolve: refused T1, "expects `Item`,
got `{qty: Int, ref: Str}`", for equal shapes. The same literal passed to an
imported `fn` unified, as docs/records.md section 3.1 says a structural type
unifies field-wise with the nominal record it meets at any declared boundary.

The fix (`src/revl/compiler.py`, `_service_type_closure`): a named service
import carries the PUBLIC types its operations name, closed over the types
those declarations name in turn. They join the privacy pass's type namespace,
so a caller's own private type of the same name is renamed apart, exactly as it
is against an imported `fn`'s module.

The self-host gate reads one text and does not load `use`d modules, so it has
no multi-module path to keep in step: it raises no objection to any of these
programs, before and after.
"""

from pathlib import Path

import pytest

from revl import compile_files
from revl.errors import RevlError

LIB = '''pub type Item = { ref: Str, qty: Int }
pub fn count(i: Item) -> Int { return i.qty }
pub service Store {
  fn put(row: Item) -> Str
}
'''

LIB2 = '''pub type Line = { ref: Str, qty: Int }
pub type Order = { line: Line, note: Str }
type Hidden = { x: Int }
pub fn h(x: Hidden) -> Int { return x.x }
pub service Orders {
  fn place(o: Order) -> Str
}
'''

_FN = '''use "./lib.rvl" { IMPORTS }
service Front { fn n() -> Int }
component FrontKit provides front: Front {
  provide front {
    fn n() = count({ ref: "a", qty: 2 })
  }
}
'''

_SVC = '''use "./lib.rvl" { IMPORTS }
PRELUDEservice Front { fn add(ref: Str, qty: Int) -> Str }
component FrontKit requires store: Store provides front: Front {
  provide front {
    fn add(ref, qty) = store.put(ARG)
  }
}
'''


def _compile(tmp_path: Path, source: str) -> dict:
    (tmp_path / "lib.rvl").write_text(LIB)
    (tmp_path / "lib2.rvl").write_text(LIB2)
    main = tmp_path / "main.rvl"
    main.write_text(source)
    return compile_files([str(main)])


def _svc(imports: str, arg: str = "{ ref: ref, qty: qty }", prelude: str = "") -> str:
    return (_SVC.replace("IMPORTS", imports).replace("ARG", arg)
            .replace("PRELUDE", prelude))


@pytest.mark.parametrize("source", [
    _FN.replace("IMPORTS", "count"),
    _FN.replace("IMPORTS", "Item, count"),
    _svc("Store"),
    _svc("Item, Store"),
], ids=["fn", "fn-with-type", "service", "service-with-type"])
def test_the_four_rows_of_the_issue_are_all_admitted(tmp_path, source):
    """The issue's table: before this only the service-only import refused."""
    ir = _compile(tmp_path, source)
    assert "Item" in ir["types"]


def test_a_wrong_shape_is_still_refused_against_the_declaring_module_s_type(tmp_path):
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, _svc("Store", arg='{ ref: ref, qty: "many" }'))
    assert excinfo.value.code == "T1"
    assert excinfo.value.message == (
        "`store.put` argument `row` expects `Item`, got `{qty: Str, ref: Str}`")


def test_the_types_an_operation_names_are_carried_transitively(tmp_path):
    source = '''use "./lib2.rvl" { Orders }
service Front { fn add(ref: Str, qty: Int) -> Str }
component FrontKit requires orders: Orders provides front: Front {
  provide front {
    fn add(ref, qty) = orders.place({ line: { ref: ref, qty: qty }, note: "x" })
  }
}
'''
    ir = _compile(tmp_path, source)
    # the two public types the signature reaches, and nothing else of lib2:
    # neither its private `Hidden` nor its `fn h`
    assert sorted(ir["types"]) == ["Line", "Order"]
    assert all(fn["name"] != "h" for fn in ir.get("functions", []))


def test_a_caller_s_own_private_type_of_the_same_name_is_renamed_apart(tmp_path):
    """Before this the caller's own `type Item = { other: Bool }` was the only
    `Item` in the merged program, so `store.put`'s argument was checked
    against the CALLER's record. The service's `Item` is the declaring
    module's now, and the caller's private one is renamed apart, as it is
    against an imported `fn`'s module."""
    ir = _compile(tmp_path, _svc("Store", prelude="type Item = { other: Bool }\n"))
    assert ir["types"]["Item"]["fields"] == {"ref": "Str", "qty": "Int"}
    assert any(name.startswith("Item__m") for name in ir["types"])
