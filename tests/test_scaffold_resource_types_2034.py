"""The Unit-hole producers follow the declared resource's type arguments (#2034).

`--resource` is what carries the type arguments, and `_resource_writes()` used
to render every offer from the checker's *static* `_HOST_ARG_SIG` — so a
`Map[Int, Str]` resource was offered `insert(<str_0: Str>, <str_1: Str>)`,
exactly as a `Map[Str, Str]` one. The fill spec is the only signal an author
gets for a `Unit` hole (the checker admits the type-correct fill either way),
so the wrong types are a misleading hint rather than a false refusal.

The declared type's arguments ARE the family's type parameters, in order, so
for `Map[K, V]` the `insert` offer is `(k: K, v: V)` and `remove` is `(k: K)`.
`Map[Str, Str]` is therefore offered exactly as before — the #2015 pin.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.fillspec import enrich  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


#: issue #1948's shape: a `Unit` method whose component acquired its own
#: resource, so a write on that resource is the method's fill.
def _spec(resource: str) -> dict:
    return {"service": "Audit", "provides": "audit", "resource": resource,
            "methods": ["record(msg: Str) -> Unit"]}


def _scaffold(resource: str) -> str:
    served = _call("revl_scaffold", _spec(resource))
    assert served["ok"] is True, served
    return served["source"]


def _unit_hole_spec(source: str) -> dict:
    """The fill spec on the `Unit` hole the scaffold wrote."""
    obligations = [ob for ob in enrich(compile_source(source, "audit.rvl"))
                   if ob["fillSpec"]["construct"] == "provide-method"]
    assert len(obligations) == 1, obligations
    return obligations[0]["fillSpec"]


def _offers(source: str) -> list[str]:
    return [p["write"] for p in _unit_hole_spec(source)["fillable"]["producers"]]


_PLACEHOLDER = re.compile(r"<(\w+): ([^>]+)>")


def _placeholder_types(offer: str) -> list[str]:
    r"""The declared types of an offer's `<name: Type>` placeholders, in order.
    `\w+` is what makes the name writable code, so a name that is not an
    identifier would not match here at all."""
    return [m.group(2) for m in _PLACEHOLDER.finditer(offer)]


def _concrete(offer: str, bindings: list[dict]) -> str:
    """An offer with its placeholders bound: a binding by type, else the
    literal its type has. Every placeholder must be bound."""
    literal = {"Str": '"..."', "Int": "0", "Bool": "false"}

    def bind(match):
        wanted = match.group(2)
        named = [b["name"] for b in bindings if b.get("type") == wanted]
        if named:
            return named[0]
        return literal[wanted]

    return _PLACEHOLDER.sub(bind, offer)


# --------------------------------------- the declared arguments are the types


def test_the_declared_type_arguments_are_the_offered_parameter_types():
    """`Map[Int, Str]` offers the write its own declaration describes."""
    assert _offers(_scaffold("Map[Int, Str]")) == [
        "resource.insert(<int_0: Int>, <str_1: Str>)",
        "resource.remove(<int_0: Int>)"]


def test_the_offer_follows_a_swapped_declaration():
    """The key and value slots are read positionally, not by name: swapping
    the declaration swaps the offered types."""
    assert _offers(_scaffold("Map[Str, Int]")) == [
        "resource.insert(<str_0: Str>, <int_1: Int>)",
        "resource.remove(<str_0: Str>)"]


def test_the_offered_types_are_the_declared_ones_for_every_argument():
    """Whatever the declaration names, the offer names it too — this is the
    assertion that fails on the static table, which always says Str."""
    for resource, args in (("Map[Int, Str]", ["Int", "Str"]),
                           ("Map[Str, Int]", ["Str", "Int"]),
                           ("Map[Bool, Str]", ["Bool", "Str"])):
        offers = _offers(_scaffold(resource))
        assert _placeholder_types(offers[0]) == args, (resource, offers)
        # `remove` takes the key alone
        assert _placeholder_types(offers[1]) == args[:1], (resource, offers)


# ------------------------------------------------------- the #2015 pin holds


def test_a_str_str_declaration_is_offered_exactly_as_before():
    """#2015's `Map[Str, Str]` case: where the static table happened to be
    right, the offer is byte-identical to the one it produced."""
    assert _offers(_scaffold("Map[Str, Str]")) == [
        "resource.insert(<str_0: Str>, <str_1: Str>)",
        "resource.remove(<str_0: Str>)"]


def test_a_resource_naming_no_host_family_still_offers_nothing():
    """The boundary is unchanged: a resource whose type names no host family
    invents no producer."""
    for resource in ("AuditResource", "Socket"):
        spec = _unit_hole_spec(_scaffold(resource))
        assert spec["fillable"]["producers"] == [], resource
        assert {"name": "resource", "type": resource} in spec["bindings"]


# ------------------------------------------------- a nested argument stays code


def test_a_nested_type_argument_keeps_a_writable_placeholder_name():
    """The placeholder is `<name: Type>`, so the name has to stay an
    identifier even when the type is not a word — otherwise the offer is not
    code an author can fill."""
    offers = _offers(_scaffold("Map[Str, List[Int]]"))
    assert offers == [
        "resource.insert(<str_0: Str>, <list_int_1: List[Int]>)",
        "resource.remove(<str_0: Str>)"]
    for offer in offers:
        names = [m.group(1) for m in _PLACEHOLDER.finditer(offer)]
        assert names and all(n.isidentifier() for n in names), offer
        # every placeholder is consumed by the offer's own type list, so no
        # `<...>` is left behind as prose
        assert len(_placeholder_types(offer)) == offer.count("<"), offer


# --------------------------------------------- the filled hole still compiles


@pytest.mark.parametrize("resource,fill", [
    ("Map[Str, Str]", "resource.insert(msg, msg)"),
    ("Map[Int, Str]", "resource.insert(0, msg)"),
    ("Map[Str, Int]", "resource.insert(msg, 0)"),
])
def test_filling_the_hole_with_the_offer_compiles_and_admits(resource, fill):
    """The offer is not merely well-typed prose: bound and substituted into
    the hole it leaves no holes, and the filled component is admissible."""
    source = _scaffold(resource)
    spec = _unit_hole_spec(source)
    assert {"name": "resource", "type": resource} in spec["bindings"]
    offered = _concrete(spec["fillable"]["producers"][0]["write"],
                        spec["bindings"])
    assert offered == fill
    filled = re.sub(r'hole\[Unit\] "produce record\'s Unit result[^"]*"',
                    offered, source, count=1)
    assert offered in filled  # the substitution landed on the method's hole
    # the acquisition and its inverse, as `Map.new`/`Map.drop` declare them
    filled = filled.replace(
        f'effect hole[{resource}] "acquire the resource '
        f'AuditProvider manages"', "effect Map.new()", 1).replace(
        'undo hole[Unit] "release `resource` fully (no residue): the inverse '
        'its acquisition declares"', "undo resource.drop()", 1)
    assert not compile_source(filled, "filled.rvl").get("holes")
    compile_source(filled, "candidate.rvl", manifest=compile_source(""))
