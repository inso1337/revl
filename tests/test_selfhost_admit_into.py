"""The MANIFEST arm of the self-host gate, differentially against the public
`revl.gate.admit_into` (issue #346, docs/design/457 section 4.5 / slice T5).

`tests/test_selfhost_lower.py` cross-checks `admit_ambient` against
`admit_src(manifest_text ++ candidate)` — a composition the gate can be handed
as ONE text. That oracle cannot reach the question this file asks, because the
running composition is not text here: it is a COMPILED IR, projected onto the
item-186 row wire by `revl.manifest.manifest_wire`, and the question is whether
the native gate and the reference agree about a candidate admitted INTO it.

So the reference here is the verb an embedder actually calls:

    revl.gate.admit_into(candidate, IR(running))      the py tier
    admit_ambient(candidate, manifest_wire(IR(running)))   the native tier

and the property is ASYMMETRIC, because the two tiers do not cover the same
layer and must not be made to pretend they do:

  * **Every native refusal is a reference refusal, with the same tag and — for
    a tag this gate spells in full — the same sentence.** This is the direction
    a gate may not err in, and the one this file's zero-tolerance assertion
    covers. A native refusal the reference does not make is a false reject; a
    native refusal under a different tag or sentence is a silent disagreement,
    which is worse than a refusal (an embedder reads the code).
  * **A native NO-OBJECTION is not an admission.** The reference may refuse
    where the native gate says nothing — the layers the native tier does not
    run. That is under-refusal, the direction this gate is allowed to err in,
    and `"" ` on this wire carries `"admitted": false` all the way out to
    `Verdict::to_json`. The pairs where it happens are named in
    `WITHHELD`, so the frontier is a list and not a shrug.

Non-vacuity is not left to the summary line: `test_the_running_signature_is_
read_rather_than_ignored` pins the pair that only a gate reading the running
SIGNATURE can tell apart, and `test_the_frontier_list_is_honest` fails if a
named frontier entry has silently started agreeing.
"""

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import gate as py_gate  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.manifest import manifest_wire  # noqa: E402

import test_selfhost_lower as lower_oracle  # noqa: E402

# The running composition of bench/admission_latency.py — a real graph (a
# provider, a consumer, an effect with a matching undo), the same bytes the
# in-process gate harness holds, so this oracle and the harness cannot drift
# into asking about different compositions.
import admission_latency as al  # noqa: E402
import inprocess_gate_harness as harness  # noqa: E402


@pytest.fixture(scope="module")
def native():
    """`admit_ambient` / `admit_src` off `selfhost/lower.rvl`, compiled by the
    reference and executed through the python backend — the same `_exec_emitted`
    shape every `tests/test_selfhost_*.py` uses."""
    return lower_oracle._exec_emitted()


@pytest.fixture(scope="module")
def running():
    """`(IR, wire)` for the running composition: the compiled IR the reference
    verb takes, and its projection onto the row wire the native verb takes."""
    ir = compile_source(al.RUNNING, "base.rvl")
    return ir, manifest_wire(ir)


def _reference(source: str, manifest: dict) -> tuple[str, str]:
    """`(tag, message)` for the reference's answer to `admit_into`, in the
    gate's own guarantee vocabulary. `("", "")` when the reference admits; a
    tag prefixed `OUT:` when the refusal is one this gate does not claim.

    The classifier is `tests/test_selfhost_lower.py`'s, imported rather than
    copied: two copies would be free to drift into disagreeing about what the
    gate even claims to decide.
    """
    try:
        compile_source(source, "candidate.rvl", manifest=dict(manifest))
    except RevlError as error:
        return (lower_oracle._classify(error), error.message)
    return ("", "")


def _native(fn, source: str, wire: str) -> tuple[str, str]:
    """`(tag, message)` for the native verdict wire (`""` | `"<TAG>|<msg>"`)."""
    verdict = fn(source, wire)
    if verdict == "":
        return ("", "")
    tag, _, message = verdict.partition("|")
    return (tag, message)


# --------------------------------------------------------------- the corpus
#
# The (running, candidate) pairs docs/design/457 section 5 names for T5, plus
# the two the wire's SIGNATURE half exists for. Each is a candidate compiled
# against the ONE running composition above; the name is what the frontier list
# and the failure messages call it.

_SAME_NAME_REPLACEMENT = """
component Kv provides store: Store {
  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(key) = key
    fn bump(n) = n
    fn put(key, value) = value
  }
}
"""

_PER_REALM_CONFLICT = """
service Cache { fn lookup(key: Str) -> Str }
component Rival provides store: Cache {
  provide store { fn lookup(key) = key }
}
"""

_WRONG_ARGUMENT_TYPE = """
service Cache { fn lookup(key: Str) -> Str }
component CacheTyped requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.bump(key) }
}
"""

_RIGHT_ARGUMENT_TYPE = """
service Cache { fn lookup(key: Str) -> Str }
component CacheTyped requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"""

_UNKNOWN_SERVICE = """
service Cache { fn lookup(key: Str) -> Str }
component CacheOrphan requires vault: Vault provides cache: Cache {
  provide cache { fn lookup(key) = key }
}
"""

CANDIDATES: dict[str, str] = {
    "cache_layer": al.CANDIDATE,
    "standalone_twin": al.CANDIDATE_STANDALONE,
    "calls_missing_method": harness._CALLS_MISSING_METHOD,
    "redeclare_running_service": harness._REDECLARE_RUNNING,
    "incomplete_provide": harness._INCOMPLETE_PROVIDE,
    "hole_draft": harness._HOLE_DRAFT,
    "syntax_error": harness._SYNTAX_ERROR,
    "same_name_replacement": _SAME_NAME_REPLACEMENT,
    "per_realm_conflict": _PER_REALM_CONFLICT,
    "wrong_argument_type": _WRONG_ARGUMENT_TYPE,
    "right_argument_type": _RIGHT_ARGUMENT_TYPE,
    "unknown_service": _UNKNOWN_SERVICE,
}

#: The pairs where the reference refuses and the native gate raises NO
#: objection — the measured frontier of the manifest arm, each with the layer
#: it is waiting on. Under-refusal is the direction this gate is allowed to err
#: in; being unable to NAME where it under-refuses is not, which is why this is
#: a list with reasons rather than a tolerance.
#:
#: A no-objection here is NOT an admission: the verdict wire carries
#: `"admitted": false` on every arm (`crates/revl-gate/src/lib.rs`), so an
#: embedder reads "the layers this tier runs raised nothing", never a green.
WITHHELD: dict[str, str] = {
    # `_admit_service_replacement`'s section-5 compatibility relation on a
    # redeclared running service. The wire carries the running surface, but the
    # relation itself is not ported; docs/design/457 T6.
    "redeclare_running_service":
        "the service-replacement compatibility relation is not ported",
    # `provision `two` is missing method `b`` — the provide-method completeness
    # rule over the candidate's OWN service. Not a manifest question at all;
    # docs/design/457 T4.
    "incomplete_provide":
        "provide-method completeness is the T4 provide-method slice",
    # The A1 HOLE draft. The reference's `refuse_admission` (T3) refuses a
    # candidate carrying an open typed hole; holes are a named family of the
    # crate's frontier and are not ported. Standalone and against the manifest
    # alike — the manifest is not what this one is waiting on.
    "hole_draft":
        "the holes family (T3 `refuse_admission`) is not ported",
    # A component header the reference's parser refuses outright. The native
    # parser recovers from this particular shape, so the BAD family reaches it
    # and the gate raises no objection. Standalone too: the manifest changes
    # nothing about it.
    "syntax_error":
        "the BAD parse family does not reach this component-header shape",
}


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_no_native_refusal_is_absent_from_the_reference(name, native, running):
    """ZERO TOLERANCE, the direction a gate may not err in: wherever the native
    manifest arm REFUSES, the reference refuses the identical bytes against the
    identical running composition, with the same tag — and, for a tag this gate
    spells in full, the same sentence.

    An out-of-slice reference refusal (`OUT:`) still counts as a refusal here:
    the native gate landing on one means it found the same defect the reference
    did, and only the tag vocabulary differs. What may never happen is a native
    refusal the reference does not make at all.
    """
    ir, wire = running
    source = CANDIDATES[name]
    tag, message = _native(native["admit_ambient"], source, wire)
    if tag == "":
        return
    ref_tag, ref_message = _reference(source, ir)
    assert ref_tag != "", (
        f"{name}: the native manifest arm refuses {tag}|{message!r}, and the "
        f"reference `admit_into` ADMITS the same bytes — a false reject")
    if ref_tag.startswith("OUT:"):
        return
    assert tag == ref_tag, (
        f"{name}: native tag {tag!r} vs reference {ref_tag!r}\n"
        f"native : {message}\nreference: {ref_message}")
    assert message == ref_message, (
        f"{name}: same tag {tag!r}, different sentence\n"
        f"native : {message}\nreference: {ref_message}")


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_the_frontier_list_is_honest(name, native, running):
    """The other half of the same statement: a pair the reference refuses and
    the native gate does not must be NAMED in `WITHHELD`, and a named pair that
    has started agreeing must be struck from it.

    This is what keeps the manifest arm's frontier a measurement. A slice that
    closes one of these deletes its line; a slice that opens a new hole cannot
    land without writing the hole down.
    """
    ir, wire = running
    source = CANDIDATES[name]
    tag, _ = _native(native["admit_ambient"], source, wire)
    ref_tag, ref_message = _reference(source, ir)
    silent = tag == "" and ref_tag != ""
    if silent:
        assert name in WITHHELD, (
            f"{name}: the reference refuses ({ref_tag}) and the native "
            f"manifest arm raises no objection, and the frontier list does not "
            f"say so: {ref_message}")
    else:
        assert name not in WITHHELD, (
            f"{name}: listed as a frontier entry, but the native arm no longer "
            f"withholds ({tag or 'admits'}) — strike it from WITHHELD")


def test_the_running_signature_is_read_rather_than_ignored(native, running):
    """NON-VACUITY for the half this slice adds: the running service's declared
    PARAMETER TYPES are read, and a call through a requirement is typed against
    them.

    The pair is the evidence. Both candidates call an operation the running
    `Store` declares, through a requirement the running composition resolves;
    one hands it the declared type and one does not. A gate that read only the
    wire's operation NAMES — which is every wire before this slice — answers
    the same thing about both, so agreeing with the reference on the pair is a
    claim a name-only gate cannot make.
    """
    ir, wire = running
    bad = _native(native["admit_ambient"], _WRONG_ARGUMENT_TYPE, wire)
    assert bad == ("T1", "`store.bump` argument `n` expects `Int`, got `Str`")
    assert bad == _reference(_WRONG_ARGUMENT_TYPE, ir), (
        "the native sentence must be the reference's own, byte for byte")

    good = _native(native["admit_ambient"], _RIGHT_ARGUMENT_TYPE, wire)
    assert good == ("", ""), (
        f"the well-typed twin must raise no objection, got {good}")

    # ... and the public verb agrees about both, which is the claim an embedder
    # actually reads. Comparing against a re-derivation of the call is not the
    # same claim as comparing against the call.
    assert py_gate.admit_into(_WRONG_ARGUMENT_TYPE, ir).admitted is False
    assert py_gate.admit_into(_RIGHT_ARGUMENT_TYPE, ir).admitted is True


def test_the_wire_carries_the_signature_it_claims_to(running):
    """The renderer's half, pinned on the bytes: the running `Store`'s row
    carries each operation's declared parameter list, names and types, and —
    for a PLAIN operation — its declared return (issue #346, docs/design/457
    T6).

    `put` is the contrast that makes the return a claim rather than a default:
    it is an `emission`, so its return is WITHHELD and its token stops at the
    parameter list. Every marking `lower.py` can stamp on a service operation
    withholds the same way, because the wire has no spelling for any of them
    and a reader deciding a call against a declaration that lost its marking
    would be deciding it against a declaration nobody sent.
    """
    _, wire = running
    rows = wire.split(";")
    assert "!services" in rows, "the exhaustiveness header is the claim"
    assert ":Store,get(key:Str):Str,bump(n:Int):Int,put(key:Str|value:Str)" \
        in rows
    assert ":AppSvc,ping():Str" in rows, (
        "an operation with no parameter is `op()`, and its return is still a "
        "claim")


def test_a_signature_the_wire_cannot_spell_is_withheld_not_mangled():
    """The renderer WITHHOLDS a parameter list whose text would need one of the
    wire's own structural characters, rather than escaping it or emitting a
    list that lost a parameter to the split.

    `Map[Str, Int]` is the real case: its comma is the operation separator. The
    row then carries the bare operation NAME, which the fold reads as silence
    about the arguments — the gate decides membership and nothing more.
    """
    source = """
service Bulk { fn load(rows: Map[Str, Int], tag: Str) -> Str }
component Loader provides bulk: Bulk {
  provide bulk { fn load(rows, tag) = tag }
}
"""
    wire = manifest_wire(compile_source(source, "bulk.rvl"))
    assert ":Bulk,load" in wire.split(";"), (
        f"the comma-bearing signature must be withheld whole: {wire}")


def test_the_empty_manifest_is_the_standalone_question(native):
    """`admit_into(src, "")` is `admit(src)` byte for byte, on every candidate
    in the corpus. The manifest parameter may only ever ADD context."""
    for name, source in sorted(CANDIDATES.items()):
        assert native["admit_ambient"](source, "") == native["admit_src"](source), name


# ------------------------------------------------------------- the generator
#
# The named pairs above are shapes a person chose. The draw below is the same
# question asked over a space nobody curated: a running service declaring one
# operation of a drawn parameter type, and a candidate calling it with a drawn
# literal. Roughly a quarter of the draws are well typed and the rest are not,
# so the oracle sees both directions rather than only the refusing one.

_TYPES = ("Str", "Int", "Bool", "Float")
_LITERALS = {"Str": '"s"', "Int": "1", "Bool": "true", "Float": "1.5"}


def _drawn_pair(rng: random.Random) -> tuple[str, str]:
    """`(running source, candidate source)` for one draw."""
    declared = rng.choice(_TYPES)
    passed = rng.choice(_TYPES)
    param = rng.choice(("key", "n", "value", "amount"))
    running = (
        f"service Drawn {{ fn op({param}: {declared}) -> Str }}\n"
        f"component Holder provides slot: Drawn {{\n"
        f"  provide slot {{ fn op({param}) = \"x\" }}\n"
        f"}}\n")
    candidate = (
        "service Drawee { fn ask(a: Str) -> Str }\n"
        "component Asker requires slot: Drawn provides ask: Drawee {\n"
        f"  provide ask {{ fn ask(a) = slot.op({_LITERALS[passed]}) }}\n"
        "}\n")
    return running, candidate


@pytest.mark.parametrize("seed", range(40))
def test_drawn_pairs_never_disagree(seed, native):
    """Over a drawn (running, candidate) pair: the native manifest arm either
    raises no objection, or refuses exactly what the reference refuses, with
    the reference's own tag and sentence. Never a refusal the reference does
    not make; never a different sentence under the same tag."""
    rng = random.Random(seed)
    running_src, candidate = _drawn_pair(rng)
    ir = compile_source(running_src, "drawn.rvl")
    wire = manifest_wire(ir)
    tag, message = _native(native["admit_ambient"], candidate, wire)
    ref_tag, ref_message = _reference(candidate, ir)
    if tag == "":
        return
    assert ref_tag != "", (
        f"seed {seed}: native refuses {tag}|{message!r}, reference admits\n"
        f"running:\n{running_src}\ncandidate:\n{candidate}")
    if ref_tag.startswith("OUT:"):
        return
    assert (tag, message) == (ref_tag, ref_message), (
        f"seed {seed}: native {tag}|{message!r} vs reference "
        f"{ref_tag}|{ref_message!r}\nrunning:\n{running_src}\n"
        f"candidate:\n{candidate}")


def test_the_draw_reaches_both_answers(native):
    """The draw above is only evidence if it produces refusals AND admissions.
    A generator that happened to draw one side would pass vacuously."""
    refused = admitted = 0
    for seed in range(40):
        running_src, candidate = _drawn_pair(random.Random(seed))
        wire = manifest_wire(compile_source(running_src, "drawn.rvl"))
        if native["admit_ambient"](candidate, wire) == "":
            admitted += 1
        else:
            refused += 1
    assert refused >= 5 and admitted >= 5, (
        f"the draw is one-sided: {refused} refused, {admitted} admitted")
