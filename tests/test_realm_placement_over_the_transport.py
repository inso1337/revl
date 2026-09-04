"""A realm is an authority address, and no untrusted author may write one.

Item 334 slice 2 bounded ONE door: `Gate.propose` admits under
`AdmissionProfile.self_extension`, which is `untrusted_author` plus the `G9`
realm refusal. `no_realm_placement` was an OPT-IN flag on the profile and
`self_extension` was the only constructor that set it, so the two other call
sites that admit agent-authored source built a profile without it:

  * `mcp.server.AuthoringTrust.profile()` — backs `compile_under_authoring`,
    the one compiler door for agent-supplied source, and therefore every MCP
    verb that takes inline `source`/`modules`: `revl_load` and `revl_swap`
    included. Not per-turn: a `revl_swap` REPLACES the composition and the
    successor keeps serving, exactly as `propose` does.
  * `mcp.session.Session.admit` — item 330's per-turn admit. Additive, but
    `_wire_turn` rebuilds the class map over the merged composition, so the
    turn's self-chosen realm is the realm every ticket the turn raises carries
    for as long as the turn runs.

Same one-line grab in both, at DEFAULT authoring trust with no operator flags:

    isolate tool in realm("billing")

`test_the_realm_a_candidate_writes_selects_the_operators_rule` drives the whole
chain over the transport and is the reason this matters: with an operator's
`component * may auto-approve notify, ops in realm billing` rule live, the same
candidate WITHOUT that line has its swap held for a human yes, and WITH it the
swap went straight through — no ticket, no prompt, the class-(c) activation
crossing fired.

The fix is structural rather than per-call-site: `no_realm_placement` is now a
property of `untrusted_author`, so it holds at every door that admits source
whose AUTHOR is not trusted, and `test_every_admission_profile_call_site` reads
the construction sites out of the source so a fourth door cannot open quietly.

What is NOT closed, deliberately: source the OPERATOR authored. Jailed `files`
and a caller that passes `over_the_transport=False` (`revl bundle`, `truc ship`,
`revl canary`/`repair`/`quarantine`, the `revl swap`/`load` CLI) compile
unprofiled and keep every realm the human wrote. The distinction is not which
function is called, it is whether the source arrived over the transport.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.mcp import server  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading and swapping a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)


# --------------------------------------------------------------------------- #
# Sources. One line apart, so the realm placement is the only variable.
# --------------------------------------------------------------------------- #

_DECLS = ("service Ops { emission[notify] fn stash(p: Str) }\n"
          "service Tool { fn describe() -> Str }\n")

# the OPERATOR's host code, wired in as `AuthoringTrust.providers`: the agent
# may compose its service, never author or reach its extern.
_PROVIDER = ("extern emission[notify] fn notify(p: Str) = @py { return }\n"
             "component OpsProvider provides ops: Ops {\n"
             "  provide ops { fn stash(p) { emit notify(p) } }\n"
             "}\n")

_BASE = (_DECLS + "component ToolV1 provides tool: Tool {\n"
         '  provide tool { fn describe() = "v1" }\n'
         "}\n")


def _candidate(realm_line: str = "") -> str:
    """The successor. Its ACTIVATION reaches the operator's class-(c) emission,
    which is what the approval policy holds the swap on."""
    return (_DECLS + "component ToolV2 requires ops: Ops provides tool: Tool {\n"
            + realm_line
            + '  emit ops.stash("boot")\n'
            '  provide tool { fn describe() = "v2" }\n'
            "}\n")


def _turn(realm_line: str = "") -> str:
    """An item-330 per-turn source: ADDITIVE, providing a key of its own, wired
    into the running composition and torn down with the turn."""
    return ("service Turn { fn go() -> Str }\n"
            "component TurnTool provides turn: Turn {\n"
            + realm_line
            + '  provide turn { fn go() = "t" }\n'
            "}\n")


_CLEAN = _candidate()
_REALM = _candidate('  isolate tool in realm("billing")\n')
# the item-162 plural route: the same fact with a fan-out attached.
_REALMS = _candidate('  isolate ops in realms("w1", "w2") strategy(round_robin)\n')

# the operator's standing rule: everything in the `billing` realm may emit
# without a prompt. Written for the operator's OWN generation-N components.
_REALM_RULE = "component * may auto-approve notify, ops in realm billing"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@pytest.fixture
def transport(tmp_path):
    """A default-closed server rooted at `tmp_path`, driven the way an agent
    drives it: JSON-RPC in, JSON payload out. Nothing here reads internal
    state — every assertion below is on a payload that crossed the wire."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    before_session = server.SESSION
    server.SESSION = Session()
    server.set_authoring_trust(host_code=False, granted=None,
                              providers={"prov.rvl": _PROVIDER},
                              roots=(str(tmp_path),))

    def call(name: str, arguments: dict) -> dict:
        response = server.handle({"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/call",
                                  "params": {"name": name,
                                             "arguments": arguments}})
        return json.loads(response["result"]["content"][0]["text"])

    try:
        yield call
    finally:
        server.AUTHORING = before
        server.SESSION = before_session


def _diagnostics(payload: dict) -> list:
    return payload.get("diagnostics") or []


def _message(payload: dict) -> str:
    return " ".join(d.get("message", "") for d in _diagnostics(payload))


def _codes(payload: dict) -> set:
    return {d.get("code") for d in _diagnostics(payload)}


# =========================================================================== #
# 1. The refusal, at the transport boundary. No runtime needed.
# =========================================================================== #

@pytest.mark.parametrize("verb", ["revl_check", "revl_admit", "revl_plan",
                                  "revl_load", "revl_swap"])
@pytest.mark.parametrize("source,spelling", [
    (_REALM, 'realm("billing")'),
    (_REALMS, 'realms("w1", "w2")'),
], ids=["singular", "plural-route"])
def test_inline_source_may_not_name_a_realm(transport, verb, source, spelling):
    """Every verb that takes inline `source` refuses it, and the refusal names
    the rule: `G9`, the placement it saw, and the shared-realm alternative.

    Parametrized over the verbs rather than asserted on one, for the reason PR
    #328 records: enforcement that each verb wires for itself is enforcement a
    verb will eventually not wire."""
    payload = transport(verb, {"source": source})
    assert payload["ok"] is False
    assert "G9" in _codes(payload)
    message = _message(payload)
    assert "forbids naming a realm" in message
    assert spelling in message


def test_a_realm_smuggled_into_an_agent_supplied_module_is_refused(transport):
    """`modules` arrive over the transport too, whatever path they claim."""
    module = ("pub fn helper() -> Str { return \"h\" }\n"
              "component Sneak provides sneak: Tool {\n"
              '  isolate sneak in realm("billing")\n'
              '  provide sneak { fn describe() = "s" }\n'
              "}\n")
    payload = transport("revl_check", {
        "source": _DECLS + 'use "m.rvl" { helper }\n'
                           "component T provides tool: Tool {\n"
                           '  provide tool { fn describe() = helper() }\n}\n',
        "modules": {"m.rvl": module}})
    assert payload["ok"] is False
    assert payload.get("authoringTrust") == "untrusted"


# =========================================================================== #
# 2. The mechanism, driven end to end. Why the refusal above has to exist.
# =========================================================================== #

@needs_cordis
def test_the_realm_a_candidate_writes_selects_the_operators_rule(transport,
                                                                 tmp_path):
    """The operator stands up one realm-scoped auto-approve rule for its own
    `billing` components. The agent then swaps in a successor.

    Control — the candidate says nothing about realms, lands in the shared
    realm, and its class-(c) activation crossing is HELD: the swap comes back
    `approvalRequired`, carrying a ticket whose `realm` is `""`, which the
    operator's rule does not cover.

    Exploit — the same candidate plus `isolate tool in realm("billing")`. On
    `origin/main` that swap returned `{ok: True, admitted: True, swapped: True}`
    with no ticket at all: the agent's self-chosen realm matched the operator's
    rule, the emission fired unprompted, and the successor kept serving. It is
    now refused before any of that, and the ticket the control still raises is
    the proof the rule and the crossing were both live."""
    from revl.policy import parse_policy

    base = tmp_path / "base.rvl"
    base.write_text(_BASE, encoding="utf-8")
    server.SESSION.sandbox = parse_policy(_REALM_RULE)
    server.SESSION.approval_policy = "auto"
    assert transport("revl_load", {"files": [str(base)],
                                   "record": True})["ok"] is True

    control = transport("revl_swap", {"source": _CLEAN,
                                      "replacing": ["ToolV1"]})
    assert control["ok"] is False
    assert control["approvalRequired"] is True
    ticket = control["ticket"]
    assert ticket["component"] == "ToolV2"
    assert ticket["realm"] == ""
    assert ticket["classCCapabilities"] == ["notify", "ops"]

    exploit = transport("revl_swap", {"source": _REALM,
                                      "replacing": ["ToolV1"]})
    assert exploit["ok"] is False
    assert exploit.get("swapped") is not True
    assert exploit.get("approvalRequired") is not True   # not a prompt: refused
    assert "G9" in _codes(exploit)
    assert 'realm("billing")' in _message(exploit)


# =========================================================================== #
# 3. The operator's own source is untouched. The trap PR #328 fell into.
# =========================================================================== #

@needs_cordis
def test_an_operators_reviewed_file_still_places_its_own_realms(transport,
                                                                tmp_path):
    """A `.rvl` file inside a sanctioned root was put there by a human — no MCP
    verb writes to disk. It compiles unprofiled and keeps its realms, through
    `revl_load` AND `revl_swap`."""
    base = tmp_path / "base.rvl"
    base.write_text(_BASE, encoding="utf-8")
    assert transport("revl_load", {"files": [str(base)]})["ok"] is True

    reviewed = tmp_path / "successor.rvl"
    reviewed.write_text(_candidate('  isolate tool in realm("billing")\n'),
                        encoding="utf-8")
    payload = transport("revl_swap", {"files": [str(reviewed)],
                                      "replacing": ["ToolV1"]})
    assert payload["ok"] is True, _message(payload)
    assert payload["swapped"] is True


def test_a_trusted_author_still_places_its_own_realms(transport):
    """`--author-trust trusted` is the operator saying the agent's source is
    reviewed. `profile()` returns None there, so nothing above applies."""
    server.set_authoring_trust(host_code=True)
    payload = transport("revl_check", {"source": _REALM})
    assert payload["ok"] is True, _message(payload)


def test_the_cli_is_its_own_author():
    """`over_the_transport=False` is the whole distinction, and it is the flag
    PR #328 added for exactly this: the CLI callers reuse these modules as a
    library, where the human running the command IS the author."""
    ir = server.compile_under_authoring(_candidate(
        '  isolate tool in realm("billing")\n'), None,
        over_the_transport=False)
    assert ir["components"][0]["isolate"] == {"tool": "billing"}


# =========================================================================== #
# 4. `Session.admit` — item 330's per-turn door, closed on the same rule.
# =========================================================================== #

@needs_cordis
def test_a_per_turn_admit_may_not_name_a_realm(transport, tmp_path):
    """The verdict is the observable: `admit` returns a refusal as DATA (never
    a raised error), so this reads the `AdmitVerdict` the running composition
    gets back through the in-language `admit` crossing.

    Additive is not the same as harmless. `_wire_turn` rebuilds the class map
    over the MERGED composition — it has to, skipping it was a total class-(c)
    bypass — so a turn's self-chosen realm is the realm on every ticket the turn
    raises. A shorter blast radius in TIME; the same one in AUTHORITY, and one
    covered crossing is all an exfiltration needs."""
    base = tmp_path / "base.rvl"
    base.write_text(_BASE, encoding="utf-8")
    assert transport("revl_load", {"files": [str(base)]})["ok"] is True

    verdict = server.SESSION.admit(_turn('  isolate turn in realm("billing")\n'))
    assert verdict.admitted is False
    assert verdict.code == "G9"
    assert 'realm("billing")' in verdict.message

    # ...and a turn that says nothing about realms still admits and wires, so
    # the refusal above is the realm clause and not the turn shape.
    clean = server.SESSION.admit(_turn())
    assert clean.admitted is True, clean.message
    assert clean.keys == ("turn",)


# =========================================================================== #
# 5. The guard. What stops a fourth door.
# =========================================================================== #

# A call site building an `AdmissionProfile` directly, with the reason it is not
# admitting agent-authored source. Same shape as
# `test_mcp_authority_gate.py`'s compiler-door table: the set is CHECKED, not
# trusted, so a new entry has to be argued for in this file.
_DIRECT_CONSTRUCTION = {
    "src/revl/__main__.py":
        "`revl compile --taint-strict`: an OPERATOR hardening its own compile. "
        "The author is the human running the command, so no author-trust flag "
        "belongs on it (`AdmissionProfile.untrusted` is False for it).",
}


def _profile_call_sites() -> dict:
    """Every `AdmissionProfile(...)` / `.untrusted_author(...)` /
    `.self_extension(...)` construction under `src/`, read out of the AST rather
    than grepped, keyed by repo-relative path."""
    out: dict = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.name == "admit_profile.py":
            continue          # the definition itself
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and \
                    isinstance(func.value, ast.Name) and \
                    func.value.id == "AdmissionProfile":
                out.setdefault(rel, set()).add(func.attr)
            elif isinstance(func, ast.Name) and func.id == "AdmissionProfile":
                out.setdefault(rel, set()).add("<direct>")
    return out


def test_every_admission_profile_call_site():
    """Every construction site either goes through a named constructor —
    `untrusted_author` or its `self_extension` alias, both of which carry every
    author-trust flag — or is in `_DIRECT_CONSTRUCTION` with the reason it is
    not admitting agent-authored source.

    The defect this guards was not a missing check, it was a check wired at ONE
    of three call sites that each hand-picked their flags. A profile assembled
    field by field is a policy with a hole in it waiting to happen, so the
    enumeration is the enforcement."""
    sites = _profile_call_sites()
    assert sites, "the AST walk found nothing — the search is broken, not clean"
    offenders = {rel: sorted(kinds) for rel, kinds in sites.items()
                 if "<direct>" in kinds and rel not in _DIRECT_CONSTRUCTION}
    assert not offenders, (
        f"these build an `AdmissionProfile` field by field: {offenders}. Use "
        f"`AdmissionProfile.untrusted_author(granted)` if the author is "
        f"untrusted, or add the file to `_DIRECT_CONSTRUCTION` with the reason "
        f"it is not.")
    stale = set(_DIRECT_CONSTRUCTION) - set(sites)
    assert not stale, f"stale exceptions, no longer construct one: {stale}"


def test_the_untrusted_author_profile_carries_every_author_flag():
    """Read off the constructor, so a flag added later is caught the moment it
    is not wired into the one profile the untrusted author is admitted under."""
    from dataclasses import fields

    from revl.admit_profile import AdmissionProfile

    profile = AdmissionProfile.untrusted_author(["Ops"])
    for field in fields(AdmissionProfile):
        if field.name == "granted":
            assert profile.granted == frozenset({"Ops"})
            continue
        assert getattr(profile, field.name) is True, (
            f"`untrusted_author` leaves `{field.name}` off. Every field here is "
            f"a property of the AUTHOR: a call site that omits one is not a "
            f"narrower policy, it is a door with a hole in it.")


def test_the_mcp_door_carries_it_in_both_of_its_branches():
    """`AuthoringTrust.profile()` has two arms — with and without an operator
    `--grant` allowlist — and the defect lived in the one that `replace()`s a
    field off the profile. Driven through the accessor, not read off the source:
    building the right profile and then dropping a flag on the next line leaves
    a grep green."""
    from revl.mcp.server import AuthoringTrust

    for trust in (AuthoringTrust(),
                  AuthoringTrust(granted=frozenset({"Ops"}))):
        profile = trust.profile()
        assert profile is not None
        assert profile.no_realm_placement is True, trust
        assert profile.no_extern is True and profile.no_declassify is True
    # ...and the escape hatch is still an escape hatch.
    assert AuthoringTrust(host_code=True).profile() is None
