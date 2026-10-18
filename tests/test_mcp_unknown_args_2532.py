"""Every verb refuses an undeclared top-level argument by name (issue #2532).

Before this, a key a verb did not declare was dropped on the floor and the
refusal the caller got blamed something else: `revl_plan {withdraw: {...}}`
answered "provide `source` or `files`" and never said that `withdraw` is not
an argument of `revl_plan` (it is one of `revl_change`; the plan verb calls
the same thing `replacing`). The server now checks each call once, at the
transport boundary, against the verb's own `inputSchema`, which says
`additionalProperties: false`: an undeclared key is a `usage` refusal that
names each such key, the declared key it stands for and the verbs that do
declare it, and nothing runs.

The per-verb tests are generated from the server's own tool list and the
per-argument tests from the schemas, so a verb or an argument added later
cannot be missed.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

#: A key no verb declares, so the refusal can only be the unknown-argument one.
BOGUS = "noSuchArgument2532"

VERBS = [tool["name"] for tool in server_mod.TOOLS]
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in server_mod.TOOLS}

#: The smallest value of each JSON type a schema can declare.
MINIMAL = {"string": "x", "integer": 1, "number": 1, "boolean": True,
           "object": {}, "array": [], "null": None}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _message(payload: dict) -> str:
    return " ".join(d.get("message", "") for d in payload.get("diagnostics", []))


def _is_unknown_argument_refusal(payload: dict) -> bool:
    return (not payload.get("ok")
            and any(d.get("category") == "usage"
                    and d.get("message", "").startswith("unknown argument")
                    for d in payload.get("diagnostics", [])))


def _minimal(schema: dict):
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = declared[0]
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if declared in MINIMAL:
        return MINIMAL[declared]
    if "oneOf" in schema:
        return _minimal(schema["oneOf"][0])
    if "anyOf" in schema:
        return _minimal(schema["anyOf"][0])
    return "x"


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    """Nothing loaded, no runtime, the jail at a scratch dir: every verb then
    refuses or answers before it can touch anything, which is all these tests
    need, because the argument check runs before any of that."""
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    server_mod.set_runtime_available(False)
    yield tmp_path
    server_mod.set_runtime_available(None)
    server_mod.AUTHORING = old


# ---------------------------------------------------------------- the rule

def test_every_schema_declares_additional_properties_false():
    assert [name for name in VERBS
            if SCHEMAS[name].get("additionalProperties") is not False] == []


@pytest.mark.parametrize("verb", VERBS)
def test_every_verb_refuses_an_undeclared_key_by_name(verb):
    payload = _call(verb, {BOGUS: 1})
    assert _is_unknown_argument_refusal(payload), payload
    message = _message(payload)
    assert f"`{BOGUS}`" in message
    assert f"`{verb}`" in message


def test_every_unknown_key_is_named_not_just_the_first():
    payload = _call("revl_state", {BOGUS: 1, "alsoNotDeclared": 2})
    message = _message(payload)
    assert f"`{BOGUS}`" in message and "`alsoNotDeclared`" in message
    assert "unknown arguments" in message


@pytest.mark.parametrize("verb,key", [(name, key) for name in VERBS
                                      for key in SCHEMAS[name]["properties"]])
def test_every_declared_argument_passes_the_argument_check(verb, key):
    """Enough that the refusal, if any, is not the unknown-argument one."""
    value = _minimal(SCHEMAS[verb]["properties"][key])
    payload = _call(verb, {key: value})
    assert not _is_unknown_argument_refusal(payload), payload


# ---------------------------------------------------------- the issue's case

def test_withdraw_on_plan_names_replacing_and_revl_change():
    payload = _call("revl_plan", {"withdraw": {"component": "X", "cascade": True}})
    assert _is_unknown_argument_refusal(payload), payload
    message = _message(payload)
    assert "`withdraw`" in message
    assert "did you mean `replacing`, or `revl_change`?" in message
    assert "provide `source`" not in message
    # two remedies (rename, or the other verb): none is mechanical, so no next
    assert "next" not in payload


def test_a_key_another_verb_declares_names_that_verb():
    payload = _call("revl_plan", {"verb": "drift"})
    message = _message(payload)
    assert "`verb` (an argument of `revl_live_query`, not of `revl_plan`)" in message


def test_a_misspelling_carries_the_renamed_call_as_next():
    payload = _call("revl_plan", {"replacin": ["A"]})
    assert "did you mean `replacing`?" in _message(payload)
    assert payload["next"] == {"tool": "revl_plan",
                               "arguments": {"replacing": ["A"]}, "ready": True}


def test_a_legacy_spelling_is_refused_with_the_declared_name():
    payload = _call("revl_query_drift", {"service": "S", "losses": ["a"]})
    assert _is_unknown_argument_refusal(payload), payload
    assert "did you mean `loses`?" in _message(payload)
    assert payload["next"]["arguments"] == {"service": "S", "loses": ["a"]}


def test_swap_declares_the_service_its_quarantine_gate_reads():
    """`_tool_swap` -> `quarantine.gate_swap` -> `_service_name` reads a
    top-level `service` (the WIT interface for the substrate battery); the
    schema declares it so the check lets it through."""
    payload = _call("revl_swap", {"source": "service S { fn f() -> Str }\n",
                                  "service": "S"})
    assert not _is_unknown_argument_refusal(payload), payload


def test_a_long_list_of_claiming_verbs_is_capped():
    payload = _call("revl_state", {"source": "x"})
    message = _message(payload)
    hint = message[message.index("(an argument of"):message.index(", not of")]
    assert hint.count("`revl_") == 3, hint
    assert " and " in hint and " more" in hint, hint


def test_a_key_nothing_claims_carries_the_call_without_it_as_next():
    payload = _call("revl_state", {BOGUS: 1})
    assert payload["next"] == {"tool": "revl_state", "arguments": {},
                               "ready": True}


# ----------------------------------------------------------------- the hatch

def test_the_hatch_checks_its_inner_call_against_the_named_verb():
    payload = _call("revl_verbs", {"name": "revl_plan",
                                   "args": {"withdraw": {"component": "X"}}})
    assert _is_unknown_argument_refusal(payload), payload
    message = _message(payload)
    assert "`withdraw`" in message and "`revl_plan`" in message
    assert "`revl_verbs` declares" not in message


def test_the_hatch_call_itself_is_checked_against_revl_verbs():
    payload = _call("revl_verbs", {"name": "revl_state", "args": {}, BOGUS: 1})
    assert _is_unknown_argument_refusal(payload), payload
    assert "`revl_verbs` declares" in _message(payload)


def test_the_hatch_still_forwards_declared_args_to_the_named_verb():
    direct = _call("revl_state", {})
    hatched = _call("revl_verbs", {"name": "revl_state", "args": {}})
    assert hatched["ok"] is True
    assert hatched.get("loaded") == direct.get("loaded")


# -------------------------------------------------------------- the boundary

def test_a_handler_to_handler_call_is_below_the_check():
    """`_call_tool` is what one handler runs another through (an undo run by
    `revl_step_back`); the check sits above it, in `handle`."""
    payload = server_mod._call_tool("revl_state", {BOGUS: 1})
    assert not _is_unknown_argument_refusal(payload), payload


def test_the_check_runs_before_the_boolean_check():
    payload = _call("revl_edit", {"commit": "nope", BOGUS: 1})
    assert _is_unknown_argument_refusal(payload), payload


def test_the_helper_is_driven_by_the_schemas():
    from revl.mcp import unknownargs  # noqa: PLC0415 - absent on base
    message, nxt = unknownargs.check("revl_plan", {"withdraw": 1},
                                     server_mod._SCHEMAS)
    assert "`revl_change`" in message and nxt is None
    assert unknownargs.check("revl_plan", {"replacing": ["A"]},
                             server_mod._SCHEMAS) == (None, None)
