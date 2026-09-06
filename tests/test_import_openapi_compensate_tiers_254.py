"""`revl import openapi` — item 254 Slice 3, the other host tiers (ts, rust).

Slices 1 and 2 landed the compensate-grade network effects on the `py` tier
(PUT-restore, DELETE-recreate, POST-delete-created, the concurrency-token
story) and deferred the other tiers to Slice 3
(docs/design/254-witnessed-network.md §7 "Re-sliced Slice 1", the "explicitly
not in Slice 1: other tiers (Slice 3)" line, kept by the Slice 2 commit).

This pins Slice 3: the same compensate-grade classification generates, compiles,
lowers and EMITS on the `ts` and `rust` host tiers.

The compensate machinery is tier-agnostic by construction — a `compensate` on an
`emission[net.<host>]` extern is the same IR whatever host seam fills the block,
so the audit surface (item 250's net-cap enumerate-not-run, the
compensation-not-inverse recovery entry) is identical to the py tier. The one
thing that is NOT tier-agnostic is the host-block dialect token: the rust backend
reads `bodies["rs"]` (`@rs`, the canonical corpus spelling) and refuses a `@rust`
body as "no @rs body", so a rust-tier import must write `@rs` to be portable.
Before Slice 3 the importer wrote `@{self.backend}` verbatim, so a rust import
generated source the rust backend could not emit; Slice 3 canonicalises the token
(`_BACKEND_DIALECT`) while the backend NAME `rust` stays user-facing.
"""

import re
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

from _backend_import import backend_emitter  # noqa: E402


# ---------------------------------------------------------- document helpers

_STR = {"type": "string"}
_OK = {"200": {"description": "ok",
               "content": {"application/json": {"schema": _STR}}}}
_CREATED = {"201": {"description": "created",
                    "content": {"application/json": {"schema": {
                        "type": "object", "required": ["id"],
                        "properties": {"id": _STR}}}}}}
_ID_PARAM = [{"name": "id", "in": "path", "required": True, "schema": _STR}]
_BODY = {"content": {"application/json": {"schema": _STR}}}
_SERVER = [{"url": "https://api.example.com/v1"}]


def _base(paths: dict) -> dict:
    return {"openapi": "3.0.3",
            "info": {"title": "Config API", "version": "1.0.0"},
            "servers": _SERVER, "paths": paths}


def _put_doc() -> dict:
    """A PUT-restore: GET preimage, PUT it back (Slice 1)."""
    return _base({"/config/{id}": {
        "get": {"operationId": "getConfig", "parameters": _ID_PARAM,
                "responses": _OK},
        "put": {"operationId": "setConfig", "parameters": _ID_PARAM,
                "requestBody": _BODY, "responses": _OK,
                "x-revl-compensate": True, "x-revl-preimage": "getConfig",
                "x-revl-undo": "setConfig"}}})


def _delete_doc() -> dict:
    """A DELETE-recreate: GET preimage, re-send it through a documented create
    (Slice 2)."""
    return _base({
        "/config/{id}": {
            "get": {"operationId": "getConfig", "parameters": _ID_PARAM,
                    "responses": _OK},
            "delete": {"operationId": "delConfig", "parameters": _ID_PARAM,
                       "responses": _OK, "x-revl-compensate": True,
                       "x-revl-preimage": "getConfig",
                       "x-revl-undo": "createConfig"}},
        "/config": {"post": {"operationId": "createConfig",
                             "requestBody": _BODY, "responses": _OK}}})


def _post_doc() -> dict:
    """A POST-delete-created: DELETE what the POST created, addressed by the
    required response key (Slice 2)."""
    return _base({
        "/things": {"post": {"operationId": "createThing", "requestBody": _BODY,
                             "responses": _CREATED, "x-revl-compensate": True,
                             "x-revl-undo": "delThing", "x-revl-undo-key": "id"}},
        "/things/{id}": {"delete": {"operationId": "delThing",
                                    "parameters": _ID_PARAM, "responses": _OK}}})


_FORMS = {"put": _put_doc, "delete": _delete_doc, "post": _post_doc}
#: the item-254 host tiers other than `py`; `wasm` has no network seam and is
#: deliberately unsupported (import_openapi refuses it).
_OTHER_TIERS = ("ts", "rust")
#: the `@<dialect>` token each tier's host block is written with.
_DIALECT = {"py": "py", "ts": "ts", "rust": "rs"}
#: the `backends/<dir>/emit.py` directory for each importer backend name (the
#: `ts` tier's emitter lives under `typescript/`).
_EMIT_DIR = {"py": "python", "ts": "typescript", "rust": "rust"}


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


def _strip_host_bodies(src: str) -> str:
    """`src` with every `@<dialect> { <host body> }` collapsed to a marker.

    The host body is the ONE tier-specific span (a single-line comment whose
    braces are neutralised to parens by `_host_comment`, so `[^{}]*` matches the
    whole body). Collapsing it lets us assert the rest of the generated program
    — the extern signatures, the `compensate` slots, the net caps, the service
    methods, the item-254 header block — is byte-identical across tiers."""
    return re.sub(r"@\w+ \{[^{}]*\}", "@<host>", src)


# ------------------------------------- the crux: it generates, compiles, emits

@pytest.mark.parametrize("form", sorted(_FORMS))
@pytest.mark.parametrize("backend", _OTHER_TIERS)
def test_each_compensate_form_generates_compiles_and_emits_on_each_tier(form, backend):
    """Every compensate-grade form (PUT/DELETE/POST) generates source that
    compiles AND emits through the tier's own backend — the deliverable Slice 3
    turns on. A `compensate` on an `emission` is not on the rule-3 witnessed
    path, so it lowers on every tier exactly as it does on py."""
    src = import_openapi(_FORMS[form](), backend=backend)
    ir = _compile_src(src)

    # a compensate reversal extern exists and is itself an outbound net crossing
    reversal = next(e for e in ir["externs"] if e["name"].endswith("_compensate"))
    assert reversal["class"] == "emission"
    assert reversal["capabilities"] and reversal["capabilities"][0].startswith("net.")
    # nothing is a proof-surface witness — the ceiling is compensate
    assert all(e["class"] != "witnessed" for e in ir["externs"])

    emitted = backend_emitter(_EMIT_DIR[backend]).emit(ir)
    text = emitted if isinstance(emitted, str) else "".join(emitted.values())
    assert reversal["name"] in text


# ------------------------------------------------ the rust dialect canonicalise

def test_the_rust_tier_writes_an_at_rs_host_block_not_at_rust():
    """The rust fix: a rust-tier import writes `@rs` (what the rust backend
    reads), never `@rust` (which parses but the backend refuses as "no @rs
    body"). The backend NAME stays `rust` — only the emitted host-block token is
    canonicalised."""
    src = import_openapi(_put_doc(), backend="rust")
    assert "= @rs {" in src
    assert "@rust" not in src
    # and it actually emits through the rust backend now
    backend_emitter("rust").emit(_compile_src(src))


def test_wasm_stays_refused_no_network_seam():
    """`wasm` is deliberately unsupported: the cordis-wasm tier has no host
    network seam, so it cannot hold an HTTP client (unchanged by Slice 3)."""
    with pytest.raises(RevlError) as exc:
        import_openapi(_put_doc(), backend="wasm")
    assert "wasm" in str(exc.value)


# --------------------------------- the host seam is the ONLY tier difference

@pytest.mark.parametrize("form", sorted(_FORMS))
@pytest.mark.parametrize("backend", _OTHER_TIERS)
def test_the_host_block_is_the_only_tier_difference(form, backend):
    """With the host blocks collapsed, a ts/rust import is byte-identical to the
    py import: the compensate classification, the net cap scope, the audit
    header and the routed reversal are all tier-agnostic. Only the host seam —
    the `@<dialect> { ... }` block the engineer fills in — differs per tier."""
    doc = _FORMS[form]
    py_stripped = _strip_host_bodies(import_openapi(doc(), backend="py"))
    other_stripped = _strip_host_bodies(import_openapi(doc(), backend=backend))
    assert other_stripped == py_stripped


@pytest.mark.parametrize("backend", _OTHER_TIERS)
def test_the_dialect_token_matches_the_tier(backend):
    """The collapsed-away host block really is written with the tier's own
    dialect token, so `test_the_host_block_is_the_only_tier_difference` is
    collapsing a real per-tier span, not an accidental match."""
    src = import_openapi(_put_doc(), backend=backend)
    assert f"= @{_DIALECT[backend]} {{" in src


# ------------------------------ the audit surface is identical across tiers

@pytest.mark.parametrize("backend", _OTHER_TIERS)
def test_the_reversal_is_enumerated_as_a_compensation_on_every_tier(backend):
    """The reversal shows on the audit recovery surface as a `compensation`, not
    a proof-grade `inverse`, on the ts/rust tiers exactly as on py."""
    ir = _compile_src(import_openapi(_put_doc(), backend=backend))
    surface = audit_report(ir)["recovery_surface"]

    forward = [e for e in surface if e["name"] == "http_config_api_set_config"]
    assert forward, "the compensated crossing is not enumerated on the audit surface"
    assert all(e["kind"] == "compensation" for e in forward)
    assert not any(e["kind"] == "inverse" for e in surface)


@pytest.mark.parametrize("backend", _OTHER_TIERS)
def test_the_generated_net_scope_is_enumerated_not_run_on_every_tier(backend):
    """The net cap the importer writes onto the compensate-grade extern is one
    the item-250 fork rewind enumerates-not-runs on every tier, so a fork rewind
    can never speculatively re-issue the remote reversal."""
    ir = _compile_src(import_openapi(_put_doc(), backend=backend))
    forward = _extern(ir, "http_config_api_set_config")
    assert forward["capabilities"] == ["net.api_example_com"]
    assert cap_scope_enumerated_not_run(forward["capabilities"]) is True


# ------------------------------------------------------ additive on every tier

@pytest.mark.parametrize("backend", _OTHER_TIERS)
def test_an_unannotated_document_is_additive_on_every_tier(backend):
    """A document with no `x-revl-compensate` carries no compensate slot, no net
    cap and no item-254 header on any tier — the feature is purely additive."""
    doc = _base({"/config/{id}": {
        "get": {"operationId": "getConfig", "parameters": _ID_PARAM,
                "responses": _OK},
        "put": {"operationId": "setConfig", "parameters": _ID_PARAM,
                "requestBody": _BODY, "responses": _OK}}})
    src = import_openapi(doc, backend=backend)
    assert "compensate" not in src
    assert "net." not in src
    assert "item 254" not in src
    ir = _compile_src(src)
    assert _extern(ir, "http_config_api_set_config").get("compensate") is None
