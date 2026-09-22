"""Items 257/513: the five tiers with no validation seam refuse a `validated`
emission by name instead of dropping it (issue #1373).

The defect this pins is not a missing feature, it is a SILENT one. Compiled at
1c8e8e69e and again at b724dd7e4 with the same numbers, one program twice over,
differing only in the `validated` modifier:

    python      1961 vs 943 chars, grammar and schema present
    typescript  byte-identical (916 vs 916)
    rust        byte-identical (5855 vs 5855)
    wasm        byte-identical (1055 vs 1055)
    go          byte-identical (5942 vs 5942)
    java        byte-identical (5238 vs 5238)

The other carrier of the same three IR keys, a `validated` emission extern with a
native body for the tier, is dropped byte-identically too (ts 421, rust 551, wasm
697, go 351, java 4174, each equal to its twin), and so is python's (266), which
by design never validates an extern's response.

The author wrote a checked boundary; five tiers emitted an unchecked one and
said nothing. A wrong lowering is caught by a byte oracle and a refusal is caught
by its message; nothing catches a tier that emits less than it was given and
stays quiet, so those five now refuse by name
(`revl.validated_boundary.refuse_validated_on_unvalidating_tier`).

Every refusal assertion here is paired with a CONTROL that still emits: the same
program without the modifier. A gate that refuses everything is not a gate, and
this file is the demonstration that the refusals fire on exactly the documents
that carry the modifier.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from _backend_import import backend_emitter  # noqa: E402

from revl import compile_source  # noqa: E402
from revl.validated_boundary import (  # noqa: E402
    UNVALIDATING_TIERS,
    refuse_validated_on_unvalidating_tier,
    validated_crossings,
)

#: The five tiers, by backend directory name. Same set as `UNVALIDATING_TIERS`,
#: asserted below so a tier added to one and not the other is a failure here
#: rather than a tier that quietly stops being checked.
TIERS = ("typescript", "rust", "wasm", "go", "java")

#: The per-tier extern body for the extern carrier, so the extern is portable to
#: the tier under test and the emit gets far enough to drop (or refuse) it. A
#: `validated` extern with no body for the tier refuses on portability first,
#: which is the reason the issue could not measure java from its own probe.
_EXTERN_BODY = {
    "python": '@py { return {"text": "x", "score": 1} }',
    "typescript": '@ts { return {text: "x", score: 1} }',
    "rust": '@rs { Answer { text: "x".to_string(), score: 1 } }',
    "wasm": "@wasm { return 0 }",
    "go": "@go { return Answer{Text: \"x\", Score: 1} }",
    "java": "@java { return null; }",
}


def _service_program(validated: bool) -> str:
    """A component whose provision is one crossing to a model service operation.

    Deliberately minimal: no `config` block (the wasm tier has no instantiation
    config channel), no arrow and no field access in the body (the stc-go
    component world lowers neither), and no parameter named `ctx` (ts and java
    both refuse that as a scaffolding collision). Each of those refuses BEFORE
    the validated gate would speak, and a probe that hits one measures the
    unrelated refusal instead of the drop."""
    modifier = "validated " if validated else ""
    return textwrap.dedent(f"""
        type Answer = {{ text: Str, score: Int }}

        service Model {{ emission {modifier}fn complete(prompt: Str) -> Answer }}
        service Loop {{ emission fn run(prompt: Str) -> Answer }}

        component Agent requires model: Model provides agent: Loop {{
          provide agent {{
            fn run(prompt) {{
              return emit model.complete(prompt)
            }}
          }}
        }}
    """)


def _extern_program(validated: bool, tier: str) -> str:
    """The other carrier of the same three IR keys: a `validated` emission
    extern. `lower.py` binds `validated` + `response_schema` +
    `response_grammar` on an extern descriptor exactly as it does on a service
    operation, and all five tiers dropped it there too."""
    modifier = "validated " if validated else ""
    return (
        "type Answer = { text: Str, score: Int }\n"
        f"extern emission {modifier}fn complete(prompt: Str) -> Answer"
        f" = {_EXTERN_BODY[tier]}\n"
        "fn ask(p: Str) -> Answer { return complete(p) }\n"
    )


def _emit(tier: str, source: str):
    """Emit `source` on `tier`, normalising the wasm tier's multi-file return to
    one string so the two twins are comparable as bytes."""
    module = backend_emitter(tier)
    out = module.emit(compile_source(source, "probe.rvl"))
    if isinstance(out, dict):
        return "\n".join(f"{k}\n{v}" for k, v in sorted(out.items()))
    return out


def _emit_error(tier: str):
    return backend_emitter(tier).EmitError


# ----------------------------------------------- the IR carries what it claims


def test_the_modifier_reaches_the_ir_on_both_carriers():
    """The premise of the whole file: `validated` is not lost before emit. Both
    carriers bind the same three keys, so a tier that emits identical bytes had
    the constraint in hand and dropped it."""
    ir = compile_source(_service_program(True), "probe.rvl")
    spec = ir["services"]["Model"]["methods"]["complete"]
    assert spec["validated"] is True
    assert spec["response_schema"]["type"] == "object"
    assert spec["response_grammar"]["format"] == "gbnf"
    assert validated_crossings(ir) == [("Model.complete", "operation")]

    ext_ir = compile_source(_extern_program(True, "python"), "probe.rvl")
    ext = next(e for e in ext_ir["externs"] if e["name"] == "complete")
    assert ext["validated"] is True
    assert "response_schema" in ext and "response_grammar" in ext
    assert validated_crossings(ext_ir) == [("complete", "extern")]

    plain = compile_source(_service_program(False), "probe.rvl")
    assert validated_crossings(plain) == []


def test_tier_list_matches_the_shared_gate():
    assert sorted(TIERS) == sorted(UNVALIDATING_TIERS)


def test_the_gate_is_a_no_op_on_the_python_tier():
    """py owns `validate_response` / `validate_retry` / the grammar registry, so
    the shared gate must not refuse there: the refusal is a tier gate, not a
    language rule."""
    ir = compile_source(_service_program(True), "probe.rvl")
    refuse_validated_on_unvalidating_tier(ir, "python")
    refuse_validated_on_unvalidating_tier(ir, "py")


# --------------------------------------------------------- the refusal, by name


@pytest.mark.parametrize("tier", TIERS)
def test_validated_service_operation_is_refused_by_name(tier):
    with pytest.raises(_emit_error(tier)) as exc:
        _emit(tier, _service_program(True))
    message = str(exc.value)
    assert "`validated` operation `Model.complete`" in message
    assert f"the {tier} tier has none of it" in message
    # A refusal has to say what to do next, or it is just a failure.
    assert "target the python tier" in message


@pytest.mark.parametrize("tier", TIERS)
def test_validated_extern_is_refused_by_name(tier):
    with pytest.raises(_emit_error(tier)) as exc:
        _emit(tier, _extern_program(True, tier))
    assert "`validated` extern `complete`" in str(exc.value)


# ------------------------------------------------------------------ the control


@pytest.mark.parametrize("tier", TIERS)
def test_the_unvalidated_twin_still_emits(tier):
    """The honest control. The gate keys on the modifier and nothing else, so
    the same program without it must still emit: non-empty, and carrying the
    crossing it was written around."""
    out = _emit(tier, _service_program(False))
    assert out.strip()
    # Case-insensitive: the go and java tiers export `Complete`.
    assert "complete" in out.lower()


@pytest.mark.parametrize("tier", TIERS)
def test_the_unvalidated_extern_twin_still_emits(tier):
    out = _emit(tier, _extern_program(False, tier))
    assert out.strip()
    assert "complete" in out.lower()


def test_python_still_lowers_it_and_differs_from_its_twin():
    """The sixth tier is the reason a refusal is the right answer and not a
    language-level removal: py lowers the modifier, and its two twins differ."""
    validated = _emit("python", _service_program(True))
    plain = _emit("python", _service_program(False))
    assert validated != plain
    assert len(validated) > len(plain)
    assert "_revl_register_grammars" in validated
    assert "_revl_validate" in validated
    assert "_revl_register_grammars" not in plain
    assert "_revl_validate" not in plain


# ------------------------------------------------- the go tier's placement path


def test_the_go_placement_path_refuses_it_too():
    """`emit_placement` renders a v3 document with top-level declarations through
    `_emit_v3_placement`, which never reaches `emit()`, so the gate is run at the
    top of `_emit_placement` as well. A placement module dropped the crossing
    exactly as the plain module did."""
    go = backend_emitter("go")
    ir = compile_source(_service_program(True), "probe.rvl")
    with pytest.raises(go.EmitError) as exc:
        go.emit_placement(ir, package="emitted")
    assert "`validated` operation `Model.complete`" in str(exc.value)


def test_the_go_placement_control_still_emits():
    go = backend_emitter("go")
    out = go.emit_placement(compile_source(_service_program(False), "probe.rvl"),
                            package="emitted")
    assert out.strip()
    assert "\f" in out, "placement emits gen + bridge either side of the sentinel"


# ----------------------------------------------- the ts tier's second rendering
#
# `--target temporal` is NOT gated, deliberately, and this is the measurement
# that says why. It is a second RENDERING of the typescript tier, `emit()`
# dispatches to it before the cordis refusals run, and on a service-method
# crossing it drops `validated` exactly as the cordis rendering did: the
# `booktrip` fixture with one emission marked `validated` emitted 7218 characters
# either way at 1c8e8e69e.
#
# It is still left alone, because unlike the five cordis renderings it is not
# uniformly silent about the modifier. It READS it: a `validated` crossing is
# pinned to the at-most-once activity group and its `retry N` budget is
# deliberately not lowered to a Temporal RetryPolicy, because a completion is a
# read with a cost rather than an idempotent write.
# `backends/typescript/test_temporal_target.py::
# test_validated_pins_to_at_most_once_even_when_keyed` pins that, and the two
# renderings of the same keyed extern differ (5807 vs 6507 chars, measured), so a
# blanket refusal here would delete a tested guarantee along with the drop.
#
# What is left is narrower than the issue's finding and needs its own decision:
# the temporal rendering derives a retry class from `validated` and still emits
# no schema check. Recorded here rather than closed quietly.


#: The keyed `validated` extern the temporal rendering treats specially, as a
#: list of lines so the embedded probe below needs no escaping of its own.
_TEMPORAL_PROBE_LINES = (
    "extern emission[model] MODIFIERidempotent(key: card)"
    " fn ask(card: Str) -> Str = @ts { return \"ok\" }",
    "component Pay {",
    "  emit ask(\"visa\")",
    "}",
)


def test_the_temporal_rendering_reads_the_modifier_rather_than_ignoring_it():
    """The reason the temporal rendering is scoped out, asserted rather than
    asserted-about. Run in its own interpreter: `emit_temporal.py` does
    `from emit import ...` and so needs the canonical `emit` module name, which
    a combined pytest process must not bind (tests/_backend_import.py says
    why)."""
    script = textwrap.dedent("""
        import importlib.util, json, sys
        from pathlib import Path
        root, lines = Path(sys.argv[1]), json.loads(sys.argv[2])
        sys.path.insert(0, str(root / "src"))
        sys.path.insert(0, str(root / "backends" / "typescript"))
        from revl import compile_source
        spec = importlib.util.spec_from_file_location(
            "emit", root / "backends" / "typescript" / "emit.py")
        emit = importlib.util.module_from_spec(spec)
        sys.modules["emit"] = emit
        spec.loader.exec_module(emit)
        import emit_temporal

        sizes = []
        for modifier in ("validated retry 2 ", ""):
            src = chr(10).join(x.replace("MODIFIER", modifier) for x in lines)
            out = emit_temporal.emit_temporal(compile_source(src, "ask.revl"))
            sizes.append(len(out))
        print("SIZES", sizes[0], sizes[1])
    """)
    got = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), json.dumps(_TEMPORAL_PROBE_LINES)],
        capture_output=True, text=True, timeout=180)
    assert got.returncode == 0, got.stderr
    validated_len, plain_len = (int(x) for x in got.stdout.split()[1:3])
    assert validated_len != plain_len, (
        "the temporal rendering is expected to READ `validated` on a keyed "
        "extern; if these are now equal it has become silent and belongs "
        "behind the gate with the other five")


# ------------------------------------------------------- no golden can have moved


def test_no_checked_in_artefact_carries_either_key():
    """The gate's blast radius, asserted rather than asserted-about: no backend
    golden carries `response_schema` or `response_grammar`, so adding the
    refusal cannot have moved one. If this ever fails, a golden acquired a
    validated crossing and the refusal above is now load-bearing for it."""
    goldens = sorted(p for p in ROOT.glob("backends/*/golden/**/*") if p.is_file())
    assert len(goldens) >= 28
    carriers = [p for p in goldens
                if "response_schema" in p.read_text(errors="replace")
                or "response_grammar" in p.read_text(errors="replace")]
    assert carriers == []

    ir_docs = sorted(p for p in ROOT.rglob("*.ir.json") if ".git" not in p.parts)
    assert len(ir_docs) >= 54
    ir_carriers = [p for p in ir_docs
                   if "response_schema" in p.read_text(errors="replace")
                   or "response_grammar" in p.read_text(errors="replace")]
    assert ir_carriers == []


def test_the_module_is_importable_without_the_package_on_the_path():
    """Each backend's wrapper falls back to putting `src/` on `sys.path` for a
    standalone `python3 emit.py` run. Pin that the fallback target exists."""
    path = ROOT / "src" / "revl" / "validated_boundary.py"
    spec = importlib.util.spec_from_file_location("_vb_probe", path)
    assert spec is not None and spec.loader is not None
