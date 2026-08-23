"""`verified effect` — inverse round-trip testing (roadmap item 26).

The checker proves an inverse is *present and well-shaped* (G4), never that it
is *correct*; docs/replay.md §4.1 names the gap ("an undo that is wrong,
partial, or a no-op"). A `verified effect` opts an activation-body effect into
a generated property test: activate the component, tear it down so the inverse
runs, and assert the observable in-process state returned to baseline — N
randomized rounds on the py reference tier.

Layers, gated so a missing runtime never reads as a pass:

* **frontend** — `verified` before `effect` parses, threads a marker into the
  IR step, and is refused outside an activation body. No runtime needed.
* **fingerprint** — the pure fold over the trace ledger that decides whether a
  round trip closed. No runtime needed.
* **execution** — real activate/teardown round trips on a real cordis.Context.
  Skipped with a reason when cordis-py is absent.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402

_HAS_CORDIS = importlib.util.find_spec("cordis") is not None
needs_cordis = pytest.mark.skipif(
    not _HAS_CORDIS,
    reason="round trips activate for real; they need the cordis-py runtime "
           "(sh backends/python/setup.sh)")


# --- a correct verified effect and a deliberately-wrong one --------------------

_CORRECT = '''
service Store { fn seed(k: Str) -> Int  fn unseed(k: Str) -> Int }
service Ready { fn ok() -> Str }

component Backing provides s: Store {
  let data = effect Map.new() undo data.drop()
  provide s {
    fn seed(k)   = data.insert(k, 1)
    fn unseed(k) = data.remove(k)
  }
}

component Seeder requires s: Store provides r: Ready {
  config { tag: Str }
  let hold = verified effect s.seed(config.tag) undo s.unseed(config.tag)
  provide r { fn ok() = "ok" }
}
'''

# the inverse is a no-op: the acquired resource is never released (new without
# drop). This is the canonical replay.md §4.1 case ("a no-op undo").
_WRONG_NOOP = '''
service W { fn ping() -> Str }
component Widget provides w: W {
  let store = verified effect Map.new() undo store.get("noop")
  provide w { fn ping() = "ok" }
}
'''

# the inverse runs but reverts the wrong thing: the key stays inserted. R1's
# acquire/release residue check (and the fault sweep) miss this; the key-level
# fingerprint catches it.
_WRONG_PARTIAL = '''
service Store { fn seed(k: Str) -> Int  fn nop(k: Str) -> Int }
service Ready { fn ok() -> Str }

component Backing provides s: Store {
  let data = effect Map.new() undo data.drop()
  provide s {
    fn seed(k) = data.insert(k, 1)
    fn nop(k)  = 0
  }
}

component Seeder requires s: Store provides r: Ready {
  config { tag: Str }
  let hold = verified effect s.seed(config.tag) undo s.nop(config.tag)
  provide r { fn ok() = "ok" }
}
'''


# ============================================================================
# frontend — parsing, IR marker, placement refusals (no runtime)
# ============================================================================

def test_verified_effect_threads_a_marker_into_the_ir_step():
    ir = compile_source(_CORRECT, "t.rvl")
    seeder = next(c for c in ir["components"] if c["name"] == "Seeder")
    effect_step = next(s for s in seeder["body"] if s["step"] == "let-effect")
    assert effect_step["verified"] is True


def test_bare_verified_effect_statement_parses_and_marks():
    src = '''
    service W { fn p() -> Str }
    component C provides w: W {
      let m = effect Map.new() undo m.drop()
      verified effect m.insert("k", "v") undo m.remove("k")
      provide w { fn p() = "ok" }
    }
    '''
    ir = compile_source(src, "t.rvl")
    body = ir["components"][0]["body"]
    anon = next(s for s in body if s["step"] == "effect")
    assert anon["verified"] is True


def test_ordinary_effect_carries_no_marker():
    ir = compile_source(_CORRECT, "t.rvl")
    backing = next(c for c in ir["components"] if c["name"] == "Backing")
    data_step = next(s for s in backing["body"] if s["step"] == "let-effect")
    assert "verified" not in data_step


def test_verified_in_a_method_body_is_refused():
    src = '''
    service W { fn put(k: Str) }
    component Widget provides w: W {
      let store = effect Map.new() undo store.drop()
      provide w { fn put(k) { verified effect store.insert(k, "1") undo store.remove(k) } }
    }
    '''
    with pytest.raises(RevlError, match="only allowed in a component activation body"):
        compile_source(src, "t.rvl")


def test_verified_without_a_following_effect_is_refused():
    src = '''
    verified fn f() -> Int { 1 }
    service W { fn p() -> Str }
    component C provides w: W {
      let x = verified store.get("k")
      provide w { fn p() = "ok" }
    }
    '''
    with pytest.raises(RevlError, match="expected `effect` after `verified`"):
        compile_source(src, "t.rvl")


def test_documents_without_verified_effects_have_no_units():
    src = '''
    service W { fn p() -> Str }
    component C provides w: W {
      let m = effect Map.new() undo m.drop()
      provide w { fn p() = "ok" }
    }
    '''
    ir = compile_source(src, "t.rvl")
    assert fault_mod.roundtrip_units(ir) == []


def test_roundtrip_units_names_the_verified_component_and_effect():
    ir = compile_source(_CORRECT, "t.rvl")
    units = fault_mod.roundtrip_units(ir)
    assert len(units) == 1
    assert units[0]["component"] == "Seeder"
    assert "verified effect `hold`" in units[0]["verified"][0]


# ============================================================================
# fingerprint — the pure fold that decides whether a round trip closed
# ============================================================================

def test_outstanding_pairs_acquire_and_release_to_empty():
    events = ["map#1.new", "map#1.insert k", "map#1.remove k", "map#1.drop"]
    assert fault_mod._outstanding(events) == {"live": [], "keys": {}, "conns": {}}


def test_outstanding_flags_an_unreleased_resource():
    fp = fault_mod._outstanding(["map#1.new"])
    assert fp["live"] == ["map#1"]


def test_outstanding_flags_an_uncleared_key():
    fp = fault_mod._outstanding(["map#1.new", "map#1.insert k"])
    assert fp["keys"] == {"map#1": ["k"]}


def test_outstanding_tracks_pool_connections():
    events = ["pool#1.open db", "pool#1.acquire conn=1 1/4", "pool#1.acquire conn=2 2/4",
              "pool#1.release conn=1 1/4"]
    fp = fault_mod._outstanding(events)
    assert fp["conns"] == {"pool#1": ["conn=2"]}


def test_fingerprint_delta_is_empty_when_nothing_changed():
    fp = {"live": ["pool#1"], "keys": {}, "conns": {}}
    assert fault_mod._fingerprint_delta(fp, fp) == []


def test_fingerprint_delta_names_an_unreleased_resource():
    base = {"live": [], "keys": {}, "conns": {}}
    final = {"live": ["map#7"], "keys": {}, "conns": {}}
    diffs = fault_mod._fingerprint_delta(base, final)
    assert diffs and "map#7" in diffs[0] and "never released" in diffs[0]


def test_random_config_respects_declared_types():
    schema = [{"name": "url", "type": "Str", "default": None},
              {"name": "size", "type": "Int", "default": 10},
              {"name": "flag", "type": "Bool", "default": None}]
    import random
    cfg = fault_mod._random_config(schema, random.Random("seed"))
    assert isinstance(cfg["url"], str) and isinstance(cfg["size"], int)
    assert cfg["size"] >= 1 and isinstance(cfg["flag"], bool)


# ============================================================================
# dossier — gauntlet-compatible shape (no runtime; fabricated results)
# ============================================================================

def test_dossier_shape_is_gauntlet_compatible():
    results = [("Seeder", ["step 1 (verified effect `hold`)"],
               [(True, None, {"tag": "abc"})] * 3)]
    dossier = fault_mod._roundtrip_dossier(results, rounds=3)
    # mirrors the fault sweep's dossier so it can fill gauntlet's
    # inverseRoundTrip slot (src/revl/mcp/gauntlet.py, roadmapItem 26)
    assert dossier["kind"] == "tested"
    assert dossier["roadmapItem"] == 26
    assert dossier["status"] == "passed"
    assert set(dossier["counts"]) == {"effects", "passed", "failed", "rounds"}
    assert dossier["counts"]["passed"] == 1


def test_dossier_records_the_first_counterexample():
    results = [("Widget", ["step 1 (verified effect `store`)"],
               [(True, None, {"tag": "a"}),
                (False, "host resource `map#3` was acquired and never released",
                 {"tag": "b"})])]
    dossier = fault_mod._roundtrip_dossier(results, rounds=16)
    assert dossier["status"] == "failed"
    entry = dossier["components"][0]
    assert entry["status"] == "fail"
    assert entry["counterexample"]["config"] == {"tag": "b"}


# ============================================================================
# execution — real round trips on the cordis-py runtime
# ============================================================================

@needs_cordis
def test_a_correct_verified_effect_survives_the_round_trip(capsys):
    ir = compile_source(_CORRECT, "t.rvl")
    failures, dossier = fault_mod.run_roundtrip_units(ir, fault_mod.roundtrip_units(ir))
    out = capsys.readouterr().out
    assert failures == 0
    assert dossier["status"] == "passed"
    assert "PASS Seeder" in out


@needs_cordis
def test_a_no_op_inverse_is_caught(capsys):
    ir = compile_source(_WRONG_NOOP, "t.rvl")
    failures, dossier = fault_mod.run_roundtrip_units(ir, fault_mod.roundtrip_units(ir))
    out = capsys.readouterr().out
    assert failures == 1
    assert dossier["status"] == "failed"
    assert "never released" in out
    assert dossier["components"][0]["counterexample"]["reason"]


@needs_cordis
def test_a_partial_inverse_that_leaves_a_key_is_caught(capsys):
    # R1's acquire/release residue check and the fault sweep both miss this;
    # the key-level fingerprint is strictly stronger for the round-trip property.
    ir = compile_source(_WRONG_PARTIAL, "t.rvl")
    failures, _ = fault_mod.run_roundtrip_units(ir, fault_mod.roundtrip_units(ir))
    out = capsys.readouterr().out
    assert failures == 1
    assert "still holds key" in out
    # the report names the exact randomized config that broke it
    assert "config" in out


@needs_cordis
def test_the_report_states_its_honesty_bound(capsys):
    ir = compile_source(_CORRECT, "t.rvl")
    fault_mod.run_roundtrip_units(ir, fault_mod.roundtrip_units(ir))
    out = capsys.readouterr().out
    assert "not a proof" in out
    assert "in-process observable state only" in out
    # names the out-of-reach categories (replay.md §4.1)
    assert "aliased references" in out
    assert "clock" in out


@needs_cordis
def test_the_example_program_round_trips(capsys):
    ir = compile_source((ROOT / "examples" / "verified_effect.rvl").read_text(), "ex.rvl")
    failures, dossier = fault_mod.run_roundtrip_units(ir, fault_mod.roundtrip_units(ir))
    assert failures == 0
    assert dossier["counts"]["effects"] == 1


@needs_cordis
def test_verified_effect_surfaces_through_revl_test(capsys):
    from revl.test import test_command
    ir = compile_source(_CORRECT, "t.rvl")
    rc = test_command(ir, backend="py")
    out = capsys.readouterr().out
    assert rc == 0
    assert "verified-effect round-trip(s) held" in out


@needs_cordis
def test_roundtrip_dossier_is_empty_passed_without_verified_effects():
    src = '''
    service W { fn p() -> Str }
    component C provides w: W {
      let m = effect Map.new() undo m.drop()
      provide w { fn p() = "ok" }
    }
    '''
    ir = compile_source(src, "t.rvl")
    dossier = fault_mod.roundtrip_dossier(ir)
    assert dossier["status"] == "passed"
    assert dossier["counts"]["effects"] == 0
