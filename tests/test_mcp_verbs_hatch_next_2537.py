"""Issue #2537: a `revl_verbs` lookup that hands back an unlisted verb's schema
names the hatch that can call it.

`tools/list` shows `disclosure.CORE`, and a function-calling client emits a call
only for a listed tool. A lookup answered with `revl_export`'s schema and
nothing else leaves the caller holding a schema for a call it cannot make, and
the hatch (#2073) that can make it is named only in a description read many
turns earlier. So the answer carries `next` (#1691): `revl_verbs {name, args}`
for each unlisted verb it returned, not ready (the arguments depend on what the
caller means to do). A listed verb gets none, and the tier does not grow.

Every case runs through the real dispatch (`handle()`).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import disclosure, remedy  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

HERE = Path(__file__).resolve().parent


def _shape_checker():
    """The `next` schema assertion #1691 pins, loaded from its own test."""
    spec = importlib.util.spec_from_file_location(
        "_next_shape_1691", HERE / "test_mcp_refusal_next_1691.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._assert_next_shape


_assert_next_shape = _shape_checker()

LISTED = "revl_check"          # in CORE
UNLISTED = "revl_export"        # outside CORE
ALSO_UNLISTED = "revl_state"    # outside CORE


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _tiered():
    before = disclosure.all_tools()
    disclosure.set_all_tools(False)
    remedy.forget()
    yield
    disclosure.set_all_tools(before)
    remedy.forget()


def _hatch_call(verb: str) -> dict:
    return {"tool": disclosure.DISCOVERY,
            "arguments": {"name": verb, "args": {}},
            "ready": False,
            "needs": "`args`: the verb's own arguments"}


def test_the_fixture_verbs_are_where_the_cases_assume():
    core = {tool["name"] for tool in disclosure.listed(server_mod._ADVERTISED)}
    assert LISTED in core
    assert UNLISTED not in core and ALSO_UNLISTED not in core


def test_an_unlisted_verb_lookup_names_the_hatch():
    answer = _call(disclosure.DISCOVERY, {"names": [UNLISTED]})
    assert answer["ok"] is True
    assert [t["name"] for t in answer["tools"]] == [UNLISTED]
    nxt = answer["next"]
    _assert_next_shape(nxt)
    assert nxt == _hatch_call(UNLISTED)
    assert nxt["tool"] == disclosure.DISCOVERY      # emittable: it is listed
    assert nxt["tool"] in {t["name"] for t in
                           disclosure.listed(server_mod._ADVERTISED)}


def test_a_listed_verb_lookup_carries_no_next():
    answer = _call(disclosure.DISCOVERY, {"names": [LISTED]})
    assert answer["ok"] is True
    assert [t["name"] for t in answer["tools"]] == [LISTED]
    assert "next" not in answer


def test_a_mixed_lookup_names_only_the_unlisted_verb():
    answer = _call(disclosure.DISCOVERY, {"names": [LISTED, UNLISTED]})
    assert [t["name"] for t in answer["tools"]] == [LISTED, UNLISTED]
    _assert_next_shape(answer["next"])
    assert answer["next"] == _hatch_call(UNLISTED)


def test_several_unlisted_verbs_get_one_entry_each_in_order():
    answer = _call(disclosure.DISCOVERY,
                   {"names": [UNLISTED, LISTED, ALSO_UNLISTED]})
    nxt = answer["next"]
    _assert_next_shape(nxt)
    assert nxt == [_hatch_call(UNLISTED), _hatch_call(ALSO_UNLISTED)]


def test_a_topic_lookup_names_every_unlisted_verb_it_returned():
    answer = _call(disclosure.DISCOVERY, {"topic": "replay"})
    returned = [t["name"] for t in answer["tools"]]
    core = {t["name"] for t in disclosure.listed(server_mod._ADVERTISED)}
    unlisted = [name for name in returned if name not in core]
    assert unlisted, "the replay topic holds verbs outside the tier"
    nxt = answer["next"]
    _assert_next_shape(nxt)
    calls = nxt if isinstance(nxt, list) else [nxt]
    assert [c["arguments"]["name"] for c in calls] == unlisted


def test_all_tools_lists_everything_so_no_lookup_needs_the_hatch():
    disclosure.set_all_tools(True)
    answer = _call(disclosure.DISCOVERY, {"names": [UNLISTED]})
    assert answer["ok"] is True and "next" not in answer


def test_the_tier_does_not_grow():
    _call(disclosure.DISCOVERY, {"names": [UNLISTED]})
    listed = [t["name"] for t in disclosure.listed(server_mod._ADVERTISED)]
    assert UNLISTED not in listed
    assert listed == [n for n in disclosure.CORE if n in listed]


def test_following_the_next_reaches_the_verb():
    """Sent as handed over (its `args` are `{}`, which `revl_export` takes), the
    `next` is the verb's own call: nothing is loaded, so it answers the refusal
    a direct `revl_export {}` answers, not the hatch's own refusal nor a lookup."""
    nxt = _call(disclosure.DISCOVERY, {"names": [UNLISTED]})["next"]
    followed = _call(nxt["tool"], nxt["arguments"])
    direct = _call(UNLISTED, {})
    assert followed["ok"] is False
    assert followed == direct
    assert "revl_export" in followed["diagnostics"][0]["message"]
    assert "tools" not in followed


needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the export needs a loaded composition, which needs the cordis-py "
           "runtime: install it with `sh backends/python/setup.sh`")

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


@needs_runtime
def test_the_next_with_args_filled_exports(tmp_path, monkeypatch):
    """The entry is the real call and not a formality: after a held change, the
    lookup's `next` sent as handed over exports the file."""
    from revl.mcp import export_jail  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    (tmp_path / "components").mkdir()
    mem = tmp_path / "components" / "memory_store.rvl"
    mem.write_text(MEM_TEXT, encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text(APP_TEXT, encoding="utf-8")
    try:
        assert _call("revl_load", {"files": [str(app), str(mem)]})["ok"] is True
        changed = _call("revl_change", {"commit": True,
                                        "edit": {"target": str(mem),
                                                 "edits": [EDIT]}})
        assert changed["ok"] is True and changed["committed"] is True, changed

        nxt = _call(disclosure.DISCOVERY, {"names": [UNLISTED]})["next"]
        exported = _call(nxt["tool"], nxt["arguments"])   # `args` is `{}`
        assert exported["ok"] is True, exported
        copy = Path(export_jail.export_copy(str(mem), server_mod._file_roots()))
        assert exported["written"] == [str(copy)]
        assert '== "b"' in copy.read_text(encoding="utf-8")
        assert mem.read_text(encoding="utf-8") == MEM_TEXT
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.pending_draft = None
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old
