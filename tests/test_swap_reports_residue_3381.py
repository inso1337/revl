"""The residue of frames disposed at a swap reaches whoever drives the session
(issue #3381).

At a swap the runtime's bracket check records one `NotReversed` fact for each
key an `undo` did not put back. The record went to the driver's list
(`_Driver._compensation_residue`), which only `revl run` read: `Session.swap`
did not name it, `unload` answered `noResidue: True`, and the list grew for
the life of the session. Now each swap answer carries `compensationResidue`
(the shape `unload` uses) and drains the list; a record no answer reported is
what `unload` reports, and `noResidue` is false with it.

Measured on base for the issue's program: swap answers carry no
`compensationResidue`, the driver's list holds 0, 5 then 10 records, and
`unload` answers `noResidue: True` with an empty `compensationResidue`.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap needs a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

# the issue's program: `undo m.remove(k)` is the inverse only while `k` was
# absent, and the handoff makes every key present from the second generation on
REMOVE = """
service Mind { fn get(k: Int) -> Int  emission fn set(k: Int, v: Int) }
component Memory provides mind: Mind {
  handoff mind: Map[Int, Int]
  let m = effect Map.new() undo m.drop()
  provide mind {
    fn get(k) = m.get(k) ?? 0
    fn set(k, v) { effect m.insert(k, v) undo m.remove(k) }
  }
}
"""

# the inverse the issue's playground switched to
INVERSE = """
service Mind { fn get(k: Int) -> Int  emission fn set(k: Int, v: Int) }
component Memory provides mind: Mind {
  handoff mind: Map[Int, Int]
  let m = effect Map.new() undo m.drop()
  provide mind {
    fn get(k) = m.get(k) ?? 0
    fn set(k, v) {
      let was = m.get(k)
      effect m.insert(k, v) undo match was { Some(w) => m.insert(k, w), None => m.remove(k) }
    }
  }
}
"""

# a successor whose activation fails: the swap disposes the running frames,
# then rolls back, and its answer is an error that names no residue
FAILING = REMOVE + """
component Bad {
  fail "successor refused on purpose"
}
"""


def _session(src):
    from revl.mcp.session import Session
    ir = compile_source(src, "memory.rvl")
    s = Session()
    s.load(ir, origin={"source": src})
    return s, ir


def _not_reversed(records):
    return [r for r in records
            if (r.get("error") or {}).get("type") == "NotReversed"]


def _three_swaps(s, ir, src):
    counts, held = [], []
    for generation in range(3):
        for k in range(5):
            s.call("mind", "set", [k, 10 * generation + k], raw=True)
        said = s.swap(ir, origin={"source": src}, agent_authored=False)
        counts.append(len(_not_reversed(said["compensationResidue"])))
        held.append(len(s._driver._compensation_residue))
    return counts, held


@needs_cordis
def test_each_swap_answer_names_the_residue_it_left_and_drains_it():
    s, ir = _session(REMOVE)
    counts, held = _three_swaps(s, ir, REMOVE)
    assert counts == [0, 5, 5]
    assert held == [0, 0, 0]
    report = s.unload()
    # every record was reported by a swap answer: none left for unload
    assert report["noResidue"] is True
    assert report["compensationResidue"] == []


@needs_cordis
def test_the_swap_record_has_unloads_residue_shape():
    s, ir = _session(REMOVE)
    _three_swaps(s, ir, REMOVE)
    for k in range(5):
        s.call("mind", "set", [k, k], raw=True)
    said = s.swap(ir, origin={"source": REMOVE}, agent_authored=False)
    rec = _not_reversed(said["compensationResidue"])[0]
    assert rec["kind"] == "bracket-fault"
    assert rec["state"] == "unresolved"
    assert rec["component"] == "Memory"
    s.unload()


@needs_cordis
def test_unload_reports_residue_no_swap_answer_reported():
    s, ir = _session(REMOVE)
    for k in range(5):
        s.call("mind", "set", [k, k], raw=True)
    s.swap(ir, origin={"source": REMOVE}, agent_authored=False)
    for k in range(5):
        s.call("mind", "set", [k, k + 1], raw=True)
    bad = compile_source(FAILING, "memory.rvl")
    with pytest.raises(Exception, match="swap rejected"):
        s.swap(bad, origin={"source": FAILING}, agent_authored=False)
    assert len(s._driver._compensation_residue) == 5
    report = s.unload()
    assert report["noResidue"] is False
    assert report["checks"]["compensations"] is False
    assert len(_not_reversed(report["compensationResidue"])) == 5


@needs_cordis
def test_control_a_true_inverse_leaves_no_residue():
    s, ir = _session(INVERSE)
    counts, held = _three_swaps(s, ir, INVERSE)
    assert counts == [0, 0, 0]
    assert held == [0, 0, 0]
    report = s.unload()
    assert report["noResidue"] is True
    assert report["compensationResidue"] == []
    assert "compensations" not in report["checks"]


@needs_cordis
def test_control_revl_run_still_prints_the_inverse_residue_key(tmp_path):
    src = tmp_path / "leaky.rvl"
    src.write_text(
        "component Leaky {\n"
        "  let m = effect Map.new() undo m.drop()\n"
        "  effect m.remove(1) undo m.insert(1, 5)\n"
        "}\n")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    out = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(src)], cwd=tmp_path,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
        timeout=120)
    text = out.stdout + out.stderr
    assert "inverse residue" in text
    assert "1 inverse failure(s) recorded" in text


# -- callers that swap on the session's behalf (review of #3381) ------------

REMOVE_V2 = REMOVE.replace("m.get(k) ?? 0", "m.get(k) ?? 1")


@needs_cordis
def test_update_apply_reports_the_residue_its_swap_drained(tmp_path,
                                                           monkeypatch):
    """`revl_update_apply` swaps through `_tool_swap`; its own answer carries
    the records that swap drained, so none is lost to the verb's fresh dict."""
    from _update_feed_fixture import pin_test_key, serve_feed, tar_gz
    from revl.mcp import server as server_mod
    from revl.mcp import update_tools
    from revl.mcp.server import handle

    def call(tool, arguments):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}})
        return response["result"]["structuredContent"]

    pin_test_key(monkeypatch)
    update_tools.reset()
    feed = serve_feed(version="9.9.9", artifact_bytes=tar_gz(
        {"memory.rvl": REMOVE_V2.encode()}, top="app-9.9.9"))
    try:
        update_tools.configure(enabled=True, repo="owner/app", project="app",
                               api_base=feed.base_url, home=str(tmp_path),
                               consents=update_tools.OPERATOR_CONSENTS)
        assert call("revl_load", {"source": REMOVE})["ok"] is True
        for k in range(5):
            call("revl_call", {"key": "mind", "method": "set", "args": [k, k]})
        swapped = call("revl_swap", {"source": REMOVE})
        assert _not_reversed(swapped["compensationResidue"]) == []
        # the handoff carried the keys over: these writes' undo does not reverse
        for k in range(5):
            call("revl_call", {"key": "mind", "method": "set",
                               "args": [k, k + 1]})
        planned = call("revl_update_apply", {"acceptUnknownVersion": True})
        applied = call("revl_update_apply", {"planHash": planned["planHash"]})
        assert applied["ok"] is True and applied["applied"] is True
        assert len(_not_reversed(applied["compensationResidue"])) == 5
        report = server_mod.SESSION.unload()
        assert report["noResidue"] is True
    finally:
        feed.close()
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        update_tools.reset()


LEAKY = """
component Leaky {
  let m = effect Map.new() undo m.drop()
  effect m.remove(1) undo m.insert(1, 5)
}
"""


@needs_cordis
def test_cli_undo_replay_keeps_the_residue_for_its_unload(tmp_path,
                                                           monkeypatch):
    """`revl undo history.json` replays the history with swaps whose answers
    it never prints; their records are held for the unload that ends it.
    Every disposal of `Leaky` leaves one record: two replay swaps, the undo's
    swap and the unload's own teardown."""
    import json
    from revl.__main__ import main
    from revl.mcp.session import Session

    s, ir = _session(LEAKY)
    for _ in range(2):
        s.swap(ir, origin={"source": LEAKY}, agent_authored=False)
    path = tmp_path / "history.json"
    path.write_text(json.dumps(s.history_document()))
    s.unload()

    seen = []
    original = Session.unload

    def spy(self):
        report = original(self)
        seen.append(report)
        return report

    monkeypatch.setattr(Session, "unload", spy)
    assert main(["undo", str(path), "--to", "1"]) == 0
    assert len(seen) == 1
    assert seen[0]["noResidue"] is False
    assert len(_not_reversed(seen[0]["compensationResidue"])) == 4
