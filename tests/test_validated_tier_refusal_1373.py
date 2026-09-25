"""Items 257/513, issue #1373: five tiers refuse a `validated` emission instead
of dropping it.

A `validated` emission is a CHECKED boundary. Item 257 derives a schema from the
declared return type, item 513 renders a decoding grammar from it, and the
emitted crossing validates the completion, builds the declared value from the
validated payload, raises a typed validation fault when it does not conform, and
honours the `retry` budget. All of that lives in the python tier.

The other five did not refuse the modifier, they DROPPED it. Reproduced on
`fc0d84ce9`, one program compiled twice differing only in `validated`:

    carrier            tier         validated   plain   identical
    service operation  python            3148    1218   no
    service operation  typescript         899     899   YES
    service operation  rust              4468    4468   YES
    service operation  wasm              2909    2909   YES
    service operation  go                5748    5748   YES
    service operation  java              6000    6000   YES
    extern             python         refused     803   no  (issue #1382)
    extern             typescript         616     616   YES
    extern             rust               647     647   YES
    extern             wasm               682     682   YES
    extern             go                 503     503   YES
    extern             java              5446     5446  YES

The java row is MEASURED, not inferred. The issue could only read java from
source (0 functional references to the three keys in its `emit.py`) because its
probe program refused first on an unrelated `Object` type-name collision; that
collision belongs to the probe's `provide` block, not to the tier. On a program
java lowers, java drops the modifier exactly like the other four.

Every test below is paired with a control that still emits, so a tier that
refused everything would fail this file rather than satisfy it.

What this file does NOT gate, on purpose:

  * the python tier, which owns the seam at the service-operation carrier and
    keeps lowering it in full. Its own narrower extern gate is issue #1382 and
    `tests/test_validated_extern_python_1382.py`.
  * `--target temporal`, a RENDERING of the typescript emitter that returns from
    `emit()` before the cordis refusals run. That target READS `validated` on an
    extern (`_retry_class` pins the crossing to at-most-once so a completion is
    never re-billed as an idempotent write), so a blanket refusal there would
    delete a tested guarantee. It drops the modifier on a service-method
    crossing, which is narrower than this issue and needs its own decision.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.validated_boundary import (  # noqa: E402
    UNVALIDATING_TIERS,
    VALIDATED_CATEGORY,
    VALIDATED_CODE,
    refuse_validated_on_unvalidating_tier,
    validated_crossings,
)

TIERS = UNVALIDATING_TIERS

_TYPES = "type Turn = Final(Str) | Uses(Str)\n"

# Carrier A, the service operation. No `provide` block: the java tier refuses a
# provide-shaped probe on an unrelated `Object` collision, and this file wants
# the validated question reached on all five.
_OPERATION = _TYPES + """
service Model { emission[model] %sfn complete(h: List[Str]) -> Turn }
component Agent requires model: Model {
  emit model.complete(["p"])
}
"""

# Carrier B, the extern. A portable body for every tier, so the only thing that
# can refuse it on a non-py tier is this gate and not the unrelated
# "no @<tier> body" portability refusal.
_EXTERN = _TYPES + (
    "extern emission[model] %sfn complete(h: Str) -> Turn = @py { return h }"
    " = @ts { return h; } = @rs { h } = @go { return h } = @java { return h; }\n"
)

# Carrier B with nothing calling it: the gate is declaration-keyed.
_EXTERN_UNCALLED = _EXTERN

# The same extern alongside a live component and a service, which is what go's
# placement entry point needs before it will place anything at all.
_EXTERN_PLACED = _EXTERN + (
    "service Ops { emission fn go(p: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn go(p) { emit complete(p) } }\n"
    "}\n"
)


def _emitter(tier: str):
    """Load a backend emitter by path under a unique module name (a bare
    `import emit` collides across backends)."""
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_1373_{tier}_emit", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    backend_dir = str(path.parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------- carrier A: operation

@pytest.mark.parametrize("tier", TIERS)
def test_a_validated_service_operation_is_refused_by_name(tier):
    """Red before this change on all five: the emitted module was byte-identical
    to the unvalidated twin and nothing said so."""
    module = _emitter(tier)
    with pytest.raises(module.EmitError) as exc:
        module.emit(compile_source(_OPERATION % "validated ", "t.rvl"))
    msg = str(exc.value)
    assert "`validated` operation `Model.complete`" in msg
    assert f"the {tier} tier does not have one" in msg
    assert "response-validation seam" in msg


@pytest.mark.parametrize("tier", TIERS)
def test_the_unvalidated_operation_twin_still_emits(tier):
    """The control. The gate keys on the modifier and nothing else, so the same
    program without it emits exactly as before."""
    out = _emitter(tier).emit(compile_source(_OPERATION % "", "t.rvl"))
    assert out


# ------------------------------------------------------------ carrier B: extern

@pytest.mark.parametrize("tier", TIERS)
def test_a_validated_extern_is_refused_by_name(tier):
    """The issue's table covered the operation carrier. `lower.py` binds the same
    three keys on an extern, and all five dropped that one too."""
    module = _emitter(tier)
    with pytest.raises(module.EmitError) as exc:
        module.emit(compile_source(_EXTERN % "validated ", "t.rvl"))
    assert "`validated` extern `complete`" in str(exc.value)


@pytest.mark.parametrize("tier", TIERS)
def test_the_unvalidated_extern_twin_still_emits(tier):
    out = _emitter(tier).emit(compile_source(_EXTERN % "", "t.rvl"))
    assert out


@pytest.mark.parametrize("tier", TIERS)
def test_the_refusal_is_declaration_keyed_not_call_site_keyed(tier):
    """`session_commit.refuse_deferred_on_ownerless_tier` keys off the CALL SITE,
    because a `deferred` extern's whole lowering is the enqueue there. `validated`
    is the other shape: the tier emits the crossing whether or not this document
    calls it, so an uncalled declaration still reaches the output as an unchecked
    boundary and is still refused."""
    ir = compile_source(_EXTERN_UNCALLED % "validated ", "t.rvl")
    assert not any(comp.get("steps") for comp in ir.get("components") or [])
    module = _emitter(tier)
    with pytest.raises(module.EmitError, match="`validated` extern `complete`"):
        module.emit(ir)


@pytest.mark.parametrize("tier", TIERS)
def test_the_retry_budget_rides_the_same_declaration(tier):
    """Slice 2's `retry N` is legal only alongside `validated`, so it is refused
    by the same gate rather than needing a second one."""
    ir = compile_source(_EXTERN % "validated retry 3 ", "t.rvl")
    assert ir["externs"][0]["retry"] == 3
    module = _emitter(tier)
    with pytest.raises(module.EmitError, match="`validated` extern `complete`"):
        module.emit(ir)


# ---------------------------------------------------- the go placement entry

def test_the_go_placement_path_refuses_it_too():
    """go has two entry points. `emit_placement` renders a v3 document with
    top-level declarations through `_emit_v3_placement`, which never reaches
    `emit()`. Both run `_DOCUMENT_REFUSALS`, so the gate goes in that list rather
    than into one entry point that happens to delegate."""
    module = _emitter("go")
    with pytest.raises(module.EmitError, match="`validated` extern `complete`"):
        module.emit_placement(compile_source(_EXTERN_PLACED % "validated ", "t.rvl"))


def test_the_go_placement_path_still_places_the_unvalidated_twin():
    module = _emitter("go")
    assert module.emit_placement(compile_source(_EXTERN_PLACED % "", "t.rvl"))


# ------------------------------------------------ the tier that is NOT gated

def test_python_is_absent_from_the_gate_and_still_lowers_the_operation():
    """The whole reason the gate names five tiers and not six. The python tier
    owns `validate_response` / `validate_retry` / the grammar registry, and the
    service-operation carrier keeps lowering in full."""
    assert "python" not in UNVALIDATING_TIERS and "py" not in UNVALIDATING_TIERS
    ir = compile_source(_OPERATION % "validated ", "t.rvl")
    refuse_validated_on_unvalidating_tier(ir, "python")  # no raise
    refuse_validated_on_unvalidating_tier(ir, "py")  # no raise
    module = _emitter("python")
    code = module.emit(ir)
    assert "_revl_validate" in code
    assert "_revl_register_grammars(" in code


def test_the_typescript_temporal_target_is_not_gated():
    """`--target temporal` returns from `emit()` before the cordis refusals run,
    and it READS `validated` on an extern to pin the crossing to at-most-once.
    Pinned as an explicit scope boundary: a later edit that moved the gate above
    the target dispatch would delete that guarantee, and this test is what would
    notice."""
    module = _emitter("typescript")
    source = (
        'extern emission[model] validated idempotent(key: card) '
        'fn ask(card: Str) -> Str = @ts { return "ok"; }\n'
        'component Pay {\n  emit ask("visa")\n}\n'
    )
    rendered = module.emit(compile_source(source, "a.rvl"), target="temporal")
    assert "proxyActivities" in rendered
    with pytest.raises(module.EmitError):
        module.emit(compile_source(source, "a.rvl"))


# ------------------------------------------------------------- the scan itself

def test_validated_crossings_names_both_carriers_in_a_stable_order():
    ir = compile_source(_OPERATION % "validated ", "t.rvl")
    assert validated_crossings(ir) == [("Model.complete", "operation")]
    ir = compile_source(_EXTERN % "validated ", "t.rvl")
    assert validated_crossings(ir) == [("complete", "extern")]


def test_a_document_with_no_validated_crossing_is_untouched():
    """Why this gate moves no artefact: nothing checked in declares one."""
    for tier in TIERS:
        ir = compile_source(_OPERATION % "", "t.rvl")
        assert validated_crossings(ir) == []
        refuse_validated_on_unvalidating_tier(ir, tier)  # no raise


def test_the_refusal_carries_the_frontend_validated_tag():
    """`lower.py` raises its own `validated` refusals under G4/validated, and so
    does the python extern gate's sibling. A refusal that agreed on the verdict
    but not the tag would read as a different guarantee to every consumer that
    buckets by tag."""
    from revl.errors import RevlError

    ir = compile_source(_OPERATION % "validated ", "t.rvl")
    with pytest.raises(RevlError) as exc:
        refuse_validated_on_unvalidating_tier(ir, "rust")
    assert exc.value.code == VALIDATED_CODE == "G4"
    assert exc.value.category == VALIDATED_CATEGORY == "validated"


# ------------------------------------- agreement with the python extern gate

_SHARED_CLAUSES = (
    "a checked boundary in the source and an unchecked one in the output, "
    "which no byte oracle can catch",
    "So the tier refuses by name rather than answering with less than it was "
    "given (items 257 and 513",
    "Drop `validated` to accept an unchecked boundary, or ",
)


def test_the_five_tier_wording_agrees_with_the_python_extern_gate():
    """Two gates, two positions, one vocabulary.

    The python tier refuses ONE carrier because the seam it has cannot reach an
    extern (#1382); these five refuse BOTH because they have no seam at all. The
    diagnostics therefore say different things, but they are deliberately not two
    independent inventions: the three load-bearing clauses are shared verbatim.
    Without this test a later edit to either message could drift them apart and
    nothing would notice, which is the failure mode this whole issue is about one
    layer up."""
    python_emit = _emitter("python")
    with pytest.raises(python_emit.EmitError) as py_exc:
        python_emit.emit(compile_source(_EXTERN % "validated ", "t.rvl"))
    python_message = str(py_exc.value)

    rust = _emitter("rust")
    with pytest.raises(rust.EmitError) as tier_exc:
        rust.emit(compile_source(_EXTERN % "validated ", "t.rvl"))
    tier_message = str(tier_exc.value)

    for clause in _SHARED_CLAUSES:
        assert clause in python_message, clause
        assert clause in tier_message, clause


def test_the_five_tiers_share_one_wording_rather_than_inventing_five():
    """The reason the scan lives in `revl.validated_boundary` and not in five
    copies. Everything but the tier name is the same text."""
    ir = compile_source(_EXTERN % "validated ", "t.rvl")
    rendered = {}
    for tier in TIERS:
        module = _emitter(tier)
        with pytest.raises(module.EmitError) as exc:
            module.emit(ir)
        rendered[tier] = str(exc.value).replace(f"the {tier} tier", "the TIER tier")
    assert len(set(rendered.values())) == 1, rendered
