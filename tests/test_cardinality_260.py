"""Emission cardinality bounds, Slice 1 (item 260).

docs/design/260-emission-cardinality-bounds.md. Slice 1 proves an exact
per-capability crossing count for non-looping, non-recursive bodies and reports
`unbounded` (loudly, with a reason) for every loop, every recursion, every
first-class emitting arrow, and every crossing behind a host extern.

The load-bearing soundness property (§5.1, and the MEDIUM count-soundness fix):
the count fold reuses the EXACT reach `_boundary` computes, mirroring its
`walk_expr` arms, so a spawn-handle emission reached through an `instance-get`
(item 246) is COUNTED, never reported 0.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.cardinality import _merge_max, cardinality  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

EXAMPLES = ROOT / "examples"


def _card(src: str) -> dict:
    return cardinality(compile_source(src, "t.rvl"))


# --------------------------------------------------------------------------
# §2.1 - the exact count for non-looping bodies
# --------------------------------------------------------------------------

def test_straight_line_req_emissions_count_exactly():
    """Two `emit`s through the same required key sum to a proved `<= 2`."""
    card = _card("""
service DB { emission[db] fn exec(s: Str) -> Int }
service Svc { emission[db] fn run() -> Int }
component C requires db: DB provides svc: Svc {
  provide svc { fn run() -> Int { emit db.exec("a"); emit db.exec("b"); return 0 } }
}
""")
    assert card["C"]["verdict"] == "bounded"
    assert card["C"]["per_capability"]["db"] == {"bound": 2, "kind": "bounded"}


def test_spawn_handle_emission_is_counted_not_zero():
    """The MEDIUM fix: a component whose ONLY crossing is a provision-method
    call on a spawn handle (`w.wl.bump(...)`, reached through an `instance-get`,
    item 246) must COUNT the crossing, never report 0. A fold keyed only on
    `emit` steps would miss it entirely."""
    card = _card("""
service KV { emission[worklog] fn put(n: Int) -> Int }
service Worklog { emission[worklog] fn bump(n: Int) -> Int }
service Probe { emission fn go() -> Int }
component Worker requires worklog: KV provides wl: Worklog {
  config { tag: Str }
  provide wl { fn bump(n) { emit worklog.put(n); return n } }
}
component App requires worklog: KV provides probe: Probe {
  let w = effect spawn Worker with { tag: "a" } undo w.dispose()
  provide probe { fn go() -> Int { emit w.wl.bump(1); return 0 } }
}
""")
    # counted through the spawn handle, attributed to the spawned method's
    # declared capability, exactly as _boundary enumerates it.
    assert card["App"]["verdict"] == "bounded"
    assert card["App"]["per_capability"]["worklog"] == {"bound": 1,
                                                         "kind": "bounded"}
    # and it is NOT 0/absent
    assert card["App"]["per_capability"]["worklog"]["bound"] == 1


# --------------------------------------------------------------------------
# item 396 - the count fold follows a service-typed arrow-parameter alias.
# The G4/G8/emission side of this crossing is closed by PR #395; #396 closes
# the cardinality asymmetry: the `cardinality: ... per activation` line used to
# appear for the direct spelling `emit w.task.run(s)` but NOT for the
# arrow-param spelling `let f = (t: Task, s) => t.run(s); f(w.task, s)`, because
# the count keyed on the direct `instance-get` and did not follow the arrow
# application. The fix mirrors `_boundary`'s arrow-parameter arm.
# --------------------------------------------------------------------------

_ARROW_PARAM_PREAMBLE = """
service Net { emission[net] fn send(msg: Str) -> Int }
service Task {
  emission[net] fn run(prompt: Str) -> Int
  fn status() -> Int
}
component Worker requires net: Net provides task: Task {
  provide task { fn run(prompt: Str) { emit net.send(prompt); return 1 }
    fn status() = 0 }
}
service Sup { emission fn go(prompt: Str) -> Int }
"""


def test_arrow_param_alias_gets_the_same_bound_as_the_direct_spelling():
    """The load-bearing #396 assertion: the emission that rides a service-typed
    arrow PARAMETER is counted, and it reports the identical `net <= 1 per
    activation` bound the direct `emit w.task.run(s)` spelling reports. Before
    the fix the arrow-param component was absent from the report entirely (no
    crossing seen), so no cardinality line was rendered for that spelling."""
    direct = _card(_ARROW_PARAM_PREAMBLE + """
component Supervisor provides sup: Sup {
  provide sup { fn go(prompt: Str) {
    let w = effect spawn Worker with { } undo w.dispose()
    let r = emit w.task.run(prompt)
    return r } }
}
""")
    arrow = _card(_ARROW_PARAM_PREAMBLE + """
component Supervisor provides sup: Sup {
  provide sup { fn go(prompt: Str) {
    let w = effect spawn Worker with { } undo w.dispose()
    let f = (t: Task, s: Str) => emit t.run(s)
    let r = f(w.task, prompt)
    return r } }
}
""")
    # the arrow-param spelling is no longer missing from the report...
    assert "Supervisor" in arrow, arrow
    # ...and its per-activation bound is byte-identical to the direct spelling.
    assert arrow["Supervisor"] == direct["Supervisor"]
    assert arrow["Supervisor"]["verdict"] == "bounded"
    assert arrow["Supervisor"]["per_capability"]["net"] == {"bound": 1,
                                                            "kind": "bounded"}


def test_arrow_param_non_emission_method_is_not_counted():
    """A service-typed parameter reached by a NON-emission method (`t.status()`)
    crosses no boundary, so the component says nothing about cardinality and is
    omitted — the direct-spelling precision (§1) holds for the alias too."""
    arrow = _card(_ARROW_PARAM_PREAMBLE + """
component Supervisor provides sup: Sup {
  provide sup { fn go(prompt: Str) {
    let w = effect spawn Worker with { } undo w.dispose()
    let f = (t: Task) => t.status()
    let r = f(w.task)
    return r } }
}
""")
    assert "Supervisor" not in arrow, arrow


# --------------------------------------------------------------------------
# §2.3 - unbounded for every loop and every recursion (Slice 1)
# --------------------------------------------------------------------------

def test_recursion_is_unbounded_with_a_reason():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn rec(n: Int) -> Int { if (n <= 0) { return 0 } let x = boom(); return rec(n - 1) }
component C provides svc: Svc {
  provide svc { fn run() -> Int { return emit rec(5) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["boom"]
    assert entry["bound"] is None
    assert entry["kind"] == "unbounded"
    assert "recursion" in entry["reason"]
    assert "rec" in entry["reason"]


def test_loop_is_unbounded_with_a_reason():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn loopy(n: Int) -> Int {
  var t = 0
  var i = 0
  while (i < n) { let x = boom(); t = t + x; i = i + 1 }
  return t
}
component C provides svc: Svc {
  provide svc { fn run() -> Int { return emit loopy(5) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["boom"]
    assert entry["bound"] is None
    assert "loop" in entry["reason"]


# --------------------------------------------------------------------------
# §5.1 - a crossing whose reach cannot be exactly counted is never 0
# --------------------------------------------------------------------------

def test_host_extern_reached_crossing_is_unbounded_never_zero():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn helper() -> Int { let x = boom(); return x }
component C provides svc: Svc {
  provide svc { fn run() -> Int { return emit helper() } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["boom"]
    assert entry["bound"] is None            # never an optimistic 0
    assert entry["kind"] == "unbounded"
    assert "host code" in entry["reason"]


def test_first_class_emitting_arrow_is_unbounded_never_zero():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn dispatch(f: () -> Int) -> Int { return f() }
fn emitter() -> Int { let x = boom(); return x }
component C provides svc: Svc {
  provide svc { fn run() -> Int { return dispatch(emitter) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    # the unnameable boundary `*` is present and unbounded, never counted 0
    star = card["C"]["per_capability"]["*"]
    assert star["bound"] is None
    assert star["kind"] == "unbounded"
    assert "first-class" in star["reason"]


# --------------------------------------------------------------------------
# §1 / Exit-5 - byte-identity of the per-component entry, top-level key present
# --------------------------------------------------------------------------

def test_crossing_free_component_entry_byte_identical_top_level_key_present():
    ir = compile_source("""
service Svc { fn run() -> Int }
component C provides svc: Svc { provide svc { fn run() = 0 } }
""", "t.rvl")
    report = audit_report(ir)
    # the top-level document gains the key even when nothing crosses (LOW fix)
    assert "cardinality" in report
    assert report["cardinality"] == {}
    # but the per-component boundary ENTRY is byte-identical to today: no
    # cardinality member is spliced into it.
    assert "cardinality" not in report["boundary"]["C"]
    assert report["boundary"]["C"] == {
        "emissions": [], "capabilities": {}, "compensated": 0,
        "awaits": 0, "externs": [],
    }


# --------------------------------------------------------------------------
# the audit surfaces carry it
# --------------------------------------------------------------------------

def test_audit_json_body_matches_audit_report(capsys):
    """`revl audit --json` carries `cardinality` and it is byte-for-byte the
    `audit_report` value (the additive-body invariant test relies on this)."""
    code = main(["audit", str(EXAMPLES / "user_cache.rvl"), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    body = {k: v for k, v in doc.items() if k not in ("schema_version", "kind")}
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    assert "cardinality" in body
    assert body["cardinality"] == audit_report(ir)["cardinality"]


def test_text_audit_renders_cardinality_line(capsys):
    code = main(["audit", str(EXAMPLES / "user_cache.rvl")])
    assert code == 0
    out = capsys.readouterr().out
    # UserCache crosses its `db` boundary exactly once per activation
    assert "cardinality:" in out
    assert "per activation" in out


# --------------------------------------------------------------------------
# the branch-count fold takes the MAX over arms, never the SUM
# --------------------------------------------------------------------------

def test_merge_max_is_max_not_sum():
    total = {"db": 1}                       # the scrutinee/cond side sums in
    _merge_max(total, [{"db": 2}, {"db": 1}, {"model": 3}])
    assert total == {"db": 3, "model": 3}   # 1 (base) + max(2,1)=2 -> 3; model 3


# ==========================================================================
# Slice 2 - bounded-iteration certification (docs/design/260 §2.2)
# ==========================================================================

# The harness `run_loop` shape: a single-fn self-recursion whose fuel `n`
# strictly decreases by 1, dominated by a base guard, invoking an emitting arrow
# once per iteration. The crossing rides the arrow (a pure top-level fn cannot
# emit through a required key), so the linear ceiling is `max_iters * per_iter`.
_HEAD = """
service Model { emission[model] fn complete(m: List[Str]) -> Str }
service Loop { emission[model] fn run(s: Str) -> Str }
"""

_RUN_LOOP = """
fn run_loop(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let r = step(msgs)
  return run_loop(msgs, step, n - 1)
}
"""


def test_linear_self_recursion_literal_fuel_certifies_to_a_finite_ceiling():
    """A single-self-call `run_loop(n)`, fuel `n` decreasing by 1 under a base
    guard, invoking an emitting arrow once per iteration, certifies to the exact
    `caps x max-iters` ceiling: model crossed at most N0 times per activation."""
    card = _card(_HEAD + _RUN_LOOP + """
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(s) -> Str {
    let msgs = [s]
    return run_loop(msgs, msgs2 => emit model.complete(msgs2), 5)
  } }
}
""")
    assert card["Agent"]["verdict"] == "bounded"
    # max_iters = ceil((5 - 0) / 1) = 5; per_iter arrow invocations = 1
    assert card["Agent"]["per_capability"]["model"] == {"bound": 5,
                                                        "kind": "bounded"}


def test_linear_self_recursion_config_fuel_is_bounded_symbolic():
    """When the initial fuel is a `config` field (not yet pinned), the ceiling is
    symbolic: `bounded-symbolic`, carrying the config expr and per-iteration
    crossings, resolved to an integer only once the manifest pins the field
    (docs/design/260 §2.2)."""
    card = _card(_HEAD + _RUN_LOOP + """
component Agent requires model: Model provides agent: Loop {
  config { max_steps: Int }
  provide agent { fn run(s) -> Str {
    let msgs = [s]
    return run_loop(msgs, msgs2 => emit model.complete(msgs2), config.max_steps)
  } }
}
""")
    assert card["Agent"]["verdict"] == "bounded-symbolic"
    assert card["Agent"]["per_capability"]["model"] == {
        "bound": None, "kind": "bounded-symbolic",
        "expr": "config.max_steps", "per_iter": 1}


def test_certified_ceiling_scales_with_the_decrement():
    """The ceiling is `ceil((N0 - c) / k)`: fuel decreasing by 2 from 8 crosses
    at most 4 times, not 8. A false linear form blind to `k` would over- or
    under-count."""
    card = _card(_HEAD + """
fn run_loop(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let r = step(msgs)
  return run_loop(msgs, step, n - 2)
}
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(s) -> Str {
    let msgs = [s]
    return run_loop(msgs, msgs2 => emit model.complete(msgs2), 8)
  } }
}
""")
    assert card["Agent"]["per_capability"]["model"] == {"bound": 4,
                                                        "kind": "bounded"}


# --------------------------------------------------------------------------
# THE CRITICAL (docs/design/260 §2.2 clause 4, Exit-3): branching / tree
# recursion fans out to ~2^n and MUST report `unbounded`, never `<= n`.
# --------------------------------------------------------------------------

def test_CRITICAL_branching_recursion_host_extern_is_unbounded():
    """The exact case from the design brief: `f(n){ if(n<=0){return}; emit_once();
    f(n-1); f(n-1) }`. Two self-calls on one path fan out exponentially; the
    capability MUST be `unbounded`, never a linear ceiling `<= n`."""
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn emit_once() -> Int = @py { return 1 }
fn f(n: Int) -> Int {
  if (n <= 0) { return 0 }
  let a = emit_once()
  let b = f(n - 1)
  return f(n - 1)
}
component C provides svc: Svc {
  provide svc { fn run() -> Int { return emit f(5) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["emit_once"]
    assert entry["bound"] is None
    assert entry["kind"] == "unbounded"
    # never a finite ceiling of any kind
    assert "bound" not in entry or entry["bound"] is None


def test_CRITICAL_branching_arrow_mediated_is_unbounded_not_linear():
    """The sharper CRITICAL: the fan-out rides an emitting ARROW, so the crossing
    is otherwise countable and a clause-4-blind recognizer WOULD certify a false
    `model <= n`. It reaches two self-calls on a path, so it MUST be `unbounded`
    with the fan-out reason, never `bounded` and never `bounded-symbolic`."""
    card = _card(_HEAD + """
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let x = step(msgs)
  let a = tree(step, msgs, n - 1)
  return tree(step, msgs, n - 1)
}
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(s) -> Str {
    let msgs = [s]
    return tree(msgs2 => emit model.complete(msgs2), msgs, 5)
  } }
}
""")
    entry = card["Agent"]["per_capability"]["model"]
    assert card["Agent"]["verdict"] == "unbounded"
    assert entry["kind"] == "unbounded"          # NOT bounded, NOT symbolic
    assert entry["bound"] is None
    # the reason names the fan-out and clause 4 - the CRITICAL fix
    assert "fans out" in entry["reason"]
    assert "clause 4" in entry["reason"]


# --------------------------------------------------------------------------
# Issue #2699: branching self-recursion with a SMALL LITERAL fuel certifies to
# `(k^(N+1) - 1) / (k - 1)` activations times the per-call crossings. Anything
# that is not a literal, or whose activation count is over the ceiling, stays
# `unbounded` with the fan-out reason.
# --------------------------------------------------------------------------

def _tree_agent(fuel: str, calls: int = 2, config: str = "") -> dict:
    recur = "\n".join(
        ("  return" if i == calls - 1 else f"  let a{i} =") + " tree(step, msgs, n - 1)"
        for i in range(calls))
    return _agent_model_verdict(f"""
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {{
  if (n <= 0) {{ return "stop" }}
  let x = step(msgs)
{recur}
}}""", f"tree(msgs2 => emit model.complete(msgs2), msgs, {fuel})", config=config)


def test_branching_recursion_literal_fuel_three_is_a_finite_bound_of_fifteen():
    """Two self-calls, fuel 3, one crossing per call: 1 + 2 + 4 + 8 = 15
    activations, `2^4 - 1`, each crossing the model once."""
    agent = _tree_agent("3")
    assert agent["verdict"] == "bounded"
    assert agent["per_capability"]["model"] == {"bound": 15, "kind": "bounded"}


def test_branching_recursion_three_calls_uses_the_geometric_sum():
    """k = 3, fuel 2: 1 + 3 + 9 = 13 activations."""
    agent = _tree_agent("2", calls=3)
    assert agent["per_capability"]["model"] == {"bound": 13, "kind": "bounded"}


def test_branching_recursion_bound_is_not_below_the_real_count():
    """The reported bound must be at least the crossings a run makes. The
    function is run by hand: each activation with fuel > 0 crosses once, and
    the leaves (fuel 0) cross zero times, so 7 crossings at fuel 3; the bound
    15 covers it."""
    def real(n):
        return 0 if n <= 0 else 1 + 2 * real(n - 1)
    agent = _tree_agent("3")
    assert agent["per_capability"]["model"]["bound"] >= real(3)


def test_branching_recursion_over_the_ceiling_stays_unbounded():
    """The same shape with fuel 5 (63 activations) and fuel 60 is over the
    ceiling: `unbounded` with the fan-out reason, never a number."""
    for fuel in ("5", "60"):
        agent = _tree_agent(fuel)
        entry = agent["per_capability"]["model"]
        assert agent["verdict"] == "unbounded", fuel
        assert entry["kind"] == "unbounded" and entry["bound"] is None, fuel
        assert "fans out" in entry["reason"] and "clause 4" in entry["reason"]


def test_branching_recursion_at_the_ceiling_is_still_certified():
    """fuel 4 is 31 activations, exactly the ceiling."""
    agent = _tree_agent("4")
    assert agent["per_capability"]["model"] == {"bound": 31, "kind": "bounded"}


def test_branching_recursion_config_fuel_stays_unbounded():
    """A config-field fuel is not a small literal: never bounded-symbolic for a
    branching recursion (the issue leaves it unbounded)."""
    agent = _tree_agent("config.max_steps", config="config { max_steps: Int }")
    entry = agent["per_capability"]["model"]
    assert agent["verdict"] == "unbounded"
    assert entry["kind"] == "unbounded"
    assert "fans out" in entry["reason"]


def test_branching_recursion_derived_fuel_stays_unbounded():
    agent = _tree_agent("1 + 2")
    assert agent["per_capability"]["model"]["kind"] == "unbounded"


def _assert_fanout_unbounded(agent: dict):
    entry = agent["per_capability"]["model"]
    assert agent["verdict"] == "unbounded"
    assert entry["kind"] == "unbounded" and entry["bound"] is None
    assert "fans out" in entry["reason"] and "clause 4" in entry["reason"]


_RUN_TREE = "tree(msgs2 => emit model.complete(msgs2), msgs, 3)"


class _TimeBound:
    """Fail fast instead of hanging CI (no pytest-timeout here): a SIGALRM
    after `seconds`, where the platform has one."""

    def __init__(self, seconds: int = 20):
        self.seconds = seconds

    def __enter__(self):
        import signal
        self._signal = signal if hasattr(signal, "SIGALRM") else None
        if self._signal is not None:
            def fire(signum, frame):
                raise AssertionError(
                    f"did not finish within {self.seconds}s (a huge literal "
                    "fuel must hit the ceiling guard, not a huge power)")
            self._old = signal.signal(signal.SIGALRM, fire)
            signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        if self._signal is not None:
            self._signal.alarm(0)
            self._signal.signal(self._signal.SIGALRM, self._old)
        return False


def test_branching_guard_is_hit_for_a_huge_fuel_without_a_huge_power():
    """`_branching_calls` stops as soon as it passes the ceiling, so a huge
    literal fuel neither builds a huge integer nor hangs."""
    from revl.cardinality import _BRANCHING_CALL_CEILING, _branching_calls
    with _TimeBound():
        for iters in (10**6, 10**18):
            assert _branching_calls(2, iters) > _BRANCHING_CALL_CEILING
            assert _branching_calls(3, iters) > _BRANCHING_CALL_CEILING
    assert _branching_calls(2, 3) == 15


def test_branching_calls_with_no_iterations_is_one_activation():
    from revl.cardinality import _branching_calls
    assert _branching_calls(2, 0) == 1
    assert _branching_calls(3, 0) == 1
    assert _branching_calls(2, 1) == 3


def test_branching_recursion_fuel_zero_is_one_activation():
    """Fuel 0 hits the base arm at once: one activation. The bound charges it
    the larger of the base and continuation counts (1), a safe over-count."""
    agent = _tree_agent("0")
    assert agent["verdict"] == "bounded"
    assert agent["per_capability"]["model"]["bound"] == 1


def test_branching_recursion_huge_literal_fuel_stays_unbounded():
    with _TimeBound():
        for fuel in ("1000000", str(10**18)):
            _assert_fanout_unbounded(_tree_agent(fuel))


def test_branching_recursion_with_a_self_call_in_a_local_arrow_stays_refused():
    """Two syntactic self-calls plus a self-call inside a local arrow invoked
    twice: the arrow makes the real call count larger than the syntactic one,
    so the branching path must refuse (the old fan-out refusal and reason).
    Fuel 2 keeps the syntactic count (3 per path) under the ceiling, so only
    the arrow rule can refuse it."""
    agent = _agent_model_verdict("""
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let x = step(msgs)
  let g = m => tree(step, m, n - 1)
  let a = g(msgs)
  let b = g(msgs)
  let c = tree(step, msgs, n - 1)
  return tree(step, msgs, n - 1)
}""", "tree(msgs2 => emit model.complete(msgs2), msgs, 2)")
    _assert_fanout_unbounded(agent)


def test_branching_recursion_with_a_self_call_in_a_loop_body_stays_refused():
    """A branching recursion whose extra self-call sits inside a `while` body
    must be refused as unbounded, never certified: the loop runs that call once
    per iteration, which the syntactic per-path count does not cover. The
    uncounted-construct check must flag a real lowered loop body, not only a
    local arrow (fuel 2 keeps the syntactic count of 2 per path under the
    ceiling)."""
    from revl.cardinality import _self_call_in_uncounted_construct
    body = """
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let x = step(msgs)
  var i = 0
  while (i < n) { let a = tree(step, msgs, n - 1); i = i + 1 }
  return tree(step, msgs, n - 1)
}"""
    ir = compile_source(_HEAD + body, "t.rvl")
    decl = next(fn for fn in ir["functions"] if fn.get("name") == "tree")
    assert _self_call_in_uncounted_construct(decl["body"], "tree") is True
    agent = _agent_model_verdict(
        body, "tree(msgs2 => emit model.complete(msgs2), msgs, 2)")
    assert agent["verdict"] == "unbounded"
    entry = agent["per_capability"]["model"]
    assert entry["kind"] == "unbounded" and entry["bound"] is None


def test_branching_recursion_that_fails_clause_one_keeps_the_fanout_reason():
    """A branching recursion whose fuel does not decrease on one back-edge was
    reported with the fan-out reason before #2699 and still is."""
    agent = _agent_model_verdict("""
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let x = step(msgs)
  let a = tree(step, msgs, n)
  return tree(step, msgs, n - 1)
}""", _RUN_TREE)
    _assert_fanout_unbounded(agent)


def test_branching_recursion_with_an_escaping_param_keeps_the_fanout_reason():
    agent = _agent_model_verdict("""
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let alias = step
  let x = alias(msgs)
  let a = tree(step, msgs, n - 1)
  return tree(step, msgs, n - 1)
}""", _RUN_TREE)
    _assert_fanout_unbounded(agent)


# --------------------------------------------------------------------------
# the recognizer is narrow: every shape it cannot certify stays `unbounded`
# --------------------------------------------------------------------------

def _agent_model_verdict(src_body: str, callsite: str, config: str = "") -> dict:
    card = _card(_HEAD + src_body + f"""
component Agent requires model: Model provides agent: Loop {{
  {config}
  provide agent {{ fn run(s) -> Str {{
    let msgs = [s]
    return {callsite}
  }} }}
}}
""")
    return card["Agent"]


def test_non_decreasing_fuel_stays_unbounded():
    """A self-call that passes `n` unchanged (no strict decrease) fails clause 1
    and stays `unbounded`, never certified (docs/design/260 §2.2 clause 1)."""
    agent = _agent_model_verdict("""
fn r(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "s" }
  let x = step(msgs)
  return r(msgs, step, n)
}""", "r(msgs, msgs2 => emit model.complete(msgs2), 5)")
    assert agent["verdict"] == "unbounded"
    assert agent["per_capability"]["model"]["kind"] == "unbounded"


def test_no_base_guard_stays_unbounded():
    """A recursion with no dominating base guard fails clause 2 and stays
    `unbounded` (docs/design/260 §2.2 clause 2)."""
    agent = _agent_model_verdict("""
fn r(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  let x = step(msgs)
  return r(msgs, step, n - 1)
}""", "r(msgs, msgs2 => emit model.complete(msgs2), 5)")
    assert agent["verdict"] == "unbounded"
    assert agent["per_capability"]["model"]["kind"] == "unbounded"


def test_non_resolvable_initial_fuel_stays_unbounded():
    """A bounded-SHAPED recursion whose initial fuel is neither a literal nor a
    config field (here `config.max_steps + 1`, an arithmetic expression) has the
    right shape but no provable ceiling, so it stays `unbounded` (clause 3)."""
    agent = _agent_model_verdict("""
fn r(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "s" }
  let x = step(msgs)
  return r(msgs, step, n - 1)
}""", "r(msgs, msgs2 => emit model.complete(msgs2), config.max_steps + 1)",
        config="config { max_steps: Int }")
    assert agent["verdict"] == "unbounded"
    entry = agent["per_capability"]["model"]
    assert entry["kind"] == "unbounded"
    assert "initial fuel" in entry["reason"]


def test_mutual_recursion_stays_unbounded():
    """Multi-fn / mutual recursion is out of scope this slice (§5.2 OPEN):
    certification is single-fn self-recursion only, so a mutual SCC stays
    `unbounded` (sound, never over-claiming)."""
    agent = _agent_model_verdict("""
fn ping(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "s" }
  let x = step(msgs)
  return pong(msgs, step, n - 1)
}
fn pong(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "s" }
  return ping(msgs, step, n - 1)
}""", "ping(msgs, msgs2 => emit model.complete(msgs2), 5)")
    assert agent["verdict"] == "unbounded"
    assert agent["per_capability"]["model"]["kind"] == "unbounded"
    assert "mutual" in agent["per_capability"]["model"]["reason"]


def test_certified_symbolic_renders_on_text_audit(tmp_path):
    """The bounded-symbolic ceiling renders under `capabilities:` as
    `model <= config.max_steps per activation (1 per iteration)` (§1.2)."""
    src = _HEAD + _RUN_LOOP + """
component Agent requires model: Model provides agent: Loop {
  config { max_steps: Int }
  provide agent { fn run(s) -> Str {
    let msgs = [s]
    return run_loop(msgs, msgs2 => emit model.complete(msgs2), config.max_steps)
  } }
}
"""
    path = tmp_path / "agent.rvl"
    path.write_text(src)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["audit", str(path)])
    assert code == 0
    out = buf.getvalue()
    assert "cardinality: model <= config.max_steps per activation" in out
    assert "(1 per iteration)" in out


def test_self_call_inside_local_arrow_invoked_twice_stays_unbounded():
    """A local arrow's body contains one static self-call node but the
    recursive path invokes that arrow twice, so its actual recursion branches."""
    agent = _agent_model_verdict("""
fn recurse_twice(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let x = step(msgs)
  let again = next => recurse_twice(next, step, n - 1)
  let first = again(msgs)
  return again(msgs)
}""", "recurse_twice(msgs, msgs2 => emit model.complete(msgs2), 5)")
    entry = agent["per_capability"]["model"]
    assert agent["verdict"] == "unbounded"
    assert entry["kind"] == "unbounded"
    assert entry["bound"] is None
    assert "recursion" in entry["reason"]
