"""`tools/heldout_scoring.py`: the scoring set a candidate cannot read.

WHAT THESE HOLD, and why each one is here rather than in prose.

The property is that at least one artifact scoring a candidate is one the
candidate cannot read. A repository cannot hide a file from a process that has
the repository, so the claim has to be carried by checks rather than by a
convention:

  * the draw is not a file           -- `test_a_scoring_run_writes_nothing...`
  * the seed is not in the tree      -- `test_a_seed_that_is_in_the_tree...`
  * the diff cannot reach the scorer -- `test_a_diff_that_reaches_the_fence...`
  * every unknown refuses            -- the four `..._refuses` cases
  * the gate can actually fail       -- `test_a_certifier_that_admits...`
  * the draw is not vacuous          -- `test_a_draw_that_reaches_no...`

The last two are the ones that keep this from becoming the sixth check in this
repository that runs on every pull request and cannot fail. One mutates the
subject and asserts the draw catches it; the other asserts that a run whose
draw never reached the guard REFUSES instead of reporting clean.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "heldout_scoring_under_test", ROOT / "tools" / "heldout_scoring.py")
heldout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(heldout)

# The fixture seed, and the first thing this file has to be honest about.
#
# It is the DIGEST of a phrase rather than the phrase, for two reasons that
# pull in opposite directions. Deterministic, so a red in CI is reproducible
# from the source rather than from a number nobody kept. Absent from the
# tracked tree, so `seed_is_in_tree` does not refuse it -- which is the whole
# mechanism working on its own test suite, since a literal seed committed here
# would be, correctly, refused.
#
# It is NOT held out from anything. A reader with this file recomputes it in a
# line. A test proves the mechanism; it does not perform a scoring run, and the
# `seed_source` field in the verdict record is where a reader tells the two
# apart.
TEST_SEED = hashlib.sha256(b"heldout-scoring slice 1 fixture").hexdigest()[:24]

# A short, ordinary draw: large enough to carry near misses of several
# families, small enough that the mutation cases below stay cheap.
DRAW = 60


@pytest.fixture(scope="module")
def census():
    return heldout.load_census()


@pytest.fixture(scope="module")
def engine(census):
    """The fast gate, built once: it compiles `selfhost/lower.rvl`."""
    return census.SelfhostEngine()


@pytest.fixture(scope="module")
def reference(census):
    ref, _oracle = census._reference()
    return ref


# --------------------------------------------------------------- the draw

def test_the_draw_is_a_pure_function_of_the_seed():
    a = heldout.draw(TEST_SEED, DRAW)
    b = heldout.draw(TEST_SEED, DRAW)
    assert heldout.draw_digest(a) == heldout.draw_digest(b)
    assert len(a) == DRAW


def test_a_different_seed_is_a_different_draw():
    a = heldout.draw(TEST_SEED, DRAW)
    b = heldout.draw(TEST_SEED[::-1], DRAW)
    assert heldout.draw_digest(a) != heldout.draw_digest(b)
    # Not merely a different digest: the programs themselves differ, so the
    # seed is choosing the set rather than only its labelling.
    assert {src for _, src in a} != {src for _, src in b}


def test_every_near_miss_family_is_reachable_from_some_seed():
    """A family that stopped being generated would narrow the draw silently."""
    seen = set()
    for salt in range(12):
        for case_id, _src in heldout.draw(TEST_SEED + str(salt), 60):
            if case_id.startswith("near:"):
                seen.add(case_id.split(":")[1])
    assert seen == set(heldout.NEAR_MISS_FAMILIES)


def test_no_generated_word_is_a_keyword():
    """A generated identifier that is a keyword is a parse error wearing the
    label `inside:`.

    The first draft of the word list carried `emit`, and eight of two hundred
    programs that claimed to sit inside the admission surface were parse errors
    instead. Held against the reference lexer's own table rather than a copy.
    """
    from revl.lexer import KEYWORDS

    assert not set(heldout._WORDS) & set(KEYWORDS)


def test_the_reference_admits_every_program_the_draw_calls_inside(reference):
    """The grammar's claim, checked rather than asserted.

    `inside:` means "inside the admission surface", and the admission surface
    is a region the REFERENCE admits. A draw whose inside half the reference
    refuses is measuring something other than what it says, and the tool
    refuses such a draw -- this is the same property on a wider sample than a
    single run pays for.
    """
    refused = []
    for case_id, src in heldout.draw(TEST_SEED, 200):
        if not case_id.startswith("inside:"):
            continue
        tag, message = reference(src)
        if tag:
            refused.append((case_id, tag, message))
    assert not refused, refused[:5]


def test_a_draw_whose_inside_half_is_refused_is_itself_refused(
        engine, census, reference):
    """The generator-defect direction is a refusal, not a finding.

    A program the generator labelled `inside:` that the reference refuses says
    the draw is not the set it claims to be. Mutating the reference into one
    that refuses everything is the cheapest way to produce that shape.
    """
    record, status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py"], census=census, engine=engine,
        reference=lambda src: ("OUT:mutated reference", "mutated"))
    assert status == heldout.REFUSED
    assert record["refusal"] == "draw-inside-refused-by-reference"


def test_a_scoring_run_writes_nothing_into_the_tree(engine, census, reference):
    """The draw must not become a file, or the next candidate reads it.

    `tests/fixtures/emit_py_corpus/` is globbed, and the census walks eight
    directories with `rglob`; a generated program dropped in either becomes a
    permanent, readable member of every corpus. This holds the whole tree
    rather than those directories, because the cheap mistake is writing
    somewhere nobody thought to check.
    """
    before = _porcelain()
    record, _status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py"], census=census, engine=engine,
        reference=reference)
    assert record["verdict"] == "clean"
    assert _porcelain() == before


def _porcelain():
    done = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


# ------------------------------------------------------- the failure direction

def _run(**kw):
    base = dict(argv_seed=TEST_SEED, env={}, count=DRAW,
                changed=["src/revl/lower.py"])
    base.update(kw)
    return heldout.run(**base)


def test_a_missing_seed_refuses_and_scores_nothing():
    record, status = _run(argv_seed="", env={})
    assert status == heldout.REFUSED
    assert record["verdict"] == "refused"
    assert record["refusal"] == "no-seed"
    assert record["buckets"] == {}


def test_a_short_seed_refuses():
    record, status = _run(argv_seed="abc")
    assert status == heldout.REFUSED
    assert record["refusal"] == "seed-too-short"


def test_the_fixture_seed_is_itself_absent_from_the_tree():
    """The suite eats its own cooking.

    A literal seed committed in this file would be refused by the tool, and
    correctly so. The fixture seed is a digest of a phrase for exactly that
    reason, and this holds the property rather than leaving it to a comment
    somebody edits away.
    """
    assert len(TEST_SEED) >= heldout.MIN_SEED_CHARS
    assert not heldout.seed_is_in_tree(TEST_SEED, ROOT)


def test_a_seed_that_is_in_the_tree_refuses():
    """The enforcement that makes "cannot read it" a check.

    The control is the other half: the same call with a seed that is not in
    the tree does not refuse for this reason, so the refusal is discriminating
    rather than unconditional.
    """
    in_tree = "gate_reference_census.py"
    assert len(in_tree) >= heldout.MIN_SEED_CHARS
    record, status = _run(argv_seed=in_tree)
    assert status == heldout.REFUSED
    assert record["refusal"] == "seed-in-tree"

    control, _ = _run(argv_seed=TEST_SEED, census=_BrokenCensus())
    assert control["refusal"] != "seed-in-tree"


class _BrokenCensus:
    """Stands in for the scorer where a test needs to get past the seed and
    fence checks without paying for the real engine."""

    def _reference(self):
        raise RuntimeError("not built for this test")

    def SelfhostEngine(self):  # noqa: N802 - mirrors the census class name
        raise RuntimeError("not built for this test")


def test_an_engine_that_will_not_build_refuses_rather_than_scoring():
    record, status = _run(census=_BrokenCensus())
    assert status == heldout.REFUSED
    assert record["refusal"].startswith("engine-unavailable:")
    assert record["buckets"] == {}


def test_a_missing_diff_refuses():
    """A run that does not know what the candidate changed cannot claim the
    candidate could not reach the scorer."""
    record, status = _run(changed=None)
    assert status == heldout.REFUSED
    assert record["refusal"] == "no-diff"


@pytest.mark.parametrize("path", heldout.HELD_OUT_FENCE)
def test_a_diff_that_reaches_the_fence_refuses(path):
    record, status = _run(changed=["src/revl/lower.py", path])
    assert status == heldout.REFUSED
    assert record["refusal"] == "diff-reaches-fence:" + path


def test_a_diff_that_stays_off_the_fence_is_scored(engine, census, reference):
    """The control for the case above: the fence refuses the scorer's own
    paths and nothing else, so an ordinary compiler change is still judged."""
    record, status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py", "selfhost/lower.rvl",
                 "crates/revl-gate/src/lib.rs", "docs/v2.0-roadmap.md"],
        census=census, engine=engine, reference=reference)
    assert status == heldout.OK
    assert record["verdict"] == "clean"


def test_the_fence_is_not_the_subject():
    """Fencing the compiler would forbid the work this loop exists to produce.

    Stated as a test because it is the mistake a later widening of the fence
    would make: every path a candidate must be able to change has to stay off
    it, and the fence has to stay a named list of scorer files rather than a
    directory that swallows the subject.
    """
    for path in heldout.HELD_OUT_FENCE:
        assert heldout.classify(path) == "fence"
    for path in ("src/revl/lower.py", "selfhost/lower.rvl",
                 "backends/python/emit.py", "crates/revl-gate/src/lib.rs",
                 "tests/test_gate_crate_admit.py"):
        assert heldout.classify(path) == "subject"
        assert not heldout.fence_verdict([path])


def test_every_fenced_path_exists():
    """A fence entry that is a typo fences nothing at all."""
    for path in heldout.HELD_OUT_FENCE + heldout.DATA_INPUTS \
            + heldout.SCORING_UNREACHED:
        assert (ROOT / path).exists(), path


# ------------------------------------------------------- non-vacuity

def test_a_certifier_that_admits_everything_is_caught_by_the_draw(
        engine, census, reference):
    """The check can fail, demonstrated rather than asserted.

    The subject is mutated -- the gate's admission certifier is replaced by one
    that certifies every program -- and the held-out draw has to notice. A
    weakened certifier issues an admission for the near misses the reference
    refuses, which is the `false-admission` bucket: the one class the census
    gives no allowance at all.

    The control is the unmutated engine in `test_a_diff_that_stays_off_the
    _fence_is_scored`, which is clean on the same seed and the same draw.
    """
    original = engine._certify
    engine._certify = lambda src: True
    try:
        record, status = heldout.run(
            argv_seed=TEST_SEED, env={}, count=DRAW,
            changed=["src/revl/lower.py"], census=census, engine=engine,
            reference=reference)
    finally:
        engine._certify = original

    assert status == heldout.DIVERGENT
    assert record["verdict"] == "divergent"
    caught = record["zero_tolerance"]["false-admission"]
    assert caught, "a certifier that admits everything went unnoticed"
    assert all(case_id.startswith("near:") for case_id in caught)


def test_a_draw_that_reaches_no_admission_arm_refuses(
        engine, census, reference):
    """A clean verdict on a draw that never exercised the guard is a vacuum.

    `agree-admit` conflates an issued admission with a bare no-objection, so
    an empty `false-admission` bucket alone does not distinguish a sound gate
    from a guard that never ran. A certifier that admits nothing produces
    exactly that shape, and the tool has to refuse rather than report clean.
    """
    original = engine._certify
    engine._certify = lambda src: False
    try:
        record, status = heldout.run(
            argv_seed=TEST_SEED, env={}, count=DRAW,
            changed=["src/revl/lower.py"], census=census, engine=engine,
            reference=reference)
    finally:
        engine._certify = original

    assert status == heldout.REFUSED
    assert record["refusal"] == "draw-reached-no-admission-arm"


def test_the_liveness_counters_are_non_zero_on_the_real_gate(
        engine, census, reference):
    """The measurement behind the two refusals above, recorded as a number so
    a later narrowing of the grammar shows up here."""
    cases = heldout.draw(TEST_SEED, DRAW)
    _buckets, liveness = heldout.score(
        cases, census, engine=engine, reference=reference)
    assert liveness["issued_admissions"] > 0
    assert liveness["near_miss_reference_refusals"] > 0


# ------------------------------------------------- the fence is complete

def test_the_whole_import_closure_of_a_scoring_run_is_classified():
    """Every in-repo file a scoring run loads is fence or subject, never neither.

    A fence is only worth the paths it names: if the draw or the verdict
    depended on a file outside it, a candidate would edit that file instead.
    The run happens in a subprocess so the closure is the tool's own and not
    pytest's, and the assertion is on the closure rather than on a list
    somebody remembered to update.
    """
    code = _CLOSURE_PROBE % (json.dumps(str(ROOT)), json.dumps(TEST_SEED))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "tests"), env.get("PYTHONPATH", "")])
    env["PYTHONWARNINGS"] = "ignore"
    done = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=900,
                          env=env, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr[-4000:]
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert payload["verdict"] == "clean", payload

    unclassified = [f for f in payload["files"] if heldout.classify(f) is None]
    assert not unclassified, (
        "a scoring run loads repo files in no category, so the fence has a "
        "hole: " + ", ".join(unclassified))
    # The fence has to be reached, or "classified" would be satisfied by a
    # closure that happens to be all subject.
    assert any(heldout.classify(f) == "fence" for f in payload["files"])
    for path in heldout.DATA_INPUTS:
        assert heldout.classify(path) is not None, path


_CLOSURE_PROBE = r'''
import importlib.util, json, os, sys, warnings
warnings.filterwarnings("ignore")
ROOT = %s
SEED = %s
spec = importlib.util.spec_from_file_location(
    "heldout_probe", os.path.join(ROOT, "tools", "heldout_scoring.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
record, status = mod.run(argv_seed=SEED, env={}, count=20,
                         changed=["src/revl/lower.py"])
real = os.path.realpath(ROOT) + os.sep
files = set()
for loaded in list(sys.modules.values()):
    path = getattr(loaded, "__file__", None)
    if not path:
        continue
    path = os.path.realpath(path)
    if path.startswith(real):
        files.add(os.path.relpath(path, os.path.realpath(ROOT)))
print(json.dumps({"verdict": record["verdict"], "files": sorted(files)}))
'''


def test_every_repo_path_the_scorer_names_is_classified():
    """The static half of the closure check.

    `sys.modules` cannot see a file loaded through `spec_from_file_location`
    without being registered, which is how the census loads the crate
    generator and the python emitter. This reads the literal `ROOT / "..."`
    joins out of the two fenced tools instead, so a newly named repo path has
    to be classified before this passes.
    """
    named = set()
    for tool in ("tools/heldout_scoring.py", "tools/gate_reference_census.py"):
        named |= _root_joins(ROOT / tool)
    assert named, "the scanner found no ROOT-relative path at all"
    unclassified = sorted(p for p in named if heldout.classify(p) is None)
    assert not unclassified, (
        "the scorer names repo paths in no category: "
        + ", ".join(unclassified))


def _root_joins(path: Path):
    """Repo-relative paths spelled `ROOT / "a" / "b"` in `path`.

    Only the MAXIMAL join of each chain: `ROOT / "tools" / "x.py"` contains
    `ROOT / "tools"` as a sub-expression, and reporting the directory as well
    as the file would make the classification ask about a prefix nobody wrote.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inner = {
        id(node.left) for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    }
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if id(node) in inner:
            continue
        parts = []
        cursor = node
        while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
            if not isinstance(cursor.right, ast.Constant) \
                    or not isinstance(cursor.right.value, str):
                parts = []
                break
            parts.append(cursor.right.value)
            cursor = cursor.left
        if not parts or not (isinstance(cursor, ast.Name)
                             and cursor.id == "ROOT"):
            continue
        out.add("/".join(reversed(parts)))
    return out


# --------------------------------------------------------------------------
# Slice 2: the bypass direction, capped by the baseline's families.
# --------------------------------------------------------------------------

def test_the_allowance_is_the_censuss_own_baseline_families():
    """Read from the baseline file, never copied into the scorer.

    A second list would drift from the one the census publishes, and the claim
    this tool makes is exactly "no `false-admit` family beyond the ones the
    repository has already recorded and somebody has read".
    """
    allowance = heldout.bypass_allowance(ROOT)
    blob = json.loads(
        (ROOT / "tools" / "gate_reference_census_baseline.json").read_text())
    expected = {name.split("/", 1)[1] for name in blob["buckets"]
                if name.split("/", 1)[0] == "false-admit"}
    assert allowance == expected
    assert allowance, "an empty allowance would make this test vacuous"


def test_the_bypass_tag_split_does_not_catch_false_admission():
    """`false-admission` carries `false-admit` as a string prefix.

    The census's own helper documents this trap; reading the bypass families
    with `startswith` would fold the zero-tolerance bucket into the bucket that
    HAS an allowance, which is the one place a prefix bug would be silent and
    would matter.
    """
    assert heldout._bypass_tag("false-admission") is None
    assert heldout._bypass_tag("false-admit/T1") == "T1"
    assert heldout._bypass_tag("false-admit") is None
    assert heldout.bypass_families({"false-admission": ["a"],
                                    "false-admit/T1": ["b"]}) == {"T1": ["b"]}


def test_an_unreadable_allowance_refuses_rather_than_choosing_a_default(
        tmp_path):
    """Neither "allow nothing" nor "allow everything" is an honest default.

    The first fails every candidate on a gap the repository already recorded;
    the second is the fail-open shape. A scorer that cannot find its allowance
    declines to answer instead.
    """
    with pytest.raises(heldout.Refusal) as caught:
        heldout.bypass_allowance(tmp_path)
    assert caught.value.reason.startswith("bypass-allowance-unavailable:")

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "gate_reference_census_baseline.json").write_text(
        json.dumps({"note": "no buckets key"}), encoding="utf-8")
    with pytest.raises(heldout.Refusal) as caught:
        heldout.bypass_allowance(tmp_path)
    assert caught.value.reason == "bypass-allowance-unavailable:no-buckets"


def test_the_baseline_is_on_the_fence_now_that_the_scorer_reads_it():
    """A candidate that widens its own allowance is refused, not scored.

    Slice 1 listed this file as `SCORING_UNREACHED` because a scoring run never
    opened it. Slice 2 reads it, so it decides what counts as a divergence, and
    that is the rule the fence is drawn by.
    """
    path = "tools/gate_reference_census_baseline.json"
    assert heldout.classify(path) == "fence"
    assert path not in heldout.SCORING_UNREACHED
    record, status = _run(changed=["src/revl/lower.py", path])
    assert status == heldout.REFUSED
    assert record["refusal"] == "diff-reaches-fence:" + path


class _NoRefusalGate:
    """The real gate with its REFUSAL arm removed.

    `_admit` answers the empty wire for every program, which is the gate's own
    spelling of "no objection". The admission certifier is left alone, so the
    inside half still reaches the admission arm and the liveness refusals do
    not fire -- this mutation has to be caught by the score, not by a vacuum
    check that would catch any broken gate at all.
    """

    def __init__(self, real):
        self._real = real
        self._scan = real._scan
        self._certify = real._certify

    def verdicts(self, sources):
        for src in sources:
            reason = self._scan(src)
            if reason is not None:
                yield ("frontier", reason)
            elif self._certify(src):
                yield ("admitted", ("", ""))
            else:
                yield ("no_objection", "")


def _in_slice(reference, tag):
    """`reference` with its refusals relabelled IN-SLICE under `tag`.

    Needed because of the measurement in
    `test_the_bypass_direction_is_not_reachable_on_this_grammar`: every
    refusal the real reference issues over this grammar is `OUT:`, and
    `bucket()` routes a no-objection against an `OUT:` refusal to
    `no-objection-out-of-slice` rather than to `false-admit`. So the predicate
    cannot be exercised on the real pair at all, and a test that pretended
    otherwise would be measuring nothing.
    """
    def relabelled(src):
        found, message = reference(src)
        return (tag, message) if found else (found, message)
    return relabelled


def test_a_gate_that_stops_refusing_is_caught_as_a_new_bypass_family(
        engine, census, reference):
    """The non-vacuity evidence for the bypass direction.

    A gate whose refusal arm is gone raises no objection to the near misses the
    reference refuses, which is `false-admit/<tag>`. The tag here is one the
    census baseline does NOT carry, so it is a bypass family nobody has read,
    found on programs nobody could read, and it has to be a divergence.

    The control is the case below, identical but for a tag the baseline
    already allows, which is clean on the same seed and the same draw.
    """
    record, status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py"], census=census,
        engine=_NoRefusalGate(engine),
        reference=_in_slice(reference, "T9NEW"))

    assert status == heldout.DIVERGENT, record
    new_families = record["bypass"]["new_families"]
    assert set(new_families) == {"T9NEW"}, record["bypass"]
    assert not set(new_families) & set(record["bypass"]["allowance"])
    assert record["bypass"]["reachable"] is True


def test_the_bypass_direction_is_not_reachable_on_this_grammar(
        engine, census, reference):
    """The measurement that stops this becoming a check that cannot fail.

    Every refusal the reference issues over an `admission-surface/v1` draw is
    `OUT:` -- duplicate services, duplicate methods, unsatisfied provisions --
    so `bucket()` sends the whole direction to `no-objection-out-of-slice` and
    `false-admit` is unreachable however broken the gate is. The predicate is
    implemented and exercised above through an injected in-slice reference;
    what it does not yet have is a grammar that reaches it, and that is slice
    3's widening past the admission surface.

    Recorded as a test and as a field in the verdict rather than as a sentence
    in a design doc, because an empty bucket and an unreachable one look
    identical in a report and this repository has confused them before.
    """
    record, status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py"], census=census, engine=engine,
        reference=reference)
    assert status == heldout.OK
    assert record["liveness"]["in_slice_reference_refusals"] == 0
    assert record["bypass"]["reachable"] is False
    assert record["bypass"]["families"] == {}
    # The direction that IS live on this grammar, for contrast: the draw
    # carries near misses the reference refuses and the gate issues real
    # admissions, which is what `false-admission` needs to be able to fire.
    assert record["liveness"]["near_miss_reference_refusals"] > 0
    assert record["liveness"]["issued_admissions"] > 0
    assert "NOT REACHABLE" in heldout.render(record)


def test_a_family_already_in_the_baseline_is_reported_and_is_not_a_finding(
        engine, census, reference):
    """The other half, and the reason slice 1 left this direction out.

    A `false-admit` tag the baseline already carries is the known gap re-found
    on new programs. Reporting it as a divergence would red on the repository's
    own recorded allowance rather than on the candidate, so it is counted in
    `bypass.families` and kept out of `bypass.new_families`.
    """
    allowed = sorted(heldout.bypass_allowance(ROOT))[0]
    record, status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py"], census=census,
        engine=_NoRefusalGate(engine),
        reference=_in_slice(reference, allowed))

    assert record["bypass"]["families"].get(allowed), record["bypass"]
    assert not record["bypass"]["new_families"], record["bypass"]
    assert status == heldout.OK
    assert record["verdict"] == "clean"


# --------------------------------------------------------------------------
# Slice 3: the surface residual, measured instead of named.
#
# Design 2.1.4 -- "the candidate special-cases the grammar's surface" -- was
# the one attack slice 1 left with no test at all. These make it executable.
# A tell is a predicate over a program's bytes that a candidate could key a
# shortcut on, and two numbers come out of it that slice 1 did not separate:
# RECOGNITION (is this program generated) and SEPARATION (is this the program
# I am supposed to refuse). Only the second is closeable here, and it is.
# --------------------------------------------------------------------------

def _replant_slice1_names(cases):
    """The slice-1 draw, in the one respect that turned out to matter.

    Slice 1's `_near_miss` spelled every added name as a literal -- `twice`,
    `Box`, `Row`, `Mystery`, `Edge`, `Comp`, `dup` -- so ten fixed words
    appeared in near misses and in nothing else. This puts that property back
    without resurrecting the old generator, which is what a later edit
    reintroducing a fixed near-miss name would do.
    """
    out = []
    for case_id, src in cases:
        if case_id.startswith("near:"):
            src = src + "\n// " + " ".join(heldout._SLICE1_NEAR_MISS_NAMES) + "\n"
        out.append((case_id, src))
    return out


def test_no_named_tell_separates_the_draw_it_ships():
    """The enforced half. A tell that separates hands over the whole verdict.

    `near:` and `inside:` is the entire scoring question, so a predicate that
    answers it from the bytes lets a gate score clean with no correctness at
    all. Every named tell has to sit under the bound on the draw as shipped.
    """
    report = heldout.tell_report(heldout.draw(TEST_SEED, 200))
    assert set(report) == set(heldout.SURFACE_TELLS)
    over = {name: stats["separation"] for name, stats in report.items()
            if stats["separation"] > heldout.SEPARATION_BOUND}
    assert not over, over
    assert heldout.separating_tells(report) == []


def test_the_tells_that_separated_slice_ones_draw_are_measured_not_asserted():
    """The before number, so the after number means something.

    Two of the three named tells separated slice 1's draw, and the second one
    is the one nobody would have guessed: `closed-vocabulary` fired on the
    inside half and NOT on the near half, because every near miss carried a
    literal name that was not in the published word list. So "every identifier
    comes from `_WORDS`" was itself a near/inside discriminator.

    Both are driven under the bound by drawing the near-miss names from the
    same vocabulary as everything else. `recognition` goes UP when this is
    done, to 1.0, and that is the honest trade: the draw becomes more
    obviously generated and stops saying which half a program is in.
    """
    replanted = _replant_slice1_names(heldout.draw(TEST_SEED, 200))
    before = heldout.tell_report(replanted)
    after = heldout.tell_report(heldout.draw(TEST_SEED, 200))

    assert set(heldout.separating_tells(before)) == {
        "near-miss-name-literals", "closed-vocabulary"}
    for name in ("near-miss-name-literals", "closed-vocabulary"):
        assert before[name]["separation"] > 0.5, before[name]
        assert after[name]["separation"] <= heldout.SEPARATION_BOUND, after[name]

    # The residual, as a number rather than a sentence: the draw is still
    # perfectly recognisable AS a draw, and this mechanism cannot change that.
    assert after["closed-vocabulary"]["recognition"] == 1.0


def test_a_draw_a_tell_separates_is_refused_rather_than_scored(
        monkeypatch, engine, census, reference):
    """The enforcement, not just the measurement.

    A draw that answers its own question is not the set it claims to be, which
    is the treatment `draw-inside-refused-by-reference` already gives a draw
    that mislabels itself. This is the check that reds if a later edit puts a
    fixed near-miss name back, and it runs on every invocation rather than
    only in a suite the candidate's CI might not run.
    """
    monkeypatch.setattr(
        heldout, "draw",
        lambda seed, count: _replant_slice1_names(_real_draw(seed, count)))
    record, status = heldout.run(
        argv_seed=TEST_SEED, env={}, count=DRAW,
        changed=["src/revl/lower.py"], census=census, engine=engine,
        reference=reference)
    assert status == heldout.REFUSED
    assert record["refusal"].startswith("draw-separable-by-tell:")
    assert "near-miss-name-literals" in record["refusal"]
    assert record["buckets"] == {}


_real_draw = heldout.draw


class _ShortcutGate:
    """The attack of design 2.1.4, built rather than described.

    A gate that does no work at all: anything a candidate-readable predicate
    calls an inside program gets an ISSUED admission, everything else is
    declined at the frontier. It is wrong about every real program in the
    repository, and the question this answers is whether the draw notices.
    """

    def __init__(self, looks_inside):
        self._looks_inside = looks_inside

    def verdicts(self, sources):
        for src in sources:
            if self._looks_inside(src):
                yield ("admitted", ("", ""))
            else:
                yield ("frontier", "shortcut declined")


def test_the_surface_shortcut_scores_clean_on_a_draw_a_tell_separates(
        census, reference):
    """The attack succeeds when a tell separates. This is why the bound exists.

    `closed-vocabulary` separated slice 1's draw, so a gate that admits every
    program whose identifiers are all in the published word list and declines
    everything else scores CLEAN: it issues admissions for the whole inside
    half and never issues one for a near miss. It has no compiler in it.
    """
    replanted = _replant_slice1_names(_real_draw(TEST_SEED, 200))
    gate = _ShortcutGate(heldout._tell_closed_vocabulary)
    buckets, liveness = heldout.score(
        replanted, census, engine=gate, reference=reference)

    assert liveness["issued_admissions"] > 0
    assert not buckets.get("false-admission"), buckets
    assert not buckets.get("gate-fault"), buckets


def test_the_same_shortcut_is_caught_on_the_draw_that_ships(
        census, engine, reference):
    """The countermeasure, measured on the same attack.

    On the shipped draw the tell no longer separates -- it fires on every
    program, inside and near alike -- so the shortcut issues admissions for the
    near misses too, and every one of those the reference refuses is a
    `false-admission`, the bucket with no allowance at all.

    The control is the real gate on the same seed and the same draw, clean.
    """
    cases = _real_draw(TEST_SEED, 200)
    gate = _ShortcutGate(heldout._tell_closed_vocabulary)
    buckets, liveness = heldout.score(
        cases, census, engine=gate, reference=reference)

    caught = buckets.get("false-admission", [])
    assert caught, "the shortcut went unnoticed on the shipped draw"
    assert all(case_id.startswith("near:") for case_id in caught)
    assert liveness["issued_admissions"] > 0

    control_buckets, _ = heldout.score(
        cases, census, engine=engine, reference=reference)
    assert not control_buckets.get("false-admission"), control_buckets


def test_the_reserved_set_covers_every_lexer_keyword():
    """The bring-up invariant, kept as a check after the vocabulary moved.

    A generated identifier that is a keyword is a parse error wearing the
    label `inside:`; `emit` in the first word list made eight of two hundred
    programs exactly that. The tool holds a FROZEN copy of the table rather
    than importing it, so that `draw()` keeps reading no file at all and a
    candidate cannot narrow the draw by editing `src/revl/lexer.py` -- which
    is SUBJECT, and must stay editable. The copy is worth nothing unless it is
    held against the real table, which is this.
    """
    from revl.lexer import KEYWORDS

    missing = sorted(set(KEYWORDS) - set(heldout._RESERVED))
    assert not missing, (
        "the lexer grew a keyword the generator does not reserve: "
        + ", ".join(missing))


def test_a_reserved_word_never_leaves_the_generator():
    """The guard, exercised rather than trusted.

    `_ident` skips a reserved word instead of emitting it. Forcing the word
    list down to a single keyword is the cheapest way to make the guard the
    only thing standing between the draw and a parse error.
    """
    import random

    rng = random.Random(0)
    with pytest.raises(heldout.Refusal) as caught:
        with _patched(heldout, "_WORDS", ("emit",)):
            with _patched(heldout, "_SUFFIXES", ("",)):
                heldout._ident(rng, set())
    assert caught.value.reason == "draw-exhausted-identifiers"

    names = set()
    for case_id, src in _real_draw(TEST_SEED, 200):
        names |= set(heldout._identifiers(src))
    leaked = {n for n in names if n in heldout._RESERVED} - _STRUCTURAL_KEYWORDS
    assert not leaked, leaked


# The keywords a draw is SUPPOSED to contain: they are the declaration forms
# the grammar emits, not identifiers it chose.
_STRUCTURAL_KEYWORDS = {"type", "service", "fn", "component", "provides",
                        "provide", "return"}


class _patched:
    def __init__(self, obj, name, value):
        self._obj, self._name, self._value = obj, name, value

    def __enter__(self):
        self._old = getattr(self._obj, self._name)
        setattr(self._obj, self._name, self._value)
        return self._obj

    def __exit__(self, *exc):
        setattr(self._obj, self._name, self._old)
        return False
