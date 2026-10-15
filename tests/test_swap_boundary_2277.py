"""A hot-swap can be asked to prove its boundary (issue #2277).

`Session.swap` disposes the predecessor generation and boots the successor in
the same process, with nothing between the two: the R4 residue report `unload`
produces was never consulted at a swap, and nothing compared the process
against where it started. With `prove_boundary=True` the swap seals the
boundary after the predecessor's teardown and before the successor's load. Any
non-baseline value refuses the swap by name and the predecessor is booted again
so it keeps serving; a clean seal carries the proof as `boundary`.

Every test drives the Session API directly, one process, one session: a
composition that answers, a swap asked for the proof, then the same call again
to see which generation is serving. Refusals are asserted by `code` on the
`SessionError` the swap raises, so on a base without the option each test fails
on its own rather than the whole file erroring at import.
"""

from __future__ import annotations

import ast
import asyncio
import collections.abc
import contextvars
import copy
import importlib.util
import itertools
import os
import sys
import threading
import weakref
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.mcp.session import Session, SessionError

ROOT = Path(__file__).resolve().parents[1]
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap boots a live cordis-py composition; install it with "
           "`sh backends/python/setup.sh` and run under its venv")

#: A component that acquires a host map and releases it: a clean teardown.
CLEAN = """
service Counter { fn next() -> Int }
component CounterSvc provides counter: Counter {
  let m = effect Map.new() undo m.drop()
  provide counter { fn next() = 7 }
}
"""

#: No host acquisition at all, for the driver that captures no host trace.
PLAIN = """
service Counter { fn next() -> Int }
component CounterSvc provides counter: Counter {
  provide counter { fn next() = 7 }
}
"""

#: A service whose one method runs a host body, so a test can make the
#: predecessor do something to the process before the swap.
_HOST = """
extern emission fn host_poke() = @py {{
{body}
    return
}}
service Probe {{ emission fn poke() }}
component ProbeSvc provides probe: Probe {{
  provide probe {{ fn poke() {{ emit host_poke() }} }}
}}
service Counter {{ fn next() -> Int }}
component CounterSvc provides counter: Counter {{
  provide counter {{ fn next() = 7 }}
}}
"""

#: The host stub the witnessed-mutation test renames (and its undo).
WITNESSED = """
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py {
    import os
    if os.path.exists(w['bak']):
        os.replace(w['bak'], w['path'])
    return
}
extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError] undo unstash(result) = @py {
    import os
    bak = p + '.bak'
    os.replace(p, bak)
    return Ok({'path': p, 'bak': bak})
}
service Ops { emission fn stash(p: Str) }
component Agent provides ops: Ops {
  provide ops { fn stash(p) { effect stash_path(p) } }
}
service Counter { fn next() -> Int }
component CounterSvc provides counter: Counter {
  provide counter { fn next() = 7 }
}
"""

#: Every check a sealed boundary carries.
CHECKS = {"residue", "hostResources", "escrow", "registers", "threads", "tasks",
          "env", "modules"}


def _ir(source: str, name: str = "boundary.rvl") -> dict:
    return copy.deepcopy(compile_source(source, name))


def _successor(source: str) -> dict:
    """`source` answering 8 instead of 7, so a swap that landed is visible."""
    return _ir(source.replace("fn next() = 7", "fn next() = 8"))


def _host(body: str) -> str:
    return _HOST.format(body="\n".join("    " + line for line in body.splitlines()))


def _next(session) -> int:
    return session.call("counter", "next")["result"]


def _refused(session, successor: dict, **options) -> SessionError:
    with pytest.raises(SessionError) as caught:
        session.swap(successor, prove_boundary=True, **options)
    return caught.value


@pytest.fixture
def session():
    live = Session()
    yield live
    if live.loaded:
        live.unload()


def _drop_fails(monkeypatch):
    """The predecessor's map is never released: its `drop` raises. The undo
    still runs, so the four cordis counters drain, but the host trace has a
    `new` with no `drop` (the compiler already refuses an `undo` that is not
    the release, so this is how a release goes missing at run time)."""
    import runtime

    def fail(self):
        raise RuntimeError("injected: the map would not drop")

    monkeypatch.setattr(runtime.Map, "drop", fail)


def test_swap_refuses_when_predecessor_leaves_residue(session, monkeypatch):
    session.load(_ir(CLEAN))
    assert _next(session) == 7
    _drop_fails(monkeypatch)
    refusal = _refused(session, _successor(CLEAN))
    assert refusal.code == "RESIDUE_LEFT", refusal
    assert refusal.boundary["checks"]["hostResources"] is False
    [unreleased] = refusal.boundary["detail"]["residue"]["unreleased"]
    assert "new() with no drop()" in unreleased
    assert refusal.boundary["noResidue"] is False
    # the base generation keeps serving
    assert _next(session) == 7
    monkeypatch.undo()


def test_clean_swap_carries_boundary_proof(session):
    session.load(_ir(CLEAN))
    state = session.swap(_successor(CLEAN), prove_boundary=True)
    boundary = state["boundary"]
    assert boundary["noResidue"] is True, boundary
    assert set(boundary["checks"]) == CHECKS
    assert all(boundary["checks"].values()), boundary["checks"]
    assert "refusals" not in boundary and "unverified" not in boundary
    assert _next(session) == 8
    # and a second proven swap seals against the same baseline
    again = session.swap(_ir(CLEAN), prove_boundary=True)
    assert again["boundary"]["noResidue"] is True, again["boundary"]
    assert _next(session) == 7


def test_swap_without_host_trace_refuses_unverified(session):
    session.load(_ir(PLAIN))
    del session._driver.host_events     # a driver that captured no host trace
    refusal = _refused(session, _successor(PLAIN))
    assert refusal.code == "BOUNDARY_UNVERIFIED", refusal
    assert refusal.boundary["unverified"] == ["hostResources"]
    assert "hostResources" not in refusal.boundary["checks"]
    assert refusal.boundary["noResidue"] is False
    assert _next(session) == 7


def test_swap_refuses_escrow_open(session, tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("deliverable", encoding="utf-8")
    session.load(_ir(WITNESSED), record=True)
    session.call("ops", "stash", [str(artifact)])   # witnessed, not committed
    assert not artifact.exists()
    refusal = _refused(session, _successor(WITNESSED))
    assert refusal.code == "ESCROW_OPEN", refusal
    assert refusal.boundary["detail"]["escrowed"] >= 1
    assert _next(session) == 7
    # the mutation is still the session's to settle: an abort reverts it
    assert session.abort()["aborted"]
    assert artifact.read_text(encoding="utf-8") == "deliverable"


def test_boundary_refuses_register_drift(session):
    """A trace observer subscribed while generation N was live and never
    unsubscribed. A user `@py` body cannot import the runtime (the backend-
    import jail refuses it), so the subscription is made from here, inside N's
    lifetime, through the runtime's own `add_trace`."""
    import runtime

    session.load(_ir(PLAIN))
    unsubscribe = runtime.add_trace(lambda event: None)
    try:
        refusal = _refused(session, _successor(PLAIN))
        assert refusal.code == "REGISTER_DRIFT", refusal
        assert refusal.boundary["detail"]["registers"] == ["_observers"]
        assert _next(session) == 7
    finally:
        unsubscribe()


def test_boundary_refuses_thread_left(session):
    stop = threading.Event()
    sys.modules["_revl_test_2277_stop"] = stop   # how the host body reaches it
    session.load(_ir(_host(
        "import sys, threading\n"
        "stop = sys.modules['_revl_test_2277_stop']\n"
        "threading.Thread(target=stop.wait, name='left-by-gen-n').start()")))
    try:
        session.call("probe", "poke")
        refusal = _refused(session, _successor(_host("pass")))
        assert refusal.code == "THREAD_LEFT", refusal
        assert refusal.boundary["detail"]["threads"] == ["left-by-gen-n"]
        assert _next(session) == 7
    finally:
        stop.set()
        for thread in threading.enumerate():
            if thread.name == "left-by-gen-n":
                thread.join()
        del sys.modules["_revl_test_2277_stop"]


def test_boundary_refuses_task_left(session):
    """The session's event loop outlives a swap, so a task generation N left
    pending on it keeps running under the successor's calls."""
    gate = asyncio.Event()
    sys.modules["_revl_test_2277_gate"] = gate   # how the host body reaches it
    session.load(_ir(_host(
        "import sys, asyncio\n"
        "gate = sys.modules['_revl_test_2277_gate']\n"
        "async def left_by_gen_n():\n"
        "    await gate.wait()\n"
        "asyncio.get_running_loop().create_task(left_by_gen_n())")))
    try:
        session.call("probe", "poke")
        refusal = _refused(session, _successor(_host("pass")))
        assert refusal.code == "TASK_LEFT", refusal
        tasks = refusal.boundary["detail"]["tasks"]
        assert len(tasks) == 1 and tasks[0].endswith("left_by_gen_n"), tasks
        assert _next(session) == 7
    finally:
        gate.set()
        session._loop.run_until_complete(asyncio.sleep(0))
        del sys.modules["_revl_test_2277_gate"]


def test_boundary_refuses_a_replaced_module(session):
    """A name the baseline held that now maps to another module object is
    what every later `import` gets, the successor's included. A standard
    library name is no excuse: it is tolerated only when it is NEW."""
    import colorsys

    session.load(_ir(_host(
        "import sys, types\n"
        "sys.modules['colorsys'] = types.ModuleType('colorsys')")))
    try:
        session.call("probe", "poke")
        refusal = _refused(session, _successor(_host("pass")))
        assert refusal.code == "MODULE_LEFT", refusal
        assert refusal.boundary["detail"]["modulesReplaced"] == ["colorsys"]
        assert _next(session) == 7
    finally:
        sys.modules["colorsys"] = colorsys


def test_boundary_refuses_env_drift(session, monkeypatch):
    key = "REVL_TEST_2277_LEFT"
    monkeypatch.delenv(key, raising=False)
    session.load(_ir(_host(f"import os\nos.environ[{key!r}] = 'secret'")))
    try:
        session.call("probe", "poke")
        refusal = _refused(session, _successor(_host("pass")))
        assert refusal.code == "ENV_DRIFT", refusal
        assert refusal.boundary["detail"]["env"] == [key]
        assert "secret" not in repr(refusal.boundary)   # keys, never values
        assert _next(session) == 7
    finally:
        os.environ.pop(key, None)


def test_boundary_refuses_module_left(session, tmp_path, monkeypatch):
    """A module a host body imported under generation N that is neither the
    generation's own, the standard library nor revl's toolchain stays cached
    process-wide, so the successor could import N's copy of it."""
    name = "revl_test_2277_left"
    (tmp_path / f"{name}.py").write_text("STATE = []\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    session.load(_ir(_host(f"import {name}")))
    try:
        session.call("probe", "poke")
        refusal = _refused(session, _successor(_host("pass")))
        assert refusal.code == "MODULE_LEFT", refusal
        assert refusal.boundary["detail"]["modules"] == [name]
        assert _next(session) == 7
    finally:
        sys.modules.pop(name, None)


#: A component with resolved config. The runtime keeps every component's
#: resolved config (secret fields included), keyed by component name, and
#: never empties it, so each test that loads one names its own component
#: (`_configured`): a name another test file also configures would make the
#: table's contents depend on test order.
CONFIGURED = """
service Counter { fn next() -> Int }
component BoundaryCfg2277Svc provides counter: Counter {
  config { a: Int = 7 }
  provide counter { fn next() { return config.a } }
}
"""

#: The same service from a component with no config at all.
UNCONFIGURED = """
service Counter { fn next() -> Int }
component OtherSvc provides counter: Counter {
  provide counter { fn next() = 8 }
}
"""


def _configured(name: str, value: int = 7) -> dict:
    return _ir(CONFIGURED.replace("BoundaryCfg2277Svc", name)
                         .replace("a: Int = 7", f"a: Int = {value}"))


def test_the_predecessors_resolved_config_is_withdrawn_not_refused(session):
    """The resolved-config table is the toolchain's residue, not the author's:
    the seal withdraws the predecessor's entry rather than refusing every
    composition that declares `config`, and the successor no longer sees the
    values its predecessor ran with."""
    import runtime

    session.load(_ir(CONFIGURED))
    assert runtime.resolved_config("BoundaryCfg2277Svc") == {"a": 7}
    state = session.swap(_ir(UNCONFIGURED), prove_boundary=True)
    boundary = state["boundary"]
    assert boundary["noResidue"] is True, boundary
    assert boundary["detail"]["discharged"] == {
        "RESOLVED_CONFIG": ["BoundaryCfg2277Svc"]}
    assert runtime.resolved_config("BoundaryCfg2277Svc") is None
    assert _next(session) == 8


def test_a_reloaded_configured_composition_seals_clean(session):
    """The MCP server pattern: one process, one session, a load, an unload and
    a load again. The second boot's baseline already holds the first load's
    config entry and the second activation replaces it, so the predecessor's
    entry is not a key "added since the baseline". It is the predecessor's own
    by name, and is withdrawn, rather than refusing REGISTER_DRIFT."""
    import runtime

    name = "BoundaryReload2277Svc"
    session.load(_configured(name))
    session.unload()
    session.load(_configured(name))
    assert runtime.resolved_config(name) == {"a": 7}
    state = session.swap(_ir(UNCONFIGURED), prove_boundary=True)
    boundary = state["boundary"]
    assert boundary["noResidue"] is True, boundary
    assert boundary["detail"]["discharged"] == {"RESOLVED_CONFIG": [name]}
    assert _next(session) == 8


def test_discharge_spares_other_entries_and_a_refusal_restores(session,
                                                              monkeypatch):
    """The discharge withdraws only the predecessor's own entries. An entry
    under another name (another session's) outlives a clean proof and a
    refused one as the very same object, and a refused proof leaves the
    predecessor its own entry when it is booted again. The second refusal is
    an entry that appears mid-generation, as a session booting alongside this
    one would write it: the proof cannot attribute that, so it refuses, and
    the entry is still not withdrawn."""
    import runtime

    name = "BoundaryKeep2277Svc"
    earlier, later, grammar = {"b": 1}, {"c": 2}, {"gbnf": "root ::= x"}
    monkeypatch.setitem(runtime.RESOLVED_CONFIG, "OtherEarlier2277", earlier)
    monkeypatch.setitem(runtime._revl_grammars, "OtherEarlier2277.m", grammar)
    session.load(_configured(name))
    state = session.swap(_configured(name, 9), prove_boundary=True)
    assert state["boundary"]["noResidue"] is True, state["boundary"]
    assert state["boundary"]["detail"]["discharged"] == {"RESOLVED_CONFIG": [name]}
    assert runtime.RESOLVED_CONFIG["OtherEarlier2277"] is earlier
    assert runtime._revl_grammars["OtherEarlier2277.m"] is grammar
    assert _next(session) == 9

    monkeypatch.setitem(runtime.RESOLVED_CONFIG, "OtherLater2277", later)
    refusal = _refused(session, _configured(name, 11))
    assert refusal.code == "REGISTER_DRIFT", refusal
    assert refusal.boundary["detail"]["registers"] == ["RESOLVED_CONFIG"]
    assert refusal.boundary["detail"]["discharged"] == {"RESOLVED_CONFIG": [name]}
    assert runtime.RESOLVED_CONFIG["OtherEarlier2277"] is earlier
    assert runtime.RESOLVED_CONFIG["OtherLater2277"] is later
    assert runtime._revl_grammars["OtherEarlier2277.m"] is grammar
    assert runtime.resolved_config(name) == {"a": 9}
    assert _next(session) == 9


def test_a_refused_seal_restores_the_withdrawn_entries():
    """`restore` puts back exactly what `discharge` took, so the tables end
    where the swap found them even before the predecessor reboots."""
    import runtime
    from revl.mcp import swap_boundary

    name = "BoundaryRestore2277Svc"
    baseline = swap_boundary.take(runtime)
    entry = {"a": 1}
    runtime.RESOLVED_CONFIG[name] = entry
    try:
        withdrawn = swap_boundary.discharge(
            baseline, runtime, {"RESOLVED_CONFIG": frozenset({name})})
        assert name not in runtime.RESOLVED_CONFIG
        swap_boundary.restore(runtime, withdrawn)
        assert runtime.RESOLVED_CONFIG[name] is entry
    finally:
        runtime.RESOLVED_CONFIG.pop(name, None)


def test_an_owed_compensation_refuses_residue_left():
    """An unresolved compensation is one of the R4 checks (`compensations`)
    the residue check folds in. Through the Session API a swap does not reach
    the seal with one owed (an `abort` that leaves one unloads), so this
    drives the seal directly."""
    import runtime
    from revl.mcp import swap_boundary

    baseline = swap_boundary.take(runtime)
    residue = {"checks": {"registry": True, "compensations": False},
               "detail": {"compensationResidue": 1}, "unverified": []}
    proof = swap_boundary.seal(baseline, runtime, residue, None)
    assert proof["checks"]["residue"] is False
    assert proof["refusals"] == ["RESIDUE_LEFT"]


def test_a_realm_placement_seals_clean(session):
    """Realm labels are a process-wide intern table of stateless identities,
    shared across generations by design, so a placement does not refuse."""
    realm = CLEAN.replace("  let m =",
                          '  isolate counter in realm("billing")\n  let m =')
    session.load(_ir(realm))
    state = session.swap(_successor(realm), prove_boundary=True)
    assert state["boundary"]["noResidue"] is True, state["boundary"]
    assert _next(session) == 8


def test_proof_on_another_tier_refuses_tier_unsupported(session):
    session.load(_ir(CLEAN))
    refusal = _refused(session, _successor(CLEAN), backend="go")
    assert refusal.code == "TIER_UNSUPPORTED", refusal
    assert _next(session) == 7
    assert session._generation == 1    # nothing was torn down


def test_swap_without_the_option_is_unchanged(session, monkeypatch):
    """No proof asked: the leaky predecessor's swap lands exactly as it always
    has, and the answer carries no `boundary`."""
    session.load(_ir(CLEAN))
    _drop_fails(monkeypatch)
    state = session.swap(_successor(CLEAN))
    assert "boundary" not in state
    assert _next(session) == 8
    monkeypatch.undo()


def _mutable_globals(module) -> set:
    """Every global of `module` that can hold state across a generation: a
    mutable container, a ContextVar or a counter bound at module level, and
    every name a function rebinds with `global`."""
    kinds = (collections.abc.Mapping, collections.abc.MutableSequence,
             collections.abc.MutableSet, weakref.WeakSet,
             contextvars.ContextVar, itertools.count)
    found = {name for name, value in vars(module).items()
             if not name.startswith("__") and isinstance(value, kinds)}
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found.update(name for node in ast.walk(tree)
                 if isinstance(node, ast.Global) for name in node.names)
    return found


def test_every_runtime_global_is_compared_or_left_out_by_name():
    """`REGISTERS` is the list the seal compares; every other mutable global
    of the runtime is in `NOT_COMPARED` with its reason. A global added to the
    runtime later fails here until it is placed in one of the two."""
    import runtime
    from revl.mcp import swap_boundary

    compared = set(swap_boundary.REGISTERS)
    left_out = set(swap_boundary.NOT_COMPARED)
    assert not compared & left_out
    assert all(swap_boundary.NOT_COMPARED.values())
    assert _mutable_globals(runtime) == compared | left_out


def test_a_register_is_fingerprinted_by_content_one_level_down():
    """A mapping of any type is compared by what it holds (the frame-by-context
    table is a WeakKeyDictionary), and a handle appended to a bucket the
    baseline already held is new."""
    from revl.mcp import swap_boundary

    class Ctx:
        pass

    ctx, frame = Ctx(), object()
    table = weakref.WeakKeyDictionary()
    before = swap_boundary._fingerprint(table)
    table[ctx] = frame
    assert swap_boundary._drifted(before, swap_boundary._fingerprint(table))

    buckets = {"Svc": [object()]}
    before = swap_boundary._fingerprint(buckets)
    buckets["Svc"].append(object())
    assert swap_boundary._drifted(before, swap_boundary._fingerprint(buckets))
    buckets["Svc"].pop()
    assert not swap_boundary._drifted(before, swap_boundary._fingerprint(buckets))
