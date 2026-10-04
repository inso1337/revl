"""Tiered disclosure of the MCP verbs (issue #1697).

The full verb set's schemas cost about 14,000 tokens on every cold start, and
an agent shown all of them at once reaches for a narrow verb where one broad
call would do. So `tools/list` shows a core tier by default: the verbs an agent
needs to author, run and change a composition, plus `revl_verbs`, which returns
any other verb's schema when it is wanted. Every verb stays callable by name,
listed or not; the tier decides only what is advertised up front.

A client that wants the whole list says so when the server starts:
`revl mcp serve --all-tools`, or `REVL_MCP_ALL_TOOLS=1` in its environment.

`TOPICS` groups every verb by what it is for, in the same sections as
docs/mcp-reference.md. A test holds it to the registry, so a new verb cannot
be left out of discovery.
"""

from __future__ import annotations

import os

DISCOVERY = "revl_verbs"

#: Listed by default. Every verb `initialize` tells an agent to use must be
#: here (tests/test_mcp_tiered_tools_1697.py holds the two together):
#:
#: * the authoring loop `initialize` names (#1704), in its order: reuse
#:   (resolve), scaffold, fill (edit), withdraw-safety (query_withdraw), check,
#:   admit, plan before a swap, and explain a diagnostic code;
#: * running it: load, call, swap;
#: * `revl_source` and `revl_change`, listed once they exist (#1741's stack);
#:   a name with no verb behind it is skipped, not listed;
#: * the way to everything else, `revl_verbs`.
CORE = ("revl_resolve", "revl_scaffold", "revl_edit", "revl_query_withdraw",
        "revl_check", "revl_admit", "revl_plan", "revl_explain",
        "revl_load", "revl_call", "revl_swap",
        "revl_source", "revl_change",
        DISCOVERY)

#: `{topic: (what it is for, verbs)}`, in docs/mcp-reference.md's order.
TOPICS: dict[str, tuple[str, tuple[str, ...]]] = {
    "author": ("check, admit, plan and ship a candidate; grammar, scaffold, "
               "format and explain", (
        "revl_check", "revl_admit", "revl_plan", "revl_ship", "revl_deploy",
        "revl_audit", "revl_tools", "revl_grammar", "revl_scaffold",
        "revl_fmt", "revl_explain", "revl_idiom")),
    "session": ("load, call, change and tear down the running composition; "
                "commit, roll back, lease, snapshot", (
        "revl_load", "revl_call", "revl_act", "revl_counterfactual",
        "revl_state", "revl_swap", "revl_edit",
        "revl_unload", "revl_commit", "revl_commit_confirm", "revl_abort",
        "revl_rollback", "revl_undo", "revl_lease", "revl_snapshot",
        "revl_restore", "revl_source", "revl_change", "revl_export")),
    "approve": ("approval tickets, revocation, escalation and quorum; "
                "distillation offers; the E-Stop; forking a session", (
        "revl_approve", "revl_revoke", "revl_escalate", "revl_override",
        "revl_quorum", "revl_distillation_offers", "revl_apply_distillation",
        "revl_revoke_distillation", "revl_estop", "revl_estop_report",
        "revl_fork", "revl_fork_confirm")),
    "grade": ("grade and prove a candidate: gauntlet, quarantine, repair, "
              "canary, resolve", (
        "revl_gauntlet", "revl_quarantine", "revl_repair", "revl_canary",
        "revl_resolve")),
    "replay": ("walk a recorded run: timeline, inspect, step back, bisect, "
               "replay forward", (
        "revl_timeline", "revl_inspect_step", "revl_step_back",
        "revl_replay_bisect", "revl_replay_forward")),
    "query": ("ask the composition: emitters, withdraw, dependents, reach, "
              "drift; live and historical queries", (
        "revl_query_emitters", "revl_query_withdraw", "revl_query_dependents",
        "revl_query_reach", "revl_query_drift", "revl_live_query",
        "revl_history_emitted_between", "revl_history_lifetime")),
}

_ALL_TOOLS = os.environ.get("REVL_MCP_ALL_TOOLS", "") not in ("", "0")


def set_all_tools(enabled: bool) -> None:
    """Advertise every verb (`True`) or the core tier (`False`)."""
    global _ALL_TOOLS
    _ALL_TOOLS = bool(enabled)


def all_tools() -> bool:
    return _ALL_TOOLS


def core(advertised: list) -> list:
    """The core tier's verbs that this server has, in CORE order."""
    by_name = {tool["name"]: tool for tool in advertised}
    return [by_name[name] for name in CORE if name in by_name]


def listed(advertised: list) -> list:
    """What `tools/list` returns: every verb, or the core tier."""
    return list(advertised) if _ALL_TOOLS else core(advertised)


def index(advertised: list) -> dict:
    """`revl_verbs` with no arguments: every topic, each verb with the first
    sentence of its description, and no schemas."""
    by_name = {tool["name"]: tool for tool in advertised}
    return {topic: {"about": about,
                    "verbs": {name: _gist(by_name[name])
                              for name in verbs if name in by_name}}
            for topic, (about, verbs) in TOPICS.items()}


def schemas(advertised: list, names) -> tuple[list, list]:
    """`(the exact advertised schemas of names, the names that are unknown)`."""
    by_name = {tool["name"]: tool for tool in advertised}
    found = [by_name[name] for name in names if name in by_name]
    unknown = [name for name in names if name not in by_name]
    return found, unknown


def topic_names(topic: str) -> tuple[str, ...] | None:
    entry = TOPICS.get(topic)
    return entry[1] if entry else None


def _gist(tool: dict) -> str:
    text = " ".join((tool.get("description") or "").split())
    end = text.find(". ")
    return text if end < 0 else text[:end + 1]
