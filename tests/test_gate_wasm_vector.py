"""The wasm gate's conformance vector (roadmap item 335, slices 0-2).

The item's own exit test: *the wasm-compiled `admit`, run under wasmtime,
returns the reference verdict on the corpus*. That is what this file drives, on
a component built from `crates/revl-gate-wasm` right here, right now.

Shape
-----
1. Build the component: `cargo build --target wasm32-unknown-unknown --release`
   over the committed crate, then `wasm-tools component new`. Nothing from this
   repo's Python is on the path while it builds.
2. Read the built artifact's OWN import section and require it to be empty.
   That is the design's soundness mechanism, and it is a property of the
   artifact, not of the WIT: with no clock, no filesystem, no random and no host
   function to consult, a verdict is a total, deterministic function of its
   arguments, so agreement measured once holds on every host.
3. Run the whole corpus through the component under wasmtime and compare with
   the REFERENCE compiler's verdicts, program by program, over the same
   `ACCEPTED_PROGRAMS` / `REJECTED_PROGRAMS` the self-host lowering oracle uses
   (imported, not copied, so "same corpus" stays literal).
4. Assert the two directions ASYMMETRICALLY, exactly as
   `tests/test_gate_crate_admit.py` does for the rust crate, because they are
   not symmetric: a wasm gate that ADMITS what the reference refuses is the
   defect class this arc exists to prevent, and a wasm gate that refuses what
   the reference admits is a false alarm a consumer acts on. The first is closed
   structurally (there is no admitting arm to reach) and held here over the
   whole corpus; the second must not happen on the covered corpus at all.
5. Record TRAPS as their own outcome and require zero of them. On wasm the rust
   panic strategy is `abort`, so the crate's `catch_unwind` fail-closed path
   cannot catch and a native gate panic traps the instance instead of returning
   `outside-frontier`. A trap is loud and is not a verdict, so it is not a false
   admission — but it is not a verdict either, and the vector says so rather
   than letting a trap read as a pass.

Why the vector is computed live rather than committed
-----------------------------------------------------
The design asks for a committed `(input, expected)` corpus generated from the
reference gate at the build sha. Computing it live from `revl.compiler` in the
same process IS "at the build sha", by construction, and cannot go stale between
a language change and a vector regeneration. The committed half of the discipline
is the crate source and its drift gate (`tests/test_gate_wasm_drift.py`).

Toolchain honesty
-----------------
This needs cargo WITH the wasm target installed, `wasm-tools`, `wasmtime`, and a
resolvable `wit-bindgen`. Where any is absent the whole module SKIPS WITH THE
REASON the tool reports, the `tests/test_gate_crate_admit.py` discipline. A
skipped tier is never green, and a green here always means a real component was
really built and really ran. `REVL_WASM_CARGO` names a cargo whose toolchain
carries the wasm target, for a machine whose default cargo does not.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "crates" / "revl-gate-wasm"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# The corpus and the reference classifier, IMPORTED from the self-host lowering
# oracle so the wasm gate is measured against the same programs and the same
# guarantee vocabulary the rust crate is. A copy here would be free to drift.
import test_selfhost_lower as oracle  # noqa: E402


def _generator():
    path = ROOT / "tools" / "build_gate_wasm.py"
    spec = importlib.util.spec_from_file_location("revl_build_gate_wasm_vector", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revl_build_gate_wasm_vector"] = module
    spec.loader.exec_module(module)
    return module


GEN = _generator()

_REASON = GEN.toolchain_reason() or (
    None if GEN.wasmtime_binary() is not None else
    "wasmtime not found (needed to run the component)")
pytestmark = pytest.mark.skipif(
    _REASON is not None,
    reason=f"needs a wasm toolchain to build and run the gate component: {_REASON}")


# ------------------------------------------------------------- the artifact


@pytest.fixture(scope="module")
def component(tmp_path_factory) -> Path:
    """The built `revl:gate` component. Built once for the module: a release
    build of the whole self-host front end is not cheap."""
    out = tmp_path_factory.mktemp("revl_gate_wasm") / "revl_gate.wasm"
    try:
        return GEN.build_component(CRATE, out)
    except RuntimeError as error:
        pytest.fail(f"the gate component failed to build:\n{error}")


def _call(component: Path, expression: str) -> tuple[bool, str]:
    """(completed, payload) for one `wasmtime run --invoke`. `completed` is
    False when the instance trapped, and then `payload` is wasmtime's stderr."""
    done = GEN.invoke(component, expression)
    if done.returncode != 0:
        return (False, (done.stderr or done.stdout or "").strip())
    return (True, done.stdout.strip())


def _verdict(component: Path, source: str) -> dict:
    """The gate's verdict for `source`, in the item-332 wire shape.

    `admit-json` is used rather than the typed record because it returns the
    crate's `Verdict::to_json` BYTES, so cross-tier agreement is a byte
    comparison and not a re-encoding that could paper over a difference. The
    typed record path is exercised separately below.
    """
    ok, payload = _call(component, f"admit-json({GEN.wave_string(source)})")
    if not ok:
        return {"verdict": "TRAPPED", "admitted": None, "code": None,
                "message": payload}
    return json.loads(GEN.unwave_string(payload))


def _reference(source: str) -> tuple[str, str]:
    """(tag, message) — ("", "") when the reference admits. The reference's own
    guarantee vocabulary, via the oracle's classifier."""
    try:
        compile_source(source, "gate-wasm.rvl")
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


CORPUS: list[tuple[str, str]] = (
    [(f"accepted: {name}", src) for name, src in oracle.ACCEPTED_PROGRAMS]
    + [(f"rejected: {name}", src) for name, src, _ in oracle.REJECTED_PROGRAMS]
)


@pytest.fixture(scope="module")
def agreement(component) -> list[tuple[str, str, tuple[str, str], dict]]:
    """(name, source, reference verdict, wasm verdict) for the whole corpus."""
    return [(name, src, _reference(src), _verdict(component, src))
            for name, src in CORPUS]


# ----------------------------------------------------- the artifact itself


def test_the_built_component_imports_nothing(component):
    """The design's headline soundness mechanism, read off the ARTIFACT.

    The gate imports no clock, no filesystem, no random and no host function, so
    nothing in the environment can change a verdict for a fixed input: there is
    no config import, no feature flag, no policy-widening channel other than the
    arguments themselves. This is also item 289's least-authority chain applied
    reflexively — an artifact whose import section proves it can consult nothing
    but what it was given.

    A build that grows an import fails here, which is the whole point of
    checking the binary rather than the WIT.
    """
    imports = GEN.component_imports(component)
    assert imports == [], (
        "the gate component grew an import, so its verdict is no longer "
        "provably a function of its arguments:\n  " + "\n  ".join(imports))


def test_the_component_is_small_enough_to_be_an_edge_artifact(component):
    """The cost baseline item 335 has to beat is the playground's Pyodide lane:
    a multi-megabyte runtime plus interpreter start, fine for a browser tab and
    disqualifying for an edge worker or a serverless cold start.

    The bound is deliberately loose — this is a regression tripwire on the order
    of magnitude, not a byte budget. What it forbids is the artifact quietly
    growing back into the thing it was built to replace.
    """
    size = component.stat().st_size
    assert size < 8 * 1024 * 1024, (
        f"the gate component is {size} bytes; an edge artifact that large is "
        f"back in Pyodide's cost class")


def test_the_version_surface_reports_the_crate_frontier_and_the_wasm_tier(component):
    """`gate-version` is how a host detects skew before trusting a cached edge
    gate's verdicts, so the frontier it reports must be the one the artifact was
    actually built from — the rust crate's, not a restated copy."""
    ok, payload = _call(component, "gate-version()")
    assert ok, payload
    version = json.loads(GEN.unwave_string(payload))
    crate = json.loads(
        (ROOT / "crates" / "revl-gate" / "GENERATED.json").read_text(encoding="utf-8"))
    from revl.gate import GATE_API_VERSION

    assert version["api"] == GATE_API_VERSION
    assert version["frontier"] == crate["frontier"]
    assert version["language"] == crate["language_version"]
    assert version["layer"] == crate["covered_layer"]
    assert version["tier"] == "wasm"


# --------------------------------------------- the release-blocking direction


def test_the_wasm_gate_issues_no_admission_for_anything_in_the_corpus(agreement):
    """THE security clause, at the edge.

    A wasm gate that refuses what the reference admits is an inconvenience. A
    wasm gate that ADMITS what the reference refuses is the defect class this
    arc exists to prevent, and at the edge there is no reference compiler nearby
    to catch it. The component ships no admitting arm at all, and this holds
    that over every corpus program: `admitted` is false everywhere, and the only
    arms are the three the crate has.
    """
    offenders = [(name, verdict) for name, _src, _ref, verdict in agreement
                 if verdict["admitted"] is not False
                 or verdict["verdict"] not in
                 ("refused", "no_objection", "outside_frontier")]
    assert not offenders, (
        "the wasm gate produced something a consumer could read as an "
        "admission:\n  "
        + "\n  ".join(f"{name}: {verdict}" for name, verdict in offenders))


def test_no_corpus_program_traps_the_instance(agreement):
    """A trap is not a false admission, but it is not a verdict either.

    On wasm the rust panic strategy is `abort`, so `revl_gate::admit`'s
    `catch_unwind` fail-closed path cannot catch: a native gate panic takes the
    instance down instead of returning `outside-frontier`. That is a host
    obligation the README states, and it must never fire on the covered corpus.
    """
    trapped = [(name, verdict["message"]) for name, _src, _ref, verdict in agreement
               if verdict["verdict"] == "TRAPPED"]
    assert not trapped, (
        "the gate component trapped instead of returning a verdict:\n  "
        + "\n  ".join(f"{name}: {msg}" for name, msg in trapped))


def test_no_false_alarm_on_the_covered_corpus(agreement):
    """The sound direction, held over the whole corpus at once: the gate must
    not REFUSE a program the reference admits either — that is a false alarm a
    consumer acts on, and on the covered corpus there is no excuse for one."""
    false_alarms = [
        (name, verdict["code"], verdict["message"])
        for name, _src, (ref_tag, _ref_msg), verdict in agreement
        if verdict["verdict"] == "refused" and ref_tag == ""
    ]
    assert not false_alarms, (
        "the wasm gate REFUSED programs the reference ADMITS, on the covered "
        "corpus:\n  "
        + "\n  ".join(f"{name}: {code} ({msg!r})"
                      for name, code, msg in false_alarms))


# ------------------------------------------------------ full corpus agreement


@pytest.mark.parametrize("index", range(len(CORPUS)),
                         ids=[name for name, _ in CORPUS])
def test_wasm_gate_and_reference_agree_on_the_covered_corpus(agreement, index):
    """The item's exit test, per program: where the reference refuses, the wasm
    gate refuses with the same code AND the same message, verbatim; where the
    reference admits, the wasm gate raises no objection.

    The corpus is the COVERED corpus, so an `outside_frontier` verdict here is a
    real finding (either the frontier guard got more conservative or the
    self-host lost a surface) and is reported in its own vocabulary rather than
    as a plain disagreement.
    """
    name, _src, (ref_tag, ref_msg), verdict = agreement[index]
    if verdict["verdict"] == "TRAPPED":
        pytest.fail(f"{name}: the component trapped — {verdict['message']}")
    if verdict["verdict"] == "outside_frontier":
        pytest.fail(
            f"{name}: the wasm gate declined to decide a program on the COVERED "
            f"corpus — {verdict['message']}")
    if ref_tag == "":
        assert verdict["verdict"] == "no_objection", (
            f"{name}: the reference admits, the wasm gate refused "
            f"{verdict['code']} ({verdict['message']!r})")
        return
    assert verdict["verdict"] == "refused", (
        f"{name}: the reference refuses {ref_tag}, the wasm gate said "
        f"{verdict['verdict']}")
    assert verdict["code"] == ref_tag, (
        f"{name}: code — wasm {verdict['code']!r} != reference {ref_tag!r} "
        f"({verdict['message']!r})")
    assert verdict["message"] == ref_msg, (
        f"{name}: message — wasm {verdict['message']!r} != reference {ref_msg!r}")


# --------------------------------------------------- determinism and shape


@pytest.fixture(scope="module")
def refusing(agreement) -> tuple[str, dict]:
    """(source, verdict) for the first corpus program the gate actually REFUSES.

    Taken off the measured corpus rather than hand-written, so these two tests
    exercise the arm that carries a code and a message instead of a
    no-objection, and cannot quietly degrade into probing the empty arm if the
    gate's coverage shifts.
    """
    for _name, source, _ref, verdict in agreement:
        if verdict["verdict"] == "refused":
            return (source, verdict)
    pytest.fail("no corpus program is refused by the gate at all")


def test_the_same_input_yields_the_same_verdict_across_runs(refusing, component):
    """Determinism, measured rather than assumed. Two fresh instantiations of
    the same artifact on the same input must produce the same bytes — which is
    what the empty import section buys, and what makes a vector measured once
    mean something on every host."""
    source, expected = refusing
    first = _verdict(component, source)
    second = _verdict(component, source)
    assert first == second == expected, (first, second, expected)


def test_the_typed_record_and_the_json_wire_carry_the_same_verdict(refusing,
                                                                   component):
    """Two exports, one verdict. `admit` hands a component-model host a typed
    record; `admit-json` hands it the crate's wire bytes. A host must not have
    to care which it reads."""
    source, wire = refusing
    ok, payload = _call(component, f"admit({GEN.wave_string(source)})")
    assert ok, payload
    # WAVE renders the record; the arm name is kebab-case on the WIT boundary
    # and snake_case on the crate's json wire, which is the one deliberate
    # difference between them.
    assert "admitted: false" in payload
    assert f'code: some("{wire["code"]}")' in payload
    assert f'kind: "{wire["verdict"].replace("_", "-")}"' in payload


def test_admit_artifact_declines_and_names_the_gap(component):
    """Design cut B is exported so its arrival is additive, and it fails closed
    today. What it must never do is answer: the item-289 chain's declared-caps
    leg is the G8 boundary projection, which has no native port, and a guessed
    declared set is precisely the wave-through this arc exists to prevent."""
    ok, payload = _call(component, 'admit-artifact("{}", "{}", [])')
    assert ok, payload
    assert "admitted: false" in payload
    assert "outside-frontier" in payload
    assert "289" in payload


# ------------------------------------------------------------- the frontier
#
# The wrapped crate's frontier has three triggers: two GENERATED lexical rows —
# the reference keywords, and the reference stdlib builtins, that the self-host
# gate does not cover — and the always-live source-size bound.
#
# Both lexical rows are EMPTY at this generation. That is a measured closure,
# not a broken derivation: item 391 ported the last eight builtins
# (`is_digit`/`is_alpha`/`is_alnum`/`is_space`, `codepoint_at`, `field`, `list`,
# `str`) into `selfhost/lower.rvl`, so `revl.lexer.KEYWORDS` and
# `revl.typecheck._BUILTIN_SIG` are now both subsets of what the self-host
# sources declare, and the difference the generator takes is empty.
#
# An empty row is a real and expected state. What it must never become is a
# SKIPPED test: a skip cannot tell "we checked and there is nothing" apart from
# "we did not check", and the two are the whole point of this gate. So the
# frontier is asserted here as a PARTITION over the reference builtin table,
# and the partition is total — every name is on exactly one side of it:
#
#   * a name the table EXCLUDES must be DECLINED — `FRONTIER_PROBES` below,
#     one probe per generated entry, nothing hand-picked (empty today);
#   * a name the table COVERS must be DECIDED, never `outside_frontier` —
#     `test_the_gate_decides_every_builtin_inside_its_frontier`, which runs the
#     whole covered set through the component and therefore always has work;
#   * and the rows themselves are RE-MEASURED from both compilers by
#     `test_the_empty_frontier_row_is_a_measurement_not_a_missing_check`, so an
#     empty row is a measurement taken in this run rather than a stale artifact
#     read back off the thing it generated.
#
# Deriving the probes from the table is also what keeps them honest in the other
# direction: this test used to hand-pick `.is_digit()`, item 391 closed that gap,
# and the probe would have gone on asserting a gap that no longer existed. A
# probe derived from the table cannot outlive the gap it probes.
#
# The size bound is deliberately NOT probed through this door: `wasmtime run
# --invoke` passes the source as a single argv element and Linux caps one
# argument at 128 KiB, half the 256 KiB bound, so the probe could not be
# delivered. `tests/test_gate_crate_admit.py` and the crate's own
# `frontier::tests::an_oversized_source_is_a_gap` hold that arm in-process
# against the very same `frontier.rs` this component is built from.

GENERATED_CRATE = json.loads(
    (ROOT / "crates" / "revl-gate" / "GENERATED.json").read_text(encoding="utf-8"))
EXCLUDED_KEYWORDS: list[str] = GENERATED_CRATE["frontier_excluded_keywords"]
EXCLUDED_BUILTINS: list[str] = GENERATED_CRATE["frontier_excluded_builtins"]


def _crate_generator():
    """`tools/build_gate_crate.py`, loaded the way `_generator()` loads the wasm
    one: the frontier derivation ITSELF, so the tables can be re-measured here
    instead of only read back off the artifact they produced."""
    path = ROOT / "tools" / "build_gate_crate.py"
    spec = importlib.util.spec_from_file_location("revl_build_gate_crate_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revl_build_gate_crate_probe"] = module
    spec.loader.exec_module(module)
    return module


def _frontier_probes() -> list[tuple[str, str]]:
    """One probe per entry in the two generated lexical rows, and nothing
    hand-picked. Empty rows yield an empty list — which is exactly why the other
    half of the partition has to be total."""
    return (
        [(f"excluded builtin .{name}()",
          f"fn f(s: Str) -> Str {{ let v = s.{name}() return s }}")
         for name in EXCLUDED_BUILTINS]
        + [(f"excluded keyword {word}", f"fn f() -> Int {{ {word} }}")
           for word in EXCLUDED_KEYWORDS]
    )


FRONTIER_PROBES = _frontier_probes()


def test_the_empty_frontier_row_is_a_measurement_not_a_missing_check():
    """The emptiness, ASSERTED. "We checked and there is nothing" is a claim
    with content; a skip is not.

    Three things are held here, none of which an empty row can satisfy by
    accident:

    1. the reference side of the derivation is non-empty. A reference table that
       came back empty would empty the DIFFERENCE too, and the generator calls
       that the one intolerable failure direction (a silently-empty self-host
       side WIDENS the excluded table, which is safe; a silently-empty reference
       side EMPTIES it, which is not);
    2. the committed rows equal a fresh `frontier_tables()` run over both
       compilers — the reference tables imported from `revl`, the self-host sets
       re-extracted from `selfhost/lexer.rvl` and `selfhost/lower.rvl` with
       anchored regexes that raise rather than return an empty set. So the
       emptiness is measured in THIS run, on the tree the component was built
       from;
    3. the probe list is one-to-one with the rows. A row that becomes non-empty
       therefore CANNOT arrive without probes: `FRONTIER_PROBES` grows with it
       and `test_an_out_of_frontier_construct_declines_rather_than_deciding`
       starts exercising the declining arm.
    """
    from revl.lexer import KEYWORDS
    from revl.typecheck import _BUILTIN_SIG

    assert len(KEYWORDS) >= 20, (
        "the reference keyword table came back with almost nothing in it; an "
        "empty frontier row derived from that would be an artifact of the "
        f"measurement, not a closure: {sorted(KEYWORDS)}")
    assert len(_BUILTIN_SIG) >= 15, (
        "the reference builtin table came back with almost nothing in it; see "
        f"above: {sorted(_BUILTIN_SIG)}")

    tables = _crate_generator().frontier_tables()
    assert tables["keywords"] == EXCLUDED_KEYWORDS, (
        "the committed keyword row is not what the two compilers measure now",
        tables["keywords"], EXCLUDED_KEYWORDS)
    assert tables["builtins"] == EXCLUDED_BUILTINS, (
        "the committed builtin row is not what the two compilers measure now",
        tables["builtins"], EXCLUDED_BUILTINS)

    assert len(FRONTIER_PROBES) == len(EXCLUDED_KEYWORDS) + len(EXCLUDED_BUILTINS), (
        "the frontier probes are no longer one-to-one with the generated rows, "
        "so a gap could open without anything probing it",
        FRONTIER_PROBES, EXCLUDED_KEYWORDS, EXCLUDED_BUILTINS)


def test_an_out_of_frontier_construct_declines_rather_than_deciding(component):
    """Fail closed at the frontier: a construct the self-host gate does not
    cover is declined with a control verdict, never decided.

    Runs one probe per generated frontier entry. Both rows are empty at this
    generation, so this test has no probe to run today — and that is covered
    work rather than skipped work only because the two tests either side of it
    assert why: the rows were re-measured, and the whole covered set is put
    through the component below.
    """
    for label, source in FRONTIER_PROBES:
        verdict = _verdict(component, source)
        assert verdict["verdict"] == "outside_frontier", (label, verdict)
        assert verdict["admitted"] is False, (label, verdict)
        assert verdict["code"] == "FRONTIER", (label, verdict)


def test_the_gate_decides_every_builtin_inside_its_frontier(component):
    """The other half of the partition, and the half that carries the load while
    the excluded rows are empty.

    Every reference stdlib builtin the frontier does NOT exclude must be
    DECIDED by the component — refused or not objected to, but never declined.
    That is the property "the frontier row is empty" actually asserts, measured
    against the artifact instead of against the table that describes it: if a
    builtin the table calls covered were in fact outside the self-host surface,
    the component would decline it here and this reds.

    One program carrying every covered name, so the whole set costs a single
    wasmtime instantiation. Arities come from `_BUILTIN_SIG` so the calls are
    structurally right; the RECEIVER type is deliberately not, because this gate
    covers the composition/guarantee layer and not the reference type layer
    (`COVERED_LAYER`), so a type mismatch is not a verdict it can reach. What
    matters is that each name lands in member position, the only position
    `frontier::scan` looks at.
    """
    from revl.typecheck import _BUILTIN_SIG

    covered = sorted(set(_BUILTIN_SIG) - set(EXCLUDED_BUILTINS))
    assert covered, (
        "no reference builtin is inside the gate's frontier at all — the "
        "covered surface cannot be empty and the gate still be useful")

    def arity(name: str) -> int:
        sig = _BUILTIN_SIG[name]
        if isinstance(sig, dict):  # a name overloaded on receiver type
            sig = next(iter(sig.values()))
        return len(sig[1])

    body = "\n".join(
        f"  let v{i} = s.{name}({', '.join(['0'] * arity(name))})"
        for i, name in enumerate(covered))
    source = f"fn f(s: Str) -> Str {{\n{body}\n  return s\n}}"
    verdict = _verdict(component, source)
    assert verdict["verdict"] != "TRAPPED", verdict
    assert verdict["verdict"] != "outside_frontier", (
        "the gate DECLINED a program built only from builtins its own frontier "
        f"table calls covered ({len(covered)} names: {covered}); the table and "
        f"the artifact disagree: {verdict}")
    assert verdict["verdict"] in ("refused", "no_objection"), verdict
