"""Item 397: atomic `insert_if_absent` on the host Map (design doc
docs/design/397-insert-if-absent.md).

The host Map's `insert` overwrites unconditionally, so "consume exactly once"
(an approval ticket, a lease, an idempotency key) could not be expressed
against it without a read-then-write hole. Item 397 adds one atomic
compare-and-set verb, `insert_if_absent(k, v) -> Bool`, spelled as an
acquisition-with-checked-result (`let fresh = effect ledger.insert_if_absent(k,
v) undo ledger.remove(k)`), with a per-tier atomicity mechanism and a
result-guarded undo (a `false` CAS registers no inverse).

These are the design's EXIT TESTS: the sequential contract cross-tier, the
concurrency contract on the loop tier, the result-guarded undo, the classifier
lifts (statement-form refused, spawn-only preserved, 401 accepts the verb, G6
pure-fn still refuses host access), and the item-402 regression (a CAS-only
writer emits a correctly-typed Map on go/rust/java).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "iia.rvl")
    return str(excinfo.value)


# The gate component used across the runtime exit tests: a claim method whose
# CAS reports whether it consumed the ticket, and a lookup so a test can see
# the stored value was not overwritten.
_GATE = """
service Gate {
  fn claim(ticket: Str, actor: Str) -> Bool
  fn lookup(ticket: Str) -> Opt[Str]
}
component TicketGate provides gate: Gate {
  let ledger = effect Map.new() undo ledger.drop()
  provide gate {
    fn claim(ticket, actor) {
      let fresh = effect ledger.insert_if_absent(ticket, actor)
                  undo ledger.remove(ticket)
      return fresh
    }
    fn lookup(ticket) = ledger.get(ticket)
  }
}
"""

# The SEQUENTIAL exit test, as a lifecycle test: first claim of a key is `true`;
# a second claim of the same key is `false` and the value is NOT overwritten;
# clean unload leaves no residue. One shared expectation, run on every tier.
_SEQUENTIAL = _GATE + """
lifecycle test "sequential compare-and-set" {
  load TicketGate
  let first = call gate.claim("t", "alice")
  assert first == true
  let second = call gate.claim("t", "bob")
  assert second == false
  let who = call gate.lookup("t")
  assert who == Some("alice")
  unload TicketGate
  assert no_residue
}
"""


@pytest.mark.parametrize("tier", ["py", "ts", "go", "rust", "java"])
def test_sequential_cas_is_deterministic_cross_tier(tier):
    """The one shared sequential contract on every tier that has the host Map:
    first `true`, second `false`, value not overwritten, no residue. Like item
    385's json bytes, the CAS must MEAN the same thing on every tier."""
    status, message = RUNNERS[tier](compile_source(_SEQUENTIAL, "iia.rvl"))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} diverged: {message}"


# The RESULT-GUARDED UNDO exit test: a losing (`false`) CAS registers NO
# inverse, so teardown never removes the winning claimant's entry. Observed two
# ways: (1) at runtime the winner's value survives a losing claim and the clean
# unload is residue-free; (2) the emitted py registers the site-spelled undo
# guarded on the bound Bool (the identity inverse on `false`).
def test_result_guarded_undo_runtime_leaves_the_winner_untouched():
    status, message = RUNNERS["py"](compile_source(_SEQUENTIAL, "iia.rvl"))
    assert status == "pass", message


def test_result_guarded_undo_is_visible_in_emitted_py():
    """The false CAS's inverse is the identity: emit guards the site-spelled
    undo on the bound result, so teardown replays `remove` only for a `true`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_iia", ROOT / "backends" / "python" / "emit.py")
    pyemit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pyemit)
    src = pyemit.emit(compile_source(_GATE, "iia.rvl"))
    # the acquire binds `fresh`, and the undo registration is guarded on it.
    assert "insert_if_absent" in src
    assert "if fresh else None" in src, \
        "the undo must be the identity inverse on a false CAS"


# The CONCURRENCY exit test on the loop tier: under any admitted concurrency,
# N `insert_if_absent(k, ...)` on one map yield EXACTLY ONE `true`. On py the
# runtime is one event loop and the CAS is a single synchronous, suspension-free
# step, so it is atomic by run-to-completion — driven here directly against the
# reference runtime's Map (no await can interleave the probe and the write).
def test_concurrency_exactly_one_true_on_the_reference_runtime():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "revl_py_runtime_iia", ROOT / "backends" / "python" / "runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)

    m = runtime.Map.new()
    trues = [m.insert_if_absent("k", f"worker-{i}") for i in range(64)]
    assert sum(1 for t in trues if t) == 1, "exactly one claimant may win"
    assert trues[0] is True, "the first claimant wins"
    # the winner's value survives every later losing CAS
    assert m.get("k") == "worker-0"
    m.drop()


def test_concurrency_atomic_mechanism_is_present_per_parallel_tier():
    """The parallel tiers (go/rust/java) must make the test+insert one atomic
    step — a lock spanning both (go/rust) or ConcurrentHashMap.putIfAbsent
    (java) — or a genuine fan-in could see two winners. Assert the mechanism is
    in each tier's emitted host-Map runtime."""
    import importlib.util

    def _emit(tier, ir):
        spec = importlib.util.spec_from_file_location(
            f"revl_{tier}_emit_iia", ROOT / "backends" / tier / "emit.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.emit(ir)

    ir = compile_source(_GATE, "iia.rvl")  # component-only (no lifecycle test)
    go_src = _emit("go", ir)
    # go: the per-op mutex is held across the membership test AND the insert.
    assert "func (m *Map[V]) InsertIfAbsent(k string, v V) bool {" in go_src
    assert "m.mu.Lock()" in go_src and "defer m.mu.Unlock()" in go_src
    rust_src = _emit("rust", ir)
    # rust: one lock() with the entry API.
    assert "pub fn insert_if_absent(&self, key: String, value: V) -> bool {" in rust_src
    assert "Entry::Vacant(e) => { e.insert(value); true }" in rust_src
    java_src = _emit("java", ir)
    # java: ConcurrentHashMap.putIfAbsent, one atomic operation.
    assert "java.util.concurrent.ConcurrentHashMap" in java_src
    assert "return values.putIfAbsent(key, value) == null;" in java_src


# --------------------------------------------------------------------------
# Classifier lifts and refusals (frontend)
# --------------------------------------------------------------------------

def test_method_body_cas_compiles():
    """The narrow grammar lift: a result-declared host CAS may be bound in a
    provide-method body (the H48 use case), and the bound Bool flows into a
    ternary/return."""
    compile_source(_GATE, "iia.rvl")


def test_statement_form_is_refused_with_the_bind_redirect():
    """A CAS whose Bool nobody reads is a plain `insert` with an unsound undo:
    refused, redirecting to a bound `let` or to `insert`."""
    err = _err("""
service Gate { fn claim(ticket: Str, actor: Str) -> Str }
component TicketGate provides gate: Gate {
  let ledger = effect Map.new() undo ledger.drop()
  provide gate {
    fn claim(ticket, actor) {
      effect ledger.insert_if_absent(ticket, actor)
      undo   ledger.remove(ticket)
      return "x"
    }
  }
}""")
    assert "insert_if_absent" in err
    assert "must be bound" in err
    assert "insert" in err  # the "or use `insert`" redirect


def test_binding_lift_is_exactly_one_verb_wide():
    """The lift admits ONLY a result-declared host CAS; a plain `Map.new()`
    acquisition in a provide-method body still refuses with the spawn-only
    message (the phase-1 instances restriction is otherwise untouched)."""
    err = _err("""
service Gate { fn claim(ticket: Str, actor: Str) -> Str }
component TicketGate provides gate: Gate {
  provide gate {
    fn claim(ticket, actor) {
      let h = effect Map.new() undo h.drop()
      return "x"
    }
  }
}""")
    assert "only `spawn` may be acquired inside a provide-method body" in err


def test_401_accepts_insert_if_absent_and_still_refuses_a_typo():
    """Item 401 (the unknown-host-verb refusal) now ADMITS insert_if_absent —
    it is part of the known host-Map surface — while a near-miss typo is still
    refused, with the full surface (now including the CAS verb) named."""
    # accepted:
    compile_source(_GATE, "iia.rvl")
    # a typo is refused, and the named surface includes insert_if_absent:
    err = _err("""
service Gate { fn claim(ticket: Str, actor: Str) -> Str }
component TicketGate provides gate: Gate {
  let ledger = effect Map.new() undo ledger.drop()
  provide gate {
    fn claim(ticket, actor) {
      let fresh = effect ledger.insert_if_absnt(ticket, actor)
                  undo ledger.remove(ticket)
      return fresh ? "a" : "b"
    }
  }
}""")
    assert "has no method `insert_if_absnt`" in err
    assert "insert_if_absent" in err


def test_cas_is_classified_exactly_like_insert_in_a_pure_fn_body():
    """The CAS adds NO new pure-fn restriction: a local host Map in a `fn`
    accepts `insert_if_absent` exactly where it accepts `insert` (the value the
    map holds never escapes the fn). Consistency, not a special case."""
    for verb in ("insert", "insert_if_absent"):
        compile_source(
            f'fn f() {{\n  let m = Map.new()\n  m.{verb}("k", "v")\n}}', "g.rvl")


def test_g6_if_around_an_effect_still_refuses():
    """G6 unchanged: a component-activation `if` arm admits only `fail`, never a
    general effect. Branching to decide WHETHER an effect runs stays unwritable;
    the CAS moved the conditional INSIDE the primitive precisely so no
    effect-guarding branch is needed in source."""
    err = _err("""
service Gate { fn claim(t: Str) -> Str }
component TicketGate provides gate: Gate {
  let ledger = effect Map.new() undo ledger.drop()
  if (true) {
    effect ledger.insert("boot", "sys")
    undo   ledger.remove("boot")
  }
  provide gate {
    fn claim(t) = "x"
  }
}""")
    assert "if" in err.lower() or "fail" in err.lower() or "G6" in err


# --------------------------------------------------------------------------
# Item 402 regression: a CAS-only writer pins a concrete Map value type
# --------------------------------------------------------------------------

# A component whose ONLY map writer is insert_if_absent (never `insert`), with
# an Int value. Before item 402 the go/rust/java V-inference scanned for the
# literal "insert" and would degrade V to the string default here; now it infers
# V structurally from ANY writer's value argument.
_CAS_ONLY_INT = """
service Counter { fn bump(key: Str, n: Int) -> Bool }
component C provides c: Counter {
  let ledger = effect Map.new() undo ledger.drop()
  provide c {
    fn bump(key, n) {
      let fresh = effect ledger.insert_if_absent(key, n)
                  undo ledger.remove(key)
      return fresh
    }
  }
}
"""


@pytest.mark.parametrize("tier,expected", [
    ("go", "Map[int]"),
    ("rust", "Map<i64>"),
    ("java", "Map<java.lang.Long>"),
])
def test_402_cas_only_writer_pins_a_concrete_map_value_type(tier, expected):
    """The 402 regression: a Map whose only writer is `insert_if_absent`
    (an Int value) emits a correctly-typed Map on go/rust/java — the concrete
    value type, NOT the historical string default."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"revl_{tier}_emit_402", ROOT / "backends" / tier / "emit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = mod.emit(compile_source(_CAS_ONLY_INT, "cas_only.rvl"))
    assert expected in src, f"{tier}: V degraded to the string default"
