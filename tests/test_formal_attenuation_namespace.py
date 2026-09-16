"""The two namespaces a crossing has in the formal differential harness.

`formal/harness/diff_corpus.py` exports one fact row per crossing and TWO
surfaces read it, in namespaces that are not the same:

  * the provide-method BOUND (`P`) measures an implementation against its own
    service's `emission[...]` declaration, and the reference names the crossing
    there by the WIRING KEY it went through — "`Cache.put` is declared
    `emission[db]`, but this implementation emits through `bus`";
  * the spawn ATTENUATION fold (`W`) compares a parent's grant against a
    child's demand ACROSS a component boundary. Two components wire the same
    boundary under whatever key each likes, so the element there is the
    DECLARED token (`lower._cap_keyed`), or the key in its own `key:` namespace
    where nothing declares one (`lower._wire_cap`).

Exporting both sides of the fold under the wiring key is a laundering hole:
`Supervisor requires kv: KvA` spawning `Leaker requires kv: KvB` reaches a
different boundary under the same spelling, and the fold saw `kv` on both sides
and derived nothing. The reference refuses that program under G4, so the model
AGREED with an implementation defect instead of catching it — the one failure
mode a differential oracle cannot report, because agreement looks like success.

This module runs in the plain `pytest tests/` job. `make formal` needs a Lean
toolchain and the python half of the harness had no test without one, so the
rule that decides the `W` row could move with nothing collecting it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: The reproducer: one wiring key, two different declared boundaries.
FIXTURE = "tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl"

#: The bound case that must survive the separation, and is LOAD-BEARING for it
#: (`test_the_bound_case_is_not_vacuous`): `Db.execute` is declared
#: `emission[audit, inner_db, wire]` — three WIRING KEYS — and `Seam`'s
#: implementation crosses two of them. Under the key namespace it is inside its
#: declaration; under the declared-token namespace it reaches `log_line` (what
#: the `audit` key's service declares) and would be refused.
BOUND_CASE = ("examples/interpose_observe.rvl", "Seam", "db", "Db", "execute")


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "formal_diff_corpus", ROOT / "formal" / "harness" / "diff_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return [r.split("\t") for r in rows]


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(["\t".join(r) for r in tsv])


def _rows(tsv, kind, rel=None):
    return [r for r in tsv
            if r and r[0] == kind and (rel is None or r[1] == rel)]


# ------------------------------------------------------- the laundering hole

def test_the_reference_refuses_the_same_key_widening():
    """The premise. If this ever stops refusing, the rest of the module is
    measuring the wrong thing and should be read again, not deleted."""
    from revl import compile_files
    from revl.diagnostics import classify
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        compile_files([str(ROOT / FIXTURE)])
    assert classify(excinfo.value)["code"] == "G4"
    assert "granting it `kv_b`" in str(excinfo.value)


def test_the_fixture_spells_one_key_over_two_boundaries(tsv):
    """Non-vacuity for the fixture itself: parent and child must actually
    agree on the wiring key and disagree on the boundary, or it is not the
    shape a key-namespaced fold is blind to."""
    binds = {(r[2], r[3]) for r in _rows(tsv, "R", FIXTURE)}
    assert binds == {("Supervisor", "kv"), ("Leaker", "kv")}
    declared = {r[4] for r in _rows(tsv, "Q", FIXTURE)}
    assert declared == {"kv_a", "kv_b"}


def test_the_model_derives_the_same_key_widening(verdicts):
    """The exit criterion. The parent holds `kv_a`, the child reaches `kv_b`,
    and the fold must see two boundaries rather than one key."""
    assert verdicts.spawns[(FIXTURE, "Supervisor", "Leaker")] == "fail"


def test_the_attenuation_fold_carries_no_bare_wiring_key(tsv, harness):
    """The invariant behind the fix, over the WHOLE corpus rather than the one
    fixture: every element of the attenuation surface (`A`, `K`, and the `F`
    row's cap column) is the unnameable `*`, a declared capability of that
    file, or a wiring key in its own `key:` namespace. A bare key among them
    is a name in the wrong namespace, which is what let the widening through.
    """
    declared: dict[str, set[str]] = {}
    for r in _rows(tsv, "Q"):
        declared.setdefault(r[1], set()).add(harness.parse_cap(r[4]).to_str())
    leaked = []
    for kind, col in (("A", 3), ("K", 4), ("F", 6)):
        for r in _rows(tsv, kind):
            cap = r[col]
            if cap == "*" or cap.startswith(harness._WIRE_NS):
                continue
            if cap not in declared.get(r[1], set()):
                leaked.append(f"{kind} {r[1]} {cap}")
    assert leaked == [], "\n".join(leaked)


def test_the_two_namespaces_cannot_collide(tsv, harness):
    """A declared token never lands in the wiring-key namespace, so a key
    spelling can never masquerade as a boundary (`lower._cap_keyed`'s own
    reason for `_wire_cap` having a namespace at all)."""
    assert not [r[4] for r in _rows(tsv, "Q")
                if r[4].startswith(harness._WIRE_NS)]


# ------------------------------------------- what `_canon_cap` was right about

def test_the_provide_method_bound_still_reads_the_wiring_key(verdicts):
    """The half the separation must not cost. `Seam` stays admitted."""
    assert verdicts.providers[BOUND_CASE] == "ok"


def test_the_bound_case_is_not_vacuous(tsv, harness):
    """...and it is admitted BECAUSE the bound reads the key namespace, not by
    accident. Re-decide the same method over the attenuation column and it
    flips to refused: the two spellings are genuinely different names for this
    document, so `test_the_provide_method_bound_still_reads_the_wiring_key`
    is measuring the choice rather than agreeing with anything."""
    rel, comp, key, svc, meth = BOUND_CASE
    entries = {r[4] for r in _rows(tsv, "Q", rel)
               if r[2] == svc and r[3] == meth}
    assert entries, "the bound case stopped declaring a scoped emission"
    frows = [r for r in _rows(tsv, "F", rel)
             if (r[2], r[3], r[4], r[5]) == (comp, key, svc, meth)]
    assert frows, "the bound case stopped reaching anything"
    bound_tokens = {harness.parse_cap(r[7]).token for r in frows}
    atten_tokens = {harness.parse_cap(r[6]).token for r in frows}
    assert bound_tokens <= entries
    assert not atten_tokens <= entries


def test_the_bound_refusals_are_still_refusals(verdicts):
    """The other direction: a provider that leaves its declaration is still
    caught. `LeakyCache.put` is declared `emission[db]` and emits through
    `bus`, which is a wiring key in both files it appears in."""
    assert verdicts.providers[(
        "examples/rejections/g4_capability_not_declared.rvl",
        "LeakyCache", "cache", "Cache", "put")] == "fail"


# ------------------------------------------------- the scope reader itself

def test_a_dotted_emission_scope_is_one_capability(tsv):
    """`emission[vault.mint]` is ONE capability whose token has two segments,
    and `emission[fs.write(path="/etc")]` is one with a valuation. A reader
    that scraped identifiers out of the scope brackets would report two and
    three; the harness asks the reference parser instead
    (`diff_corpus._bound_index` reads `md.capabilities`), and this pins that it
    keeps doing so."""
    dotted = [r[4] for r in _rows(
        tsv, "Q", "examples/rejections/gsecret_service_return_discloses.rvl")
        if r[2] == "Vault" and r[3] == "get"]
    assert dotted == ["vault.mint"]
    parameterized = [r[4] for r in _rows(
        tsv, "Q", "examples/rejections/g4_spawn_widens_parameter.rvl")
        if r[2] == "EtcStore" and r[3] == "ingest"]
    assert parameterized == ['fs.write(path="/etc")']
