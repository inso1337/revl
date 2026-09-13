"""Ambient admission, the replacement leg: a withdrawn provision a RETAINED
running component still requires is refused (roadmap item 186 / issue #86,
docs/design/186-ambient-admission-guarantees.md, "Replacement semantics").

The gap this closes. `compile_files(X, manifest=M, replacing=R)` links `X`
against `M \\ R`, and G2/G3 over that union see every CONFLICT — two providers
of a key, a cycle closing through the manifest. What the union cannot see is a
LOSS: a key `R` provided and nothing provides again just leaves the provider
table, and a retained running consumer of it produces no edge at all. Before
this, such an admission was granted silently and the consumer deactivated into
PENDING at runtime, holding its resources, with no diagnostic anywhere.

Failure direction. Every new refusal here fails CLOSED: the composition that
was running keeps running, unchanged, and the admission is the thing that does
not happen. The control tests below are the other half — they pin that the
legitimate replacement (the key is re-provided) and the already-unmet
requirement (nothing was lost) still ADMIT, so the refusal cannot be passing
vacuously by refusing everything.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402

# Db --db--> Store: one provider, one retained consumer. The smallest shape in
# which a withdrawal can strand something.
RUNNING = """
service Database { fn query(sql: Str) -> List[Row] }
service Cache { fn get(key: Str) -> Opt[Str] }

component Db provides db: Database {
  let pool = effect Pool.open("u", 1) undo pool.close()
  provide db { fn query(sql) = pool.query(sql) }
}
component Store requires db: Database provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache { fn get(key) = m.get(key) }
}
"""


@pytest.fixture
def running():
    return compile_source(RUNNING)


def _admit(source: str, running: dict, replacing: tuple = ()):
    """Admit `source` against the running composition, the way a swap does."""
    return compile_source(source, "candidate.rvl", manifest=running,
                          replacing=replacing)


# ------------------------------------------------------------------ refusals

#: `Db` redeclared WITHOUT its `db` provision — the implicit same-name
#: replacement `compile_files` performs.
DROPS_DB = """
component Db {
  let pool = effect Map.new() undo pool.drop()
}
"""


def test_a_same_named_replacement_that_drops_a_consumed_provision_is_refused(running):
    """The common hot-swap shape: the replacement keeps the name and loses the
    key. `Store` is still running and still requires `db`."""
    with pytest.raises(RevlError) as caught:
        _admit(DROPS_DB, running)
    message = str(caught.value)
    assert "withdraws the running provider of `db`" in message
    assert "`Store` still requires it" in message
    # named by the consumer AND the withdrawn provider, not just "a key is gone"
    assert "`Db`" in message


def test_the_withdrawal_refusal_classifies_as_an_admission_rejection(running):
    with pytest.raises(RevlError) as caught:
        _admit(DROPS_DB, running)
    record = classify(caught.value)
    assert record["code"] == "G2"
    assert record["category"] == "admission"


def test_an_explicit_replacing_withdrawal_that_strands_a_consumer_is_refused(running):
    """`replacing=` is the other door into the same loss: `Db` is withdrawn and
    the candidate re-provides nothing."""
    unrelated = """
    service Ping { fn go() -> Int }
    component Pinger provides ping: Ping { provide ping { fn go() = 1 } }
    """
    with pytest.raises(RevlError, match="withdraws the running provider of `db`"):
        _admit(unrelated, running, replacing=("Db",))


def test_re_providing_the_key_in_another_realm_does_not_satisfy_the_consumer(running):
    """The ambiguous case, refused rather than admitted: the replacement DOES
    provide `db`, but isolated into a realm the retained consumer does not
    read. Provision disjointness is per-(key, realm), so the shared-realm
    `Store` is as stranded as if nothing provided `db` at all — a per-key check
    would have wrongly admitted this."""
    realmed = """
    component Db provides db: Database {
      isolate db in realm("shadow")
      let pool = effect Pool.open("u", 1) undo pool.close()
      provide db { fn query(sql) = pool.query(sql) }
    }
    """
    with pytest.raises(RevlError) as caught:
        _admit(realmed, running)
    assert "withdraws the running provider of `db`" in str(caught.value)
    assert "`Store` still requires it" in str(caught.value)


def test_the_refusal_leaves_the_running_composition_untouched(running):
    """Failure direction, stated as a property: refusing is the outcome in
    which nothing changes. The running IR the caller passed in is not mutated
    by the refused admission."""
    import copy

    snapshot = copy.deepcopy(running)
    with pytest.raises(RevlError):
        _admit(DROPS_DB, running)
    assert running == snapshot


# ------------------------------------------------------- non-vacuity controls

def test_a_replacement_that_keeps_the_provision_still_admits(running):
    """THE control. Same withdrawal, same retained consumer — the only
    difference is that the replacement re-provides `db`, which is the whole
    point of a hot-swap. It must admit, or the refusal above is just "no"."""
    kept = """
    component Db provides db: Database {
      let pool = effect Pool.open("sqlite://", 1) undo pool.close()
      provide db { fn query(sql) = pool.query(sql) }
    }
    """
    ir = _admit(kept, running)
    assert sorted(e["name"] for e in ir["manifest"]["components"]) == ["Db", "Store"]


def test_a_key_nothing_retained_requires_may_be_withdrawn(running):
    """`cache` is provided by `Store` and consumed by nobody in this running
    composition, so dropping it strands no one and admits."""
    drops_cache = """
    component Store requires db: Database {
      let m = effect Map.new() undo m.drop()
    }
    """
    ir = _admit(drops_cache, running)
    assert sorted(e["name"] for e in ir["manifest"]["components"]) == ["Db", "Store"]


def test_an_already_unmet_requirement_is_not_a_refusal():
    """Only the transition met -> unmet is refused. A composition admitted
    incrementally — the consumer first, its provider later — has a running
    component whose key nothing provides; a later, unrelated withdrawal must
    not suddenly report that pre-existing state as this admission's fault."""
    partial = compile_source("""
    service Database { fn query(sql: Str) -> List[Row] }
    service Ping { fn go() -> Int }
    component Store requires db: Database {
      let m = effect Map.new() undo m.drop()
    }
    component Pinger provides ping: Ping { provide ping { fn go() = 1 } }
    """)
    # `Pinger` goes; `Store`'s `db` was unmet before this admission and stays
    # unmet after it, which is not a loss this admission caused.
    ir = _admit("""
    service Pong { fn go() -> Int }
    component Ponger provides pong: Pong { provide pong { fn go() = 1 } }
    """, partial, replacing=("Pinger",))
    assert sorted(e["name"] for e in ir["manifest"]["components"]) == \
        ["Ponger", "Store"]


def test_a_cold_compile_is_unaffected():
    """No manifest, no withdrawal: the check is inert and the composition that
    compiled before compiles now."""
    assert compile_source(RUNNING)["manifest"]["loadOrder"] == ["Db", "Store"]


def test_a_pure_addition_against_a_running_manifest_still_admits(running):
    """A candidate that replaces nothing withdraws nothing."""
    ir = _admit("""
    service Ping { fn go() -> Int }
    component Pinger provides ping: Ping { provide ping { fn go() = 1 } }
    """, running)
    assert sorted(e["name"] for e in ir["manifest"]["components"]) == \
        ["Db", "Pinger", "Store"]
