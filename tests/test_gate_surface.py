"""The embeddable-gate library surface — roadmap item 332 (py deliverable).

Exit tests for the `revl.gate` module (docs/design/332-embeddable-gate-api.md):

* LAYER 1, the verdict surface: `admit` returns the SAME accept/refuse verdict
  as the reference compiler on the same source, message verbatim (an accepted
  program AND a refused one); `compile_to` emits real reference target source;
  `gate_version()` returns the api/language/frontier pin.
* LAYER 2, the session facade: `Gate.load/admit/call/commit` runs a tool
  crossing with the same classification/approval behavior as `revl run`; a
  class-(c) crossing with NO approver REFUSES (fail closed); with an approver it
  fires once.
* ADDITIVITY: a standalone compile is unchanged; a second live `Gate` is refused.

The runtime proofs need a live cordis-py composition (install with
`sh backends/python/setup.sh`, run under its venv); the layer-1 verdict and
version tests are pure and always run.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from revl.gate import (
    Gate,
    GateError,
    GateRefused,
    Verdict,
    admit,
    admit_into,
    compile_to,
    gate_version,
)
from revl.holes import refuse_admission

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the layer-2 session facade is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)


@pytest.fixture(autouse=True)
def _no_leaked_gate():
    """Every test must leave the process-global single-gate slot clear, so an
    ordering-dependent second-Gate refusal never leaks between tests."""
    import revl.gate as gate_mod
    yield
    gate_mod._ACTIVE_GATE = None


# A program the reference compiler ACCEPTS, and one it REFUSES.
_ACCEPTED = (
    "service S { fn f(x: Int) -> Int }\n"
    "component C provides s: S {\n"
    "  provide s { fn f(x) = x }\n"
    "}\n"
)
# G1: a component reaches a service no component in the composition provides.
_REFUSED = (
    "service S { fn f(x: Int) -> Int }\n"
    "component C requires dep: Missing provides s: S {\n"
    "  provide s { fn f(x) = x }\n"
    "}\n"
)
# A DRAFT: it compiles (the checker gives a verdict on the parts written) but
# carries an open typed hole, so the admission gate (`refuse_admission`, T3)
# refuses to let it run. `compile_source` alone ACCEPTS it; every admission
# entrypoint must REFUSE it (docs/holes.md).
_HOLE_DRAFT = (
    "service Cache { fn get(key: Str) -> Str }\n"
    "component C provides c: Cache {\n"
    '  provide c { fn get(key) = hole "look up in the store" }\n'
    "}\n"
)
# A second post-check_and_lower admission refusal: two open holes in one draft,
# a different service, exercising the same gate with a different message body.
_HOLE_DRAFT_TWO = (
    "service Store {\n"
    "  fn get(key: Str) -> Str\n"
    "  fn put(key: Str, val: Str) -> Str\n"
    "}\n"
    "component D provides d: Store {\n"
    "  provide d {\n"
    '    fn get(key) = hole "the read path"\n'
    '    fn put(key, val) = hole "the write path"\n'
    "  }\n"
    "}\n"
)


# --------------------------------------------------------------------------- #
# Layer 1: the verdict matches the reference compiler, verbatim.
# --------------------------------------------------------------------------- #

def test_admit_matches_reference_on_an_accepted_program():
    # the reference gate: compile_source itself (compiler.py:243).
    ref_ok = True
    try:
        compile_source(_ACCEPTED)
    except RevlError:
        ref_ok = False

    verdict = admit(_ACCEPTED)
    assert ref_ok is True
    assert verdict.admitted is True
    assert verdict.code is None
    assert verdict.message is None


def test_admit_matches_reference_on_a_refused_program_verbatim():
    # the reference verdict for the SAME source, captured directly.
    with pytest.raises(RevlError) as exc:
        compile_source(_REFUSED)
    ref_message = str(exc.value)
    ref_code = getattr(exc.value, "code", None)

    verdict = admit(_REFUSED)
    assert verdict.admitted is False
    # the security clause: the gate refuses exactly what the reference refuses,
    # and the message is the reference diagnostic VERBATIM.
    assert verdict.message == ref_message
    assert verdict.code == ref_code


def _refusal(fn):
    """Run `fn` and return the (code, message) of the RevlError it raises, or
    None if it did not raise. Used to compare admission verdicts across
    entrypoints byte for byte."""
    try:
        fn()
    except RevlError as error:
        return (getattr(error, "code", None), str(error))
    return None


@pytest.mark.parametrize("draft", [_HOLE_DRAFT, _HOLE_DRAFT_TWO])
def test_admit_refuses_hole_draft_agreeing_with_every_admission_gate(draft):
    """The item-332 false-admit fix (adversarial review): `admit` must apply the
    admission gate it claims to be, not just `compile_source`. A draft with an
    open typed hole compiles but may never run; `refuse_admission`, `admit_into`
    (`compile_source(..., manifest=...)`), and `revl run` all REFUSE it, so
    `admit` must too, with the SAME verdict verbatim (the security clause).

    Differential: `admit`, `admit_into`, and `refuse_admission` produce the
    IDENTICAL (code, message), and `Gate.load` also refuses (via the session
    layer, which carries its own hole diagnostic)."""
    # the reference the draft compiles as: an accepted DRAFT, one open hole.
    document = compile_source(draft)  # does not raise — a draft is checkable.

    # 1) refuse_admission — the admission gate itself.
    gate_refusal = _refusal(lambda: refuse_admission(document))
    assert gate_refusal is not None, "refuse_admission must refuse a hole draft"
    code, message = gate_refusal
    assert code == "T3"

    # 2) admit_into — the runtime-admission entrypoint (manifest = the draft's
    #    own document, a running composition to admit into).
    into = admit_into(draft, document)
    assert into.admitted is False
    assert (into.code, into.message) == (code, message)

    # 3) admit — the layer-1 entrypoint under test. Before the fix it ADMITTED
    #    this draft (the false-admit); now it refuses with the SAME verdict.
    verdict = admit(draft)
    assert verdict.admitted is False, (
        "admit must never admit what the reference refuses to run")
    assert (verdict.code, verdict.message) == (code, message)


def test_admit_is_not_admitted_by_compile_source_alone():
    """The defect in one line: `compile_source` ACCEPTS the hole draft (it is a
    checkable draft), and `admit` — which used to be exactly that call — now
    REFUSES it. The two verdicts must diverge, or the admission gate is absent."""
    compile_source(_HOLE_DRAFT)  # a draft compiles: no raise.
    assert admit(_HOLE_DRAFT).admitted is False


def test_compile_to_emits_reference_target_source():
    emit = compile_to(_ACCEPTED, "py")
    assert emit.verdict.admitted is True
    assert emit.output is not None
    assert "Generated by the revl cordis-py backend" in emit.output


def test_compile_to_refusal_carries_no_output():
    emit = compile_to(_REFUSED, "py")
    assert emit.verdict.admitted is False
    assert emit.output is None


def test_compile_to_unknown_tier_fails_closed():
    emit = compile_to(_ACCEPTED, "cobol")
    assert emit.verdict.admitted is False
    assert emit.verdict.code == "UNKNOWN_TIER"
    assert emit.output is None


def test_verdict_from_native_splits_at_first_pipe():
    # the tier-agnostic parser the crate reuses: empty admits, TAG|message
    # refuses, and a message carrying `|` survives intact.
    assert Verdict.from_native("").admitted is True
    v = Verdict.from_native("G2|provider table collides on a|b")
    assert v.admitted is False
    assert v.code == "G2"
    assert v.message == "provider table collides on a|b"


def test_gate_version_returns_the_frontier_pin():
    info = gate_version()
    assert set(info) == {"api", "language", "frontier"}
    assert info["api"] == "1.0.0"
    assert info["frontier"] == f"reference-full:{info['language']}"


# --------------------------------------------------------------------------- #
# Layer 2: the session facade, against a live composition.
# --------------------------------------------------------------------------- #

# The running composition (reused from the item-330 admit-crossing fixture): a
# granted tool surface (`Ops`) plus the in-language admit crossing.
_BASE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_TURN_OK = (
    "service Turn { emission fn run(p: Str, sink: Str) }\n"
    "component TurnComp requires ops: Ops provides turn: Turn {\n"
    "  provide turn {\n"
    '    fn run(p, sink) { emit ops.stash(p); emit ops.shout(sink, "from-turn") }\n'
    "  }\n"
    "}\n"
)


def _base_sources():
    from revl._paths import stdlib_root
    admit_path = str(stdlib_root() / "admit.rvl")
    base_abs = os.path.abspath("gate_base.rvl")
    # admit.rvl rides as a real on-disk co-root (value None), the base as an
    # in-memory draft — exactly the shape an agent loop uses.
    return {base_abs: _BASE, admit_path: None}


def _mutated(path):
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path):
    return os.path.exists(path) and not os.path.exists(path + ".bak")


def _lines(sink):
    return Path(sink).read_text().splitlines() if os.path.exists(sink) else []


@needs_cordis
def test_gate_load_admit_call_commit_runs_a_tool_crossing(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("payload", encoding="utf-8")
    sink = str(tmp_path / "sink.log")

    gate = Gate()
    try:
        gate.load(_base_sources())

        # the per-turn admit verdict equals the reference compiler's: the turn
        # composes only granted providers, so it is admitted, and hands back a
        # Handle, never the emitted artifact.
        result = gate.admit(_TURN_OK, granted=["Ops"])
        assert result.admitted, result.message
        assert "turn" in result.keys
        assert result.handle is not None

        # run the turn through the handle: the witnessed fs mutation and the
        # emission register into the enclosing 245 frame.
        result.handle.call("turn", "run", [str(artifact), sink])
        assert _mutated(str(artifact)), "witnessed mutation did not apply"
        assert _lines(sink) == ["announce:from-turn"]

        # commit: the witnessed mutation persists, residue-free — decision for
        # decision the same as the same fixture through `revl run`/`revl mcp`.
        committed = gate.commit()
        assert committed["committed"]
        assert committed["noResidue"], committed.get("checks")
        assert _mutated(str(artifact))
    finally:
        gate.close()


@needs_cordis
def test_gate_admit_refusal_leaves_the_running_composition_untouched(tmp_path):
    gate = Gate()
    try:
        gate.load(_base_sources())
        # grant NOTHING: the turn's reach of `ops` is outside the allowlist.
        result = gate.admit(_TURN_OK, granted=[])
        assert not result.admitted
        assert result.code == "R2"
        assert result.handle is None
        # nothing was wired: the running composition is untouched.
        assert "turn" not in gate._session._driver._namespace()
    finally:
        gate.close()


# The approval fixture: `shout` is a plain emission (class (c)); `stash` is a
# witnessed rename (class (a)).
_APPROVAL_BASE = _BASE


@needs_cordis
def test_class_c_crossing_refuses_fail_closed_without_approver(tmp_path):
    sink = str(tmp_path / "sink.log")
    gate = Gate(approval_policy="auto", approver=None)
    try:
        gate.load(_APPROVAL_BASE)
        # a class-(c) crossing with NO approver fails closed: nothing fires.
        with pytest.raises(GateRefused) as exc:
            gate.call("ops", "shout", [sink, "hi"])
        assert exc.value.ticket is not None
        assert _lines(sink) == [], "the host body must NOT have run"
    finally:
        gate.close()


@needs_cordis
def test_class_a_crossing_auto_approves_under_policy(tmp_path):
    # additivity of the policy: a witnessed (class-a) crossing proceeds silently,
    # same as `revl mcp serve --approval-policy auto`.
    artifact = tmp_path / "a.txt"
    artifact.write_text("x", encoding="utf-8")
    gate = Gate(approval_policy="auto")
    try:
        gate.load(_APPROVAL_BASE)
        out = gate.call("ops", "stash", [str(artifact)])
        assert "result" in out
        assert _mutated(str(artifact))
    finally:
        gate.close()


@needs_cordis
def test_class_c_crossing_fires_once_with_an_approving_approver(tmp_path):
    sink = str(tmp_path / "sink.log")
    seen = []

    def approver(ticket):
        seen.append(ticket)
        return True

    gate = Gate(approval_policy="auto", approver=approver)
    try:
        gate.load(_APPROVAL_BASE)
        out = gate.call("ops", "shout", [sink, "hi"])
        assert "result" in out
        assert _lines(sink) == ["announce:hi"], "the approved crossing fired once"
        assert len(seen) == 1, "the approver was consulted exactly once"
    finally:
        gate.close()


@needs_cordis
def test_class_c_crossing_denied_by_approver_fails_closed(tmp_path):
    sink = str(tmp_path / "sink.log")
    gate = Gate(approval_policy="auto", approver=lambda _t: False)
    try:
        gate.load(_APPROVAL_BASE)
        with pytest.raises(GateRefused):
            gate.call("ops", "shout", [sink, "hi"])
        assert _lines(sink) == []
    finally:
        gate.close()


# --------------------------------------------------------------------------- #
# Additivity.
# --------------------------------------------------------------------------- #

def test_standalone_compile_is_unchanged_by_the_facade():
    # a program that never imports the facade compiles exactly as before; here we
    # assert the reference path the facade wraps is untouched.
    before = compile_source(_ACCEPTED)
    after = compile_source(_ACCEPTED)
    assert before == after
    # and the facade's layer-1 verdict does not perturb it.
    admit(_ACCEPTED)
    assert compile_source(_ACCEPTED) == before


def test_second_live_gate_is_refused():
    first = Gate()
    try:
        with pytest.raises(GateError) as exc:
            Gate()
        assert "single-gate-per-process" in str(exc.value)
    finally:
        first.close()
    # after close, a fresh Gate constructs cleanly.
    second = Gate()
    second.close()


def test_dropped_gate_without_close_does_not_block_construction():
    """Finding 3 (robustness): a Gate constructed and dropped WITHOUT `close()`
    must not soft-brick the process. The single-gate slot is a weak reference,
    so a collected gate frees the slot on its own — a subsequent `Gate()`
    constructs cleanly instead of refusing forever."""
    g = Gate()
    del g  # no close(): the only strong reference is gone, so it is collected.

    # the slot is now free (the weak reference is dead); a fresh gate constructs.
    survivor = Gate()
    try:
        assert survivor.loaded is False
    finally:
        survivor.close()


def test_dropped_gate_frees_the_slot_but_a_live_gate_still_blocks():
    """The liveness fix must NOT weaken the single-gate invariant: a gate that
    is still LIVE (referenced) blocks a second construction exactly as before;
    only a dropped-and-collected gate frees the slot."""
    live = Gate()
    try:
        with pytest.raises(GateError) as exc:
            Gate()
        assert "single-gate-per-process" in str(exc.value)
    finally:
        live.close()


@needs_cordis
def test_gate_load_refuses_the_hole_draft():
    """`Gate.load` (the layer-2 admission entrypoint) refuses a draft with an
    open typed hole — the same draft `admit`/`admit_into`/`refuse_admission`
    refuse. Its message comes from the session layer (docs/holes.md), but the
    refusal is the same admission decision, and it names the open hole."""
    gate = Gate()
    try:
        with pytest.raises(GateError) as exc:
            gate.load(_HOLE_DRAFT)
        assert "hole" in str(exc.value).lower()
    finally:
        gate.close()


# --------------------------------------------------------------------------- #
# item 479: unknown-field refusal at the IR boundary (refuse by name, do not
# ignore). A staged IR document re-entering the frontend as the runtime-
# admission `manifest` carries a top-level field this schema revision never
# emits — a forged document, or a gate-crate/frontend skew. The frontend must
# REFUSE it and NAME the field, never silently ignore it. These are pure
# (layer-1) negatives: no live composition, always run.
# --------------------------------------------------------------------------- #

# A second composition (a different service) admitted INTO the running one, so
# the candidate itself is clean and the only thing under test is the manifest.
_CANDIDATE = (
    "service T { fn g(x: Int) -> Int }\n"
    "component D provides t: T {\n"
    "  provide t { fn g(x) = x }\n"
    "}\n"
)


def _running_ir() -> dict:
    """A well-formed IR document for `_ACCEPTED`, the staged IR under test."""
    return compile_source(_ACCEPTED)


def test_clean_staged_ir_still_admits():
    """A control: an unmodified compiled IR document round-trips as the manifest
    with no false positive — the refusal fires only on an unknown field."""
    verdict = admit_into(_CANDIDATE, _running_ir())
    assert verdict.admitted, verdict.message


def test_unknown_top_level_field_refuses_by_name_via_admit_into():
    """The core negative: inject a bogus top-level field into the staged IR and
    the runtime-admission entrypoint refuses, NAMING the field."""
    forged = _running_ir()
    forged["schemaRevision"] = 999  # a member this frontend never emits
    verdict = admit_into(_CANDIDATE, forged)
    assert not verdict.admitted
    assert "`schemaRevision`" in verdict.message
    assert "unknown top-level field" in verdict.message


def test_unknown_top_level_field_refuses_by_name_via_compile_source():
    """The same refusal at the raw compiler boundary (`compile_source(...,
    manifest=...)`), naming the field, so the negative does not depend on the
    gate facade."""
    forged = _running_ir()
    forged["mysteryMeat"] = {"anything": True}
    with pytest.raises(RevlError) as exc:
        compile_source(_CANDIDATE, manifest=forged)
    message = str(exc.value)
    assert "`mysteryMeat`" in message
    assert "refuses" in message


def test_unknown_field_refusal_names_every_unknown_field():
    """Two injected fields are BOTH named, sorted, so an operator sees the whole
    unaccounted-for surface, not just the first one."""
    forged = _running_ir()
    forged["zebra"] = 1
    forged["alpha"] = 2
    with pytest.raises(RevlError) as exc:
        compile_source(_CANDIDATE, manifest=forged)
    message = str(exc.value)
    assert "`alpha`" in message and "`zebra`" in message
    # sorted: alpha is named before zebra
    assert message.index("`alpha`") < message.index("`zebra`")


def test_refusal_does_not_leak_into_the_ignored_path():
    """The refusal fires BEFORE any manifest member is read, so an unknown field
    can never ride along an otherwise-successful admission (the silent-ignore
    defect this closes). The candidate here would admit against a clean manifest
    (see the control above), so a non-refusal would be exactly the false-admit."""
    forged = _running_ir()
    forged["driftedGateField"] = "smuggled"
    verdict = admit_into(_CANDIDATE, forged)
    assert not verdict.admitted
    assert "driftedGateField" in verdict.message


# item 479, companion half: a KNOWN field (`ir_version`) carrying an
# unrecognized schema REVISION must also refuse by name. A field-name check
# alone would wave through a document forged at (or drifted to) a revision this
# frontend cannot decode — the same undetected-skew defect one level down.


def test_unknown_schema_revision_refuses_by_name_via_admit_into():
    """A staged IR at a schema revision this frontend does not know refuses at
    the runtime-admission entrypoint, NAMING the offending revision."""
    forged = _running_ir()
    forged["ir_version"] = 999  # a revision no frontend emitted
    verdict = admit_into(_CANDIDATE, forged)
    assert not verdict.admitted
    assert "unknown schema revision" in verdict.message
    assert "999" in verdict.message


def test_unknown_schema_revision_refuses_by_name_via_compile_source():
    """The same refusal at the raw compiler boundary, naming the revision, so
    the negative does not depend on the gate facade."""
    forged = _running_ir()
    forged["ir_version"] = "v42"
    with pytest.raises(RevlError) as exc:
        compile_source(_CANDIDATE, manifest=forged)
    message = str(exc.value)
    assert "unknown schema revision" in message
    assert "v42" in message


def test_known_schema_revision_still_admits():
    """A control: the frontend's own stamped `ir_version` is a known revision,
    so the schema-revision refusal never fires on a genuine document."""
    verdict = admit_into(_CANDIDATE, _running_ir())
    assert verdict.admitted, verdict.message


def test_absent_schema_revision_is_not_refused():
    """A bare manifest may omit `ir_version` (an unversioned handoff); absence
    is not an unknown revision, so it must not trip the refusal."""
    bare = _running_ir()
    del bare["ir_version"]
    verdict = admit_into(_CANDIDATE, bare)
    assert verdict.admitted, verdict.message


# The public-surface compat gate (an EXACT pin of `revl.gate.__all__`, plus
# `gate_version()` compat semantics) is item 338's public compat gate, in
# tests/test_gate_compat.py. It supersedes the subset check that used to live
# here (332's stage-1 import-surface test): a subset check can only catch a
# promised name silently DISAPPEARING, never one silently APPEARING, and 338
# owns the exact contract a dependency actually pins against.
