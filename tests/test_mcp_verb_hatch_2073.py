"""Every verb is callable by a function-calling model, at no tier cost
(issues #2042, #2073).

`tools/list` advertises `disclosure.CORE`, and the authoring harness builds the
model's tool list from exactly that:

    tools = bridge.openai_schemas() + FILE_TOOLS        # 14 MCP verbs + 3

A function-calling model emits a call for a tool it was SHOWN and for no other,
so a schema `revl_verbs` returns as text is a document, not a tool. Measured
(issue #2042, EXP A `gen-scaffold`, `max-turns 14`, one run): the agent did the
work, got `admitted: true` and left no artifact — `plain.rvl` byte-identical to
its base, `has_component: false` — because `revl_export`, the one verb that
writes the held source out, was reachable by name and not by tool. The failure
is invisible in token counts.

The remedy is the escape hatch, and it is not a fifteenth listed verb: it is a
second FORM of `revl_verbs`, which is already in the tier. `revl_verbs {name,
args}` is resolved before dispatch to the named verb's OWN name, so it runs the
whole pipeline and returns what a direct call returns — the same jail, the same
authoring and operator gates, the same runtime gate, the same handler, the same
`disk` divergence field, the same `sessionState` footer. The tier stays at 14.

The claim in `disclosure.py` — "every verb stays callable by name, listed or
not" — was true of the transport and false of the model's interface. These
tests hold the hatch to making it true of both.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import disclosure, repeat  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.persist import ORIGIN_FILES_CONTENT  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

ADVERTISED = {tool["name"]: tool for tool in server_mod._ADVERTISED}
#: CORE may name a verb a later PR adds; only the ones this server has list.
PRESENT_CORE = [name for name in disclosure.CORE if name in ADVERTISED]

#: `revl mcp serve`'s cold-start price for route 2, `REVL_MCP_ALL_TOOLS=1`,
#: measured in issue #2073. The hatch has to be nowhere near it.
ALL_TOOLS_COST = 22810


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _list() -> list:
    return handle({"jsonrpc": "2.0", "id": 1,
                   "method": "tools/list"})["result"]["tools"]


def _hatch(verb: str, args: dict | None = None) -> dict:
    """Call `verb` through the hatch, the way a model that cannot see it must."""
    return _call(disclosure.DISCOVERY,
                 {disclosure.HATCH_NAME: verb,
                  disclosure.HATCH_ARGS: {} if args is None else args})


@pytest.fixture(autouse=True)
def _tiered():
    """The advertised tier, which is the whole question: not all-tools."""
    before = disclosure.all_tools()
    disclosure.set_all_tools(False)
    repeat.forget()
    yield
    disclosure.set_all_tools(before)
    repeat.forget()


# --------------------------------------------- the model's actual vocabulary

def test_the_hatch_is_in_the_tool_list_the_model_is_given():
    """Evidence 1: the harness builds the model's vocabulary from the
    advertised tier, so the hatch has to be INSIDE it — a hatch merely nameable
    in a payload is the defect being fixed."""
    tools = _list()                                  # what `tools/list` sends
    assert [t["name"] for t in tools] == PRESENT_CORE
    hatch = [t for t in tools if t["name"] == disclosure.DISCOVERY]
    assert len(hatch) == 1, "the discovery verb is listed"
    props = hatch[0]["inputSchema"]["properties"]
    assert disclosure.HATCH_NAME in props
    assert disclosure.HATCH_ARGS in props
    assert props[disclosure.HATCH_NAME]["type"] == "string"
    assert props[disclosure.HATCH_ARGS]["type"] == "object"


def test_the_hatch_does_not_grow_the_tier_or_add_a_verb():
    """The budget decision: `CORE` is still 14 and the listed set is still
    exactly `CORE`. The hatch is `revl_verbs`' second form, so there is no new
    verb to gate, to plan, to give a topic or to count."""
    assert len(disclosure.CORE) <= 14
    assert disclosure.CORE[-1] == disclosure.DISCOVERY
    names = [t["name"] for t in _list()]
    assert len(names) == 14
    assert "revl_verb" not in names           # no new listed verb, singular
    assert names.count(disclosure.DISCOVERY) == 1
    # ...and it did not get there by NOT having a hatch: the hatch exists, as
    # a second form of the verb that was already in the tier
    assert disclosure.HATCH_NAME in ADVERTISED[disclosure.DISCOVERY]["inputSchema"]["properties"]
    assert disclosure.HATCH_ARGS in ADVERTISED[disclosure.DISCOVERY]["inputSchema"]["properties"]
    # the hatch adds no verb to the registry, so it adds no topic either
    placed = [n for _about, verbs in disclosure.TOPICS.values() for n in verbs]
    assert set(placed) == set(ADVERTISED) - {disclosure.DISCOVERY}


def test_the_hatch_costs_about_a_hundred_tokens_not_the_all_tools_price():
    """Evidence 4: route 2 costs +22,810 cold-start tokens. The hatch is two
    properties on a verb already advertised, and that is the whole price."""
    tool = ADVERTISED[disclosure.DISCOVERY]
    added = {k: tool["inputSchema"]["properties"][k]
             for k in (disclosure.HATCH_NAME, disclosure.HATCH_ARGS)}
    cost = len(json.dumps(added)) / 4                # ~4 characters per token
    assert cost < 200, cost
    assert cost < ALL_TOOLS_COST / 100, (cost, ALL_TOOLS_COST)
    full = len(json.dumps(server_mod._ADVERTISED)) / 4
    core = len(json.dumps(_list())) / 4
    assert full - core > 40 * cost, (full - core, cost)


def test_the_hatch_makes_the_verb_annotation_honest():
    """A hatch that reaches `revl_export` and `revl_change` means
    `revl_verbs` is not read-only, and MCP annotations are per TOOL, not per
    call. Claiming otherwise is the lie this repo calls out by name
    (`tests/fixtures/mcp_proxy_fake_upstream.py`, and the whole point of
    `mcp/composed.py`: a hint nobody can check is not worth advertising)."""
    ann = ADVERTISED[disclosure.DISCOVERY]["annotations"]
    assert ann["readOnlyHint"] is False
    assert ann["destructiveHint"] is True
    # and the lookup form it used to be alone still writes nothing
    for verb in ("revl_export", "revl_change", "revl_swap", "revl_rollback"):
        if verb in ADVERTISED:
            assert ADVERTISED[verb]["annotations"]["destructiveHint"] is True, verb


# ------------------------------- a hatched call is the direct call, exactly

@pytest.mark.parametrize("verb,args", [
    ("revl_state", {}),
    ("revl_fmt", {"source": "service S { fn f() -> Int }\n"}),
    ("revl_explain", {"code": "G4"}),
])
def test_a_hatched_call_returns_what_the_direct_call_returns(verb, args):
    """Evidence 2: indistinguishable — the same payload, field for field,
    `sessionState` footer included. Nothing in the answer says it came through
    the hatch, because nothing about it did."""
    direct = _call(verb, args)
    hatched = _hatch(verb, args)
    assert hatched == direct, (hatched, direct)
    assert "sessionState" in hatched
    assert hatched["sessionState"] == direct["sessionState"]


def test_a_hatched_refusal_is_the_same_refusal():
    """A refusal, too: the gate runs on the real verb, so a runtime-gated verb
    called through the hatch refuses by its OWN name and with its own fix —
    the per-verb validation is not skipped, it is the point."""
    direct = _call("revl_export", {})
    hatched = _hatch("revl_export", {})
    assert hatched == direct, (hatched, direct)
    if not hatched["ok"]:
        assert "revl_export" in hatched["diagnostics"][0]["message"]


def test_the_hatch_asks_the_gates_about_the_real_verb(monkeypatch):
    """The operator gate is keyed by verb name. The hatch resolves to the real
    name before the pipeline, so a hatched call is gated as the direct call is
    — never as the discovery verb, which would be an ungated bypass."""
    seen = []
    real = server_mod._operator.decide

    def spy(session, name, arguments):
        seen.append(name)
        return real(session, name, arguments)

    monkeypatch.setattr(server_mod._operator, "decide", spy)
    assert _hatch("revl_fmt",
                  {"source": "service S { fn f() -> Int }\n"})["ok"] is True
    assert seen == ["revl_fmt"], seen


# ------------------------------------------------- honest about failure

@pytest.mark.parametrize("arguments,expected", [
    ({"name": "revl_nope", "args": {}}, "revl_nope"),
    ({"name": "revl_state"}, "`args`"),
    ({"name": 123, "args": {}}, "`name`"),
    ({"name": "", "args": {}}, "`name`"),
    ({"name": "  ", "args": {}}, "`name`"),
    ({"name": "revl_state", "args": []}, "`args`"),
    ({"name": "revl_state", "args": "x"}, "`args`"),
])
def test_a_malformed_hatch_refuses_by_name(arguments, expected):
    """An unknown verb, a missing `args` or a bad args shape gets a diagnostic
    naming what is wrong — never a silent no-op, and never the lookup answered
    in place of the call that was asked for."""
    payload = _call(disclosure.DISCOVERY, arguments)
    assert payload["ok"] is False, payload
    message = payload["diagnostics"][0]["message"]
    assert expected in message, message
    assert payload["diagnostics"][0]["severity"] == "error"
    # the lookup is not silently substituted for the call
    assert not {"listed", "topics", "tools"} & set(payload)
    assert payload.get("next", {}).get("tool") == disclosure.DISCOVERY


def test_a_malformed_hatch_does_not_reach_a_handler():
    """`_tool_verbs` is not the hatch's dispatcher: a hatch that got past
    `handle` still refuses rather than answering the index."""
    payload = server_mod._tool_verbs({"name": "revl_nope", "args": {}})
    assert payload["ok"] is False
    assert "listed" not in payload


def test_the_missing_args_refusal_points_at_the_verb_not_the_index():
    """Issue #2110: the refusal for a verb named without `args` used to hand
    back `revl_verbs {}` — the tier index, which by construction omits every
    verb outside the tier. `revl_export` is outside it, so that `next` cannot
    contain the answer, and the model was left guessing `args: {}` to elicit
    the `path` requirement. The refusal names `names`, and its `next` is the
    lookup of the verb the caller actually asked for."""
    verb = "revl_export"
    # the regression only bites for a verb the tier index cannot answer with
    assert verb not in [t["name"] for t in _list()]

    payload = _call(disclosure.DISCOVERY, {disclosure.HATCH_NAME: verb})
    assert payload["ok"] is False, payload
    message = payload["diagnostics"][0]["message"]
    assert "names" in message and verb in message, message

    nxt = payload["next"]
    assert nxt["tool"] == disclosure.DISCOVERY
    assert nxt["arguments"] == {"names": [verb]}, nxt
    assert nxt["ready"] is True

    # following `next` answers the question the refusal was asked: the verb's
    # schema, out of the FULL advertised list rather than the tier
    followed = _call(nxt["tool"], nxt["arguments"])
    assert followed["ok"] is True, followed
    assert [t["name"] for t in followed["tools"]] == [verb]
    assert "path" in followed["tools"][0]["inputSchema"]["properties"]
    # ...where the call `next` used to point at genuinely cannot
    assert verb not in _call(disclosure.DISCOVERY, {})["listed"]


# ------------------------------------------------- the data-loss path (#2042)

MEM_TEXT = ("service Store { fn find(term: Str) -> Bool }\n"
            "\n"
            "component MemoryStore provides store: Store {\n"
            "  provide store {\n"
            '    fn find(term: Str) -> Bool = term == "a"\n'
            "  }\n"
            "}\n")

APP_TEXT = ("service Api { fn lookup(term: Str) -> Bool }\n"
            "\n"
            "component ApiImpl requires store: Store provides api: Api {\n"
            "  provide api { fn lookup(term: Str) -> Bool = store.find(term) }\n"
            "}\n")

EDIT = {"anchor": 'term == "a"', "replacement": 'term == "b"'}

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change loop boots a composition and needs the cordis-py "
           "runtime — install it with `sh backends/python/setup.sh`")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    (tmp_path / "components").mkdir()
    mem = tmp_path / "components" / "memory_store.rvl"
    mem.write_text(MEM_TEXT, encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text(APP_TEXT, encoding="utf-8")
    yield {"mem": mem, "app": app}
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


@needs_runtime
def test_the_authoring_loop_can_reach_revl_export_through_the_hatch(tree):
    """Evidence 3, the whole of #2042: an agent with the advertised tool list
    and no other can now finish the loop. The list it was given does not name
    `revl_export`; the hatch is how it gets there, and the export reconciles
    the `disk` divergence the change loop already reports."""
    # 1. the vocabulary the model is handed — no `revl_export` in it
    listed = [t["name"] for t in _list()]
    assert "revl_export" not in listed, listed
    assert disclosure.DISCOVERY in listed, "the hatch is listed"

    # 2. the loop, driven through the tools the model actually has
    loaded = _call("revl_load", {"files": [str(tree["app"]), str(tree["mem"])]})
    assert loaded["ok"] is True, loaded
    changed = _call("revl_change", {"commit": True,
                                    "edit": {"target": str(tree["mem"]),
                                             "edits": [EDIT]}})
    assert changed["ok"] is True and changed["committed"] is True, changed

    # 3. the change is held, not written: the trap #2032 reports
    assert changed["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}
    assert tree["mem"].read_text(encoding="utf-8") == MEM_TEXT

    # 4. find the hatch in the list, and export through it
    hatch = [t for t in _list() if t["name"] == disclosure.DISCOVERY][0]
    props = hatch["inputSchema"]["properties"]
    assert disclosure.HATCH_NAME in props and disclosure.HATCH_ARGS in props
    exported = _hatch("revl_export")
    assert exported["ok"] is True, exported
    assert exported["written"] == [str(tree["mem"])]
    # 5. the artifact exists, and the loop closes in one call
    assert tree["mem"].read_text(encoding="utf-8") != MEM_TEXT
    assert '== "b"' in tree["mem"].read_text(encoding="utf-8")
    assert exported["disk"] == {"inSync": True, "stale": []}


@needs_runtime
def test_the_hatched_export_is_byte_identical_to_the_direct_one(tree):
    """Evidence 2 on the data-loss verb itself: the hatched export writes what
    the direct export writes and reports what it reports."""
    loaded = _call("revl_load", {"files": [str(tree["app"]), str(tree["mem"])]})
    assert loaded["ok"] is True, loaded
    assert _call("revl_change", {"commit": True,
                                 "edit": {"target": str(tree["mem"]),
                                          "edits": [EDIT]}})["ok"] is True
    held = server_mod.SESSION.origin[ORIGIN_FILES_CONTENT][str(tree["mem"])]

    hatched = _hatch("revl_export")
    assert hatched["ok"] is True, hatched
    assert hatched["written"] == [str(tree["mem"])]
    assert tree["mem"].read_text(encoding="utf-8") == held
    assert hatched["disk"] == {"inSync": True, "stale": []}
    # nothing left to write, and it says so — the direct answer, verbatim
    assert _hatch("revl_export") == _call("revl_export", {})


@needs_runtime
def test_the_disk_field_rides_on_a_hatched_change(tree):
    """The hatch inherits the `disk` divergence (#2032) rather than
    duplicating it: `_ride_disk` keys on the REAL verb name, which is what the
    hatch forwards."""
    loaded = _call("revl_load", {"files": [str(tree["app"]), str(tree["mem"])]})
    assert loaded["ok"] is True, loaded
    hatched = _hatch("revl_change", {"commit": True,
                                     "edit": {"target": str(tree["mem"]),
                                              "edits": [EDIT]}})
    assert hatched["ok"] is True and hatched["committed"] is True, hatched
    assert hatched["disk"] == {"inSync": False, "stale": [str(tree["mem"])]}
    assert _hatch("revl_source", {"symbol": "MemoryStore"})["disk"] == \
        {"inSync": False, "stale": [str(tree["mem"])]}


@needs_runtime
def test_the_hatch_reads_a_verb_it_cannot_see_and_then_calls_it(tree):
    """The documented two-step, end to end: `revl_verbs {names}` returns the
    schema, and `revl_verbs {name, args}` is the call. Neither needs the verb
    to be listed."""
    assert "revl_export" not in [t["name"] for t in _list()]
    schema = _call(disclosure.DISCOVERY, {"names": ["revl_export"]})
    assert schema["ok"] is True
    assert schema["tools"][0]["name"] == "revl_export"
    assert _hatch("revl_state")["ok"] is True
