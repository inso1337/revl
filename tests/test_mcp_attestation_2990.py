"""Issue #2990: MCP server attestation, a pinned tool manifest, and the
untrusted origin label every external answer carries.

Three layers are exercised here:

* the record itself (`revl.mcp.attestation`): one asymmetric signature over a
  server id, a session, an expiry and per-tool schema digests, verified under a
  key the client pins;
* the proxy's adoption of it (`Proxy._adopt_attestation` / `_list_upstream_tools`):
  the same address answering with another key, a manifest that changed after
  the pin (refused by tool name), and a missing record all refuse;
* the surface (`revl.mcp.schema.render_imported_source(origin=...)`): an MCP
  answer enters a program as `Untrusted[...]`, so the G9 sink rule applies.

The record is deliberately not a tool-safety claim: it says the pinned server
signed this manifest, never that the tools are honest. The origin label says
the answer is untrusted data, not that the attestation is missing.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.errors import RevlError  # noqa: E402
from revl.mcp import attestation as att  # noqa: E402
from revl.mcp import proxy as proxy_mod  # noqa: E402
from revl.mcp.schema import (classify_imported_tools,  # noqa: E402
                             render_imported_source)
from revl.peer_identity import identity_from_seed  # noqa: E402

SEED_A = b"mcp-origin-a"
SEED_B = b"mcp-origin-b"
SERVER_ID = "notes"

TOOLS = [
    {"name": "list_notes", "description": "List every note.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "delete_note", "description": "Move a note to the trash.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"}},
                     "required": ["id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


def _identity(seed=SEED_A):
    return identity_from_seed("mcp-server", seed)


def _record(identity=None, tools=None, *, session=None, server_id=SERVER_ID,
            ttl=3600.0, capabilities=None):
    return att.make_attestation(identity or _identity(), server_id=server_id,
                                tools=TOOLS if tools is None else tools,
                                session=session, ttl=ttl,
                                capabilities=capabilities)


def _pin(identity=None, tools=None, *, capabilities=None):
    return att.make_pin(identity or _identity(), server_id=SERVER_ID,
                        tools=TOOLS if tools is None else tools,
                        capabilities=capabilities)


# ---------------------------------------------------------------- the record

def test_an_attestation_verifies_under_the_pinned_key():
    record = _record()
    ok, reason = att.verify_attestation(record, _identity().public_key)
    assert ok, reason
    assert record["sign_alg"] == "ecdsa-p256-sha256"
    assert record["key_id"] == _identity().key_id
    assert record["manifest_digest"] == att.manifest_digest(record["tools"])


def test_a_pin_round_trips_through_a_file(tmp_path):
    record = _record()
    target = tmp_path / "notes.pin.json"
    att.write_pin(target, _pin())
    assert att.verify_pin(record, att.load_pin(target))["ok"] is True


def test_a_changed_tool_is_refused_and_named():
    pin = _pin()
    changed = [{"name": "list_notes", "description": "List every note.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True, "destructiveHint": False}},
               TOOLS[1]]
    verdict = att.verify_pin(_record(tools=changed), pin)
    assert verdict["ok"] is False
    assert verdict["changed"] == ["list_notes"]
    assert "list_notes" in verdict["reason"]


def test_a_tool_added_after_the_pin_is_refused_and_named():
    added = TOOLS + [{"name": "exfiltrate", "inputSchema": {"type": "object"}}]
    verdict = att.verify_pin(_record(tools=added), _pin())
    assert verdict["ok"] is False
    assert verdict["changed"] == ["exfiltrate"]
    assert "exfiltrate" in verdict["reason"]


def test_the_same_address_serving_another_server_is_refused():
    """The pinned key is the verifier's, never the record's: a second identity
    answering at the same address signs a perfectly valid record under its own
    key, and that is exactly the substitution the pin exists to catch."""
    other = _identity(SEED_B)
    verdict = att.verify_pin(_record(other), _pin())
    assert verdict["ok"] is False
    assert "different public key" in verdict["reason"]


def test_an_expired_attestation_is_refused():
    now = datetime.now(timezone.utc)
    record = _record(ttl=60.0)
    ok, reason = att.verify_attestation(
        record, _identity().public_key, now=now + timedelta(seconds=120))
    assert ok is False
    assert "expired" in reason


def test_verify_pin_enforces_expiry_without_a_caller_supplied_clock():
    """The proxy calls `verify_pin` with no clock, so the default path must
    still refuse an expired record."""
    record = _record(ttl=0.0)
    verdict = att.verify_pin(record, _pin())
    assert verdict["ok"] is False
    assert "expired" in verdict["reason"]


def test_a_pin_without_a_manifest_is_refused(tmp_path):
    target = tmp_path / "empty.pin.json"
    target.write_text('{"kind": "revl.mcp.pin", "version": "1.0", '
                      '"server_id": "notes", "public_key": "00"}')
    with pytest.raises(att.AttestationError) as caught:
        att.load_pin(target)
    assert "tools" in str(caught.value)


def test_a_manifest_that_lists_one_tool_twice_is_refused():
    twice = [TOOLS[0], dict(TOOLS[0])]
    with pytest.raises(att.AttestationError) as caught:
        att.tool_digests(twice)
    assert "twice" in str(caught.value)


def test_a_server_declared_capability_scope_is_signed():
    record = _record(capabilities=["notes.read"])
    assert record["capabilities"] == ["notes.read"]
    ok, reason = att.verify_attestation(record, _identity().public_key)
    assert ok, reason


# ---------------------------------------------------------------- the proxy

class _FakeUpstream:
    """A stdio-shaped upstream with canned answers: one value per request, in
    order, so the same 'address' can answer with two identities."""

    def __init__(self, **answers):
        self.answers = {k: list(v) if isinstance(v, list) else v
                        for k, v in answers.items()}
        self.on_notification = None
        self.tools_changed = False
        self.sent = []

    def request(self, method, params=None, observe=None):
        answer = self.answers[method]
        return answer.pop(0) if isinstance(answer, list) else answer

    def notify(self, method, params=None):
        self.sent.append((method, params))


def _proxy(*, pin=None, **answers):
    return proxy_mod.Proxy(_FakeUpstream(**answers), pin=pin,
                           stdout=object())


def _meta(record):
    return {"_meta": {att.META_ATTESTATION: record}}


def test_a_pinned_upstream_is_adopted_from_initialize_and_tools_list():
    record = _record()
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(record), phase="initialize")
    upstream = _FakeUpstream()
    proxy.upstream = upstream
    proxy.upstream.answers["tools/list"] = {"tools": TOOLS, **_meta(record)}
    tools = proxy._list_upstream_tools()
    assert [t["name"] for t in tools] == ["list_notes", "delete_note"]
    assert proxy.attestation == record


def test_a_pinned_proxy_refuses_an_upstream_with_no_attestation():
    proxy = _proxy(pin=_pin())
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._adopt_attestation({"serverInfo": {"name": "notes"}},
                                 phase="initialize")
    assert "no attestation" in str(caught.value)


def test_the_same_address_answering_with_another_key_mid_exchange_is_refused():
    """initialize is signed by the pinned key; the tools/list that follows is
    signed by a second identity at the same address. The session and server id
    match, so only the signature can catch it, and it does."""
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(_record()), phase="initialize")
    second = _record(_identity(SEED_B))
    proxy.upstream.answers["tools/list"] = {"tools": TOOLS, **_meta(second)}
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._list_upstream_tools()
    assert "different public key" in str(caught.value)


def test_a_tool_list_that_changed_after_the_pin_is_refused_by_name():
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(_record()), phase="initialize")
    changed = [dict(TOOLS[0], description="now it does something else"),
               TOOLS[1]]
    proxy.upstream.answers["tools/list"] = {
        "tools": changed, **_meta(_record(tools=changed))}
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._list_upstream_tools()
    assert "list_notes" in str(caught.value)
    assert "does not match the pin" in str(caught.value)


def test_a_pinned_proxy_refuses_a_paginated_manifest():
    """A pin covers one signed list. A server that pages its tools could hide
    a tool past page one, so a pinned proxy refuses pagination rather than
    pinning a prefix."""
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(_record()), phase="initialize")
    proxy.upstream.answers["tools/list"] = [
        {"tools": [TOOLS[0]], "nextCursor": "p2", **_meta(_record(tools=[TOOLS[0]]))},
        {"tools": [TOOLS[1]], **_meta(_record(tools=[TOOLS[1]]))},
    ]
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._list_upstream_tools()
    assert "paginates" in str(caught.value)


def test_the_pinned_capability_scope_bounds_what_is_proxied():
    proxy = _proxy(pin=_pin(capabilities=["list_notes"]))
    proxy.manifest = {"tools": TOOLS}
    _, excluded = proxy._classify()
    assert [t["name"] for t in excluded] == ["delete_note"]
    assert "capability scope" in excluded[0]["excludedBecause"]


def test_an_unpinned_upstream_keeps_its_record_but_is_not_attested():
    record = _record()
    proxy = _proxy()
    proxy._adopt_attestation(_meta(record), phase="initialize")
    assert proxy.attestation == record
    label = att.origin(proxy.attestation, server_name="notes")
    assert label["trust"] == "untrusted"
    assert label["attested"] is True
    assert label["server"] == SERVER_ID


def test_the_origin_label_of_an_unattested_answer_is_still_untrusted():
    label = att.origin(None, server_name="notes")
    assert label == {"kind": "mcp", "trust": "untrusted", "attested": False,
                     "server": "notes"}


# ---------------------------------------------------------------- the surface

def _origin_surface(*, trust_read_only=False):
    classified = classify_imported_tools({"tools": TOOLS},
                                         trust_read_only=trust_read_only)
    return render_imported_source(
        classified, service="Upstream", key="upstream", backend="py",
        signature="json", origin="net",
        host_body=lambda tool, role: 'return "x"')


def test_an_emission_crossing_declares_net_and_returns_untrusted():
    source = _origin_surface()
    assert "extern emission[net] fn mcp_list_notes" in source
    assert "emission fn list_notes(arguments: Str) -> Untrusted[Str]" in source
    assert "extern witnessed[net] fn mcp_delete_note" not in source


def test_the_origin_surface_compiles():
    proxy_mod.Proxy._compile(_origin_surface(trust_read_only=True))


def test_a_plan_cannot_treat_an_mcp_answer_as_authority():
    """The G9 rule, applied to the MCP origin: a tool's answer is evidence, so
    handing it straight to a `Trusted` sink is refused by name."""
    source = _origin_surface(trust_read_only=True) + '''
extern emission fn take(cmd: Trusted[Str]) -> Unit = @py { return }
service Plan { emission fn run() }
component PlanProvider requires upstream: Upstream provides plan: Plan {
  provide plan {
    fn run() { emit take(upstream.list_notes("{}")) }
  }
}
'''
    with pytest.raises(RevlError) as caught:
        proxy_mod.Proxy._compile(source)
    assert caught.value.code == "G9"
    assert "take" in str(caught.value)


# ---------------------------------------------------------------- the server

def test_a_serve_attestation_verifies_and_its_pin_covers_the_served_tools():
    """The two halves meet: `revl mcp serve` signs the list it actually
    serves, the pin it writes is what `revl mcp proxy` loads, and the proxy
    accepts the records without knowing which process produced them."""
    from revl.mcp import server as server_mod

    identity = _identity()
    server_mod.set_attestation(identity, server_id="revl-mcp-serve",
                               ttl=600.0)
    try:
        init = server_mod.handle({"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}})
        listed = server_mod.handle({"jsonrpc": "2.0", "id": 2,
                                    "method": "tools/list", "params": {}})
        served = listed["result"]["tools"]
        pin = server_mod.attestation_pin(served)
    finally:
        server_mod.set_attestation(None)

    init_record = init["result"]["_meta"][att.META_ATTESTATION]
    list_record = listed["result"]["_meta"][att.META_ATTESTATION]
    assert init_record["session"] == list_record["session"]
    assert att.verify_pin(list_record, pin)["ok"] is True
    assert set(pin["tools"]) == {tool["name"] for tool in served}

    proxy = _proxy(pin=pin)
    proxy._adopt_attestation(init["result"], phase="initialize")
    proxy.upstream.answers["tools/list"] = listed["result"]
    assert [t["name"] for t in proxy._list_upstream_tools()] \
        == [tool["name"] for tool in served]


def test_the_call_trace_records_the_untrusted_origin():
    label = att.origin(_record())
    trace = proxy_mod._origin_trace(
        [{"channel": "host", "subject": "new", "detail": ""}], label)
    assert trace[0]["channel"] == "host", "the call's own events are kept"
    assert trace[-1] == {"channel": "origin", "subject": SERVER_ID,
                         "detail": "untrusted mcp answer"}
    unattested = proxy_mod._origin_trace(
        None, att.origin(None, server_name="notes"))
    assert "unattested" in unattested[-1]["detail"]


def test_a_served_tool_list_that_does_not_match_the_signed_manifest_is_refused():
    """The blocking review on #3103: the record can be genuine and still travel
    beside an edited list. Comparing the record to the pin says nothing about
    what the upstream actually served, so the served page is digested too."""
    record = _record()  # signed over TOOLS, matches the pin
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(record), phase="initialize")
    edited = [dict(TOOLS[0], description="edited after signing"), TOOLS[1]]
    proxy.upstream.answers["tools/list"] = {
        "tools": edited, **_meta(record)}
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._list_upstream_tools()
    assert "does not match the manifest it signed" in str(caught.value)
    assert "list_notes" in str(caught.value)


def test_a_served_tool_list_with_an_unsigned_extra_tool_is_refused():
    record = _record()
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(record), phase="initialize")
    proxy.upstream.answers["tools/list"] = {
        "tools": TOOLS + [{"name": "exfiltrate", "inputSchema": {"type": "object"}}],
        **_meta(record)}
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._list_upstream_tools()
    assert "exfiltrate" in str(caught.value)


def test_an_expired_record_stops_being_relayed_and_answered():
    """Review item 3 on #3103: expiry was checked only at connect, so a
    long-lived proxy kept relaying an expired record."""
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(_record()), phase="initialize")
    assert proxy._attestation_refusal() is None
    assert att.META_ATTESTATION in proxy.describe()["_meta"]

    proxy.attestation = _record(ttl=0.0)  # the proxy outlived its window
    refusal = proxy._attestation_refusal()
    assert refusal is not None and "expired" in refusal
    assert "_meta" not in proxy.describe()
    response = proxy.handle({"jsonrpc": "2.0", "id": 1,
                             "method": "initialize", "params": {}})
    assert "expired" in response["error"]["message"]
    # a mutation staged before expiry and approved after it reaches the
    # upstream through the host body, not through `call_tool`, so the check
    # lives in `_upstream_call` too
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._upstream_call(TOOLS[0], {}, None)
    assert "expired" in str(caught.value)


def test_an_unpinned_expired_record_is_not_relayed():
    proxy = _proxy()
    proxy.attestation = _record(ttl=0.0)
    assert "_meta" not in proxy.describe()
    assert proxy._attested_origin() is None


def test_an_unpinned_attesting_upstream_that_paginates_is_compared_as_a_whole():
    """The signed record can cover the whole manifest while the response is
    delivered in pages. Compare the accumulated list, not page one against the
    full signed list (fix-verify #3103)."""
    record = _record()  # signs all of TOOLS
    proxy = _proxy()
    proxy.upstream.answers["tools/list"] = [
        {"tools": TOOLS[:1], "nextCursor": "p2", **_meta(record)},
        {"tools": TOOLS[1:], **_meta(record)},
    ]
    tools = proxy._list_upstream_tools()
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]


def test_an_unpinned_served_list_mismatch_is_proxied_unchanged():
    """Review item 3 at 5172d0073: without a pin the record is unverified, so
    an inconsistent record under `_meta` does not refuse an upstream the proxy
    would otherwise relay unchanged. The pinned side is the next test."""
    record = _record()
    proxy = _proxy()
    edited = [dict(TOOLS[0], description="edited after signing"), TOOLS[1]]
    proxy.upstream.answers["tools/list"] = {"tools": edited, **_meta(record)}
    tools = proxy._list_upstream_tools()
    assert tools == edited


def test_a_pinned_served_list_mismatch_is_refused():
    record = _record()
    proxy = _proxy(pin=_pin())
    proxy._adopt_attestation(_meta(record), phase="initialize")
    edited = [dict(TOOLS[0], description="edited after signing"), TOOLS[1]]
    proxy.upstream.answers["tools/list"] = {"tools": edited, **_meta(record)}
    with pytest.raises(proxy_mod.UpstreamError) as caught:
        proxy._list_upstream_tools()
    assert "does not match the manifest it signed: list_notes" in str(
        caught.value)


def _narrowed_proxy():
    """A pin narrowed to `list_notes` against a server that attests both
    tools, adopted from initialize and tools/list."""
    record = _record()
    proxy = _proxy(pin=_pin(capabilities=["list_notes"]))
    proxy._adopt_attestation(_meta(record), phase="initialize")
    proxy.upstream.answers["tools/list"] = {"tools": TOOLS, **_meta(record)}
    proxy.manifest = {"tools": proxy._list_upstream_tools()}
    return proxy


def test_a_narrowed_pin_is_adopted_and_excludes_the_tool_outside_it():
    """Review item 1 at 5172d0073: `verify_pin` refused a narrowed pin whole at
    initialize, so the exclusion in `_classify` never ran."""
    proxy = _narrowed_proxy()
    assert proxy.attestation is not None
    proxied, excluded = proxy._classify()
    assert [t["name"] for t in proxied] == ["list_notes"]
    assert [t["name"] for t in excluded] == ["delete_note"]
    assert excluded[0]["excludedBecause"] == \
        "outside the pinned capability scope"


def test_a_tool_outside_the_narrowed_pin_is_not_listed_or_callable():
    proxy = _narrowed_proxy()
    proxied, excluded = proxy._classify()
    proxy.tools, proxy.excluded = proxied, excluded
    proxy.by_name = {t["name"]: t for t in proxied}
    proxy._ensure_loaded = lambda: None
    listed = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "list_notes" in names and "delete_note" not in names
    called = proxy.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "delete_note",
                                      "arguments": {"id": "n1"}}})
    assert called["error"]["message"] == "unknown tool: delete_note"


def test_a_narrowed_pin_still_refuses_a_changed_or_missing_tool():
    """The scope only narrows: a pinned tool the server no longer attests, or
    an attested tool that differs from the pin, is refused as before."""
    pin = _pin(capabilities=["list_notes"])
    edited = [dict(TOOLS[0], description="edited"), TOOLS[1]]
    for tools, named in ((TOOLS[:1], "delete_note"), (edited, "list_notes")):
        proxy = _proxy(pin=pin)
        with pytest.raises(proxy_mod.UpstreamError) as caught:
            proxy._adopt_attestation(_meta(_record(tools=tools)),
                                     phase="initialize")
        assert f"does not match the pin: {named}" in str(caught.value)


def test_an_expired_pinned_proxy_refuses_prompts_and_resources():
    """Review item 2 at 5172d0073: the passthrough branch forwarded without
    asking `_attestation_refusal`."""
    proxy = _proxy(pin=_pin(), **{"prompts/get": {"messages": []},
                                  "resources/read": {"contents": []}})
    proxy._adopt_attestation(_meta(_record()), phase="initialize")
    live = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                         "params": {"name": "p"}})
    assert "result" in live
    proxy.attestation = _record(ttl=0.0)
    for method in ("prompts/get", "resources/read"):
        response = proxy.handle({"jsonrpc": "2.0", "id": 2, "method": method,
                                 "params": {"name": "p", "uri": "notes://1"}})
        assert "expired" in response["error"]["message"], method
