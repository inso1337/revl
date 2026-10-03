"""Every verb that compiles or edits reports each provided operation's effect
class, and an edit or swap reports the class it CHANGED (issue #1707, part 1).

The policy reads one thing: the checked effect class of a call, the worst over
its whole reach (docs/harness-gate-guide.md, "The three action classes"). A
class-(a) `witnessed` op is auto-approved with no prompt. Factor it behind a
helper that also announces what it did, and the op becomes class (c): one
prompt per call. (A helper that only relays the witnessed call keeps class (a)
since #1718.) Before this, no verb said a class had moved. `revl_check`,
`revl_admit`, `revl_swap` and `revl_edit` all answered `ok: true` with the
same summary, and the first sign of the change was an `approvalRequired` on
the next call.

What this file holds to:

* `revl_check` and `revl_load` list each provided operation with its class and
  the crossings at that class;
* `revl_admit`, `revl_plan`, `revl_swap` and `revl_edit` add the class diff
  against the running composition, and a warning for each operation whose
  class ROSE that names the operation and the crossing that raised it;
* an unchanged class and a class that falls produce no warning.

The measurement this file starts from (`test_the_class_change_is_real_in_the_class_map`):
the base fixture's `ops.stash` is class (a); after the announcing-helper edit it
is class (c), raised by the forwarding `emit stage.stage` and the `announce` it
now reaches; after the pure-helper edit it stays (a). On main before this
change, none of that appeared in any response.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

# The guide's fixture with real host bodies: `stash` is class (a), a witnessed
# rename with its registered inverse; `enqueue` is (b); `shout` is (c).
BASE = (
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
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

# The natural refactor: the witnessed rename moves into a helper component, and
# `ops.stash` forwards to it. Nothing else changes.
HELPER = (
    "service Stage {\n"
    "  emission fn stage(p: Str)\n"
    "}\n"
    "component Stager provides stage: Stage {\n"
    "  provide stage {\n"
    "    fn stage(p) {\n"
    "      effect stash_path(p)\n"
    "      emit announce(\"log\", p)\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# The same refactor with a PURE helper: since #1718 a relay over a witnessed
# op keeps class (a), so this one changes no class and warns nothing.
PURE_HELPER = HELPER.replace("      emit announce(\"log\", p)\n", "")

EDITS = [
    {"anchor": "component Agent provides ops: Ops {",
     "replacement": HELPER + "component Agent requires stage: Stage provides ops: Ops {"},
    {"anchor": "fn stash(p) { effect stash_path(p) }",
     "replacement": "fn stash(p) { emit stage.stage(p) }"},
]


def _apply(source: str, edits: list) -> str:
    for patch in edits:
        assert patch["anchor"] in source
        source = source.replace(patch["anchor"], patch["replacement"], 1)
    return source


WRAPPED = _apply(BASE, EDITS)
PURE_WRAPPED = WRAPPED.replace(HELPER, PURE_HELPER)
RAISED = ["`emit stage.stage` in Agent", "`announce` (emission extern) in Stager"]


@pytest.fixture(autouse=True)
def _host_code_server(monkeypatch):
    """A fresh session on a server that admits operator-authored host bodies,
    the trust these fixtures' `@py` externs need; restored afterwards."""
    from revl.mcp import server
    from revl.mcp.session import Session
    before = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    server.set_authoring_trust(host_code=True)
    yield
    if server.SESSION.loaded:
        server.SESSION.unload()
    server.AUTHORING = before


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _classes(payload: dict) -> dict:
    return {(e["key"], e["method"]): e["class"] for e in payload["effectClasses"]}


def _rose(payload: dict) -> dict:
    return {(w["key"], w["method"]): w for w in payload["effectClassWarnings"]}


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------

def test_the_class_change_is_real_in_the_class_map():
    """What the policy decides on, before and after each helper edit. A pure
    relay keeps class (a) (#1718); a helper that also announces makes the
    witnessed op class (c), raised by the forwarding emission and the
    announcement it now reaches."""
    from revl.mcp.approval import ClassMap
    def reach(src):
        return ClassMap(compile_source(src, "x.rvl")).classify_call("ops", "stash")
    assert reach(BASE)["class"] == "a"
    assert reach(PURE_WRAPPED)["class"] == "a"
    after = reach(WRAPPED)
    assert after["class"] == "c"
    raised = [c for c in after["crossings"] if c["actionClass"] == "c"]
    assert [(c["kind"], c.get("key"), c.get("name")) for c in raised] == [
        ("emission", "stage", None), ("extern", None, "announce")]


# ---------------------------------------------------------------------------
# every compile reports each provided operation's class
# ---------------------------------------------------------------------------

def test_check_reports_each_provided_operations_class():
    checked = _call("revl_check", {"source": BASE})
    assert checked["ok"] is True
    assert _classes(checked) == {("ops", "stash"): "a", ("ops", "enqueue"): "b",
                                 ("ops", "shout"): "c"}
    stash = next(e for e in checked["effectClasses"] if e["method"] == "stash")
    assert stash["component"] == "Agent"
    assert [c["name"] for c in stash["raisedBy"]] == ["stash_path"]


def test_check_names_the_crossing_that_sets_a_class():
    checked = _call("revl_check", {"source": WRAPPED})
    stash = next(e for e in checked["effectClasses"]
                 if (e["key"], e["method"]) == ("ops", "stash"))
    assert stash["class"] == "c"
    assert [c["text"] for c in stash["raisedBy"]] == RAISED
    assert _classes(checked)[("stage", "stage")] == "c"


# ---------------------------------------------------------------------------
# admission against a running composition reports the class diff
# ---------------------------------------------------------------------------

def test_admit_reports_the_operation_whose_class_rose_and_the_crossing():
    """The no-runtime half: `revl_admit` against the running IR."""
    running = compile_source(BASE, "base.rvl")
    admitted = _call("revl_admit", {"source": WRAPPED,
                                    "manifest": running, "replacing": ["Agent"]})
    assert admitted["ok"] is True and admitted["admitted"] is True, admitted
    warning = _rose(admitted)[("ops", "stash")]
    assert (warning["before"], warning["after"]) == ("a", "c")
    assert warning["component"] == "Agent"
    assert [c["text"] for c in warning["crossings"]] == RAISED
    assert "ops.stash" in warning["message"] and "stage.stage" in warning["message"]
    assert "prompt" in warning["message"]
    changes = {(c["key"], c["method"]): (c["before"], c["after"])
               for c in admitted["effectClassChanges"]}
    assert changes[("ops", "stash")] == ("a", "c")
    assert changes[("stage", "stage")] == (None, "c")   # a new operation


def test_an_unchanged_class_and_a_falling_class_are_not_warned():
    running = compile_source(WRAPPED, "wrapped.rvl")
    # the reverse edit: ops.stash falls from (c) back to (a)
    admitted = _call("revl_admit", {"source": BASE, "manifest": running,
                                    "replacing": ["Agent", "Stager"]})
    assert admitted["ok"] is True, admitted
    assert admitted["effectClassWarnings"] == []
    changes = {(c["key"], c["method"]): (c["before"], c["after"])
               for c in admitted["effectClassChanges"]}
    assert changes[("ops", "stash")] == ("c", "a")
    assert changes[("stage", "stage")] == ("c", None)   # an operation withdrawn


def test_a_class_preserving_relay_refactor_warns_nothing():
    """The pure helper is the refactor #1718 made class-preserving: the diff
    lists the new operation and no operation rose."""
    running = compile_source(BASE, "base.rvl")
    admitted = _call("revl_admit", {"source": PURE_WRAPPED, "manifest": running,
                                    "replacing": ["Agent"]})
    assert admitted["ok"] is True, admitted
    assert admitted["effectClassWarnings"] == []
    assert [(c["key"], c["method"], c["before"], c["after"])
            for c in admitted["effectClassChanges"]] == [("stage", "stage", None, "a")]


def test_plan_reports_the_class_diff_too():
    running = compile_source(BASE, "base.rvl")
    planned = _call("revl_plan", {"source": WRAPPED,
                                  "manifest": running, "replacing": ["Agent"]})
    assert (_rose(planned)[("ops", "stash")]["after"]) == "c", planned
    assert "resultingIR" not in planned


# ---------------------------------------------------------------------------
# edit and swap on a live composition (cordis-py)
# ---------------------------------------------------------------------------

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session verbs need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


@needs_runtime
def test_load_reports_each_provided_operations_class():
    loaded = _call("revl_load", {"source": BASE})
    assert loaded["ok"] is True, loaded
    assert _classes(loaded)[("ops", "stash")] == "a"


@needs_runtime
def test_an_edit_that_wraps_a_witnessed_op_in_a_helper_warns():
    """The issue's exit test: the edit names the op and the crossing."""
    _call("revl_load", {"source": BASE})
    edited = _call("revl_edit", {"edits": EDITS})
    assert edited["ok"] is True and edited["swapped"] is True, edited
    warning = _rose(edited)[("ops", "stash")]
    assert (warning["before"], warning["after"]) == ("a", "c")
    assert [c["text"] for c in warning["crossings"]] == RAISED
    assert _classes(edited)[("ops", "stash")] == "c"


@needs_runtime
def test_a_swap_reports_the_same_diff():
    _call("revl_load", {"source": BASE})
    swapped = _call("revl_swap", {"source": WRAPPED})
    assert swapped["ok"] is True and swapped["swapped"] is True, swapped
    assert _rose(swapped)[("ops", "stash")]["after"] == "c"


@needs_runtime
def test_a_swap_that_keeps_every_class_warns_nothing():
    _call("revl_load", {"source": BASE})
    swapped = _call("revl_swap", {"source": BASE.replace("'deliver:'", "'q:'")})
    assert swapped["ok"] is True, swapped
    assert swapped["effectClassWarnings"] == []
    assert swapped["effectClassChanges"] == []


def test_ship_surfaces_the_class_diff_at_the_top():
    running = compile_source(BASE, "base.rvl")
    shipped = _call("revl_ship", {"source": WRAPPED, "manifest": running,
                                  "replacing": ["Agent"]})
    assert shipped["ok"] is True, shipped
    assert _rose(shipped)[("ops", "stash")]["after"] == "c"
    assert _classes(shipped)[("ops", "stash")] == "c"
