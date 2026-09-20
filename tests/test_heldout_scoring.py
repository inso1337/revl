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


def _tracked():
    """The tracked tree, as a set of repo-relative paths."""
    done = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return set(done.stdout.splitlines())


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


def test_a_change_to_a_fenced_file_selects_this_file(path=None):
    """The fence's own test has to RUN when a fenced file moves.

    This is how issue #1307 reached main. `tools/gate_reference_census.py`
    began naming `tools/corpus_provenance.py`, the pre-merge selector mapped
    that file to the census and provenance tests, and neither of those holds
    the classification. The test that does was never selected, so the change
    landed and main was red on a path no gate had run.

    Derived on both sides: the selector reads `HELD_OUT_FENCE` out of the tool
    by AST, and this holds every entry of the same tuple.
    """
    spec = importlib.util.spec_from_file_location(
        "affected_tests_under_heldout", ROOT / "tools" / "affected_tests.py")
    affected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(affected)

    assert affected.held_out_fence(ROOT) == frozenset(heldout.HELD_OUT_FENCE)

    missed, targeted = [], []
    for fenced in heldout.HELD_OUT_FENCE:
        chosen = affected.select([fenced], ROOT)
        if chosen["full"]:
            continue  # FULL runs it too, just not narrowly.
        targeted.append(fenced)
        if "tests/test_heldout_scoring.py" not in set(chosen["pytest"]):
            missed.append(fenced)
    assert not missed, (
        "changing these fenced files does not select the test that holds the "
        "fence: " + ", ".join(missed))
    # Not vacuous: a fence whose every entry fell back to FULL would satisfy
    # the loop above without the selector ever naming this file.
    assert targeted, "no fenced file produced a targeted selection"


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

    "Repo file" means TRACKED file. The probe reports everything it loaded
    from under `ROOT`, and a checkout whose virtualenv lives in it has
    `site-packages` under `ROOT` too -- the probe imports
    `tests/test_selfhost_lower.py`, which imports pytest, so that developer
    gets a hundred `site-packages` paths reported as holes in the fence. They
    are not holes. The fence is about what a candidate can read and change,
    and that is the tracked tree: the same definition `seed_is_in_tree` uses,
    for the same reason.
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

    tracked = _tracked()
    loaded = [f for f in payload["files"] if f in tracked]
    assert loaded, "the probe reported no tracked repo file at all"

    unclassified = [f for f in loaded if heldout.classify(f) is None]
    assert not unclassified, (
        "a scoring run loads repo files in no category, so the fence has a "
        "hole: " + ", ".join(unclassified))
    # The fence has to be reached, or "classified" would be satisfied by a
    # closure that happens to be all subject.
    assert any(heldout.classify(f) == "fence" for f in loaded)
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
    joins out of the fenced files instead, so a newly named repo path has to
    be classified before this passes.

    The files scanned are DERIVED from `HELD_OUT_FENCE`. They used to be a
    hand-written pair naming two of the five, and a path first named in
    `tools/build_gate_crate.py` was therefore scanned by nothing: the same
    drift this test exists to catch, one level up. A fence entry this scanner
    cannot parse is a refusal rather than a silent skip.
    """
    unscannable = [p for p in heldout.HELD_OUT_FENCE if not p.endswith(".py")]
    assert not unscannable, (
        "a fenced file this scanner cannot read, so paths it names are held "
        "by nothing: " + ", ".join(unscannable))
    named = set()
    for tool in heldout.HELD_OUT_FENCE:
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
