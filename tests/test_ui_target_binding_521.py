"""The computer-use target record (roadmap item 521, issue #1195, Slice 4).

Slice 1 closed the family's namespace and slice 2 derived its taint classes.
Between them a declared UI token is one of five spellings and an observed
value is `Untrusted` by derivation. Neither says anything about the target's
SHAPE, and the shape is what a later step reads.

WHAT WAS WRITABLE BEFORE THIS SLICE, MEASURED ON `4cfc8f32` (slice 2's head).
The family's own canonical program declared `ui.find` returning `Str` and
`ui.click` taking `Str`, and compiled. So an actuation named its target BY
NAME, and a name is re-resolved at every use:

  * nothing bound the actuation to the observation it came from. The evidence
    that justified "this is the Approve button" was not carried anywhere, so
    no later step could check that the control acted on was the control that
    had been looked at;
  * nothing expired. A target resolved before a dialog opened was still
    spellable afterwards, and the spelling still resolved, to a different
    control. That is design 532 §4.1's rung-1 failure direction reached from
    rung 0, by waiting;
  * nothing survived a phase boundary. Item 522's transaction (PR #1287)
    re-resolves a target by name at every phase, and says so in its own "what
    is not verified": its postcondition verdict is POSITIONAL - it reports
    that a read follows an actuation in the same method, not that the read
    checks that actuation - because with no target identity to bind to, that
    is the strongest statement available.

WHAT THIS SLICE ADDS. A registry-owned record (`ui_family.TARGET_FIELDS`),
required of any program that declares a target-carrying verb, plus two
signature obligations: a producer verb returns it and an actuation verb takes
it. Three refusals, all `G8`/`boundary` like slice 1's; no guarantee code is
registered.

THE FAILURE DIRECTION. This WIDENS what is refused and every program it
refuses was previously admitted, so it is stated rather than implied: a
computer-use program written against slices 1 to 3 does not compile here until
its target record is declared and its signatures name it. There is no profile
that turns the obligation off, which is the point - the `Untrusted` half of
`Untrusted[UiTarget]` is slice 2's derivation and IS profile-gated on
`taint_strict`, and the `UiTarget` half is this slice's and is not. The two
halves are separated on purpose and `test_the_two_halves_are_separately_
gated` measures the separation.

SCOPE. Nothing here admits a rung token: `ui.click.pixel` is still refused at
the declaration site by slice 1, and slice 3 owns the prefix-closure check.
Nothing here executes a UI step either: no `@py` body in this file is ever
run, and revl still drives no desktop (design 532 §7, item 539).

THE TWO OPEN QUESTIONS PR #1284 LEFT, answered here by measurement rather than
by argument, at the bottom of the file:
  * `test_the_selfhost_gate_does_not_decide_a_ui_program` - the self-host gate
    returns "" (no objection) on three programs the reference refuses under
    G8, so design 532 §9's obligation has been outstanding since SLICE 1, not
    since slice 3 as §9 records. Measured, not repaired: the repair is a
    frontier marker plus a crate regeneration and belongs to slice 3.
  * `test_widening_the_persistence_sink_set_with_ui_would_misclassify_three_
    verbs` - `retention.persistence_sink_of` reads a token's HEAD, so adding
    `ui` to it would make `ui.click` and `ui.find` durable storage alongside
    `ui.download`. The set cannot express "only `ui.download`".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import retention, ui_family  # noqa: E402
from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402


# ------------------------------------------------------------------ fixtures

RECORD = """type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}
"""

# The family in its post-slice-4 spelling. `ui.text` carries its item-522
# `compensate` because a compensatable verb without one is a G4 refusal and
# would mask the G8 answer this file measures.
FAMILY = (
    'extern emission[screen.observe] fn screen_observe(region: Str) -> Str\n'
    '  = @py { return "" }\n'
    'extern emission[ui.find] fn ui_find(seen: Str, name: Str) -> UiTarget\n'
    '  = @py { return None }\n'
    'extern emission[ui.click] fn ui_click(target: UiTarget) -> Int\n'
    '  = @py { return 0 }\n'
    'extern emission[ui.text] fn ui_text(target: UiTarget, s: Str)\n'
    '  compensate restore_field() = @py { return }\n'
    'extern emission[ui.download] fn ui_download(target: UiTarget) -> Str\n'
    '  = @py { return "" }\n'
    'fn restore_field() { }\n'
)

WORKER = (
    'service Worker { emission fn approve(region: Str) -> Int }\n'
    'component Billing provides worker: Worker {\n'
    '  provide worker {\n'
    '    fn approve(region) {\n'
    '      let seen = emit screen_observe(region)\n'
    '      let target = emit ui_find(seen, "Approve")\n'
    '      return emit ui_click(target)\n'
    '    }\n'
    '  }\n'
    '}\n'
)

CANONICAL = RECORD + FAMILY + WORKER


def _refusal(src: str, profile=None) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "ui_target_521.rvl", profile=profile)
    return excinfo.value


def _admits(src: str, profile=None) -> dict:
    return compile_source(src, "ui_target_521.rvl", profile=profile)


# ----------------------------------------------------------- the registry

def test_the_field_table_and_the_reason_table_name_the_same_fields() -> None:
    """Two parallel dicts, so the drift they could carry is pinned rather than
    trusted: a field with no recorded reason is a field nobody has to justify
    next time the list is edited, and the refusals quote the reason."""
    assert set(ui_family.TARGET_FIELDS) == set(ui_family.TARGET_FIELD_BINDS)
    for name, why in ui_family.TARGET_FIELD_BINDS.items():
        assert why.strip(), name


def test_every_field_is_a_concrete_scalar() -> None:
    """The binding has to be readable by every party that reads a target: a
    later phase, an audit line, a receipt. A generic or author-declared field
    type would make "what a target holds" depend on the program, which is the
    one thing a registry-owned shape exists to remove."""
    assert set(ui_family.TARGET_FIELDS.values()) <= {"Str", "Int", "Bool"}


def test_expiry_is_in_the_table_and_is_an_instant() -> None:
    """Design 532 §10 names this slice's oracle as `a target with no expiry is
    refused`. This is its registry half; the compile half is
    `test_a_target_without_an_expiry_is_refused` below."""
    assert ui_family.TARGET_FIELDS["expiry"] == "Int"
    assert "staleness" in ui_family.TARGET_FIELD_BINDS["expiry"]


def test_the_producers_and_consumers_are_admissible_spellings() -> None:
    """A verb in either list that `refusal()` would reject is a table that
    cannot be exercised - the drift direction a hand-kept second list has."""
    spellings = set(ui_family.spellings())
    for token in ui_family.TARGET_PRODUCERS + ui_family.TARGET_CONSUMERS:
        assert token in spellings, token
        assert ui_family.refusal(token, "emission") is None, token


def test_the_two_lists_are_disjoint() -> None:
    """A verb that both resolved and acted on a target would be an actuation
    that chooses its own target, which is the `ui.act(target)` shape design
    532 §11 refuses."""
    assert not (set(ui_family.TARGET_PRODUCERS)
                & set(ui_family.TARGET_CONSUMERS))


def test_screen_observe_is_neither_a_producer_nor_a_consumer() -> None:
    """It returns observed CONTENT, and content is not a target. Making it one
    would mean every screen read minted authority, which is the inverse of the
    item's premise: a target discovered by looking at pixels is a claim
    requiring verification, and `ui.find` is where that claim is made."""
    assert "screen.observe" not in ui_family.TARGET_PRODUCERS
    assert "screen.observe" not in ui_family.TARGET_CONSUMERS


def test_the_rendered_shape_is_a_declaration_that_parses() -> None:
    """The refusals quote `target_record_shape()` as the repair. A repair an
    author cannot paste is a refusal that names no allowed space (item 274)."""
    rendered = ui_family.target_record_shape()
    program = Parser(rendered + "\n", "shape.rvl").parse()
    decl = next(d for d in program.type_decls
                if d.name == ui_family.TARGET_TYPE)
    assert {f.name: f.type for f in decl.fields} == ui_family.TARGET_FIELDS


def test_a_qualified_target_type_reads_as_the_target() -> None:
    """`Untrusted[UiTarget]` and `UiTarget` are the same declaration once the
    item-249 qualifier is read, so an author may write either."""
    strip = ui_family.strip_qualifiers_shallow
    assert strip("Untrusted[UiTarget]") == "UiTarget"
    assert strip("Trusted[UiTarget]") == "UiTarget"
    assert strip("UiTarget") == "UiTarget"
    assert strip(None) is None


def test_a_list_of_targets_is_not_a_target() -> None:
    """Shallow on purpose. A consumer that takes `List[UiTarget]` has not named
    WHICH target it acts on, so the binding it carries is not the binding the
    actuation used - the same defect a bare string has, one level out."""
    assert ui_family.strip_qualifiers_shallow("List[UiTarget]") != "UiTarget"


# ------------------------------------------------- the record, at compile time

def test_the_canonical_program_admits() -> None:
    """The control. Every refusal below is this program with one thing
    changed, so a refusal that fired here would make the rest measure
    nothing."""
    _admits(CANONICAL)


def test_a_program_with_no_target_record_is_refused() -> None:
    src = (FAMILY.replace("-> UiTarget", "-> Str")
                 .replace("target: UiTarget", "target: Str")
           + WORKER.replace('let target = emit ui_find(seen, "Approve")\n',
                            'let target = emit ui_find(seen, "Approve")\n'))
    error = _refusal(src)
    assert "must declare the target record `UiTarget`" in error.message
    assert "type UiTarget = {" in (error.hint or "")


def test_a_target_without_an_expiry_is_refused() -> None:
    """Design 532 §10's named oracle for this slice, and it is refused by the
    same rule as the other nine fields rather than by a special case - a
    special case is a rule with one instance, which is a rule nobody applies
    to the next field."""
    error = _refusal(RECORD.replace("  expiry: Int\n", "") + FAMILY + WORKER)
    assert "does not carry `expiry`" in error.message
    assert "staleness" in (error.hint or "")


@pytest.mark.parametrize("field", sorted(ui_family.TARGET_FIELDS))
def test_every_registry_field_is_individually_required(field: str) -> None:
    """Each field, dropped on its own. Without this the `expiry` test above
    would pin one field and leave nine that could be dropped silently."""
    line = f"  {field}: {ui_family.TARGET_FIELDS[field]}\n"
    assert line in RECORD
    error = _refusal(RECORD.replace(line, "") + FAMILY + WORKER)
    assert f"does not carry `{field}`" in error.message
    assert ui_family.TARGET_FIELD_BINDS[field][:24] in (error.hint or "")


def test_a_field_at_the_wrong_type_is_refused_naming_both() -> None:
    """A target whose expiry is a string is a target whose staleness is
    compared by whatever the reader guesses the format is. The registry owns
    the type for the same reason it owns the field."""
    error = _refusal(
        RECORD.replace("  expiry: Int\n", "  expiry: Str\n") + FAMILY + WORKER)
    assert "declares `expiry` as `Str`, not `Int`" in error.message


def test_a_field_the_registry_does_not_name_is_admitted() -> None:
    """The check is a FLOOR and not a ceiling, stated as a decision rather
    than an omission: an extra field neither adds nor removes authority, so
    refusing it would be a false refusal with no soundness gain, while a
    MISSING field is a binding no later reader can recover."""
    _admits(RECORD.replace("\n}", "\n  tenant: Str\n}") + FAMILY + WORKER)


def test_a_target_declared_as_a_variant_is_refused() -> None:
    """`UiTarget` has to be a record: a variant carries no fields, so nothing
    reads the binding off it."""
    error = _refusal(
        "type UiTarget = Resolved | Missing\n" + FAMILY + WORKER)
    assert "must declare the target record `UiTarget`" in error.message


# --------------------------------------------------------- the signatures

def test_a_producer_returning_a_bare_string_is_refused() -> None:
    """The half that makes the record reachable. A `ui.find` returning `Str`
    is the pre-slice-4 program, and it is what this slice exists to stop."""
    error = _refusal(
        RECORD + FAMILY.replace(
            'fn ui_find(seen: Str, name: Str) -> UiTarget',
            'fn ui_find(seen: Str, name: Str) -> Str') + WORKER)
    assert "must return `UiTarget`, not `Str`" in error.message
    assert "re-resolved at every use" in (error.hint or "")


def test_a_producer_may_write_the_qualifier_or_leave_it_off() -> None:
    """Both spellings admit. Requiring the author to WRITE `Untrusted[...]`
    would contradict slice 2's own argument: sink-ness and source-ness come
    from the side that grants the authority, never from the author."""
    _admits(RECORD + FAMILY.replace(
        'fn ui_find(seen: Str, name: Str) -> UiTarget',
        'fn ui_find(seen: Str, name: Str) -> Untrusted[UiTarget]') + WORKER)


@pytest.mark.parametrize("verb,extern", [
    ("ui.click", "ui_click"),
    ("ui.text", "ui_text"),
    ("ui.download", "ui_download"),
])
def test_an_actuation_with_no_target_parameter_is_refused(
        verb: str, extern: str) -> None:
    """The half that closes the re-resolution race. An actuation that receives
    its target as a bare value resolves it a second time, and a name resolved
    twice is two targets - which is the check-to-use race item 522's
    transaction hits at every phase boundary."""
    broken = FAMILY.replace(f"fn {extern}(target: UiTarget",
                            f"fn {extern}(target: Str")
    assert broken != FAMILY
    error = _refusal(RECORD + broken + WORKER.replace(
        "return emit ui_click(target)", "return 0"))
    assert f"`emission[{verb}]` acts on a target" in error.message
    assert f"extern `{extern}`" in error.message


def test_a_list_of_targets_does_not_satisfy_an_actuation() -> None:
    error = _refusal(RECORD + FAMILY.replace(
        "fn ui_click(target: UiTarget", "fn ui_click(target: List[UiTarget]")
        + WORKER.replace("return emit ui_click(target)", "return 0"))
    assert "`emission[ui.click]` acts on a target" in error.message


def test_the_target_may_sit_in_any_parameter_position() -> None:
    """A capability token carries no parameter roles (item 294's parameters
    narrow the capability, they do not name the parameters), so revl cannot
    say WHICH parameter is the target and does not pretend to. It says one of
    them is. That limit is the same one slice 2 recorded for the taint roles,
    and it is a limit rather than a choice."""
    _admits(RECORD + FAMILY.replace(
        "fn ui_text(target: UiTarget, s: Str)",
        "fn ui_text(s: Str, target: UiTarget)") + WORKER)


def test_screen_observe_is_unaffected() -> None:
    """It carries no target obligation, so its `Str` return is untouched. A
    check that tightened it would have refused the observation that produces
    the evidence a target is bound to."""
    assert "fn screen_observe(region: Str) -> Str" in CANONICAL
    _admits(CANONICAL)


# ------------------------------------------------------------- the scope cuts

def test_a_service_method_carries_no_target_obligation() -> None:
    """A service method's `emission[ui.find]` scope funnels through the same
    parser hook slice 1 uses, and deliberately does not reach this check: the
    obligation belongs to the declaration that actually CROSSES, and a service
    method declares an interface. Item 522's `teardown_refusal` makes the same
    cut for the same reason, so the family has one rule and not two."""
    _admits(RECORD
            + "service Looker { emission[ui.find] fn look(r: Str) -> Str }\n"
            + FAMILY + WORKER)


def test_a_program_with_no_computer_use_verb_is_untouched() -> None:
    """The inertness control. One loop over the extern list finds nothing, so
    a program that declares no UI verb reaches every downstream section
    exactly as it did before this slice."""
    _admits(
        'extern emission[db.write] fn w(row: Str) -> Int = @py { return 0 }\n'
        'service Store { emission fn go(row: Str) -> Int }\n'
        'component S provides store: Store {\n'
        '  provide store {\n'
        '    fn go(row) { return emit w(row) }\n'
        '  }\n'
        '}\n')


def test_a_program_that_declares_uitarget_and_no_ui_verb_is_untouched() -> None:
    """`UiTarget` is reserved only where the family is declared. A program with
    no UI verb that happens to use the name owns it, because refusing there
    would be a refusal with no authority behind it."""
    _admits("type UiTarget = { whatever: Str }\n"
            "pub fn pick(t: UiTarget) -> Str { return t.whatever }\n")


def test_the_refusal_is_a_boundary_refusal_and_registers_no_new_code() -> None:
    """Every refusal in this slice is raised by `G8`, which already ships with
    its reproducers and its `docs/rejections.md` row. A slice that needed a
    new guarantee code would be a slice making a new promise, and this one
    makes the existing enumeration promise true of a target."""
    error = _refusal(RECORD.replace("  expiry: Int\n", "") + FAMILY + WORKER)
    assert error.code == "G8"
    assert classify(error)["code"] == "G8"


# ------------------------------------------ the binding, in the IR and in taint

def test_the_evidence_fields_are_present_in_the_ir() -> None:
    """Design 532 §10's other oracle half for this slice. The record is an
    ordinary revl record, so it reaches the IR through the type table with no
    new lowering - which is the same argument section 6 makes about the audit
    surface: spelling a UI fact in a construct revl already carries is what
    makes every reader of that construct read it for free."""
    ir = _admits(CANONICAL)
    spec = ir["types"]["UiTarget"]
    assert spec["kind"] == "record"
    assert spec["fields"] == ui_family.TARGET_FIELDS


def test_the_actuation_declares_the_record_in_the_ir() -> None:
    """What item 522 needs and PR #1287 named: a target that survives a phase
    boundary. The click's declared parameter type IS the record, so a
    transaction reading the IR has a target identity to bind a postcondition
    to instead of a position in a method body."""
    ir = _admits(CANONICAL)
    click = next(e for e in ir["externs"] if e["name"] == "ui_click")
    assert any(ui_family.strip_qualifiers_shallow(t) == "UiTarget"
               for t in _param_types(click)), click


def _param_types(extern: dict) -> list:
    """The declared parameter types of a lowered extern, whichever spelling
    the IR carries them under."""
    params = extern.get("params") or []
    out = []
    for p in params:
        if isinstance(p, dict):
            out.append(p.get("type"))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append(p[1])
        else:
            out.append(p)
    return out


def test_the_two_halves_are_separately_gated() -> None:
    """`Untrusted[UiTarget]` is two claims with two owners, and conflating them
    would hide which one a program actually has.

    The `UiTarget` half is this slice's: unconditional, no profile turns it
    off, and the canonical program above does not compile without it. The
    `Untrusted` half is slice 2's derivation: profile-gated on `taint_strict`
    exactly as every other item-249 Slice D class is. So the canonical program
    admits WITHOUT strict mode (the record is there) and a target reaching an
    actuation refuses WITH it (the derivation is there)."""
    _admits(CANONICAL)
    error = _refusal(CANONICAL, AdmissionProfile(taint_strict=True))
    assert classify(error)["code"] == "G9"
    assert "screen" in error.message


def test_the_endorsement_still_admits_the_program_with_a_record() -> None:
    """The repair item 249 ships is unchanged by the record: what is endorsed
    is a value, and the value is now a target rather than a string. If the
    record had broken the declassifier, slice 4 would have closed the only
    door slice 2 left open."""
    endorsed = WORKER.replace(
        "service Worker { emission fn approve(region: Str) -> Int }",
        "service Worker { emission endorse[screen] fn approve(region: Str) "
        "-> Int }").replace(
        "      return emit ui_click(target)\n",
        '      let ok = endorse[screen](target, reason = "the operator '
        'confirmed the control")\n'
        "      return emit ui_click(ok)\n")
    _admits(RECORD + FAMILY + endorsed, AdmissionProfile(taint_strict=True))


# ------------------------------------------------------------- non-vacuity

def test_non_vacuity_the_programs_that_flipped() -> None:
    """Measured, not asserted in prose. Each of these compiled on `4cfc8f32`
    (slice 2's head) and is a G8 refusal here. The canonical program is the
    control: it compiles on BOTH trees, because the record and the typed
    signatures were always writable - what slice 4 changed is that they are
    now required."""
    flipped = [
        # the family's own canonical program in its pre-slice-4 spelling
        (FAMILY.replace("-> UiTarget", "-> Str")
               .replace("target: UiTarget", "target: Str") + WORKER),
        # the record present, the producer still returning a bare string
        RECORD + FAMILY.replace(
            'fn ui_find(seen: Str, name: Str) -> UiTarget',
            'fn ui_find(seen: Str, name: Str) -> Str') + WORKER,
        # the record present, an actuation still taking a bare string
        RECORD + FAMILY.replace("fn ui_click(target: UiTarget",
                                "fn ui_click(target: Str")
        + WORKER.replace("return emit ui_click(target)", "return 0"),
        # the record present but missing its expiry
        RECORD.replace("  expiry: Int\n", "") + FAMILY + WORKER,
    ]
    assert len(flipped) == 4
    for src in flipped:
        assert _refusal(src).code == "G8"
    _admits(CANONICAL)


# ==========================================================================
# The two questions PR #1284 left open, answered by measurement.
# ==========================================================================

@pytest.fixture(scope="module")
def selfhost_admit():
    """`selfhost/lower.rvl`'s `admit_src`, compiled by revl and executed
    through the python backend - the same harness
    `tests/test_selfhost_lower.py` uses. Slow (it compiles the self-host
    front end), which is why it is one module-scoped fixture."""
    import importlib.util
    import types as _types

    from revl import compile_files

    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    spec = importlib.util.spec_from_file_location(
        "pyemit_ui_target_521", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = _types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["admit_src"]


def test_the_selfhost_gate_does_not_decide_a_ui_program(selfhost_admit) -> None:
    """The first of PR #1284's two open questions, measured.

    Design 532 §9 says the self-host gate is already fail-closed for this
    construct because `admit_src` issues no admission (item 417), and records
    the obligation as firing "when a slice of this item starts deciding
    admission on a UI token". Both halves were read rather than measured, and
    the measurement moves the date: SLICE 1 already decides admission on a UI
    token - it raises three G8 refusals over one - and the self-host gate
    returns "" on all three. "" is NO OBJECTION, which is agreement by
    silence, and §9 names agreement by silence as the thing the marker exists
    to prevent.

    What this is and is not. It is not a false ADMISSION: the native gate's
    verdict vocabulary has no `admitted` arm (`gate.Verdict.kind`), so "" from
    the self-host means the reference must be asked. It IS a gap nothing
    reports: the self-host differential oracles compare verdicts on a corpus
    whose refusals are in-slice, a G8 refusal classifies OUT-OF-SLICE, and so
    every oracle stays green over a check the self-host does not run. That is
    item 391's own finding, restated by 417, arriving on this family.

    Not repaired here, and the reason is scope rather than difficulty: the
    repair is a named decline, which means a frontier-table entry in
    `tools/build_gate_crate.py` (and its `build_gate_wasm.py` twin) and a
    crate regeneration. §9 ties that to slice 3, which carries a crate
    regeneration anyway for the prefix-closure check.
    """
    family = (
        'extern emission[screen.observe] fn screen_observe(r: Str) -> Str\n'
        '  = @py { return "" }\n'
        'extern emission[{token}] fn act(t: Str) -> Int = @py { return 0 }\n'
    )
    worker = (
        'service W { emission fn go(r: Str) -> Int }\n'
        'component C provides w: W {\n'
        '  provide w {\n'
        '    fn go(r) { return emit act(r) }\n'
        '  }\n'
        '}\n'
    )
    for token in ("ui", "ui.drag", "ui.click.pixel"):
        src = family.replace("{token}", token) + worker
        reference = _refusal(src)
        assert reference.code == "G8", token
        assert selfhost_admit(src) == "", (
            f"the self-host gate now has a verdict on `{token}`; if that is a "
            f"port of the item-521 checks, this test should assert the tag "
            f"rather than the silence (design 532 §9)")


def test_widening_the_persistence_sink_set_with_ui_would_misclassify_three_verbs(
) -> None:
    """The second of PR #1284's two open questions, measured.

    The question: should `retention.persistence_sink_of` gain `ui`, since
    `ui.download` does put a file on the host and that is the shape the set
    describes? The measured answer is NO, not as the set stands, and the
    reason is the one design 532 §5.1 already gives for the taint roles.

    `persistence_sink_of` reads a token's dotted HEAD, by construction and by
    its own docstring ("only the FIRST capability is consulted for its head").
    The head of four of the five verbs is `ui`. So `ui` in that set makes
    `ui.click` and `ui.find` durable storage alongside `ui.download`, and a
    `Retained[T, P]` value reaching a click - an actuation that stores nothing
    - is refused as writing past-deadline data to a store. Three refusals for
    one true one is not a conservative widening, it is a wrong classification
    that happens to include a right one.

    Measured below at the function rather than through a compile, so the test
    states the mechanism rather than one program's verdict. A per-verb
    registry entry (the shape `TAINT_ROLES` has) would express "only
    `ui.download`", and that is a change to item 472's module with its own
    failure direction and its own note. Left alone rather than widened on a
    hunch, which is where PR #1284 left it and why.
    """
    assert "ui" not in retention.PERSISTENCE_SINK_SCOPES
    assert "screen" not in retention.PERSISTENCE_SINK_SCOPES
    for token in ("ui.download", "ui.click", "ui.find", "screen.observe"):
        assert retention.persistence_sink_of([token]) is None, token

    widened = frozenset(set(retention.PERSISTENCE_SINK_SCOPES) | {"ui"})
    caught = [token for token in ("ui.download", "ui.click", "ui.find",
                                  "ui.text")
              if str(token).split(".", 1)[0] in widened]
    assert caught == ["ui.download", "ui.click", "ui.find", "ui.text"], (
        "the head rule catches every `ui` verb, not only the one that lands a "
        "file on the host")
