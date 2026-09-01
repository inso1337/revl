"""`revl import openapi` — item 254 Slice 1, compensate-grade network effects.

The recast (docs/design/254-witnessed-network.md, "Revision" §) is authoritative:
a network reversal is COMPENSATE-grade (item 247), NOT a proof-surface witness.
The pre-revision `witnessed[net.*]` write cannot compile — `_check_witnessed_inverse`
(lower.py:2153) refuses a witnessed inverse whose callee is emission-classified,
and this importer classifies every PUT as an emission. So Slice 1 attaches an
item-247 `compensate` slot to the PUT's `emission[net.<host>]` extern instead.

The crux under test is that this LOWERS cleanly: a `compensate` on an `emission`
is not on the rule-3 path, so the generated program compiles where a witnessed
net inverse would have been refused. The rest pins the honesty: PUT-only verb
gate, the reversal enumerated on the audit surface tagged compensated-not-undone
(never a witness/noResidue proof), the item-250 cap-scope enumerate-not-run
predicate (incl. the mixed [fs, net.x] taint case), and byte-identical output for
a document that carries no annotation.
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

_STR = {"type": "string"}
_OK = {"200": {"description": "ok",
               "content": {"application/json": {"schema": _STR}}}}
_ID_PARAM = [{"name": "id", "in": "path", "required": True, "schema": _STR}]
_BODY = {"content": {"application/json": {"schema": _STR}}}


def _doc(put_op: dict, *, verb: str = "put", server: bool = True) -> dict:
    """A GET-preimage + write-verb document over one `/config/{id}` resource."""
    get_op = {"operationId": "getConfig", "parameters": _ID_PARAM, "responses": _OK}
    doc = {"openapi": "3.0.3",
           "info": {"title": "Config API", "version": "1.0.0"},
           "paths": {"/config/{id}": {"get": get_op, verb: put_op}}}
    if server:
        doc["servers"] = [{"url": "https://api.example.com/v1"}]
    return doc


def _put(**extra) -> dict:
    op = {"operationId": "setConfig", "parameters": _ID_PARAM,
          "requestBody": _BODY, "responses": _OK}
    op.update(extra)
    return op


_ANNOTATED = {"x-revl-compensate": True, "x-revl-preimage": "getConfig",
              "x-revl-undo": "setConfig"}


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


# ----------------------------------------- the crux: it lowers as a compensate

def test_a_put_with_the_three_annotations_lowers_as_a_compensate_emission():
    """A PUT declared compensate-grade emits an `emission[net.<host>]` extern
    WITH a compensate slot, and it COMPILES — the crux the recast turns on. It
    is a `compensate` on an `emission`, never a witnessed `undo`, so the rule-3
    `_check_witnessed_inverse` refusal (lower.py:2153) is not on this path."""
    src = import_openapi(_doc(_put(**_ANNOTATED)), backend="py")

    assert "extern emission[net.api_example_com] fn http_config_api_set_config" in src
    assert "compensate http_config_api_set_config_compensate()" in src

    ir = _compile_src(src)  # would raise RevlError(code=G5) if refused as witnessed
    forward = _extern(ir, "http_config_api_set_config")
    assert forward["class"] == "emission"
    assert forward["capabilities"] == ["net.api_example_com"]
    assert forward.get("compensate") is not None
    # it is NOT a witnessed inverse: no proof-grade undo is attached
    assert forward.get("undo") is None
    assert forward["class"] != "witnessed"

    # the reversal is itself an outbound net crossing (a real emission)
    reversal = _extern(ir, "http_config_api_set_config_compensate")
    assert reversal["class"] == "emission"
    assert reversal["capabilities"] == ["net.api_example_com"]


def test_no_extern_is_ever_classified_witnessed():
    """The classification ceiling is `compensate`, not `witnessed`: nothing in
    the generated program is a proof-surface witness."""
    ir = _compile_src(import_openapi(_doc(_put(**_ANNOTATED)), backend="py"))
    assert all(ext["class"] != "witnessed" for ext in ir["externs"])


# --------------------------------------------------------------- the verb gate

@pytest.mark.parametrize("verb", ["post", "patch"])
def test_compensate_is_a_hard_error_off_an_idempotent_verb(verb):
    """The verb gate (attack 3): the promotion is honoured only on PUT. On a
    POST/PATCH — verbs the RFC does NOT define idempotent — it is a HARD error,
    so the annotation can never invent idempotence."""
    op = _put(**{"x-revl-compensate": True, "x-revl-preimage": "getConfig"})
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(op, verb=verb), backend="py")
    msg = str(exc.value)
    assert verb.upper() in msg
    assert "idempotent" in msg


def test_delete_compensate_is_deferred_to_slice_2():
    op = {"operationId": "setConfig", "parameters": _ID_PARAM, "responses": _OK,
          "x-revl-compensate": True, "x-revl-preimage": "getConfig"}
    with pytest.raises(RevlError) as exc:
        import_openapi(_doc(op, verb="delete"), backend="py")
    assert "Slice 2" in str(exc.value)


def test_the_engineer_flag_promotes_the_same_way_as_the_annotation():
    """`--compensate` / `--preimage` mirror the `x-revl-*` annotations."""
    src = import_openapi(_doc(_put()), backend="py",
                         compensate=["setConfig"],
                         preimage=["setConfig=getConfig"])
    assert "compensate http_config_api_set_config_compensate()" in src
    _compile_src(src)


def test_a_preimage_that_is_not_safe_is_refused():
    """The preimage must be a SAFE read; an emission read cannot serve as one
    (item 254 §1.2)."""
    doc = _doc(_put(**{"x-revl-compensate": True, "x-revl-preimage": "setConfig"}))
    with pytest.raises(RevlError) as exc:
        import_openapi(doc, backend="py")
    assert "safe" in str(exc.value)


# ---------------------------------------- the audit surface: compensated-not-undone

def test_the_reversal_is_enumerated_on_the_audit_surface_as_a_compensation():
    """The reversal shows on the audit recovery surface as its own entry, tagged
    a `compensation` — NOT an `inverse`. An `inverse` is the proof-grade
    witnessed/undo reversal; a `compensation` is best-effort and audit-surface.
    There is no witness/noResidue proof claim for it anywhere in the IR."""
    ir = _compile_src(import_openapi(_doc(_put(**_ANNOTATED)), backend="py"))
    surface = audit_report(ir)["recovery_surface"]

    forward = [e for e in surface if e["name"] == "http_config_api_set_config"]
    assert forward, "the compensated crossing is not enumerated on the audit surface"
    assert all(e["kind"] == "compensation" for e in forward)
    # never enumerated as a proof-grade inverse (undo/witness)
    assert not any(e["kind"] == "inverse" for e in surface)


# ---------------------------------------- item 250: the cap-scope rewind predicate

def test_item_250_predicate_enumerates_not_runs_any_net_tainted_scope():
    """HIGH 2: a reversal is enumerated-not-run by a fork rewind IFF its scope
    contains ANY non-host-confined cap. A single net cap taints the whole
    reversal, so the mixed [fs, net.x] scope is never speculatively fired even
    though its fs component alone would have run."""
    # a pure host-confined (fs) reversal RUNS on a fork rewind
    assert cap_scope_enumerated_not_run(["fs"]) is False
    assert cap_scope_enumerated_not_run(['fs.write(path="/data")']) is False

    # a net reversal is enumerated-not-run (a speculative remote PUT is residue)
    assert cap_scope_enumerated_not_run(["net.api_example_com"]) is True

    # the mixed case: net.x taints the whole scope even beside a host-confined fs
    assert cap_scope_enumerated_not_run(["fs", "net.x"]) is True

    # a bare crossing whose boundary is un-nameable is enumerated-not-run too
    assert cap_scope_enumerated_not_run([]) is True


def test_the_generated_net_scope_is_enumerated_not_run():
    """The scope the importer actually writes onto the compensate-grade extern
    is one the item-250 rewind enumerates-not-runs — so a fork rewind can never
    speculatively re-issue this remote PUT."""
    ir = _compile_src(import_openapi(_doc(_put(**_ANNOTATED)), backend="py"))
    forward = _extern(ir, "http_config_api_set_config")
    assert cap_scope_enumerated_not_run(forward["capabilities"]) is True


# ------------------------------------------------- If-Match / HIGH 1 posture

def test_no_version_token_is_marked_best_effort_may_clobber():
    src = import_openapi(_doc(_put(**_ANNOTATED)), backend="py")
    op_line = next(ln for ln in src.splitlines() if "reversal issued" in ln)
    assert "best-effort-may-clobber" in op_line
    assert "If-Match" in src  # the recommendation is still stated in the header


def test_if_match_annotation_switches_to_the_loud_failure_posture():
    op = _put(**_ANNOTATED, **{"x-revl-if-match": True})
    src = import_openapi(_doc(op), backend="py")
    assert "FAILS LOUDLY" in src
    # the PER-OPERATION reversal line states the loud posture, not may-clobber
    # (the top-level header still explains both postures in general prose)
    op_line = next(ln for ln in src.splitlines() if "reversal issued" in ln)
    assert "If-Match" in op_line
    assert "best-effort-may-clobber" not in op_line


# ------------------------------------------------------ additive / byte-identical

def test_a_put_with_no_annotation_is_unchanged_and_additive():
    """A document with no `x-revl-compensate` emits exactly what item 44 makes:
    a bare `emission idempotent fn`, no net cap, no compensate slot, no item-254
    header block. The feature is purely additive."""
    src = import_openapi(_doc(_put()), backend="py")
    assert "compensate" not in src
    assert "net." not in src
    assert "item 254" not in src
    assert "extern emission fn http_config_api_set_config(" in src
    ir = _compile_src(src)
    assert _extern(ir, "http_config_api_set_config").get("compensate") is None


def test_no_annotation_matches_the_prior_importer_byte_for_byte():
    """Belt-and-braces on additivity: the plain PUT path must be byte-identical
    whether or not the compensate machinery is present in the module. Generating
    with empty engineer flags must equal generating with none at all."""
    doc = _doc(_put())
    assert import_openapi(doc, backend="py") == import_openapi(
        doc, backend="py", compensate=[], preimage=[], undo=[], if_match=[])
