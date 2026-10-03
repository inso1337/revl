"""The erase report reads a class-preserving relay the way the class map does
(issue #1707, follow-up to the relay rule in `approval.ClassMap`).

The class map gives a service emission its target's class when the target is
one provide-method scope of the composition whose reach is (a) or (b), so a
relay over a witnessed op is class (a). `erase_report._crossings` kept tagging
every service emission class (c) and counted it in the irreversible totals,
so the same relay showed up twice in an erasure report: once as the witnessed
extern it reaches (correctly, in the `witnessed` bucket) and once as a bare
`emit` seam that crosses nothing.

The report now asks the class map which emissions it relaxed
(`ClassMap.relayed_emissions`, the one resolver, not a copy). It lists those
under `relayed`, with the class they took, and leaves them out of the totals.
An emission the class map keeps at (c) is counted exactly as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import erase_report  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402

SOURCE = """
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py { return }
extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError] undo unstash(result) = @py { return Ok({}) }
extern emission fn announce(sink: Str, msg: Str) = @py { return }
service Ops {
  emission fn stash(p: Str)
  emission fn shout(sink: Str, msg: Str)
}
service Relay {
  emission fn stash(p: Str)
  emission fn loud(sink: Str, msg: Str)
}
component Agent provides ops: Ops {
  isolate ops in realm("alpha")
  provide ops {
    fn stash(p) { effect stash_path(p) }
    fn shout(sink, msg) { emit announce(sink, msg) }
  }
}
component Front requires ops: Ops provides relay: Relay {
  isolate ops in realm("alpha")
  isolate relay in realm("alpha")
  provide relay {
    fn stash(p) { emit ops.stash(p) }
    fn loud(sink, msg) { emit ops.shout(sink, msg) }
  }
}
"""


def _ir() -> dict:
    return compile_source(SOURCE, "erase_relay.rvl")


def _crossings() -> dict:
    return erase_report.build_report(_ir(), "alpha", prove_residue=False)[
        "boundaryCrossings"]


def test_the_class_map_relays_the_witnessed_forward_only():
    cm = ClassMap(_ir())
    assert cm.classify_call("relay", "stash")["class"] == "a"
    assert cm.classify_call("relay", "loud")["class"] == "c"


def test_a_class_preserving_relay_is_not_a_bare_crossing():
    cross = _crossings()
    assert "emit:Front:ops.stash" not in cross["bareTokens"]
    assert [e["label"] for e in cross["emissions"]] == ["ops.shout"]


def test_the_relay_is_listed_with_the_class_it_took():
    relayed = _crossings()["relayed"]
    assert [(r["component"], r["label"], r["actionClass"]) for r in relayed] == [
        ("Front", "ops.stash", "a")]
    assert relayed[0]["token"] == "relay:Front:ops.stash"


def test_the_totals_count_only_what_is_irreversible():
    cross = _crossings()
    # Front's `emit ops.shout` (its target is class (c)) and Agent's `announce`
    assert cross["total"] == 2
    assert sorted(cross["bareTokens"]) == ["emit:Front:ops.shout",
                                           "host:Agent:announce"]
    # the witnessed rename the relay reaches is still listed, as before
    assert [w["name"] for w in cross["witnessed"]] == ["stash_path"]


def test_the_report_reads_the_class_maps_resolver():
    """One resolver: the relayed seams the report lists are exactly the
    emissions the class map relaxed, with the same classes."""
    ir = _ir()
    from_map = {(sid.split(":")[0], f"{key}.{method}"): cls
                for (sid, key, method), cls in ClassMap(ir).relayed_emissions().items()}
    listed = {(r["component"], r["label"]): r["actionClass"]
              for r in erase_report.build_report(ir, "alpha", prove_residue=False)[
                  "boundaryCrossings"]["relayed"]}
    assert listed == from_map


def test_the_render_names_the_relay_outside_the_bare_lines():
    text = erase_report.render(
        erase_report.build_report(_ir(), "alpha", prove_residue=False))
    relay_lines = [ln for ln in text.splitlines() if "ops.stash" in ln]
    assert relay_lines and all("[BARE]" not in ln for ln in relay_lines)
    assert any("[RELAY (a)]" in ln for ln in relay_lines)
