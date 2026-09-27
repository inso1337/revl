"""`revl mcp proxy` (issue #1463): gate an existing MCP server with no `.rvl`.

Two halves. The classification tests need no runtime: they pin that the proxy
and `revl mcp import` read ONE classifier, that a contradicted read-only claim
and an unclassifiable tool both land on `emission`, and that `--undo` makes a
tool `witnessed`. The end-to-end tests drive a real fake upstream server
(`tests/fixtures/mcp_proxy_fake_upstream.py`) through a live session, so they
need the cordis-py runtime and skip without it, like every session test.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import proxy as proxy_mod  # noqa: E402
from revl.mcp import schema  # noqa: E402
from revl.mcp.schema import (classify_imported_tools, import_tools,  # noqa: E402
                             parse_undo_specs)

FAKE = ROOT / "tests" / "fixtures" / "mcp_proxy_fake_upstream.py"

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the proxy gates calls through a live cordis-py session; install it "
           "with `sh backends/python/setup.sh` and run under its venv",
)


def _fake_manifest(*, odd: bool = False) -> dict:
    """The fake upstream's own tools/list, read from the fixture itself."""
    spec = importlib.util.spec_from_file_location("_mcp_proxy_fake_manifest", FAKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {"tools": module.TOOLS + (module.ODD_TOOLS if odd else [])}


def _by_name(tools: list) -> dict:
    return {t["name"]: t for t in tools}


# ---------------------------------------------------------------- classification

def test_the_three_fake_tools_classify_as_the_issue_expects():
    tools = _by_name(classify_imported_tools(_fake_manifest()))
    assert tools["list_notes"]["effect"] == "plain"
    assert tools["list_notes"]["readOnlyClaim"] == "unchecked"
    assert tools["delete_note"]["effect"] == "emission"
    # the liar's claim cannot be refuted before it runs: on paper it is plain,
    # and the verdict says the claim is unchecked rather than trusted
    assert tools["touch_note"]["effect"] == "plain"
    assert tools["touch_note"]["readOnlyClaim"] == "unchecked"


def test_the_proxy_surface_is_the_import_classification_not_a_second_one(monkeypatch):
    """One path: the proxy's generated externs carry exactly the classes the
    import's generated externs carry, and the proxy calls the import's
    classifier to get them."""
    manifest = _fake_manifest(odd=True)
    undo = parse_undo_specs(["delete_note=restore_note"])
    imported = compile_source(import_tools(manifest, backend="py", undo=undo), "i.rvl")
    imported_classes = {e["name"]: e["class"] for e in imported["externs"]}

    calls = []
    real = schema.classify_imported_tools

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(proxy_mod, "classify_imported_tools", spy)
    proxy = proxy_mod.Proxy(_NoUpstream(), undo=undo, stdout=io.StringIO())
    proxy.manifest = manifest
    proxy._build()
    assert calls, "the proxy must classify through the import's classifier"
    proxied_classes = {e["name"]: e["class"] for e in proxy.ir["externs"]}
    # the proxy's identifiers carry a fixed `tool_` prefix; the classes match
    assert {k.replace("mcp_tool_", "mcp_"): v
            for k, v in proxied_classes.items()} == imported_classes


def test_a_self_contradicting_read_only_claim_is_flagged_and_not_trusted():
    tools = _by_name(classify_imported_tools(_fake_manifest(odd=True)))
    liar = tools["self_contradicting"]
    assert liar["effect"] == "emission"
    assert liar["readOnlyClaim"] == "contradicted"
    assert "destructiveHint" in liar["reasons"][0]
    source = import_tools(_fake_manifest(odd=True))
    assert "emission fn self_contradicting" in source


def test_a_revl_provenance_emission_contradicts_a_read_only_claim():
    manifest = {"tools": [{"name": "q", "annotations": {"readOnlyHint": True},
                           "x-revl": {"classification": "emission"}}]}
    (tool,) = classify_imported_tools(manifest)
    assert (tool["effect"], tool["readOnlyClaim"]) == ("emission", "contradicted")


@pytest.mark.parametrize("name", ["malformed_annotations", "stringly_read_only"])
def test_an_unclassifiable_tool_takes_the_most_restrictive_class(name):
    tool = _by_name(classify_imported_tools(_fake_manifest(odd=True)))[name]
    assert tool["effect"] == "emission"
    assert tool["unclassifiable"]


def test_a_nameless_or_duplicated_tool_is_unclassifiable():
    manifest = {"tools": [
        {"annotations": {"readOnlyHint": True}},
        {"name": "twice", "annotations": {"readOnlyHint": True}},
        {"name": "twice", "annotations": {"readOnlyHint": True}},
        "not even an object",
    ]}
    tools = classify_imported_tools(manifest)
    assert [t["effect"] for t in tools] == ["emission"] * 4
    assert [t["callable"] for t in tools] == [False, True, True, False]
    assert len({t["op"] for t in tools}) == 4, "identifiers stay unique"


def test_an_observed_crossing_withdraws_the_claim_through_the_same_classifier():
    manifest = _fake_manifest()
    (touch,) = [t for t in classify_imported_tools(
        manifest, distrust={"touch_note": "the upstream said so"})
        if t["name"] == "touch_note"]
    assert (touch["effect"], touch["readOnlyClaim"]) == ("emission", "observed")


def test_distrusting_every_hint_leaves_nothing_plain():
    tools = classify_imported_tools(_fake_manifest(), trust_read_only=False)
    assert {t["effect"] for t in tools} == {"emission"}
    assert _by_name(tools)["list_notes"]["readOnlyClaim"] == "distrusted"


def test_a_declared_undo_makes_the_tool_witnessed_and_compiles():
    undo = parse_undo_specs(["delete_note=restore_note:result"])
    tools = _by_name(classify_imported_tools(_fake_manifest(), undo=undo))
    assert tools["delete_note"]["effect"] == "witnessed"
    assert tools["delete_note"]["undo"] == {"tool": "restore_note", "with": "result"}
    ir = compile_source(import_tools(_fake_manifest(), undo=undo, backend="py"), "u.rvl")
    classes = {e["name"]: e["class"] for e in ir["externs"]}
    assert classes["mcp_delete_note"] == "witnessed"
    assert classes["undo_mcp_delete_note"] == "acquire"


def test_an_undo_on_a_read_only_claim_contradicts_the_claim():
    undo = parse_undo_specs(["touch_note=restore_note"])
    tools = _by_name(classify_imported_tools(_fake_manifest(), undo=undo))
    assert tools["touch_note"]["effect"] == "witnessed"
    assert tools["touch_note"]["readOnlyClaim"] == "contradicted"


@pytest.mark.parametrize("specs", [["nope=restore_note"], ["delete_note=nope"],
                                   ["delete_note=delete_note"]])
def test_an_undo_the_server_cannot_honour_is_refused(specs):
    with pytest.raises(ValueError):
        classify_imported_tools(_fake_manifest(), undo=parse_undo_specs(specs))


@pytest.mark.parametrize("specs", [["delete_note"], ["=x"], ["a="], ["a=:result"],
                                   ["a=b", "a=c"]])
def test_a_malformed_undo_spec_is_refused(specs):
    with pytest.raises(ValueError):
        parse_undo_specs(specs)


def test_a_reserved_word_parameter_no_longer_breaks_the_import():
    manifest = {"tools": [{"name": "type", "inputSchema": {
        "type": "object", "properties": {"type": {"type": "string"},
                                         "a-b": {"type": "string"},
                                         "a_b": {"type": "string"}}}}]}
    compile_source(import_tools(manifest, backend="py"), "kw.rvl")


# ---------------------------------------------------------------- end to end

class _NoUpstream:
    """Stands in where no upstream is contacted (surface building only)."""

    on_notification = None
    tools_changed = False


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """A live proxy over the fake upstream, bound to a fresh server session."""
    from revl.mcp import server
    from revl.mcp.session import Session

    def build(*, undo=(), odd=False, trust_read_only=True):
        session = Session()
        session._wal_path = str(tmp_path / "proxy.wal")
        monkeypatch.setattr(server, "SESSION", session)
        command = [sys.executable, str(FAKE)] + (["--odd-tools"] if odd else [])
        upstream = proxy_mod.Upstream(command, timeout=60)
        out = io.StringIO()
        proxy = proxy_mod.Proxy(upstream, undo=parse_undo_specs(list(undo)),
                                trust_read_only=trust_read_only, stdout=out)
        proxy.activate()
        upstream.start()
        opened.append(proxy)
        proxy.connect()
        return proxy

    opened: list = []
    try:
        yield build
    finally:
        for proxy in opened:
            proxy.deactivate()
            proxy.upstream.close()
        assert proxy_mod._ACTIVE is None


def _call(proxy, name, arguments=None):
    response = proxy.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments or {}}})
    return response["result"]


def _upstream_state(proxy) -> dict:
    """What the upstream holds, read behind the proxy's back."""
    result = proxy.upstream.request("tools/call", {"name": "list_notes",
                                                   "arguments": {}})
    return result["structuredContent"]


@needs_cordis
def test_the_read_only_tool_proceeds_without_a_prompt(gated):
    proxy = gated()
    result = _call(proxy, "list_notes")
    assert result["isError"] is False
    assert result["structuredContent"]["notes"]["n1"] == "first note"
    verdict = result["_meta"]["revl/proxy"]
    assert verdict["classification"] == "plain"
    assert verdict["readOnlyClaim"] == "unchecked"


@needs_cordis
def test_the_destructive_tool_needs_a_human_yes_and_nothing_fires_before_it(gated):
    proxy = gated()
    refused = _call(proxy, "delete_note", {"id": "n1"})
    assert refused["isError"] is True
    ticket = refused["structuredContent"]["ticket"]
    assert refused["structuredContent"]["approvalRequired"] is True
    assert ticket["method"] == "tool_delete_note"
    assert _upstream_state(proxy)["calls"] == [], "the upstream was never reached"

    approved = _call(proxy, "revl_approve", {"hash": ticket["hash"]})
    assert approved["structuredContent"]["ok"] is True
    fired = _call(proxy, "delete_note", {"id": "n1"})
    assert fired["isError"] is False
    assert _upstream_state(proxy)["calls"] == ["delete_note"]

    # the approval was single-use: the same call asks again
    again = _call(proxy, "delete_note", {"id": "n2"})
    assert again["structuredContent"]["approvalRequired"] is True
    assert _upstream_state(proxy)["calls"] == ["delete_note"]


@needs_cordis
def test_the_lying_tool_is_flagged_on_observation_and_gated_from_then_on(gated):
    proxy = gated()
    first = _call(proxy, "touch_note", {"id": "n1"})
    # the first call cannot be stopped: its claim was all there was to go on
    assert first["isError"] is False
    verdict = first["_meta"]["revl/proxy"]
    assert verdict["readOnlyClaim"] == "observed"
    assert verdict["classification"] == "emission"
    assert "notifications/resources/updated" in verdict["flagged"]

    listed = proxy.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    touch = _by_name(listed["result"]["tools"])["touch_note"]
    assert touch["annotations"]["readOnlyHint"] is False
    assert touch["x-revl"]["readOnlyClaim"] == "observed"

    second = _call(proxy, "touch_note", {"id": "n1"})
    assert second["structuredContent"]["approvalRequired"] is True
    assert _upstream_state(proxy)["calls"] == ["touch_note"]
    # the client heard about both the upstream's signal and the new surface
    sent = [json.loads(line)["method"] for line in
            proxy.stdout.getvalue().splitlines()]
    assert "notifications/resources/updated" in sent
    assert "notifications/tools/list_changed" in sent


@needs_cordis
def test_a_declared_undo_rolls_the_proxied_call_back_on_abort(gated, tmp_path):
    proxy = gated(undo=["delete_note=restore_note"])
    result = _call(proxy, "delete_note", {"id": "n1"})
    assert result["isError"] is False, "a witnessed call needs no prompt"
    assert result["_meta"]["revl/proxy"]["actionClass"] == "a"
    assert "n1" in _upstream_state(proxy)["trash"]

    aborted = _call(proxy, "revl_abort")
    assert aborted["structuredContent"]["ok"] is True
    assert aborted["structuredContent"]["noResidue"] is True
    state = _upstream_state(proxy)
    assert state["notes"]["n1"] == "first note" and "n1" not in state["trash"]
    assert state["calls"][-1] == "restore_note"

    records = [json.loads(line) for line in
               (tmp_path / "proxy.wal").read_text(encoding="utf-8").splitlines()]
    kinds = [r.get("record") for r in records]
    assert "discharge-descriptor" in kinds and "aborted" in kinds

    # the proxy boots the next generation, so the client can carry on
    assert _call(proxy, "list_notes")["isError"] is False


@needs_cordis
def test_a_committed_witnessed_call_is_kept(gated):
    proxy = gated(undo=["delete_note=restore_note:result"])
    assert _call(proxy, "delete_note", {"id": "n2"})["isError"] is False
    manifest = _call(proxy, "revl_commit")["structuredContent"]["manifest"]
    confirmed = _call(proxy, "revl_commit_confirm", {"hash": manifest["hash"]})
    assert confirmed["structuredContent"]["ok"] is True
    state = _upstream_state(proxy)
    assert "n2" in state["trash"] and "restore_note" not in state["calls"]


@needs_cordis
def test_estop_refuses_every_later_call_before_it_reaches_the_upstream(gated):
    proxy = gated()
    # the halt is process-global by design; put it back for the next test
    import runtime as rt  # the py backend, on sys.path once a session booted

    try:
        stopped = _call(proxy, "revl_estop", {"reason": "test halt"})
        assert stopped["structuredContent"]["ok"] is True
        refused = _call(proxy, "list_notes")
        assert refused["isError"] is True
        assert "E-STOPPED" in refused["structuredContent"]["diagnostics"][0]["message"]
        assert _upstream_state(proxy)["calls"] == []
    finally:
        rt.clear_estop()
        rt.arm_estop_latch(None)
        rt._LIVE_FRAMES.clear()


@needs_cordis
def test_unclassifiable_upstream_tools_each_need_a_human_yes(gated):
    proxy = gated(odd=True)
    for name in ("self_contradicting", "malformed_annotations", "stringly_read_only"):
        refused = _call(proxy, name)
        assert refused["structuredContent"]["approvalRequired"] is True, name
    assert _upstream_state(proxy)["calls"] == []


@needs_cordis
def test_a_reclassification_that_cannot_go_live_refuses_the_flagged_tool(gated,
                                                                         monkeypatch):
    from revl.mcp.session import SessionError

    proxy = gated()

    def refuse_swap(*_args, **_kwargs):
        raise SessionError("swap refused for the test")

    monkeypatch.setattr(proxy.session, "swap", refuse_swap)
    _call(proxy, "touch_note", {"id": "n1"})
    refused = _call(proxy, "touch_note", {"id": "n1"})
    assert refused["isError"] is True
    assert "refused until the proxy restarts" in \
        refused["structuredContent"]["diagnostics"][0]["message"]
    assert _upstream_state(proxy)["calls"] == ["touch_note"]
    # the honest tool beside it keeps working
    assert _call(proxy, "list_notes")["isError"] is False


@needs_cordis
def test_the_cli_proxies_stdio_both_ways_and_aborts_an_uncommitted_session(tmp_path):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "delete_note", "arguments": {"id": "n1"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "revl_proxy_verdicts", "arguments": {}}},
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    done = subprocess.run(
        [sys.executable, "-m", "revl", "mcp", "proxy",
         "--wal", str(tmp_path / "cli.wal"), "--undo", "delete_note=restore_note",
         "--", sys.executable, str(FAKE)],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path), env=env)
    assert done.returncode == 0, done.stderr
    replies = {m["id"]: m for m in map(json.loads, done.stdout.splitlines()) if "id" in m}
    assert set(replies) == {1, 2, 3, 4, 5}
    assert replies[1]["result"]["serverInfo"]["name"] == "revl-mcp-proxy"
    names = [t["name"] for t in replies[2]["result"]["tools"]]
    assert names[:4] == ["list_notes", "delete_note", "restore_note", "touch_note"]
    assert "revl_approve" in names and "revl_estop" in names
    assert replies[3]["result"]["isError"] is False
    assert [r["uri"] for r in replies[4]["result"]["resources"]] == ["note://n2"]
    verdicts = _by_name([{**v, "name": v["tool"]} for v in
                         replies[5]["result"]["structuredContent"]["tools"]])
    assert verdicts["delete_note"]["classification"] == "witnessed"
    assert "aborted, replayed 1 declared undo(s)" in done.stderr
    records = [json.loads(line) for line in
               (tmp_path / "cli.wal").read_text(encoding="utf-8").splitlines()]
    assert records[-1] == {"record": "aborted", "replayed": [2]}


def test_the_cli_refuses_to_start_without_an_upstream(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    done = subprocess.run([sys.executable, "-m", "revl", "mcp", "proxy"],
                          capture_output=True, text=True, timeout=120,
                          cwd=str(tmp_path), env=env)
    assert done.returncode == 1
    assert "name the upstream server" in done.stderr
