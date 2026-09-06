"""The decision-boundary gate (roadmap item 417, issue #108, exit step 2).

Item 417's finding: the `revl-gate` crate decides the composition/guarantee
layer (`G1`..`G4`, `A1`, `PRELUDE`, and parse failures as `BAD`) and runs NO
type layer at all, so it can only REFUSE and issues no admissions. That is the
honest, load-bearing property of the whole 332/335/336/337/338 arc. The exit
this file closes: 335 (the wasm edge gate), 336 slice 2 (the native LSP's
native `admit`), 337 (a non-py mesh receiver's embedded gate) and 338 (`cargo
add revl-gate`) MUST NOT lean on the crate as a full admission gate; they are
GATED HERE on one explicit statement of what the crate does and does not decide.

Two halves, both pure-Python (no rust toolchain, so this runs everywhere):

1. THE STATEMENT, stated once and shared. `COVERED_LAYER` in
   `tools/build_gate_crate.py` is the single sentence. This file asserts each of
   the four dependent surfaces is pinned to it, so none can silently start
   reading a `no_objection` as an admission. The crate's own three-way identity
   (lib.rs / README / GENERATED.json) is held by `test_gate_crate_drift.py`; the
   wasm three-way identity by `test_gate_wasm_drift.py`. What is NEW here is
   gating the *consumers* (336/337/338) on the same words, in one place.

2. THE EXECUTABLE MEANING. "Runs no type layer" is not prose here: the five
   type-layer probes item 417 measured are run through the REFERENCE and shown
   to be real refusals (so a full gate genuinely owes them), while the crate's
   published surface (`issues_admissions: false`, a `Verdict` enum with no
   `Admitted` arm) proves structurally that the crate can never admit them.

Distinct from item 429's job. 429 owns `tests/test_selfhost_lower.py`'s oracle
and corpus and the family that makes the self-host's MISSING type layer loud in
the differential. This file does the opposite and complementary thing: it pins
the crate's decision boundary as a GUARANTEE that currently holds, so the four
dependents cannot over-read it. It touches neither the oracle nor its generator.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from revl.gate import admit

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "crates" / "revl-gate"
WASM = ROOT / "crates" / "revl-gate-wasm"
LSP = ROOT / "crates" / "revl-lsp"
CONSUMER = ROOT / "examples" / "ecosystem-consumer-rs"
CONTRACT = ROOT / "docs" / "gate-dependency-contract.md"


def _generator():
    """Load `tools/build_gate_crate.py` by path, the way the drift gate does, so
    the canonical `COVERED_LAYER` constant comes from the file under test."""
    path = ROOT / "tools" / "build_gate_crate.py"
    spec = importlib.util.spec_from_file_location("revl_build_gate_crate", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revl_build_gate_crate"] = module
    spec.loader.exec_module(module)
    return module


GEN = _generator()

# The five programs item 417 measured: the reference REFUSES every one, the
# self-host gate objected to none, because each needs a type layer the crate
# does not run. Kept verbatim so the boundary this file gates is the one 417
# named, not a paraphrase of it.
TYPE_LAYER_PROBES = (
    'fn f() -> Int { return "s" }',
    "fn f() -> Int { return undefined_name }",
    "fn f() -> { }",
    "fn f() -> Int { }",
    "component C provides s: S { }",
)


def _crate_meta() -> dict:
    return json.loads((CRATE / "GENERATED.json").read_text(encoding="utf-8"))


# ------------------------------------------------------- the statement itself


def test_the_crate_states_what_it_decides_and_does_not_decide():
    """The anchor. One sentence, and it names both halves: the layer decided and
    the layer NOT decided. The four dependents below are gated on these words."""
    meta = _crate_meta()
    assert meta["covered_layer"] == GEN.COVERED_LAYER
    assert meta["issues_admissions"] is False
    assert meta["verdict_arms"] == ["refused", "no_objection", "outside_frontier"]
    assert "admitted" not in meta["verdict_arms"]
    # Both halves must be spelled out, or a reader cannot know where the gap is.
    assert "composition" in GEN.COVERED_LAYER and "guarantee" in GEN.COVERED_LAYER
    assert "NOT the reference type layer" in GEN.COVERED_LAYER


def test_the_verdict_enum_has_no_admitting_arm():
    """The statement is backed by the type, not only by prose: there is no
    `Verdict::Admitted`, so no source — type-layer or otherwise — has a path to
    an admission out of this crate."""
    lib_rs = (CRATE / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "pub enum Verdict {" in lib_rs
    after = lib_rs.split("pub enum Verdict {", 1)[1]
    body = after[: after.index("\n}")]  # the enum's own closing brace, at line start
    assert "Refused" in body and "NoObjection" in body and "OutsideFrontier" in body
    assert "Admitted" not in body, (
        "a `Verdict::Admitted` arm would contradict `issues_admissions: false` "
        "and let the crate decide the type layer it does not run"
    )


# ---------------------------------------------- the four dependents, gated


def test_335_wasm_edge_gate_is_gated_on_the_same_statement():
    """335: the wasm component is the crate repackaged, so its statement must be
    the crate's statement, byte for byte, with the same absent admitting arm."""
    wasm = json.loads((WASM / "GENERATED.json").read_text(encoding="utf-8"))
    assert wasm["covered_layer"] == GEN.COVERED_LAYER
    assert wasm["issues_admissions"] is False


def test_336_native_lsp_is_gated_on_the_same_statement():
    """336 slice 2: the native LSP may add squiggles but must never short-circuit
    the reference on a clean or single native result, precisely because the gate
    runs no type layer. native.rs must carry that reasoning, not just import."""
    native = (LSP / "src" / "native.rs").read_text(encoding="utf-8")
    assert "issues no admissions" in native
    assert "does NOT run the reference type layer" in native


def test_337_polyglot_mesh_receiver_is_gated_on_the_same_statement():
    """337: a non-py receiver re-admits at the seam with its embedded crate gate.
    The contract must state that gate is refuse-only, so the seam relies on a
    refusal and never on a crate `no_objection` read as acceptance."""
    contract = CONTRACT.read_text(encoding="utf-8")
    assert "The rust gate issues no admissions at all." in contract
    assert "**not** the reference type" in contract
    assert "a gate with no admission arm cannot commit it" in contract


def test_338_rust_cargo_consumer_treats_the_gate_as_refuse_only():
    """338: the out-of-tree `cargo add revl-gate` consumer must act on a refusal
    (REJECT) and ESCALATE everything else to the reference — never admit
    locally. The example is the executable statement of the contract."""
    main_rs = (CONSUMER / "src" / "main.rs").read_text(encoding="utf-8")
    assert "REJECT" in main_rs and "ESCALATE" in main_rs
    assert "never an admission" in main_rs
    assert "this crate issues no admissions" in main_rs


# --------------------------------------------- the executable meaning of it


def test_the_type_layer_boundary_is_real_the_reference_refuses_the_probes():
    """"Runs no type layer" is a claim about a real gap: the reference REFUSES
    every one of item 417's five probes. If any of these ever started being
    admitted by the reference, the boundary this file gates would be fiction."""
    for src in TYPE_LAYER_PROBES:
        verdict = admit(src)
        assert verdict.admitted is False, (
            f"the reference must refuse {src!r} for the type-layer boundary to "
            f"mean anything; got admitted={verdict.admitted}"
        )


def test_the_crate_cannot_admit_the_type_layer_probes():
    """The other side of the same boundary, held structurally so it needs no
    rust toolchain: with `issues_admissions: false` and no `Admitted` arm, the
    crate has no path to admit ANY of the type-layer probes. It answers
    `no_objection`/`outside_frontier` (never admission), which a consumer gated
    per the tests above must escalate rather than accept."""
    meta = _crate_meta()
    assert meta["issues_admissions"] is False
    assert not any(arm.startswith("admit") for arm in meta["verdict_arms"]), (
        "no admitting arm may exist, or the type-layer probes could be admitted"
    )
