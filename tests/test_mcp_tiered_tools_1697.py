"""Issue #1697: tiered disclosure of the MCP verbs.

By default `tools/list` carries the core tier (`disclosure.CORE`), which ends
with the discovery verb `revl_verbs`. `revl_verbs` returns every other verb's
exact schema on demand, and a verb that is not listed is still callable by
name. `revl mcp serve --all-tools` (or `REVL_MCP_ALL_TOOLS=1`) lists every verb.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import disclosure, repeat  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

ADVERTISED = {tool["name"]: tool for tool in server_mod._ADVERTISED}
#: CORE may name a verb a later PR adds (revl_source, revl_change); only the
#: ones this server has are listed
PRESENT_CORE = [name for name in disclosure.CORE if name in ADVERTISED]
NOT_CORE = sorted(set(ADVERTISED) - set(disclosure.CORE))


def _list() -> list:
    return handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _tiered():
    before = disclosure.all_tools()
    disclosure.set_all_tools(False)
    repeat.forget()
    yield
    disclosure.set_all_tools(before)
    repeat.forget()


# ------------------------------------------------------- the default list

def test_the_default_list_is_the_core_tier_ending_in_discovery():
    listed = _list()
    assert [t["name"] for t in listed] == PRESENT_CORE
    assert listed[-1]["name"] == disclosure.DISCOVERY
    for tool in listed:
        assert tool == ADVERTISED[tool["name"]]   # the exact schema, not a stub


def test_the_core_tier_is_much_smaller_than_the_full_list():
    core = len(json.dumps(_list()))
    full = len(json.dumps(server_mod._ADVERTISED))
    assert core * 3 < full, (core, full)


def _named_in_instructions() -> set:
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    return set(re.findall(r"\brevl_[a-z_]+", init["result"]["instructions"]))


def test_every_verb_initialize_names_is_in_the_core_tier():
    """What `initialize` tells an agent to use is what `tools/list` shows, so
    the two cannot drift. Taken with the runtime available, because the
    runtime-gate announcement (#1692) names verbs that are UNAVAILABLE, which
    is the opposite of a recommendation."""
    server_mod.set_runtime_available(True)
    try:
        named = _named_in_instructions()
    finally:
        server_mod.set_runtime_available(None)
    assert named, "the instructions name no verb at all"
    assert named <= set(disclosure.CORE), sorted(named - set(disclosure.CORE))
    assert named <= set(PRESENT_CORE), "an instruction names a verb that does not exist"


def test_the_core_tier_stays_small():
    assert len(disclosure.CORE) <= 14
    assert disclosure.CORE[-1] == disclosure.DISCOVERY


def test_initialize_tells_the_client_how_to_find_the_rest():
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "call revl_verbs" in init["result"]["instructions"]


# ------------------------------------------------------- discovery

def test_every_verb_has_exactly_one_topic():
    assert disclosure.DISCOVERY in ADVERTISED, "the discovery verb is a verb"
    placed = [name for _about, verbs in disclosure.TOPICS.values() for name in verbs]
    assert len(placed) == len(set(placed)), "a verb is in two topics"
    assert set(placed) == set(ADVERTISED) - {disclosure.DISCOVERY}


def test_no_arguments_is_the_index_without_schemas():
    payload = _call(disclosure.DISCOVERY, {})
    assert payload["ok"] is True
    assert payload["listed"] == PRESENT_CORE
    topics = payload["topics"]
    assert list(topics) == list(disclosure.TOPICS)
    for topic in topics.values():
        assert topic["about"]
        for gist in topic["verbs"].values():
            assert isinstance(gist, str) and gist
    assert "inputSchema" not in json.dumps(topics)


@pytest.mark.parametrize("name", NOT_CORE)
def test_any_other_verb_returns_its_exact_schema(name):
    payload = _call(disclosure.DISCOVERY, {"names": [name]})
    assert payload["ok"] is True
    assert payload["tools"] == [ADVERTISED[name]]


@pytest.mark.parametrize("topic", list(disclosure.TOPICS))
def test_a_topic_returns_its_verbs_schemas(topic):
    payload = _call(disclosure.DISCOVERY, {"topic": topic})
    names = [t["name"] for t in payload["tools"]]
    assert names == list(disclosure.TOPICS[topic][1])
    assert all(t == ADVERTISED[t["name"]] for t in payload["tools"])


@pytest.mark.parametrize("arguments", [{"topic": "nope"}, {"names": ["revl_nope"]}])
def test_an_unknown_topic_or_verb_refuses_with_the_index_as_next(arguments):
    payload = _call(disclosure.DISCOVERY, arguments)
    assert payload["ok"] is False
    assert payload["next"] == {"tool": disclosure.DISCOVERY, "arguments": {},
                               "ready": True}


# ------------------------------------------------- unlisted still callable

def test_an_unlisted_verb_is_still_callable_by_name():
    assert "revl_fmt" not in [t["name"] for t in _list()]
    payload = _call("revl_fmt", {"source": "service S { fn f() -> Int }\n"})
    assert payload["ok"] is True, payload


# ------------------------------------------------------- the opt-out

def test_all_tools_lists_every_verb():
    assert len(_list()) == len(PRESENT_CORE)   # tiered first
    disclosure.set_all_tools(True)
    assert _list() == server_mod._ADVERTISED
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "revl_verbs" not in init["result"]["instructions"]


def _serve(*flags: str, env_extra: dict | None = None) -> list:
    """`revl mcp serve` for real: initialize, then tools/list."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "REVL_MCP_NO_REEXEC": "1"}
    env.pop("REVL_MCP_ALL_TOOLS", None)
    env.update(env_extra or {})
    stdin = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
             '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    proc = subprocess.run([sys.executable, "-P", "-m", "revl", "mcp", "serve", *flags],
                          input=stdin, capture_output=True, text=True, env=env,
                          cwd=str(ROOT), timeout=300, check=False)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line]
    return [t["name"] for t in replies[1]["result"]["tools"]]


def test_the_serve_flag_and_the_environment_opt_out():
    assert _serve() == PRESENT_CORE
    assert len(_serve("--all-tools")) == len(ADVERTISED)
    assert len(_serve(env_extra={"REVL_MCP_ALL_TOOLS": "1"})) == len(ADVERTISED)
