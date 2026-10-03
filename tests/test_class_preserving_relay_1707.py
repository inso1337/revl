"""A relay over witnessed operations keeps class (a) (issue #1707, part 2).

The class fold used to give every `emit key.method(...)` service emission class
(c) on its own, whatever the target did. So a relay over a class-(a) witnessed
op reached the witnessed extern, which is (a), and the forwarding emission,
which is (c), and the worst of the two made the relay prompt on every call
(D1 in docs/harness-gate-guide.md).

That (c) was a placeholder for "the target is unknown". A service emission
crosses no host boundary itself: it runs the target operation's body in the
same session, and every boundary that body crosses is already in the caller's
reach closure, which the fold walks. So when the target resolves to one
provide-method scope of the composition, the emission now takes the class of
that scope's own reach closure. What stays (c):

* a target whose reach has any class-(c) crossing (the worst rule is unchanged,
  so a relay that also reaches a non-witnessed crossing is still (c));
* a target the composition does not provide (a host-provided or missing key),
  a routed require, and a require bound `carrying(...)`, none of which name one
  scope;
* a `compensate`d service emission (247: a compensation offsets an
  irreversible crossing, it does not make the call revertible).
* a target of no class, one that crosses no checked boundary at all: the
  `emission` marking on the service operation is then the only boundary signal,
  and only checked witnessed or deferred crossings may supersede it.

The derived inverse is not synthesized. Each inner witnessed effect registers
its own checked inverse as it fires, so aborting the relay replays them in the
reverse of the order they fired, which is exactly the reverse sequence of the
inner inverses. The run-level tests below prove that against the live
cordis-py runtime, with an order-sensitive pair whose wrong-order replay leaves
the file in the wrong place.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402

# The guide's fixture with real host bodies: `stash` (a), `enqueue` (b),
# `shout` (c). `Front` relays over them.
SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('deliver:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn undo_note(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('undo:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "service Relay {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn stash_chain(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn stash_and_shout(p: Str, sink: Str)\n"
    "  emission fn stash_compensated(p: Str, sink: Str)\n"
    "}\n"
    "service Outer {\n"
    "  emission fn stash(p: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
    "component Front requires ops: Ops provides relay: Relay {\n"
    "  provide relay {\n"
    "    fn stash(p) { emit ops.stash(p) }\n"
    # two witnessed renames whose inverses only compose in LIFO order:
    # p -> p.bak, then p.bak -> p.bak.bak
    "    fn stash_chain(p) {\n"
    "      emit ops.stash(p)\n"
    "      emit ops.stash(p + \".bak\")\n"
    "    }\n"
    "    fn enqueue(sink, msg) { emit ops.enqueue(sink, msg) }\n"
    "    fn stash_and_shout(p, sink) {\n"
    "      emit ops.stash(p)\n"
    "      emit ops.shout(sink, p)\n"
    "    }\n"
    "    fn stash_compensated(p, sink) {\n"
    "      emit ops.stash(p) compensate undo_note(sink, p)\n"
    "    }\n"
    "  }\n"
    "}\n"
    "component Gateway requires relay: Relay provides outer: Outer {\n"
    "  provide outer {\n"
    "    fn stash(p) { emit relay.stash(p) }\n"
    "  }\n"
    "}\n"
)


def _ir() -> dict:
    return compile_source(SOURCE, "relay.rvl")


def _class(key: str, method: str, ir=None) -> str | None:
    return ClassMap(ir or _ir()).classify_call(key, method)["class"]


# ---------------------------------------------------------------------------
# the class map
# ---------------------------------------------------------------------------

def test_the_direct_ops_keep_their_classes():
    assert [_class("ops", m) for m in ("stash", "enqueue", "shout")] == ["a", "b", "c"]


def test_a_relay_over_a_witnessed_op_is_class_a():
    assert _class("relay", "stash") == "a"


def test_a_relay_over_two_witnessed_calls_is_class_a():
    assert _class("relay", "stash_chain") == "a"


def test_a_relay_of_a_relay_is_class_a():
    """The relaxation is a fixed point: `outer.stash` forwards to
    `relay.stash`, which forwards to the witnessed op."""
    assert _class("outer", "stash") == "a"


def test_a_relay_over_a_deferred_op_is_class_b():
    assert _class("relay", "enqueue") == "b"


def test_a_relay_that_also_reaches_a_non_witnessed_crossing_is_class_c():
    """The issue's third exit test: the worst rule is unchanged."""
    reach = ClassMap(_ir()).classify_call("relay", "stash_and_shout")
    assert reach["class"] == "c"
    assert "announce" in reach["classC"]


def test_a_compensated_relay_emission_stays_class_c():
    assert _class("relay", "stash_compensated") == "c"


def test_a_relay_to_a_key_the_composition_does_not_provide_stays_class_c():
    """Compiled as a candidate against a running composition, the relay's IR
    holds no provider for `ops`: the target's reach is unknown, so the
    emission keeps its (c)."""
    agent = SOURCE[SOURCE.index("component Agent"):SOURCE.index("component Front")]
    running = compile_source(SOURCE.split("component Front")[0], "running.rvl")
    candidate = compile_source(SOURCE.replace(agent, ""), "front.rvl", manifest=running)
    assert sorted(c["name"] for c in candidate["components"]) == ["Front", "Gateway"]
    assert _class("relay", "stash", candidate) == "c"


def test_a_relay_to_a_target_of_no_class_stays_class_c():
    """`Store.db.execute` is declared `emission` and its body crosses nothing
    the fold can check. The declaration stands (tests/
    test_505_cold_load_activation_authority.py relies on exactly this)."""
    src = (
        "service Db { emission fn execute(sql: Str) -> Int }\n"
        "service S { emission fn run(sql: Str) -> Int }\n"
        "component Store provides db: Db { provide db { fn execute(sql) = 1 } }\n"
        "component Front requires db: Db provides s: S {\n"
        "  provide s { fn run(sql) { return emit db.execute(sql) } }\n"
        "}\n")
    ir = compile_source(src, "stub.rvl")
    assert _class("db", "execute", ir) is None
    assert _class("s", "run", ir) == "c"


def test_the_relayed_crossing_says_what_it_relays():
    reach = ClassMap(_ir()).classify_call("relay", "stash")
    seams = [c for c in reach["crossings"] if c["kind"] == "emission"]
    assert len(seams) == 1
    assert seams[0]["actionClass"] == "a" and seams[0]["relay"] is True
    assert reach["classC"] == set()


# ---------------------------------------------------------------------------
# the run: the derived inverse undoes the effect
# ---------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the run-level proof needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


def _session():
    from revl.mcp.session import Session
    session = Session()
    session.approval_policy = "auto"
    session.load(_ir(), record=True)
    return session


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    return str(p)


@needs_cordis
def test_a_relayed_witnessed_call_fires_with_no_prompt_and_abort_undoes_it(path):
    session = _session()
    session.call("relay", "stash", [path])
    assert not os.path.exists(path) and os.path.exists(path + ".bak")
    owner = session._owner
    assert owner.prompts["perCall"] == 0
    assert owner.approvals["prompted"] == 0 and owner.approvals["silent"] == 1
    result = session.abort()
    assert result["aborted"] and result["noResidue"]
    assert Path(path).read_text(encoding="utf-8") == "deliverable"
    assert not os.path.exists(path + ".bak")


@needs_cordis
def test_the_derived_inverse_is_the_reverse_sequence_of_the_inner_inverses(path):
    """`stash_chain` renames p -> p.bak, then p.bak -> p.bak.bak. Undoing the
    first rename before the second finds no p.bak and leaves the file at
    p.bak, so only a LIFO replay lands it back at p."""
    session = _session()
    session.call("relay", "stash_chain", [path])
    assert os.path.exists(path + ".bak.bak") and not os.path.exists(path)
    assert session._owner.prompts["perCall"] == 0
    session.abort()
    assert Path(path).read_text(encoding="utf-8") == "deliverable"
    assert not os.path.exists(path + ".bak") and not os.path.exists(path + ".bak.bak")


@needs_cordis
def test_a_relay_of_a_relay_also_undoes_on_abort(path):
    session = _session()
    session.call("outer", "stash", [path])
    assert session._owner.prompts["perCall"] == 0
    session.abort()
    assert Path(path).read_text(encoding="utf-8") == "deliverable"


@needs_cordis
def test_a_relay_that_reaches_a_non_witnessed_crossing_still_prompts(path, tmp_path):
    from revl.mcp.approval import ApprovalRequired
    sink = str(tmp_path / "sink.log")
    session = _session()
    with pytest.raises(ApprovalRequired):
        session.call("relay", "stash_and_shout", [path, sink])
    assert os.path.exists(path) and not os.path.exists(sink)   # nothing fired
    assert session._owner.prompts["perCall"] == 1
