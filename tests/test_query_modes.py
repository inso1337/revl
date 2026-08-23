"""One query surface, three time modes (roadmap item 34, docs/queries.md §9).

The five verbs and the envelope are unchanged; two modes are added on top:

* **live** — the same verbs answered against the session's actual loaded
  generation (post-swap), with the envelope's spent hot-swap caveat replaced by
  the live one and a `live` block reconciling declared provisions against what
  is *served now*;
* **historical** — queries against a recorded run: "which emissions crossed
  between steps X and Y?" over a replay timeline, and "everything a component
  touched during its life" combining item 27's lifecycle trace with the
  recorded effect timeline.

The pure layer (envelope relabelling, the historical reads over constructed
trace/timeline dicts) needs no runtime. The end-to-end layer (`@needs_cordis`)
boots a real composition, swaps it, and proves a live query sees the swap the
static IR would not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from revl import query  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.mcp import server as _server  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

MESH = os.path.join(ROOT, "tests", "fixtures", "query_mesh.rvl")

# a swap-compatible pair: withdrawing Pg breaks App in A, and breaks nothing in
# B, which has dropped App. The static answer for one is the wrong answer for
# the other — which is the whole point of a live mode.
SRC_A = """
service Db { fn ping() -> Str }
component Pg provides db: Db { provide db { fn ping() = "ok" } }
component App requires db: Db { }
"""
SRC_B = """
service Db { fn ping() -> Str }
component Pg provides db: Db { provide db { fn ping() = "ok" } }
"""


try:  # the same availability gate test_run.py / test_replay.py use
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")


def _fake_timeline(component="Store"):
    """A replay recording in `Recorder.as_dict()` shape — the shape
    `session.timeline()` returns — with two emissions among other steps."""
    return {"components": [{
        "component": component,
        "steps": [
            {"index": 0, "kind": "effect", "label": "cells/effect",
             "site": "m:1", "source": "yield lambda: cells.drop()"},
            {"index": 1, "kind": "emission", "label": "ledger.append",
             "detail": {"key": "ledger", "method": "append",
                        "service": "Ledger", "args": ["v"]},
             "site": "m:2", "source": "emit ledger.append(v)"},
            {"index": 2, "kind": "provision", "label": "provide kv",
             "detail": {"key": "kv"}},
            {"index": 3, "kind": "emission", "label": "kv.set",
             "detail": {"key": "kv", "method": "set", "service": "Kv",
                        "args": ["report", "x"]},
             "compensatedBy": 4},
            {"index": 4, "kind": "compensation", "label": "compensate kv.set",
             "detail": {"for": 3}},
        ],
    }]}


def _lifecycle_trace(component="Store"):
    return [
        wr.make_event(0, 0, wr.LOAD, component, "PENDING -> ACTIVE",
                      wr.cause_requirements([{"component": "Journal",
                                              "key": "ledger"}])),
        wr.make_event(1, 1, wr.WITHDRAW, component, "ACTIVE -> DISPOSED",
                      wr.cause_provider_withdrawn("Journal", "ledger")),
    ]


# ---------------------------------------------------------- envelope / modes


def test_static_query_defaults_to_static_mode():
    ir = compile_files([MESH])
    result = query.withdrawal(ir, "Journal")
    assert result["mode"] == query.MODE_STATIC


def test_as_live_relabels_the_envelope():
    ir = compile_files([MESH])
    static = query.reach(ir, "Store")
    # reach carries the hot-swap caveat statically
    assert query._ASSUMPTION_SWAP in static["assumptions"]

    live = query.as_live(static, {"generation": 3, "servedKeys": ["ledger"],
                                  "componentStates": {"Store": "ACTIVE"}})
    assert live["mode"] == query.MODE_LIVE
    # the spent caveat is gone; the live one is first
    assert query._ASSUMPTION_SWAP not in live["assumptions"]
    assert live["assumptions"][0] == query._ASSUMPTION_LIVE
    assert query._ASSUMPTION_LIVE_SERVED in live["assumptions"]
    assert live["live"]["generation"] == 3
    assert live["live"]["servedKeys"] == ["ledger"]


def test_live_withdraw_marks_provisions_not_served_now():
    ir = compile_files([MESH])
    # Journal provides `ledger`; say it has drifted out of service
    live = query.live_query(ir, "withdraw",
                            {"generation": 2, "servedKeys": [],
                             "componentStates": {"Journal": "PENDING"}},
                            "Journal")
    served = {p["key"]: p["servedNow"] for p in live["provides"]}
    assert served == {"ledger": False}
    assert live["live"]["notServedNow"] == ["ledger"]


def test_live_query_reflects_a_post_swap_change_static_would_not():
    """The flagship: withdrawing Pg breaks App on the pre-swap IR, and breaks
    nothing on the post-swap one. A live query answers for the world that is
    actually loaded, so it must give the post-swap answer."""
    ir_a = compile_source(SRC_A, "a.rvl")
    ir_b = compile_source(SRC_B, "b.rvl")

    static_a = query.withdrawal(ir_a, "Pg")
    assert [c["component"] for c in static_a["cascade"]] == ["App"]

    # live, against the swapped-in generation B
    live_b = query.live_query(ir_b, "withdraw",
                              {"generation": 2, "servedKeys": ["db"],
                               "componentStates": {"Pg": "ACTIVE"}}, "Pg")
    assert live_b["mode"] == query.MODE_LIVE
    assert live_b["cascade"] == []  # nothing breaks — App is gone post-swap


# ---------------------------------------------------------- historical mode


def test_emitted_between_windows_the_recorded_crossings():
    result = query.emitted_between(_fake_timeline(), 1, 2)
    assert result["ok"] and result["mode"] == query.MODE_HISTORICAL
    assert result["precision"] == query.EXACT
    # only the emission at step 1 falls in [1, 2]; the one at step 3 does not
    assert [(e["step"], e["key"], e["method"]) for e in result["emissions"]] \
        == [(1, "ledger", "append")]
    assert result["crossings"] == 1


def test_emitted_between_wider_window_and_compensation_flag():
    result = query.emitted_between(_fake_timeline(), 0, 4)
    assert result["crossings"] == 2
    by_step = {e["step"]: e for e in result["emissions"]}
    assert by_step[1]["compensated"] is False
    assert by_step[3]["compensated"] is True
    # `uncompensated` isolates the bare crossing
    assert [e["step"] for e in result["uncompensated"]] == [1]


def test_emitted_between_labels_the_recorded_world_in_assumptions():
    result = query.emitted_between(_fake_timeline(), 0, 4)
    assert query._ASSUMPTION_RECORDED in result["assumptions"]
    assert query._ASSUMPTION_RECORDING_SCOPE in result["assumptions"]


def test_emitted_between_bad_window_and_unknown_component():
    assert query.emitted_between(_fake_timeline(), 5, 2)["ok"] is False
    miss = query.emitted_between(_fake_timeline(), 0, 9, component="Ghost")
    assert miss["ok"] is False
    assert "Store" in miss["known"]


def test_lifetime_combines_trace_span_and_recorded_effects():
    record = {"trace": _lifecycle_trace(), "timeline": _fake_timeline()}
    result = query.lifetime(record, "Store")
    assert result["ok"] and result["mode"] == query.MODE_HISTORICAL
    # the life span comes from item 27's lifecycle trace
    assert result["life"]["loaded"]["seq"] == 0
    assert result["life"]["withdrew"]["seq"] == 1
    assert "Journal" in result["life"]["withdrew"]["note"]
    # what it touched comes from the recorded effect timeline
    assert result["counts"] == {"effect": 1, "provision": 1,
                                "emission": 2, "compensation": 1}
    assert [e["method"] for e in result["emissions"]] == ["append", "set"]


def test_lifetime_reuses_item27_trace_format_verbatim():
    """The historical mode must read item 27's format, not a parallel one:
    a Trace object built from the same events is accepted directly."""
    trace = wr.Trace(_lifecycle_trace())
    result = query.lifetime({"trace": trace}, "Store")
    assert result["ok"]
    assert result["life"]["withdrew"]["transition"] == "ACTIVE -> DISPOSED"
    assert result["recorded"] is False  # no timeline given, only the trace


def test_lifetime_needs_at_least_one_recorded_source():
    assert query.lifetime({}, "Store")["ok"] is False


# ---------------------------------------------------------- CLI


def _cli(*args):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    return subprocess.run([sys.executable, "-m", "revl", *args],
                          capture_output=True, text=True, env=env)


def test_cli_emitted_between(tmp_path):
    tl = tmp_path / "tl.json"
    tl.write_text(json.dumps(_fake_timeline()))
    out = _cli("query", "emitted-between", "--timeline", str(tl),
               "--from", "0", "--to", "4", "--json")
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["mode"] == "historical"
    assert result["crossings"] == 2


def test_cli_touched(tmp_path):
    tl = tmp_path / "tl.json"
    tl.write_text(json.dumps(_fake_timeline()))
    trace = tmp_path / "trace.jsonl"
    wr.write_trace(_lifecycle_trace(), str(trace))
    out = _cli("query", "touched", "Store", "--trace", str(trace),
               "--timeline", str(tl), "--json")
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["mode"] == "historical"
    assert result["life"]["loaded"]["event"] == "load"
    assert result["counts"]["emission"] == 2


# ---------------------------------------------------------- MCP wiring


def test_mcp_registers_the_session_bound_query_tools():
    names = {t["name"] for t in _server._ADVERTISED}
    assert {"revl_live_query", "revl_history_emitted_between",
            "revl_history_lifetime"} <= names


def test_mcp_live_query_needs_a_loaded_session():
    saved = _server.SESSION
    _server.SESSION = Session()
    try:
        result = _server._tool_live_query({"verb": "withdraw",
                                           "component": "Pg"})
        assert result["ok"] is False
    finally:
        _server.SESSION = saved


def test_mcp_history_emitted_between_accepts_inline_timeline():
    result = _server._tool_history_emitted_between(
        {"from": 0, "to": 4, "timeline": _fake_timeline()})
    assert result["ok"] and result["crossings"] == 2


# ---------------------------------------------------------- end to end


@needs_cordis
def test_live_query_reads_the_running_session_after_a_swap():
    """The live mode over a real runtime: load A, hot-swap to B, and the live
    withdraw sees B — App is gone, so nothing breaks — while the static query on
    A's IR still names App. Also proves the generation counter advances."""
    saved = _server.SESSION
    _server.SESSION = Session()
    try:
        ir_a = compile_source(SRC_A, "a.rvl")
        ir_b = compile_source(SRC_B, "b.rvl")
        _server.SESSION.load(ir_a)
        assert _server.SESSION.live_state()["generation"] == 1
        _server.SESSION.swap(ir_b)
        assert _server.SESSION.live_state()["generation"] == 2

        live = _server._tool_live_query({"verb": "withdraw", "component": "Pg"})
        assert live["mode"] == "live"
        assert live["cascade"] == []          # reflects B, the loaded generation
        assert live["live"]["generation"] == 2
        assert "db" in live["live"]["servedKeys"]

        # the static IR of A would have said App breaks — the mode is the
        # difference, not a different query
        assert [c["component"] for c in query.withdrawal(ir_a, "Pg")["cascade"]] \
            == ["App"]
    finally:
        if _server.SESSION.loaded:
            _server.SESSION.unload()
        _server.SESSION = saved


@needs_cordis
def test_history_emitted_between_over_a_real_recording():
    """A real recorded run: call a service that emits, then query which
    emissions crossed. Reads the same recording the replay tools read."""
    saved = _server.SESSION
    _server.SESSION = Session()
    try:
        ir = compile_files([MESH])
        _server.SESSION.load(ir, record=True)
        # Announcer.say emits host_write through the pure stratum
        _server.SESSION.call("voice", "say", ["hi"])
        result = _server._tool_history_emitted_between(
            {"from": 0, "to": 1000})
        assert result["ok"] and result["mode"] == "historical"
        # at least one emission was recorded and windowed
        assert result["crossings"] >= 1
    finally:
        if _server.SESSION.loaded:
            _server.SESSION.unload()
        _server.SESSION = saved
