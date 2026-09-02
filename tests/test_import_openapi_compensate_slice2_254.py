"""`revl import openapi` — item 254 Slice 2, the other two compensate routes.

Slice 1 landed the `PUT`-with-`GET`-preimage form and deferred the rest
(docs/design/254-witnessed-network.md §7). This is that deferred list, minus the
per-tier work: the `DELETE`-recreate form, the `POST`-with-documented-delete
form, and the HIGH 1 concurrency-token story.

The classification ceiling is unchanged and is re-pinned here for the new forms:
COMPENSATE-grade, audit surface, never a proof surface. What the new forms add is
a second way to get the promotion wrong, so most of this file is about the
refusals. The posture is the family's: a compensation that cannot be shown to be
addressed at what the forward effect touched is REFUSED, not emitted best-effort
and hoped over — the same reason the recovery machinery reports `outcome=
"unknown"` for an unkeyed spent inverse instead of guessing at it.
"""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.import_openapi import import_openapi  # noqa: E402
from revl.lower import cap_scope_enumerated_not_run  # noqa: E402


# ---------------------------------------------------------- document helpers

_CFG = {"$ref": "#/components/schemas/Config"}
_ID = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
_CONFIG_SCHEMA = {"type": "object", "required": ["id", "value"],
                  "properties": {"id": {"type": "string"},
                                 "value": {"type": "string"}}}


def _doc(*, get: dict | None = None, delete: dict | None = None,
         post: dict | None = None, schema: dict | None = None,
         root: dict | None = None) -> dict:
    """A `/config` collection + `/config/{id}` resource over one `Config`.

    Every operation is spelled out per test rather than shared, because what is
    under test is exactly which combination the importer will and will not
    accept.
    """
    resource: dict = {}
    if get is not None:
        resource["get"] = get
    if delete is not None:
        resource["delete"] = delete
    paths: dict = {}
    if resource:
        paths["/config/{id}"] = resource
    if post is not None:
        paths["/config"] = {"post": post}
    doc = {"openapi": "3.0.3",
           "info": {"title": "Config API", "version": "1.0.0"},
           "servers": [{"url": "https://api.example.com/v1"}],
           "components": {"schemas": {"Config": schema or _CONFIG_SCHEMA}},
           "paths": paths}
    doc.update(root or {})
    return doc


def _get() -> dict:
    return {"operationId": "getConfig", "parameters": _ID,
            "responses": {"200": {"description": "ok",
                                  "content": {"application/json": {"schema": _CFG}}}}}


def _delete(**extra) -> dict:
    op = {"operationId": "deleteConfig", "parameters": _ID,
          "responses": {"204": {"description": "gone"}}}
    op.update(extra)
    return op


def _post(**extra) -> dict:
    op = {"operationId": "createConfig",
          "requestBody": {"required": True,
                          "content": {"application/json": {"schema": _CFG}}},
          "responses": {"201": {"description": "made",
                                "content": {"application/json": {"schema": _CFG}}}}}
    op.update(extra)
    return op


_RECREATE = {"x-revl-compensate": True, "x-revl-preimage": "getConfig",
             "x-revl-undo": "createConfig"}
_DELETE_CREATED = {"x-revl-compensate": True, "x-revl-undo": "deleteConfig",
                   "x-revl-undo-key": "id"}


def _recreate_doc(**extra) -> dict:
    op = dict(_RECREATE)
    op.update(extra)
    return _doc(get=_get(), delete=_delete(**op), post=_post())


def _created_doc(**extra) -> dict:
    op = dict(_DELETE_CREATED)
    op.update(extra)
    return _doc(delete=_delete(), post=_post(**op))


def _compile_src(src: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".rvl", delete=False) as handle:
        handle.write(src)
        path = handle.name
    return compile_files([path])


def _extern(ir: dict, name: str) -> dict:
    for ext in ir["externs"]:
        if ext["name"] == name:
            return ext
    raise KeyError(name)


def _claim(src: str, needle: str) -> str:
    return next(ln for ln in src.splitlines() if needle in ln and ln.startswith("  //"))


# ------------------------------------------- DELETE: the `recreate` route

def test_a_delete_with_a_documented_recreate_lowers_as_a_compensate_emission():
    """The Slice 1 crux, re-run for the recreate form: a `DELETE` promoted with
    a preimage and a documented create emits an `emission[net.<host>]` extern
    with a compensate slot, and it COMPILES. It is a `compensate` on an
    `emission`, never a witnessed `undo`, so the rule-3 refusal is not on this
    path here either."""
    src = import_openapi(_recreate_doc(), backend="py")

    assert "extern emission[net.api_example_com] fn http_config_api_delete_config" in src
    assert "compensate http_config_api_delete_config_compensate()" in src

    ir = _compile_src(src)
    forward = _extern(ir, "http_config_api_delete_config")
    assert forward["class"] == "emission"
    assert forward["capabilities"] == ["net.api_example_com"]
    assert forward.get("compensate") is not None
    assert forward.get("undo") is None
    assert all(ext["class"] != "witnessed" for ext in ir["externs"])

    # the reversal is itself an outbound crossing the fork rewind must not fire
    reversal = _extern(ir, "http_config_api_delete_config_compensate")
    assert reversal["capabilities"] == ["net.api_example_com"]
    assert cap_scope_enumerated_not_run(reversal["capabilities"]) is True


def test_the_recreate_reversal_is_enumerated_as_a_compensation_not_an_inverse():
    ir = _compile_src(import_openapi(_recreate_doc(), backend="py"))
    surface = audit_report(ir)["recovery_surface"]
    forward = [e for e in surface if e["name"] == "http_config_api_delete_config"]
    assert forward, "the compensated DELETE is not enumerated on the audit surface"
    assert all(e["kind"] == "compensation" for e in forward)
    assert not any(e["kind"] == "inverse" for e in surface)


def test_the_recreate_states_the_soft_delete_residue_as_OPEN():
    """Attack 2's undetectable half. A recreate restores a resource on the API
    author's notion of equivalence; a soft delete or a re-stamped
    server-assigned field is invisible in the document. The generated surface
    must SAY so on the operation, not only in the file header."""
    src = import_openapi(_recreate_doc(), backend="py")
    residue = _claim(src, "OPEN")
    assert "attack 2" in residue
    assert "soft delete" in residue
    # and the ceiling is still stated, on the operation
    assert "NOT a proof surface" in _claim(src, "COMPENSATE-grade")


def test_a_delete_whose_recreate_cannot_carry_the_preimage_is_refused():
    """Attack 2's DETECTABLE half: if the create's request body cannot hold what
    the preimage returns, the recreate builds a different resource. The schema
    gap is a refusal, not a downgrade — a compensation that restores something
    else is worse than no compensation, because the audit surface would claim
    the crossing was offset."""
    post = _post(requestBody={"required": True,
                              "content": {"application/json": {"schema": {"type": "string"}}}})
    doc = _doc(get=_get(), delete=_delete(**_RECREATE), post=post)
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    msg = str(exc.value)
    assert "attack 2" in msg
    assert "Str" in msg and "Config" in msg


def test_a_recreate_that_takes_no_body_at_all_is_refused():
    doc = _doc(get=_get(), delete=_delete(**_RECREATE),
               post={"operationId": "createConfig",
                     "responses": {"201": {"description": "made"}}})
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert "no request body" in str(exc.value)


def test_a_recreate_wired_to_a_safe_operation_is_refused():
    """A reversal must cross the boundary the forward effect crossed. An
    operation weakened to a plain `fn` crosses nothing, so it cannot be one."""
    doc = _doc(get=_get(), delete=_delete(**_RECREATE),
               post=_post(**{"x-revl-emission": False}))
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert "emission" in str(exc.value)


def test_a_preimage_with_no_response_body_captures_no_pre_state():
    get = {"operationId": "getConfig", "parameters": _ID,
           "responses": {"204": {"description": "nothing"}}}
    doc = _doc(get=get, delete=_delete(**_RECREATE), post=_post())
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert "no pre-state" in str(exc.value) or "captures no pre-state" in str(exc.value)


# --------------------------------------- POST: the `delete-created` route

def test_a_post_with_a_documented_delete_and_a_key_lowers_as_a_compensate():
    """The `POST` form the verb gate now admits — and admits ONLY in this shape.
    It re-issues nothing, so it never rests on `POST` idempotence; the forward
    operation stays a bare `emission fn` (no `idempotent` word appears on it),
    and the reversal is addressed by a documented response key."""
    src = import_openapi(_created_doc(), backend="py")

    assert "extern emission[net.api_example_com] fn http_config_api_create_config" in src
    assert "compensate http_config_api_create_config_compensate()" in src
    assert "emission idempotent fn create_config" not in src

    ir = _compile_src(src)
    forward = _extern(ir, "http_config_api_create_config")
    assert forward["class"] == "emission"
    assert forward["capabilities"] == ["net.api_example_com"]
    assert forward.get("compensate") is not None
    assert forward.get("undo") is None
    assert all(ext["class"] != "witnessed" for ext in ir["externs"])


def test_the_delete_created_reversal_is_enumerated_as_a_compensation():
    ir = _compile_src(import_openapi(_created_doc(), backend="py"))
    surface = audit_report(ir)["recovery_surface"]
    forward = [e for e in surface if e["name"] == "http_config_api_create_config"]
    assert forward, "the compensated POST is not enumerated on the audit surface"
    assert all(e["kind"] == "compensation" for e in forward)
    assert not any(e["kind"] == "inverse" for e in surface)


def test_the_generated_stub_says_the_forward_call_must_stash_the_key():
    """The half an implementer is most likely to leave out: the key has to be
    captured by the FORWARD body, or the reversal has nothing to address."""
    src = import_openapi(_created_doc(), backend="py")
    forward_stub = next(ln for ln in src.splitlines()
                        if "POST https://api.example.com/v1/config" in ln)
    assert "stash the response key id" in forward_stub
    reversal_stub = next(ln for ln in src.splitlines()
                         if "best-effort reversal: DELETE" in ln)
    assert "addressed by the stashed id" in reversal_stub


def test_deleting_the_created_resource_does_not_undo_its_consequences():
    """§3 over the network, in the shape this form takes: the row goes away, the
    welcome email does not. Stated on the operation."""
    residue = _claim(import_openapi(_created_doc(), backend="py"), "OPEN")
    assert "does not undo" in residue


@pytest.mark.parametrize("drop,expected", [
    ("x-revl-undo-key", "no response key"),
    ("x-revl-undo", "no delete operation"),
])
def test_an_incomplete_post_route_is_refused(drop, expected):
    """Half a route is a refusal. The `delete-created` form has no defaults: a
    `POST` is not its own inverse and its response field cannot be guessed."""
    op = {k: v for k, v in _DELETE_CREATED.items() if k != drop}
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(delete=_delete(), post=_post(**op)), backend="py")
    assert expected in str(exc.value)


def test_an_unkeyed_post_compensation_is_refused_not_guessed():
    """THE fail-closed hinge of this form. A `POST` whose response cannot name
    what it created has no addressable reversal, and a reversal aimed at nothing
    would report success from teardown either way. It is refused."""
    post = _post(**dict(_DELETE_CREATED, **{"responses": {"201": {"description": "made"}}}))
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(delete=_delete(), post=post), backend="py")
    msg = str(exc.value)
    assert "decodes no response body" in msg
    assert "cannot be compensated" in msg


def test_a_key_that_may_be_absent_is_not_a_key():
    """An `Opt[T]` field is an unkeyed compensation wearing a key's name: the
    response is allowed to omit it, and then the reversal has nothing. Only a
    required, non-nullable field counts."""
    schema = {"type": "object", "required": ["value"],
              "properties": {"id": {"type": "string"},
                             "value": {"type": "string"}}}
    doc = _doc(delete=_delete(), post=_post(**_DELETE_CREATED), schema=schema)
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert "not a required field" in str(exc.value)


def test_a_key_read_from_a_non_record_response_is_refused():
    post = _post(**dict(_DELETE_CREATED,
                        **{"responses": {"201": {"description": "made",
                                                 "content": {"application/json":
                                                             {"schema": {"type": "string"}}}}}}))
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(delete=_delete(), post=post), backend="py")
    assert "not a record" in str(exc.value)


def test_a_reversal_that_needs_more_than_the_key_is_refused():
    """One captured value addresses one parameter. A delete that needs more
    inputs than the response names cannot be addressed from this POST alone, so
    the promotion is refused rather than emitted with a hole in it."""
    delete = _delete(parameters=_ID + [{"name": "tenant", "in": "query",
                                        "required": True,
                                        "schema": {"type": "string"}}])
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(delete=delete, post=_post(**_DELETE_CREATED)), backend="py")
    assert "one response key cannot address it" in str(exc.value)


@pytest.mark.parametrize("undo,why", [("getConfig", "GET"), ("createConfig", "POST")])
def test_a_reversal_wired_to_the_wrong_verb_is_refused(undo, why):
    """A reversal wired to the wrong verb is not a weaker reversal; it is a
    second forward effect wearing a reversal's name. The route form fixes the
    verb, and a mismatch is a refusal."""
    doc = _doc(get=_get(), delete=_delete(),
               post=_post(**dict(_DELETE_CREATED, **{"x-revl-undo": undo})))
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert why in str(exc.value)


def test_a_post_that_also_names_a_preimage_is_a_conflicting_claim():
    """`delete-created` removes what the POST made; there is no pre-state. A
    document that claims both is asking for two different reversals and is told
    so rather than silently given one."""
    post = _post(**dict(_DELETE_CREATED, **{"x-revl-preimage": "getConfig"}))
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(get=_get(), delete=_delete(), post=post), backend="py")
    assert "no pre-state to restore" in str(exc.value)


# ---------------------------------------- HIGH 1: the concurrency-token story

@pytest.mark.parametrize("factory,token", [
    (_recreate_doc, "If-None-Match"),
    (_created_doc, "If-Match"),
])
def test_each_form_names_the_token_its_reversal_issues_under(factory, token):
    """The token is per FORM, not one blanket sentence: a recreate is a create,
    so its precondition is `If-None-Match: *` — fail loudly if the resource came
    back rather than overwrite whatever took its place."""
    src = import_openapi(factory(**{"x-revl-if-match": True}), backend="py")
    posture = _claim(src, "reversal issued")
    assert token in posture
    assert "FAILS LOUDLY" in posture
    assert "best-effort-may-clobber" not in posture


@pytest.mark.parametrize("factory", [_recreate_doc, _created_doc])
def test_without_a_token_each_form_admits_may_clobber_explicitly(factory):
    posture = _claim(import_openapi(factory(), backend="py"), "reversal issued")
    assert "best-effort-may-clobber" in posture
    assert "succeeds silently" in posture


@pytest.mark.parametrize("factory", [_recreate_doc, _created_doc])
def test_require_if_match_turns_the_may_clobber_path_into_a_refusal(factory):
    """HIGH 1 asked for a refusal path AND an explicit may-clobber path, never a
    silent clobber. Slice 1 shipped the second; this is the first, and it is the
    importing engineer's choice, not the document author's."""
    with pytest.raises(RevlError) as exc:
        import_openapi(factory(), backend="py", require_if_match=True)
    msg = str(exc.value)
    assert "claims no version/ETag token" in msg
    assert "x-revl-if-match" in msg


@pytest.mark.parametrize("factory", [_recreate_doc, _created_doc])
def test_require_if_match_accepts_an_endpoint_that_claims_a_token(factory):
    src = import_openapi(factory(**{"x-revl-if-match": True}), backend="py",
                         require_if_match=True)
    assert "FAILS LOUDLY" in src
    _compile_src(src)


def test_the_document_root_may_ask_for_the_strict_policy_too():
    """Parity with the rest of the annotation family: the document author can
    ask for the strict posture even when the importing engineer does not."""
    doc = _recreate_doc()
    doc["x-revl-require-if-match"] = True
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert "claims no version/ETag token" in str(exc.value)


# --------------------------------------------------- the gate that stays shut

def test_patch_is_still_refused_in_every_form():
    """The one verb the gate closes outright. `PATCH` is not idempotent and a
    partial merge has no inverse a document can name — neither route shape
    rescues it."""
    patch = {"operationId": "patchConfig", "parameters": _ID,
             "requestBody": {"required": True,
                             "content": {"application/json": {"schema": _CFG}}},
             "responses": {"200": {"description": "ok",
                                   "content": {"application/json": {"schema": _CFG}}}},
             "x-revl-compensate": True, "x-revl-preimage": "getConfig",
             "x-revl-undo": "patchConfig"}
    doc = _doc(get=_get(), post=_post())
    doc["paths"]["/config/{id}"]["patch"] = patch
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    msg = str(exc.value)
    assert "PATCH" in msg and "idempotent" in msg


def test_the_engineer_flags_promote_the_new_forms_the_same_way():
    """`--compensate`/`--preimage`/`--undo`/`--undo-key` are the out-of-band
    equivalents, and they resolve to byte-identical source."""
    engineer = import_openapi(
        _doc(get=_get(), delete=_delete(), post=_post()), backend="py",
        compensate=["deleteConfig"], preimage=["deleteConfig=getConfig"],
        undo=["deleteConfig=createConfig"])
    assert engineer == import_openapi(_recreate_doc(), backend="py")

    keyed = import_openapi(
        _doc(delete=_delete(), post=_post()), backend="py",
        compensate=["createConfig"], undo=["createConfig=deleteConfig"],
        undo_key=["createConfig=id"])
    assert keyed == import_openapi(_created_doc(), backend="py")


def test_an_unannotated_document_is_untouched_by_slice_2():
    """Additive, still: with no annotation and no flags, no verb gains a net
    cap, a compensate slot or an item-254 header block."""
    src = import_openapi(_doc(get=_get(), delete=_delete(), post=_post()),
                         backend="py")
    assert "compensate" not in src
    assert "net." not in src
    assert "item 254" not in src
