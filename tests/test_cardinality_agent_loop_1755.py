"""Issue #1755: an agent loop that carries state certifies, and an arrow that
reaches the loop inside data is never dropped (item 77(a) / item 260 §2.2).

Before: clause 1 required every non-fuel argument of the self-call to be passed
unchanged, so a loop that grows its history (`h.push(act(a))`) was `unbounded`
with a reason that blamed the fuel. And a parameter whose call-site argument
carried a crossing but was not invoked directly (a record field, a list
element, a `let` alias) got multiplicity 0, so the component vanished from the
cardinality report, which reads as crossing-free.

After: the identity rule binds only function-typed parameters, and a
parameter that carries a crossing may be used only by invoking it or threading
it unchanged into the recursive call; any other use is `unbounded` with its
own reason.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.cardinality import _type_mentions_fn, cardinality  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cordis-py runtime not installed (sh backends/python/setup.sh)")

HEAD = """
type Action = Final(Str) | Search(Str) | Write(Str)
service Model  { emission[model] fn next(h: List[Str]) -> Action }
service Search { fn find(q: Str) -> Str }
service Fs     { emission[fs] fn write(body: Str) -> Str }
service Agent  { emission[model, fs] fn run(p: Str) -> Str }
"""

#: the agent component: the model decides, a dispatch arrow sends each action
#: to its capability (a pure read, or an emission through `fs`)
AGENT = """
component Agent requires model: Model, search: Search, fs: Fs provides agent: Agent {
  config { max_steps: Int = 5 }
  provide agent {
    fn run(p) {
      return agent_loop([p],
        h => emit model.next(h),
        a => match a {
          Search(q) => search.find(q),
          Write(b) => emit fs.write(b),
          Final(x) => x,
        },
        FUEL)
    }
  }
}
"""

SINGLE_EDGE = """
fn agent_loop(h: List[Str], decide: (List[Str]) -> Action,
              act: (Action) -> Str, n: Int) -> Str {
  if (n <= 0) { return "max_steps" }
  let a = decide(h)
  return match a {
    Final(x) => x,
    _ => agent_loop(h.push(act(a)), decide, act, n - 1),
  }
}
"""

#: one back-edge per action arm: still linear, one arm runs per iteration
PER_ARM = """
fn agent_loop(h: List[Str], decide: (List[Str]) -> Action,
              act: (Action) -> Str, n: Int) -> Str {
  if (n <= 0) { return "max_steps" }
  return match decide(h) {
    Final(a) => a,
    Search(q) => agent_loop(h.push(act(Search(q))), decide, act, n - 1),
    Write(b) => agent_loop(h.push(act(Write(b))), decide, act, n - 1),
  }
}
"""


def _agent(loop: str, fuel: str = "config.max_steps") -> dict:
    src = HEAD + loop + AGENT.replace("FUEL", fuel)
    return cardinality(compile_source(src, "t.rvl"))["Agent"]


# ------------------------------------------------- stateful loops certify

@pytest.mark.parametrize("loop", [SINGLE_EDGE, PER_ARM], ids=["single-edge", "per-arm"])
def test_an_agent_loop_that_grows_its_history_is_bounded_symbolic(loop):
    card = _agent(loop)
    assert card["verdict"] == "bounded-symbolic"
    for cap in ("model", "fs"):
        assert card["per_capability"][cap] == {
            "bound": None, "kind": "bounded-symbolic",
            "expr": "config.max_steps", "per_iter": 1}, cap


@pytest.mark.parametrize("loop", [SINGLE_EDGE, PER_ARM], ids=["single-edge", "per-arm"])
def test_with_a_literal_fuel_the_ceiling_is_exact(loop):
    card = _agent(loop, fuel="4")
    assert card["verdict"] == "bounded"
    assert card["per_capability"]["model"] == {"bound": 4, "kind": "bounded"}
    assert card["per_capability"]["fs"] == {"bound": 4, "kind": "bounded"}


# ------------------------------------------------- function-typed arguments

HEAD_ONE = """
service Model { emission[model] fn complete(m: List[Str]) -> Str }
service Loop { emission[model] fn run(s: Str) -> Str }
"""


def _one(loop: str, arg: str) -> dict:
    src = HEAD_ONE + loop + """
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(x) -> Str { return run_loop([x], ARG, 5) } }
}
""".replace("ARG", arg)
    return cardinality(compile_source(src, "t.rvl"))["Agent"]


def test_a_function_argument_rebound_on_the_back_edge_stays_unbounded():
    loop = """
fn run_loop(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let r = step(msgs)
  return run_loop(msgs, m => step(m), n - 1)
}
"""
    card = _one(loop, "m => emit model.complete(m)")
    entry = card["per_capability"]["model"]
    assert entry["kind"] == "unbounded"
    assert "different value for a function-typed parameter" in entry["reason"]


# ------------------------------------------------- an arrow inside data

ESCAPES = {
    "record field": ("type Steps = { step: (List[Str]) -> Str }\n", "s: Steps",
                     "let f = s.step\n  let r = f(msgs)", "s",
                     "{ step: m => emit model.complete(m) }"),
    "list element": ("", "fs: List[(List[Str]) -> Str]",
                     "let f = fs[0]\n  let r = f(msgs)", "fs",
                     "[m => emit model.complete(m)]"),
    "let alias": ("", "step: (List[Str]) -> Str",
                  "let f = step\n  let r = f(msgs)", "step",
                  "m => emit model.complete(m)"),
}


@pytest.mark.parametrize("shape", sorted(ESCAPES))
def test_an_arrow_reaching_the_loop_through_data_is_unbounded_not_dropped(shape):
    types, sig, use, pname, arg = ESCAPES[shape]
    loop = types + f"""
fn run_loop(msgs: List[Str], {sig}, n: Int) -> Str {{
  if (n <= 0) {{ return "stop" }}
  {use}
  return run_loop(msgs, {pname}, n - 1)
}}
"""
    card = _one(loop, arg)   # KeyError before: Agent was missing entirely
    entry = card["per_capability"]["model"]
    assert entry["kind"] == "unbounded"
    assert "other than by invoking it or passing it unchanged" in entry["reason"]


def test_a_record_holding_an_arrow_moved_to_another_position_is_refused():
    """`Steps` holds a function, so it is function-typed for the identity rule:
    swapping two of them on the back-edge is a rebinding, never certified."""
    loop = """
type Steps = { step: (List[Str]) -> Str }
fn run_loop(a: Steps, b: Steps, n: Int) -> Str {
  if (n <= 0) { return "stop" }
  let f = a.step
  let r = f(["x"])
  return run_loop(b, a, n - 1)
}
"""
    src = HEAD_ONE + loop + """
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(x) -> Str {
    return run_loop({ step: m => emit model.complete(m) }, { step: m => "q" }, 5)
  } }
}
"""
    entry = cardinality(compile_source(src, "t.rvl"))["Agent"]["per_capability"]["model"]
    assert entry["kind"] == "unbounded"
    assert "different value for a function-typed parameter" in entry["reason"]


# ------------------------------------------------- the type test

@pytest.mark.parametrize("written,types,expected", [
    ("List[Str]", {}, False),
    ("Map[Str, List[Int]]", {}, False),
    ("(Int) -> Int", {}, True),
    ("List[(Int) -> Int]", {}, True),
    ("Steps", {"Steps": {"kind": "record", "fields": {"step": "(Str) -> Str"}}}, True),
    ("Box", {"Box": {"kind": "record", "fields": {"n": "Int"}}}, False),
    ("Action", {"Action": {"kind": "variant", "cases": [
        {"name": "A", "payload": "Str"}, {"name": "B", "payload": None}]}}, False),
    ("Thunk", {"Thunk": {"kind": "variant", "cases": [
        {"name": "Later", "payload": "() -> Int"}]}}, True),
    ("Tree", {"Tree": {"kind": "variant", "cases": [
        {"name": "Leaf", "payload": "Int"}, {"name": "Node", "payload": "List[Tree]"}]}},
     False),
    ("T", {}, True),        # a generic parameter could be a function
    (None, {}, True),       # an unknown type keeps the identity rule
])
def test_whether_a_type_can_hold_a_function(written, types, expected):
    assert _type_mentions_fn(written, types) is expected


# ------------------------------------------------- it still runs (a control)

@needs_runtime
def test_the_agent_loop_runs_on_py():
    from revl.mcp import server
    script = """
fn script(n: Int) -> Action {
  if (n == 1) { return Search("revl") }
  if (n == 2) { return Write("notes") }
  return Final("done after " + n.to_str())
}
component ScriptedModel provides model: Model { provide model { fn next(h) = script(h.length()) } }
component Index provides search: Search { provide search { fn find(q) = "hit:" + q } }
component Disk provides fs: Fs { provide fs { fn write(b) = "wrote:" + b } }
"""
    src = HEAD + SINGLE_EDGE + script + AGENT.replace("FUEL", "config.max_steps")

    def call(tool, arguments):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": tool, "arguments": arguments}}
                             )["result"]["structuredContent"]

    try:
        assert call("revl_load", {"source": src})["ok"] is True
        result = call("revl_call", {"key": "agent", "method": "run", "args": ["go"]})
        assert result["ok"] is True and result["result"] == "done after 3", result
    finally:
        if server.SESSION.loaded:
            server.SESSION.unload()
