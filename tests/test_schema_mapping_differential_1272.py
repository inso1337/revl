"""The two type-to-schema mappings, and the differential that holds them
together (issue #1272, docs/design/1272-two-type-to-schema-mappings.md).

revl renders a surface type to a JSON-Schema-shaped dict in two places:

  `src/revl/mcp/schema.py:json_schema_for`   one self-contained fragment, for
                                             the MCP projection and the item-257
                                             validating boundary;
  `src/revl/export_openapi.py:_OpenApiExporter._schema_ref`
                                             a `$ref` into an OpenAPI document's
                                             `components/schemas` block, for
                                             `revl export openapi`.

Item 257's design note used to read as though the second did not exist. It does,
it renders several constructors differently on purpose, and nothing held the two
together. This file is what holds them: it renders the SAME corpus of types
through BOTH mappings and walks the two results in lockstep against the surface
type, classifying every position as an agreement or as one of a CLOSED set of
named divergences.

The assertion is two-sided:

  a divergence that is NOT in the documented set fails  -> one mapping drifted;
  a documented divergence that is never OBSERVED fails  -> the mappings moved
                                                           together and the note
                                                           now overstates them.

Either way the note and the tree are re-checked against each other in the same
commit, rather than a third consumer discovering the gap years later (which is
exactly how the `Opt` erasure of issue #1263 surfaced).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.export_openapi import (  # noqa: E402
    _OpenApiExporter,
    _split_generic,
    export_openapi,
)
from revl.mcp.schema import json_schema_for  # noqa: E402

# --------------------------------------------------------------------------
# the shared corpus: one set of type declarations, rendered through both
# --------------------------------------------------------------------------

TYPES = {
    "Note": {"kind": "record", "fields": {
        "id": "Str",
        "tags": "List[Str]",
        "hits": "Map[Str, Int]",
        "subtitle": "Opt[Str]",
    }},
    "Inner": {"kind": "record", "fields": {"n": "Int"}},
    "Step": {"kind": "variant", "cases": [{"name": "Ready"}, {"name": "Done"}]},
    "Turn": {"kind": "variant", "cases": [
        {"name": "Final", "payload": "Str"},
        {"name": "Calls", "payload": "Inner"},
        {"name": "Idle"},
    ]},
}

#: every surface type both mappings render. A type only ONE of them renders is
#: not a divergence in the walk, it is a refusal, and has its own test below.
#:
#: `Opt[Opt[T]]` and `Opt[Unit]` are deliberately NOT here. They are the
#: null-ambiguous spellings of issue #1263, where the two mappings fail in two
#: different ways rather than diverging in one; they have their own tests below.
CORPUS = [
    "Str", "Int", "Bool", "Float", "Unit",
    "List[Str]", "List[List[Int]]", "List[Note]",
    "Map[Str, Int]", "Map[Int, Str]", "Map[Str, Note]",
    "Opt[Str]", "Opt[Note]", "Opt[List[Int]]",
    "Note", "Inner", "Step", "Turn",
]

#: The closed set of ways the two mappings are allowed to differ. Every entry is
#: justified in docs/design/1272-two-type-to-schema-mappings.md; adding one here
#: without adding it there is the drift this file exists to catch.
DOCUMENTED_DIVERGENCES = {
    "AGREE",
    "REF_VS_INLINE",
    "UNIT_NULL_VS_EMPTY",
    "MAP_KEY_DROPPED",
    "OPT_NULLABLE_VS_ONEOF",
    "RECORD_OPEN_VS_CLOSED",
    "RECORD_OPT_FIELD_NOT_REQUIRED",
    "VARIANT_TAG_KEYS",
}

#: the scalars both tables carry with the same rendering
_SHARED_SCALARS = ("Str", "Int", "Bool", "Float")

#: the OpenAPI mapping RAISES for these; the inline mapping degrades or renders
#: them. Not a divergence class: a refusal, checked on its own.
_EXPORT_REFUSES = ("Bytes", "Result[Str, Int]", "Nope", "Opt[Bytes]")


def _exporter() -> _OpenApiExporter:
    return _OpenApiExporter({"types": TYPES})


def _walk(surface, inline, openapi, path, observed, exporter):
    """Compare the two renderings of `surface` in lockstep, driven by the
    SURFACE type rather than by either schema, so a constructor that one mapping
    stopped rendering is a failure and not a silently skipped subtree."""
    if isinstance(openapi, dict) and set(openapi) == {"$ref"}:
        name = openapi["$ref"].rsplit("/", 1)[1]
        assert name in TYPES, f"{path}: `$ref` to an unknown component `{name}`"
        observed.add("REF_VS_INLINE")
        openapi = exporter._component_schema(name)

    head, args = _split_generic(surface)

    if surface in _SHARED_SCALARS:
        assert inline == openapi, (
            f"{path}: scalar `{surface}` renders differently: "
            f"inline={inline!r} openapi={openapi!r}")
        observed.add("AGREE")
        return

    if surface == "Unit":
        assert inline == {"type": "null"}, f"{path}: inline Unit is {inline!r}"
        assert openapi == {}, f"{path}: openapi Unit is {openapi!r}"
        observed.add("UNIT_NULL_VS_EMPTY")
        return

    if head == "List" and len(args) == 1:
        for name, schema in (("inline", inline), ("openapi", openapi)):
            assert set(schema) == {"type", "items"}, f"{path}: {name} {schema!r}"
            assert schema.get("type") == "array", f"{path}: {name} {schema!r}"
        observed.add("AGREE")
        _walk(args[0], inline["items"], openapi["items"],
              f"{path}/items", observed, exporter)
        return

    if head == "Map" and len(args) == 2:
        for name, schema in (("inline", inline), ("openapi", openapi)):
            assert set(schema) == {"type", "additionalProperties"}, \
                f"{path}: {name} {schema!r}"
            assert schema.get("type") == "object", f"{path}: {name} {schema!r}"
        # both mappings drop K identically; that is an agreement between them
        # and an inexactness they share, which `fully_expressible` refuses
        # upstream for a non-`Str` key.
        observed.add("MAP_KEY_DROPPED" if args[0] != "Str" else "AGREE")
        _walk(args[1], inline["additionalProperties"],
              openapi["additionalProperties"],
              f"{path}/additionalProperties", observed, exporter)
        return

    if head == "Opt" and len(args) == 1:
        assert inline.get("nullable") is True, (
            f"{path}: the inline mapping no longer spells `{surface}` with "
            f"`nullable`: {inline!r}. If 257's open question was settled and "
            "the two mappings now agree on `Opt`, drop "
            "OPT_NULLABLE_VS_ONEOF from the divergence table here and from "
            "docs/design/1272-two-type-to-schema-mappings.md.")
        assert set(openapi) == {"oneOf"}, f"{path}: openapi {openapi!r}"
        assert len(openapi["oneOf"]) == 2, f"{path}: openapi {openapi!r}"
        assert openapi["oneOf"][1] == {"type": "null"}, \
            f"{path}: openapi {openapi!r}"
        observed.add("OPT_NULLABLE_VS_ONEOF")
        _walk(args[0], {k: v for k, v in inline.items() if k != "nullable"},
              openapi["oneOf"][0], f"{path}/Opt", observed, exporter)
        return

    spec = TYPES.get(surface)
    assert spec is not None, f"{path}: `{surface}` is in neither mapping's walk"

    if spec["kind"] == "record":
        fields = spec["fields"]
        for name, schema in (("inline", inline), ("openapi", openapi)):
            assert schema.get("type") == "object", f"{path}: {name} {schema!r}"
            assert set(schema.get("properties", {})) == set(fields), \
                f"{path}: {name} properties {sorted(schema['properties'])}"
        # the OpenAPI document closes the object; the inline fragment does not
        assert "additionalProperties" not in inline, f"{path}: {inline!r}"
        assert openapi.get("additionalProperties") is False, f"{path}: {openapi!r}"
        observed.add("RECORD_OPEN_VS_CLOSED")
        # the inline fragment requires every field (absence is not nullability);
        # the OpenAPI document drops a bare-`Opt` field from `required`
        assert inline.get("required") == sorted(fields), f"{path}: {inline!r}"
        optional = {n for n, t in fields.items() if _split_generic(t)[0] == "Opt"}
        assert sorted(openapi.get("required", [])) == sorted(set(fields) - optional), \
            f"{path}: openapi required {openapi.get('required')!r}"
        if optional:
            observed.add("RECORD_OPT_FIELD_NOT_REQUIRED")
        for fname, ftype in fields.items():
            _walk(ftype, inline["properties"][fname],
                  openapi["properties"][fname],
                  f"{path}/{surface}.{fname}", observed, exporter)
        return

    assert spec["kind"] == "variant", f"{path}: unknown kind {spec['kind']!r}"
    cases = spec["cases"]
    for name, schema in (("inline", inline), ("openapi", openapi)):
        assert set(schema) == {"oneOf"}, f"{path}: {name} {schema!r}"
        assert len(schema["oneOf"]) == len(cases), f"{path}: {name} {schema!r}"
    # same arms, same order, same closedness: only the two key SPELLINGS differ
    observed.add("VARIANT_TAG_KEYS")
    for case, arm_i, arm_o in zip(cases, inline["oneOf"], openapi["oneOf"]):
        where = f"{path}/{surface}:{case['name']}"
        for name, arm in (("inline", arm_i), ("openapi", arm_o)):
            assert arm.get("type") == "object", f"{where}: {name} {arm!r}"
            assert arm.get("additionalProperties") is False, f"{where}: {name} {arm!r}"
        assert arm_i["properties"].get("tag") == {"const": case["name"]}, \
            f"{where}: inline {arm_i!r}"
        assert arm_o["properties"].get("$kind") == {"const": case["name"]}, \
            f"{where}: openapi {arm_o!r}"
        if case.get("payload"):
            assert sorted(arm_i.get("required", [])) == ["tag", "value"], f"{where}"
            assert sorted(arm_o.get("required", [])) == ["$kind", "$value"], f"{where}"
            _walk(case["payload"], arm_i["properties"]["value"],
                  arm_o["properties"]["$value"], f"{where}/payload",
                  observed, exporter)
        else:
            assert arm_i.get("required") == ["tag"], f"{where}: inline {arm_i!r}"
            assert arm_o.get("required") == ["$kind"], f"{where}: openapi {arm_o!r}"
            assert set(arm_i["properties"]) == {"tag"}, f"{where}"
            assert set(arm_o["properties"]) == {"$kind"}, f"{where}"


def run_differential(corpus=CORPUS, types=TYPES):
    """Render every corpus type through both mappings and walk the pair.

    Returns the set of divergence classes observed. Raises `AssertionError` on
    the first position the two mappings differ in a way this file does not name.

    The differential runs the inline mapping in `validated=True` mode. That is
    the mode whose variant rendering is uniform (every variant tagged), which is
    the OpenAPI mapping's only rendering; `validated=False` additionally
    shortens an all-nullary variant to a bare `enum`, a divergence with a reason
    of its own that is pinned separately below.
    """
    observed: set = set()
    exporter = _OpenApiExporter({"types": types})
    for surface in corpus:
        _walk(surface,
              json_schema_for(surface, types, validated=True),
              exporter._schema_ref(surface),
              surface, observed, exporter)
    return observed


# --------------------------------------------------------------------------
# the differential
# --------------------------------------------------------------------------

def test_the_two_mappings_diverge_only_where_the_note_says():
    """The load-bearing test. Both directions of the ratchet."""
    observed = run_differential()
    unexpected = observed - DOCUMENTED_DIVERGENCES
    assert not unexpected, (
        "the two type-to-schema mappings diverge in a way "
        "docs/design/1272-two-type-to-schema-mappings.md does not name: "
        f"{sorted(unexpected)}")
    missing = DOCUMENTED_DIVERGENCES - observed
    assert not missing, (
        "a divergence the note documents is no longer observable on the "
        f"corpus: {sorted(missing)}. If the mappings converged, say so in "
        "docs/design/1272-two-type-to-schema-mappings.md and drop the entry.")


def test_the_differential_is_not_vacuous(monkeypatch):
    """A differential that cannot fail proves nothing. Perturb EACH mapping in
    turn and require the walk to catch it, so neither side is being read past."""
    import revl.export_openapi as export_mod
    import revl.mcp.schema as inline_mod

    # (1) the OpenAPI mapping drifts: `Int` becomes a `number`
    monkeypatch.setitem(export_mod._SCALARS, "Int", {"type": "number"})
    with pytest.raises(AssertionError, match="renders differently"):
        run_differential()
    monkeypatch.undo()

    # (2) the inline mapping drifts: `Str` becomes a `number`
    monkeypatch.setitem(inline_mod._JSON_TYPES, "Str", {"type": "number"})
    with pytest.raises(AssertionError, match="renders differently"):
        run_differential()
    monkeypatch.undo()

    # (3) a structural drift on the OpenAPI side: a record stops being closed
    real_component = export_mod._OpenApiExporter._component_schema

    def open_records(self, name):
        schema = real_component(self, name)
        schema.pop("additionalProperties", None)
        return schema

    monkeypatch.setattr(export_mod._OpenApiExporter, "_component_schema",
                        open_records)
    with pytest.raises(AssertionError):
        run_differential()
    monkeypatch.undo()

    # (4) the control: unperturbed, the same corpus walks clean
    assert run_differential() == DOCUMENTED_DIVERGENCES


def test_the_control_type_agrees_exactly_in_both_mappings():
    """A second control, independent of the walk: the shared scalar core and the
    list/map skeleton are byte-identical dicts in both mappings."""
    exporter = _exporter()
    for surface in ("Str", "Int", "Bool", "Float",
                    "List[Str]", "Map[Str, Int]", "List[List[Int]]"):
        assert json_schema_for(surface, TYPES, validated=True) == \
            exporter._schema_ref(surface), surface


# --------------------------------------------------------------------------
# the refusals: what only one mapping renders
# --------------------------------------------------------------------------

def test_the_openapi_mapping_refuses_what_the_inline_one_degrades():
    """Not a divergence in the walk: the OpenAPI mapping RAISES where the inline
    mapping renders or honestly degrades. An OpenAPI document is read by a third
    party, so an unconstrained `x-revlType` stub in it would read as a contract
    and constrain nothing; refusing is the right call and is recorded as one of
    the reasons the two mappings are two."""
    exporter = _exporter()
    for surface in _EXPORT_REFUSES:
        assert json_schema_for(surface, TYPES, validated=True) != {}, surface
        with pytest.raises(RevlError):
            exporter._schema_ref(surface)


def test_enum_shorthand_is_why_the_differential_runs_in_validated_mode():
    """`validated=False` shortens an all-nullary variant to a bare `enum`; the
    OpenAPI mapping always tags. Pinned here so the mode choice in
    `run_differential` is a recorded decision and not an accident."""
    exporter = _exporter()
    assert json_schema_for("Step", TYPES, validated=False) == \
        {"enum": ["Ready", "Done"]}
    assert set(exporter._schema_ref("Step")) == {"$ref"}
    assert set(exporter._component_schema("Step")) == {"oneOf"}
    assert set(json_schema_for("Step", TYPES, validated=True)) == {"oneOf"}


# --------------------------------------------------------------------------
# the null-ambiguous spellings (issue #1263), where the two mappings fail in
# two different ways rather than diverging in one
# --------------------------------------------------------------------------

def test_the_openapi_mapping_renders_a_null_ambiguous_oneof():
    """`Opt[Opt[Str]]` does not ERASE in the OpenAPI mapping the way it does in
    the inline one; it renders a `oneOf` in which `null` matches TWO arms.
    Distinct is not decidable."""
    arms = _exporter()._schema_ref("Opt[Opt[Str]]")["oneOf"]
    assert arms[1] == {"type": "null"}
    assert arms[0]["oneOf"][1] == {"type": "null"}
    matching_null = [a for a in arms
                     if a == {"type": "null"} or "oneOf" in a]
    assert len(matching_null) == 2


def test_a_nested_opt_reaches_the_openapi_mapping_through_the_RESPONSE(tmp_path):
    """The reachability correction (issue #1272).

    The route gate runs `fully_expressible` over the BOUND INPUTS only
    (`lower.py`, the `bind` loop). `_route_response_ir` classifies the return
    type and never gates it, so a routed operation RETURNING a null-ambiguous
    `Opt` compiles, and the second mapping renders its ambiguous `oneOf` into a
    real exported document. This holds with or without the issue-#1263 input
    gate, which is why it is pinned here with no skip."""
    app = tmp_path / "app.rvl"
    app.write_text(
        'service S {\n'
        '  route get "/nested/{id}"\n'
        '  fn nested(id: Str) -> Opt[Opt[Str]]\n'
        '}\n\n'
        'component C provides s: S {\n'
        '  provide s { fn nested(id) = None }\n'
        '}\n', encoding="utf-8")
    import json
    doc = json.loads(export_openapi(compile_files([str(app)]), service="S"))
    schema = doc["paths"]["/nested/{id}"]["get"]["responses"]["200"] \
        ["content"]["application/json"]["schema"]
    assert schema["oneOf"][1] == {"type": "null"}
    assert schema["oneOf"][0]["oneOf"][1] == {"type": "null"}


def test_a_nested_opt_is_refused_on_a_bound_input(tmp_path):
    """The other half: a null-ambiguous `Opt` reached through a BOUND INPUT is
    refused before the second mapping sees it."""
    pytest.importorskip(
        "revl.mcp.schema",
        reason="the surface-type null predicate ships with issue #1263")
    from revl.mcp import schema as schema_mod
    if not hasattr(schema_mod, "admits_json_null"):
        pytest.skip("requires `admits_json_null` (issue #1263 / PR #1270); "
                    "this tree predates the input gate")
    app = tmp_path / "app.rvl"
    app.write_text(
        'type Wrap = { deep: Opt[Opt[Str]] }\n\n'
        'service S {\n'
        '  route post "/w"\n'
        '  emission fn put(w: Wrap) -> Unit\n'
        '}\n\n'
        'component C provides s: S {\n'
        '  provide s { fn put(w) = unit_of(w) }\n'
        '}\n\n'
        'fn unit_of(w: Wrap) -> Unit { return () }\n', encoding="utf-8")
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(app)])
    assert "Opt[Opt[Str]]" in str(excinfo.value)
