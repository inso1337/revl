"""`revl import openapi` — an OpenAPI 3.x document -> revl source.

Two claims are under test, the same two the rest of the import family carries.

The first is mechanical: **the generated source compiles**. A codegen that
emits plausible-looking text is worthless, so every fixture here is
round-tripped through `compile_files` and the resulting IR is inspected for
the services, types and extern classifications it should carry.

The second is the judgement. WIT says nothing about reversibility, so
`revl import wit` classifies everything `emission`. OpenAPI *does* say
something: RFC 9110 §9.2.1 defines `GET`/`HEAD`/`OPTIONS`/`TRACE` as safe, and
an author who writes `get:` adopts that contract. This importer honours that
in-band claim and refuses to infer anything beyond it — `PUT` and `DELETE` are
idempotent by specification and stay `emission`, because repeating an
operation says nothing about undoing it. The idempotency of `PUT`/`DELETE`
(§9.2.2) is a *second* claim the importer carries into the source the same
way: those operations import as `emission idempotent fn`, the checked IR
property that earns the runtime its auto-retry right (roadmap item 44, docs/
delivery-semantics.md). Every classification, and every override of one, is
written next to the operation it applies to.

`test_a_plain_operation_backed_by_an_emission_is_rejected` is why the plain
form is still safe: G4 makes a service declaration an upper bound, so the
compiler itself refuses a plain operation whose provider reaches an
irreversible call.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.import_openapi import (  # noqa: E402
    import_openapi, import_openapi_file, load_document,
)

FIXTURES = ROOT / "tests" / "fixtures" / "openapi"


def _compile(source: str, tmp_path: Path, name: str = "imported.rvl") -> dict:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return compile_files([str(path)])


def _methods(ir: dict, service: str) -> dict:
    return ir["services"][service]["methods"]


def _extern_classes(ir: dict) -> dict:
    return {extern["name"]: extern["class"] for extern in ir["externs"]}


# ---------------------------------------------------------- document helpers

_OK_STR = {"200": {"description": "ok",
                   "content": {"application/json": {"schema": {"type": "string"}}}}}


def _document(paths: dict, schemas: dict | None = None, **extra) -> dict:
    doc = {"openapi": "3.0.3",
           "info": {"title": "Probe", "version": "1.0.0"},
           "paths": paths}
    if schemas is not None:
        doc["components"] = {"schemas": schemas}
    doc.update(extra)
    return doc


def _with_schema(schema: dict) -> dict:
    """A one-operation document whose response carries `schema`."""
    return _document({"/x": {"get": {"responses": {
        "200": {"description": "ok",
                "content": {"application/json": {"schema": schema}}}}}}})


def _refusal(document: dict, **kwargs) -> str:
    with pytest.raises(RevlError) as excinfo:
        import_openapi(document, filename="bad.json", **kwargs)
    return str(excinfo.value)


# ------------------------------------------------- fixture 1: the small case

def test_small_document_becomes_a_service():
    source = import_openapi_file(str(FIXTURES / "heartbeat.json"))
    # the service is named from `info.title`
    assert "service Heartbeat {" in source
    assert "\n  fn get_status() -> GetStatusResponse" in source
    assert "emission fn post_status()" in source
    # the inline response schema is given a name derived from its operation
    assert "type GetStatusResponse = { ok: Bool, checked_at: Str, detail: Opt[Str] }" in source
    # the header states the trust direction rather than implying safety
    assert "TRUSTED, NOT CHECKED (G8)" in source
    assert "Source document:" in source and "heartbeat.json" in source
    assert "// API: Heartbeat 1.0.0" in source
    assert "// Server: https://probe.example.com" in source


def test_small_document_compiles(tmp_path):
    ir = _compile(import_openapi_file(str(FIXTURES / "heartbeat.json")), tmp_path)
    methods = _methods(ir, "Heartbeat")
    assert methods["get_status"]["emission"] is False
    assert methods["get_status"]["returns"] == "GetStatusResponse"
    assert methods["post_status"]["emission"] is True
    # `202 accepted` carries no content, so the operation returns nothing
    assert methods["post_status"]["returns"] is None
    assert _extern_classes(ir) == {"http_heartbeat_get_status": "pure",
                                   "http_heartbeat_post_status": "emission"}
    assert [c["name"] for c in ir["components"]] == ["HeartbeatProvider"]
    assert ir["components"][0]["provides"] == {"heartbeat": "Heartbeat"}


# ------------------------------- fixture 2: $ref, enums, optionals, verb mix

def test_petstore_maps_the_whole_schema_family(tmp_path):
    ir = _compile(import_openapi_file(str(FIXTURES / "petstore.json")), tmp_path)
    types = ir["types"]

    # `enum` -> a payload-free ADT
    assert types["Status"]["kind"] == "variant"
    assert [c["name"] for c in types["Status"]["cases"]] == \
        ["Available", "Pending", "Sold"]

    # `oneOf` + `discriminator` -> an ADT, one case per mapped schema
    assert types["Animal"]["cases"] == [{"name": "Cat", "payload": "Cat"},
                                        {"name": "Dog", "payload": "Dog"}]

    # object -> record; required/optional -> T vs Opt[T]; camelCase -> snake_case
    assert types["Pet"]["kind"] == "record"
    assert types["Pet"]["fields"] == {
        "id": "Int",                       # required
        "name": "Str",                     # required
        "status": "Status",                # required, via $ref
        "kind": "Opt[Animal]",             # optional discriminated union
        "tag": "Opt[Str]",                 # optional *and* nullable -> one Opt
        "weight_kg": "Opt[Float]",         # `number` -> Float
        "photo_urls": "Opt[List[Str]]",    # array -> List[T]
        "labels": "Opt[Map[Str, Str]]",    # additionalProperties -> Map[Str, T]
        "address": "Opt[PetAddress]",      # inline object -> a named record
        "companion": "Opt[Pet]",           # recursive $ref
    }
    assert types["PetAddress"]["fields"] == {"city": "Str", "postcode": "Opt[Str]"}
    assert types["PetPage"]["fields"] == {"items": "List[Pet]", "next": "Opt[Str]"}
    # an inline request body is named after its operation
    assert types["SearchPetsBody"]["fields"] == {"query": "Str", "tags": "Opt[List[Str]]"}


def test_petstore_operations_carry_their_parameters(tmp_path):
    ir = _compile(import_openapi_file(str(FIXTURES / "petstore.json")), tmp_path)
    methods = _methods(ir, "PetStore")

    # a path-item-level parameter (`trace`) reaches every operation under it,
    # and an optional query parameter is `Opt[T]`
    assert methods["list_pets"]["params"] == [
        {"name": "trace", "type": "Opt[Bool]"},
        {"name": "limit", "type": "Opt[Int]"},
        {"name": "status", "type": "Opt[Status]"},
        {"name": "sku", "type": "Opt[Str]"},
    ]
    assert methods["list_pets"]["returns"] == "PetPage"

    # a request body is the last parameter, required -> not Opt
    assert methods["create_pet"]["params"][-1] == {"name": "body", "type": "Pet"}

    # a path parameter comes first, in template order, and is never Opt
    assert methods["get_pets_by_pet_id"]["params"] == [{"name": "pet_id", "type": "Int"}]
    # `204 No Content` -> no return type
    assert methods["delete_pets_by_pet_id"]["returns"] is None


def test_petstore_compiles_to_a_provider(tmp_path):
    ir = _compile(import_openapi_file(str(FIXTURES / "petstore.json")), tmp_path)
    assert ir["components"][0]["provides"] == {"pet_store": "PetStore"}
    assert _extern_classes(ir) == {
        "http_pet_store_list_pets": "pure",
        "http_pet_store_create_pet": "emission",
        "http_pet_store_get_pets_by_pet_id": "pure",
        "http_pet_store_delete_pets_by_pet_id": "emission",
        "http_pet_store_read_logbook": "emission",
        "http_pet_store_search_pets": "emission",
    }


def test_a_scalar_component_schema_is_inlined_not_declared(tmp_path):
    """revl has no type alias: `type Sku = Str` would declare a one-case
    *variant* named `Str`, which is a different (and wrong) thing."""
    source = import_openapi_file(str(FIXTURES / "petstore.json"))
    assert "type Sku" not in source
    assert "sku: Opt[Str]" in source
    assert "note: `#/components/schemas/Sku` is `Str` — inlined at its use sites" in source
    assert "Sku" not in _compile(source, tmp_path)["types"]


def test_a_recursive_schema_is_expressed_rather_than_refused(tmp_path):
    """`Pet.companion: Pet` needs no special case — a revl record field may
    name the record it belongs to."""
    ir = _compile(import_openapi_file(str(FIXTURES / "petstore.json")), tmp_path)
    assert ir["types"]["Pet"]["fields"]["companion"] == "Opt[Pet]"


def test_the_host_bodies_carry_the_request_they_stand_for():
    source = import_openapi_file(str(FIXTURES / "petstore.json"))
    # the server URL is folded in, and the path template's braces are neutralised
    # so they cannot unbalance the host block
    assert "// DELETE https://api.example.com/v1/pets/(petId) — send the request" in source
    # a `{` or `}` inside a host block would unbalance it, since the lexer reads
    # the body by scanning braces, so the template's braces are neutralised
    bodies = [line for line in source.splitlines() if line.lstrip().startswith("= @")]
    assert len(bodies) == 6
    inner = [body[body.index("{") + 1:body.rindex("}")] for body in bodies]
    assert not any("{" in body or "}" in body for body in inner)
    assert any("/pets/(petId)" in body for body in inner)


# ---------------------------------------------------------- the emission rule

def _one(method: str, **operation) -> dict:
    body = {"responses": _OK_STR}
    body.update(operation)
    return _document({"/thing": {method: body}})


@pytest.mark.parametrize("method,emission", [
    ("get", False), ("head", False), ("options", False), ("trace", False),
    ("post", True), ("put", True), ("patch", True), ("delete", True),
])
def test_safe_by_specification_is_the_only_thing_that_weakens_the_default(
        method, emission, tmp_path):
    source = import_openapi(_one(method), filename="probe.json")
    ir = _compile(source, tmp_path)
    name = f"{method}_thing"
    assert _methods(ir, "Probe")[name]["emission"] is emission
    assert _extern_classes(ir)[f"http_probe_{name}"] == \
        ("emission" if emission else "pure")


def test_the_verb_is_recorded_as_a_claim_not_a_proof():
    source = import_openapi(_one("get"), filename="probe.json")
    assert ("`GET` is safe by RFC 9110 §9.2.1: the API author's claim about "
            "their server, not a property this compiler checked") in source
    assert "The claim is not a proof." in source


def test_idempotent_is_not_reversible():
    """PUT and DELETE are idempotent by specification, which says nothing
    about undoing them — the generated header says so rather than leaving a
    reader to assume the importer confused the two. The delivery claim now
    rides alongside: the operation imports as `emission idempotent fn`."""
    source = import_openapi(_one("put"), filename="probe.json")
    assert "`PUT` is not safe by specification: `emission` (G8)" in source
    assert "RFC 9110 §9.2.2 defines PUT" in source
    assert "repeating safely says nothing about undoing" in source
    assert "emission idempotent fn put_thing()" in source


def test_x_revl_emission_true_overrides_a_safe_verb(tmp_path):
    source = import_openapi(_one("get", **{"x-revl-emission": True}),
                            filename="probe.json")
    assert "`emission` by assertion: `x-revl-emission: true` in the document" in source
    assert _methods(_compile(source, tmp_path), "Probe")["get_thing"]["emission"] is True


def test_x_revl_emission_false_weakens_an_unsafe_verb(tmp_path):
    source = import_openapi(_one("post", **{"x-revl-emission": False}),
                            filename="probe.json")
    assert "plain by assertion: `x-revl-emission: false` in the document" in source
    assert _methods(_compile(source, tmp_path), "Probe")["post_thing"]["emission"] is False


def test_the_importing_engineer_can_weaken_an_unsafe_verb(tmp_path):
    source = import_openapi(_one("post"), filename="probe.json", pure=["post_thing"])
    assert ("plain by assertion: `--pure post_thing` at import time — HTTP does "
            "not call `POST` safe") in source
    assert _methods(_compile(source, tmp_path), "Probe")["post_thing"]["emission"] is False


def test_the_importing_engineer_can_strengthen_a_safe_verb(tmp_path):
    source = import_openapi(_one("get"), filename="probe.json",
                            emission=["GET /thing"])
    assert "`emission` by assertion: `--emission GET /thing` at import time" in source
    assert _methods(_compile(source, tmp_path), "Probe")["get_thing"]["emission"] is True


def test_an_emission_claim_beats_a_pure_claim_from_any_source():
    """Strengthening takes one voice, weakening takes unanimity: the safe
    direction is the one that survives a disagreement."""
    source = import_openapi(_one("get", **{"x-revl-emission": False}),
                            filename="probe.json", emission=["get_thing"])
    assert "`emission` by assertion: `--emission get_thing`" in source
    assert "emission fn get_thing" in source


@pytest.mark.parametrize("handle", ["get_thing", "GET /thing", "peek"])
def test_an_override_can_be_named_three_ways(handle):
    document = _one("get", operationId="peek")
    if handle == "get_thing":
        document["paths"]["/thing"]["get"].pop("operationId")
    source = import_openapi(document, filename="probe.json", emission=[handle])
    assert "`emission` by assertion" in source


def test_an_override_that_matches_nothing_is_an_error():
    """Silently leaving it classified by its verb would be safe in one
    direction and dishonest in both: the engineer believes they changed it."""
    message = _refusal(_one("get"), emission=["get_thnig"])
    assert ("--pure/--emission/--compensate named 'get_thnig', which is not an "
            "operation" in message)
    assert "a typo here would silently leave the operation classified by its verb" in message


def test_a_plain_operation_backed_by_an_emission_is_rejected(tmp_path):
    """Why a generated plain `fn` is trustworthy: G4 makes a service
    declaration an upper bound, so the compiler — not this importer — refuses
    the mismatch that under-declaring would require."""
    source = import_openapi(_one("get"), filename="probe.json")
    broken = source.replace("extern pure fn", "extern emission fn")
    with pytest.raises(RevlError) as excinfo:
        _compile(broken, tmp_path, "broken.rvl")
    assert "declared plain, but this implementation reaches" in str(excinfo.value)


# ------------------------------------------------------------ name handling

@pytest.mark.parametrize("method,path,expected", [
    ("get", "/pets", "get_pets"),
    ("get", "/pets/{id}", "get_pets_by_id"),
    ("get", "/pets/{petId}/toys/{toyId}", "get_pets_by_pet_id_toys_by_toy_id"),
    ("delete", "/pets/{id}", "delete_pets_by_id"),
    ("get", "/", "get"),
    ("get", "/api/v1/user-profiles", "get_api_v1_user_profiles"),
])
def test_a_path_and_method_become_one_readable_operation_name(method, path, expected):
    parameters = [{"name": name, "in": "path", "required": True,
                   "schema": {"type": "string"}}
                  for name in ("id", "petId", "toyId") if "{" + name + "}" in path]
    document = _document({path: {method: {"parameters": parameters,
                                          "responses": _OK_STR}}})
    assert f"fn {expected}(" in import_openapi(document, filename="p.json")


def test_an_operation_id_wins_over_the_derived_name():
    document = _document({"/pets/{id}": {"get": {
        "operationId": "findPetById",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "responses": _OK_STR}}})
    source = import_openapi(document, filename="p.json")
    assert "fn find_pet_by_id(id: Str)" in source
    assert "get_pets_by_id" not in source


def test_two_operations_that_generate_one_name_collide_loudly():
    """`operationId` is unique per the specification, but two of them can
    still land on one revl name once case is normalised."""
    document = _document({
        "/a": {"get": {"operationId": "listPets", "responses": _OK_STR}},
        "/b": {"get": {"operationId": "list_pets", "responses": _OK_STR}},
    })
    message = _refusal(document)
    assert "both become the revl operation `list_pets`" in message
    assert "give one of them a distinct `operationId`" in message


def test_a_schema_named_after_a_revl_builtin_is_renamed(tmp_path):
    document = _with_schema({"$ref": "#/components/schemas/Result"})
    document["components"] = {"schemas": {"Result": {
        "type": "object", "required": ["code"],
        "properties": {"code": {"type": "integer"}}}}}
    source = import_openapi(document, filename="p.json")
    assert "type ResultTy = { code: Int }" in source
    assert "note: schema `Result` -> `ResultTy` (`Result` is a revl built-in)" in source
    assert "ResultTy" in _compile(source, tmp_path)["types"]


def test_a_name_that_would_be_a_revl_keyword_gains_an_underscore(tmp_path):
    document = _document({"/x": {"get": {
        "parameters": [{"name": "type", "in": "query",
                        "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "object", "required": ["match"],
                       "properties": {"match": {"type": "string"}}}}}}}}}})
    source = import_openapi(document, filename="p.json")
    assert "fn get_x(type_: Opt[Str])" in source
    assert "match_: Str" in source
    assert _compile(source, tmp_path)["types"]["GetXResponse"]["fields"] == {"match_": "Str"}


def test_an_enum_case_shadowing_a_builtin_constructor_is_reported():
    source = import_openapi(
        _with_schema({"type": "string", "enum": ["ok", "waiting"]}), filename="p.json")
    assert "type GetXResponse = Ok | Waiting" in source
    assert "shadows revl's built-in `Ok` constructor" in source


def test_an_inline_schema_colliding_with_a_component_name_is_refused():
    """The inline response of `GET /x` wants the name `GetXResponse`, which a
    component schema already holds."""
    document = _with_schema({"type": "object", "required": ["a"],
                             "properties": {"a": {"$ref": "#/components/schemas/GetXResponse"}}})
    document["components"] = {"schemas": {"GetXResponse": {
        "type": "object", "required": ["b"], "properties": {"b": {"type": "string"}}}}}
    message = _refusal(document)
    assert "two schemas both generate the revl type `GetXResponse`" in message


def test_a_ref_cycle_with_no_shape_in_it_is_refused():
    """`A -> B -> A` through bare `$ref`s has no record to break on. A cycle
    *through* a record is fine — see `Pet.companion`."""
    document = _with_schema({"$ref": "#/components/schemas/A"})
    document["components"] = {"schemas": {"A": {"$ref": "#/components/schemas/B"},
                                          "B": {"$ref": "#/components/schemas/A"}}}
    message = _refusal(document)
    assert "is a `$ref` cycle with no shape in it" in message
    assert "revl records may name themselves" in message


def test_two_schemas_that_generate_one_revl_type_collide():
    document = _document(
        {"/x": {"get": {"responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"type": "object", "required": ["a"],
                                            "properties": {
                                                "a": {"$ref": "#/components/schemas/pet-tag"},
                                                "b": {"$ref": "#/components/schemas/petTag"}}}}}}}}}},
        {"pet-tag": {"type": "object", "required": ["v"],
                     "properties": {"v": {"type": "string"}}},
         "petTag": {"type": "object", "required": ["w"],
                    "properties": {"w": {"type": "string"}}}})
    message = _refusal(document)
    assert "two schemas both generate the revl type `PetTag`" in message
    assert "revl types share one module namespace" in message


# -------------------------------------------------- refusals (the messages)

@pytest.mark.parametrize("schema,expected,remedy", [
    ({"allOf": [{"type": "string"}]}, "`allOf`",
     "flatten the members into one schema"),
    ({"anyOf": [{"type": "string"}]}, "`anyOf`",
     "if you meant exactly one, write `oneOf` with a `discriminator`"),
    ({"not": {"type": "string"}}, "`not`", "revl types are structural"),
    ({"oneOf": [{"$ref": "#/components/schemas/A"}]}, "a `oneOf` with no `discriminator`",
     "Add `discriminator: {propertyName: <field>}`"),
    ({"type": "object"}, "a free-form `object` (no `properties`)",
     "give `additionalProperties` a schema so it becomes `Map[Str, T]`"),
    ({"type": "object", "properties": {"a": {"type": "string"}},
      "additionalProperties": {"type": "string"}},
     "an object with both `properties` and open `additionalProperties`",
     "a revl record holds exactly its declared fields"),
    ({"type": "string", "enum": ["a", 2]}, "a non-string `enum`",
     "a revl ADT case is a *name*"),
    ({"type": "integer", "enum": [1, 2]}, "a non-string `enum`",
     "Drop the `enum` to keep the underlying scalar (`Int`)"),
    ({"type": "array"}, "an `array` schema with no `items`",
     "revl's `List[T]` is homogeneous"),
    ({"$ref": "other.json#/Pet"}, "a `$ref` to `other.json#/Pet`",
     "bundle the spec first"),
    ({"$ref": "#/components/schemas/Nope"}, "`$ref` to an undefined schema `Nope`",
     "`components/schemas` defines"),
    ({}, "a schema with no `type` and no `properties`, `enum`, `$ref` or `oneOf`",
     "revl has no dynamic value type"),
    ({"type": "null"}, "`type: \"null\"`", "absence in revl is `Opt[T]`"),
    ({"type": ["string", "integer"]}, "a multi-type schema",
     "anything wider needs a `oneOf` with a `discriminator`"),
    ({"type": "geometry"}, "an unknown schema type `geometry`", "known:"),
])
def test_an_unsupported_schema_names_itself_its_place_and_the_way_out(
        schema, expected, remedy):
    message = _refusal(_with_schema(schema))
    assert expected in message
    assert remedy in message
    assert " at #/" in message                 # every refusal carries a pointer
    assert message.startswith("bad.json:")     # ...and a file


def test_a_oneof_member_that_is_not_a_ref_is_refused():
    document = _with_schema({
        "oneOf": [{"$ref": "#/components/schemas/A"}, {"type": "string"}],
        "discriminator": {"propertyName": "kind"}})
    document["components"] = {"schemas": {"A": {"type": "object", "required": ["kind"],
                                                "properties": {"kind": {"type": "string"}}}}}
    message = _refusal(document)
    assert "a `oneOf` member that is not a `$ref`" in message
    assert "lift the inline schema into `components/schemas`" in message


@pytest.mark.parametrize("location", ["header", "cookie"])
def test_a_header_or_cookie_parameter_is_refused_with_the_way_forward(location):
    document = _document({"/x": {"get": {
        "parameters": [{"name": "X-Trace", "in": location, "schema": {"type": "string"}}],
        "responses": _OK_STR}}})
    message = _refusal(document)
    assert f"an `in: {location}` parameter (`X-Trace`) on `GET /x`" in message
    assert "a header or cookie is transport, not an operation argument" in message
    assert "Set it inside the generated `extern`'s `@ts` body" in message


def test_an_undeclared_path_template_variable_is_refused():
    message = _refusal(_document({"/pets/{id}": {"get": {"responses": _OK_STR}}}))
    assert "the path template `/pets/{id}` uses {id}, which is not declared" in message
    assert "an undeclared template variable has no type" in message


def test_a_path_parameter_missing_from_the_template_is_refused():
    document = _document({"/pets": {"get": {
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "responses": _OK_STR}}})
    message = _refusal(document)
    assert "path parameter `id` does not appear in the path template `/pets`" in message


def test_a_non_json_response_names_the_media_types_it_found():
    document = _document({"/x": {"get": {"responses": {"200": {
        "description": "ok",
        "content": {"text/csv": {"schema": {"type": "string"}}}}}}}})
    message = _refusal(document)
    assert "a `200` response carrying only text/csv" in message
    assert "handle the encoding inside the generated `extern`'s host body" in message


def test_a_non_json_request_body_is_refused():
    document = _document({"/x": {"post": {
        "requestBody": {"required": True, "content": {
            "application/octet-stream": {"schema": {"type": "string"}}}},
        "responses": _OK_STR}}})
    assert "a request body carrying only application/octet-stream" in _refusal(document)


def test_an_operation_with_no_2xx_response_is_refused():
    document = _document({"/x": {"get": {"responses": {
        "default": {"description": "whatever"}, "404": {"description": "gone"}}}}})
    message = _refusal(document)
    assert "an operation with no 2xx response (declared: 404, default)" in message
    assert "Declare the concrete success code" in message


def test_an_unknown_path_item_field_is_refused():
    message = _refusal(_document({"/x": {"connect": {"responses": _OK_STR}}}))
    assert "an unknown path-item field `connect`" in message
    assert "or an `x-` extension" in message


def test_an_x_extension_on_a_path_item_is_ignored():
    document = _document({"/x": {"x-internal": True, "get": {"responses": _OK_STR}}})
    assert "fn get_x()" in import_openapi(document, filename="p.json")


def test_a_swagger_2_document_says_how_to_convert():
    with pytest.raises(RevlError) as excinfo:
        import_openapi({"swagger": "2.0", "info": {"title": "Old"}, "paths": {}},
                       filename="bad.json")
    message = str(excinfo.value)
    assert "an OpenAPI 2.0 (Swagger 2.0) document" in message
    assert "swagger2openapi" in message


def test_a_document_that_is_not_3x_is_refused():
    message = _refusal({"info": {"title": "X"}, "paths": {}})
    assert "not an OpenAPI 3.x document (`openapi` is null)" in message


def test_a_document_with_no_paths_is_refused():
    message = _refusal({"openapi": "3.0.3", "info": {"title": "X"}, "paths": {}})
    assert "declares no `paths` to import" in message


def test_a_document_with_only_empty_path_items_is_refused():
    message = _refusal(_document({"/x": {"summary": "nothing here"}}))
    assert "declares no operations to import" in message


def test_a_document_with_no_title_asks_for_the_service_name():
    message = _refusal({"openapi": "3.0.3", "info": {}, "paths": {"/x": {}}})
    assert "no `info.title` to name the service after" in message
    assert "pass `--service NAME`" in message


def test_an_unknown_backend_is_refused_and_explains_the_missing_one():
    with pytest.raises(RevlError) as excinfo:
        import_openapi(_one("get"), filename="p.json", backend="wasm")
    # WHY the flag is missing, not a pinned sentence: the tier has no host
    # network seam. It is emphatically NOT a value width — `Int` is an i64
    # there and rich values cross as canonical-ABI pointers (#218), and
    # tests/test_importer_wasm_tier_claims.py checks that against the emitter.
    assert "cannot hold an HTTP client" in str(excinfo.value)
    assert "i32" not in str(excinfo.value)


def test_a_refusal_points_at_a_line_in_the_source_text():
    """A JSON pointer is the authoritative location, but a human reading the
    file wants a line too, so one is recovered from the raw text."""
    text = (FIXTURES / "inheritance.json").read_text(encoding="utf-8")
    with pytest.raises(RevlError) as excinfo:
        import_openapi_file(str(FIXTURES / "inheritance.json"))
    assert excinfo.value.line > 0
    assert "allOf" in text.splitlines()[excinfo.value.line - 1]
    assert "#/components/schemas/Item/allOf" in str(excinfo.value)


# --------------------------------------------------------------- YAML, JSON

def test_yaml_is_delegated_rather_than_guessed_at():
    """revl has no dependencies, and a hand-rolled YAML subset would mis-read
    an anchor or a folded scalar *silently* — the one failure mode this whole
    family exists to avoid. So YAML is delegated to PyYAML if it happens to be
    importable, and otherwise refused with the conversion command."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("PyYAML is importable here, so the delegation path is taken")
    with pytest.raises(RevlError) as excinfo:
        load_document("openapi: '3.0.3'\n", filename="spec.yaml")
    message = str(excinfo.value)
    assert "no YAML parser is available" in message
    assert "will not guess at YAML rather than parse it" in message
    assert "yaml.safe_load" in message


def test_malformed_json_reports_its_own_line():
    with pytest.raises(RevlError) as excinfo:
        load_document('{\n  "openapi": "3.0.3",\n  ,\n}\n', filename="spec.json")
    assert "is not valid JSON" in str(excinfo.value)
    assert excinfo.value.line == 3


def test_a_document_that_is_not_an_object_is_refused():
    with pytest.raises(RevlError) as excinfo:
        import_openapi([1, 2, 3], filename="spec.json")
    assert "the OpenAPI document is not an object (found list)" in str(excinfo.value)


# ------------------------------------------------------------------- the CLI

def test_cli_writes_generated_source(tmp_path):
    out = tmp_path / "petstore.rvl"
    assert main(["import", "openapi", str(FIXTURES / "petstore.json"),
                 "-o", str(out)]) == 0
    assert "service PetStore {" in out.read_text(encoding="utf-8")
    assert compile_files([str(out)])["services"]["PetStore"]


def test_cli_backend_choice_reaches_the_host_bodies(tmp_path):
    out = tmp_path / "heartbeat.rvl"
    assert main(["import", "openapi", str(FIXTURES / "heartbeat.json"),
                 "--backend", "py", "-o", str(out)]) == 0
    source = out.read_text(encoding="utf-8")
    assert "= @py { #" in source
    assert compile_files([str(out)])["externs"][0]["bodies"].keys() == {"py"}


def test_cli_service_name_override(tmp_path):
    out = tmp_path / "renamed.rvl"
    assert main(["import", "openapi", str(FIXTURES / "heartbeat.json"),
                 "--service", "probe-api", "-o", str(out)]) == 0
    ir = compile_files([str(out)])
    assert "ProbeApi" in ir["services"]
    assert ir["components"][0]["provides"] == {"probe_api": "ProbeApi"}


def test_cli_overrides_reach_the_classification(tmp_path):
    out = tmp_path / "petstore.rvl"
    assert main(["import", "openapi", str(FIXTURES / "petstore.json"),
                 "--pure", "POST /search", "--emission", "listPets",
                 "-o", str(out)]) == 0
    methods = compile_files([str(out)])["services"]["PetStore"]["methods"]
    assert methods["search_pets"]["emission"] is False
    assert methods["list_pets"]["emission"] is True


def test_cli_reports_a_refusal_and_exits_nonzero(capsys):
    assert main(["import", "openapi", str(FIXTURES / "inheritance.json")]) == 1
    assert "unsupported OpenAPI construct: `allOf`" in capsys.readouterr().err


def test_cli_json_diagnostics(capsys):
    assert main(["import", "openapi", str(FIXTURES / "inheritance.json"),
                 "--json-diagnostics"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    diagnostic = payload["diagnostics"][0]
    assert diagnostic["message"].startswith("unsupported OpenAPI construct: `allOf`")
    assert diagnostic["file"].endswith("inheritance.json")


def test_cli_missing_file(tmp_path, capsys):
    assert main(["import", "openapi", str(tmp_path / "absent.json")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_cli_still_imports_wit(tmp_path):
    """The subcommand table grew; the sibling importer is untouched."""
    out = tmp_path / "greeter.rvl"
    assert main(["import", "wit", str(ROOT / "tests/fixtures/wit/greeter.wit"),
                 "-o", str(out)]) == 0
    assert compile_files([str(out)])["services"]["Greeter"]
