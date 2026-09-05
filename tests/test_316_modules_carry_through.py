"""Issue #316: a candidate's `modules` reach every compile the verb runs.

Two drops of the same argument, one shape.

  1. `revl_admit` compiled `source` and `files` and forgot `modules`. A
     candidate `revl_check` accepted with its own `use` module was refused by
     the verb that decides whether it may enter the composition, with
     ``cannot find imported module``. `revl_audit` and `revl_tools` had the same
     hole: each verb hand-copied two of the three candidate arguments out of
     its call and passed them to `_compile` by keyword, so the third had a
     default to fall into. That is the per-verb-wiring shape
     `tests/test_mcp_authority_gate.py` describes for the authority checks:
     enforcement each author must remember is enforcement that will not be.
  2. With `--provider` configured, `compile_under_authoring`'s composition
     compile (the second of item 334's two) built its virtual filesystem from
     the providers alone, so an agent module that resolved for the decision
     compile failed the compile that loads.

The fix is positional: one reader (`server._candidate_of`) hands `_compile`
the whole `(source, files, modules)` triple and `modules` has no default, and
the door seeds both of its compiles with every in-memory module, providers
first. This file holds the guard that keeps a fourth verb from dropping it: a
table of every advertised verb whose schema accepts `modules`, each driven
with a candidate that `use`s a module only the call carries. A verb that
accepts `modules` and is not in the table fails here.

The other half is that carrying `modules` must not become a way to smuggle
source past the authoring profile: a module with a realm placement or a raw
host-body extern is refused through `revl_admit` exactly as inline source is,
under both the one-compile and the two-compile shape.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import server  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs cordis-py (backends/python/.venv)",
)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

# the agent's own module: reachable ONLY through the call's `modules` map
LIB = 'pub fn helper() -> Str { return "h" }\n'
MODULES = {"lib.rvl": LIB}

DECLS = "service Tool { fn describe() -> Str }\n"

# the candidate, which `use`s the agent's module by the path the map names
CANDIDATE = (DECLS + 'use "lib.rvl" { helper }\n'
             "component T provides tool: Tool {\n"
             "  provide tool { fn describe() = helper() }\n"
             "}\n")

# what is running: the provider the candidate replaces
RUNNING = (DECLS + "component Old provides tool: Tool {\n"
           '  provide tool { fn describe() = "old" }\n'
           "}\n")

# the OPERATOR's host code, wired in as `AuthoringTrust.providers`: puts the
# door on its two-compile path
PROVIDER = ("service Ops { emission[notify] fn stash(p: Str) }\n"
            "extern emission[notify] fn notify(p: Str) = @py { return }\n"
            "component OpsProvider provides ops: Ops {\n"
            "  provide ops { fn stash(p) { emit notify(p) } }\n"
            "}\n")
PROVIDERS = {"prov.rvl": PROVIDER}

# a module smuggling a realm placement (an authority address)
REALM_MODULE = LIB + ("component Sneak provides sneak: Tool {\n"
                      '  isolate sneak in realm("billing")\n'
                      '  provide sneak { fn describe() = "s" }\n'
                      "}\n")
# a module smuggling a raw host-body extern: the item-329 exploit, one hop out
EXTERN_MODULE = 'pub extern pure fn helper() -> Str = @py { return "h" }\n'

# the two texts a dropped `modules` produces, depending on whether the compile
# had a manifest (a `use` that resolves against nothing) or not (`compile_source`
# refusing a bare `use` structurally)
DROP_SIGNS = ("cannot find imported module",
              "`use` declarations need `modules=`")


# --------------------------------------------------------------------------- #
# Harness: JSON-RPC in, payload out, nothing read off internal state
# --------------------------------------------------------------------------- #

def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1,
                              "method": "tools/call",
                              "params": {"name": name,
                                         "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def _set_trust(providers: dict | None = None, roots: tuple = (),
               host_code: bool = False) -> None:
    server.set_authoring_trust(host_code=host_code, granted=None,
                               providers=providers, roots=roots)


@pytest.fixture(autouse=True)
def _closed_authoring(monkeypatch):
    """The default, closed authoring trust over a fresh session, restored
    afterwards."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    _set_trust()
    yield
    server.AUTHORING = before


@pytest.fixture
def running() -> dict:
    return compile_source(RUNNING, "running.rvl")


def _text(payload: dict) -> str:
    return json.dumps(payload)


def _messages(payload: dict) -> str:
    return " ".join(str(d.get("message", ""))
                    for d in payload.get("diagnostics") or [])


# =========================================================================== #
# 1. The issue's reproducer
# =========================================================================== #

def test_revl_check_and_revl_admit_agree_on_a_candidate_with_modules(running):
    """The headline: the two verbs disagreed on the same input, and the one
    that decides (admit) was the one that lost the argument."""
    checked = _call("revl_check", {"source": CANDIDATE, "modules": MODULES})
    admitted = _call("revl_admit", {"source": CANDIDATE, "modules": MODULES,
                                    "manifest": running,
                                    "replacing": ["Old"]})
    assert checked["ok"] is True
    assert admitted["ok"] is True, _messages(admitted)
    assert admitted["admitted"] is True
    assert admitted["loadOrder"] == ["T"]


def test_the_composition_compile_carries_the_agents_modules_under_providers():
    """Drop 2. With providers configured the door compiles twice; the second
    compile, the one that loads, must resolve the agent's `use` too."""
    _set_trust(providers=PROVIDERS)
    ir = server.compile_under_authoring(CANDIDATE, None, modules=MODULES)
    names = [c["name"] for c in ir["manifest"]["components"]]
    assert "T" in names and "OpsProvider" in names, names


def test_the_door_still_refuses_a_candidate_whose_module_is_not_carried():
    """Non-vacuity of the row above: the same candidate with NO modules is
    refused on the `use`, so a passing compile really did resolve it."""
    _set_trust(providers=PROVIDERS)
    with pytest.raises(RevlError) as refused:
        server.compile_under_authoring(CANDIDATE, None, modules=None)
    assert "cannot find imported module" in str(refused.value)


def test_a_jailed_file_may_use_an_in_memory_module(tmp_path):
    """The `files` branch of the door: a candidate on disk whose `use` names a
    module the call carries in memory resolves, with and without providers."""
    path = tmp_path / "cand.rvl"
    path.write_text(CANDIDATE, encoding="utf-8")
    _set_trust(roots=(str(tmp_path),))
    alone = _call("revl_check", {"files": [str(path)], "modules": MODULES})
    assert alone["ok"] is True, _messages(alone)
    _set_trust(providers=PROVIDERS, roots=(str(tmp_path),))
    composed = _call("revl_check", {"files": [str(path)], "modules": MODULES})
    assert composed["ok"] is True, _messages(composed)
    assert composed["loadOrder"] == ["T", "OpsProvider"]


def test_an_agent_module_cannot_displace_a_provider():
    """The merge order is providers FIRST. An agent-supplied entry at a
    provider's path is ignored, in both compiles."""
    _set_trust(providers=PROVIDERS)
    hijack = {"prov.rvl": DECLS + "component OpsProvider provides tool: Tool {\n"
                                  '  provide tool { fn describe() = "mine" }\n'
                                  "}\n"}
    ir = server.compile_under_authoring(
        DECLS + "component T provides tool: Tool {\n"
                '  provide tool { fn describe() = "t" }\n}\n',
        None, modules=hijack)
    by_name = {c["name"]: c for c in ir["manifest"]["components"]}
    assert by_name["OpsProvider"]["provides"] == ["ops"], by_name


# =========================================================================== #
# 2. The security control: `modules` is not a way past the profile
# =========================================================================== #

@pytest.mark.parametrize("providers", [None, PROVIDERS],
                         ids=["one-compile", "two-compile"])
@pytest.mark.parametrize("module, code, sign", [
    (REALM_MODULE, "G9", "forbids naming a realm"),
    (EXTERN_MODULE, "G8", "forbids new `extern`/host-block declarations"),
], ids=["realm-placement", "raw-extern"])
def test_a_module_is_refused_through_revl_admit_as_inline_source_is(
        running, providers, module, code, sign):
    """A module arrives over the transport too, whatever path it claims. Now
    that `revl_admit` carries it, it must carry it INTO the profile: the
    refusal an inline candidate gets is the refusal its module gets, on the
    one-compile and the two-compile path alike."""
    _set_trust(providers=providers)
    payload = _call("revl_admit", {"source": CANDIDATE,
                                   "modules": {"lib.rvl": module},
                                   "manifest": running,
                                   "replacing": ["Old"]})
    assert payload["ok"] is False
    assert payload.get("admitted") is not True
    diagnostics = payload.get("diagnostics") or []
    assert any(d.get("code") == code for d in diagnostics), diagnostics
    assert sign in _messages(payload)
    # and it was the PROFILE that refused, not a `use` that failed to resolve
    assert not any(s in _text(payload) for s in DROP_SIGNS)


def test_the_extern_refusal_is_the_profile_not_the_parser(running):
    """Non-vacuity of the raw-extern row: a trusted author admits the same
    module, so the refusal above is authoring trust, not a malformed extern."""
    _set_trust(host_code=True)
    payload = _call("revl_admit", {"source": CANDIDATE,
                                   "modules": {"lib.rvl": EXTERN_MODULE},
                                   "manifest": running,
                                   "replacing": ["Old"]})
    assert payload["ok"] is True, _messages(payload)
    assert payload["admitted"] is True


# =========================================================================== #
# 3. The verb table: every verb that accepts `modules` carries it
# =========================================================================== #

def _ok(payload: dict) -> bool:
    return payload.get("ok") is True


def _verdict_not_refused(payload: dict) -> bool:
    """The grading verbs: admission proved, whatever the battery then found
    (the scratch boot may need cordis; a wasm trap is a verdict of its own)."""
    admission = (payload.get("proved") or {}).get("admission") \
        or ((payload.get("gauntlet") or {}).get("proved") or {}).get("admission") \
        or {}
    return payload.get("verdict") != "rejected" \
        and admission.get("status") != "refused"


def _repair_planned(payload: dict) -> bool:
    return (payload.get("incident") or {}).get("status") == "planned"


def _repair_arguments(running: dict) -> dict:
    return {
        "component": "Old",
        "candidate": {"source": CANDIDATE, "modules": MODULES},
        "selfRepairPolicy": {"eligible": [{"component": "*"}],
                             "mayTouch": ["*"], "ackOnWiden": False},
        "trace": [{"channel": "fault", "subject": "Old", "detail": "boom"}],
        "apply": False,
    }


# Every advertised verb whose input schema accepts `modules` (top-level, or
# inside a `candidate`), with the arguments that drive it to its compile and
# the predicate a carried-through module satisfies. `None` for the arguments
# means the verb needs a booted composition (cordis); its row is checked by
# `test_the_booting_verbs_carry_modules` instead.
#
# The builder takes the running composition's IR. Keep the candidate itself
# out of here: `test_every_modules_verb_carries_them` adds the SAME
# `source` + `modules` to every row, so a row cannot quietly test something
# else.
MODULES_VERBS = {
    "revl_check": (lambda ir: {}, _ok),
    "revl_admit": (lambda ir: {"manifest": ir, "replacing": ["Old"]}, _ok),
    "revl_plan": (lambda ir: {"manifest": ir, "replacing": ["Old"]},
                  lambda p: _ok(p) and p.get("admissible") is True),
    "revl_ship": (lambda ir: {"manifest": ir, "replacing": ["Old"],
                              "apply": False},
                  lambda p: _ok(p) and p.get("stoppedAt") is None),
    "revl_audit": (lambda ir: {}, _ok),
    "revl_tools": (lambda ir: {}, _ok),
    "revl_gauntlet": (lambda ir: {}, _verdict_not_refused),
    "revl_quarantine": (lambda ir: {}, _verdict_not_refused),
    "revl_query_emitters": (lambda ir: {"target": "Tool"}, _ok),
    "revl_query_withdraw": (lambda ir: {"component": "T"}, _ok),
    "revl_query_dependents": (lambda ir: {"target": "tool"}, _ok),
    "revl_query_reach": (lambda ir: {"component": "T"}, _ok),
    "revl_query_drift": (lambda ir: {"service": "Tool"}, _ok),
    # the candidate rides inside `candidate`, not at the top level
    "revl_repair": (_repair_arguments, _repair_planned),
    # need a booted composition
    "revl_load": None,
    "revl_swap": None,
}

# rows whose candidate is nested: where the SAME source + modules go
_NESTED = {"revl_repair": "candidate"}


def _accepts_modules(tool: dict) -> bool:
    """Top-level `modules`, or a `candidate` object whose contract names it
    (`revl_repair` documents its candidate as `{source|files|modules}` in the
    description rather than as sub-properties)."""
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    if "modules" in props:
        return True
    candidate = props.get("candidate") or {}
    if candidate.get("type") != "object":
        return False
    return "modules" in (candidate.get("properties") or {}) \
        or "modules" in str(candidate.get("description") or "")


def test_every_verb_that_accepts_modules_is_in_the_table():
    """The completeness guard, in the shape of
    `test_every_advertised_tool_is_gated_or_recorded_as_ungated`: a verb that
    advertises `modules` is in `MODULES_VERBS`, or this fails. The table is
    checked against the schema, not trusted."""
    advertised = {t["name"] for t in server.TOOLS if _accepts_modules(t)}
    assert not (advertised - set(MODULES_VERBS)), (
        "these MCP verbs accept `modules` and are not checked to carry them: "
        + ", ".join(sorted(advertised - set(MODULES_VERBS)))
        + ". Add a row to MODULES_VERBS driving the verb with a candidate that "
          "`use`s a module only the call carries.")
    assert not (set(MODULES_VERBS) - advertised), (
        "these rows name a verb that no longer advertises `modules`: "
        + ", ".join(sorted(set(MODULES_VERBS) - advertised)))


@pytest.mark.parametrize("verb", [v for v, row in MODULES_VERBS.items()
                                  if row is not None])
def test_every_modules_verb_carries_them(verb, running):
    """Each verb, driven with a candidate whose only `use` is satisfied by the
    call's `modules`. A drop shows up as one of two texts; neither may."""
    build, satisfied = MODULES_VERBS[verb]
    arguments = build(running)
    slot = _NESTED.get(verb)
    if slot is None:
        arguments = {**arguments, "source": CANDIDATE, "modules": MODULES}
    else:
        arguments[slot] = {"source": CANDIDATE, "modules": MODULES}
    payload = _call(verb, arguments)
    text = _text(payload)
    assert not any(s in text for s in DROP_SIGNS), \
        f"{verb} dropped the candidate's `modules`: {_messages(payload)}"
    assert satisfied(payload), f"{verb}: {text[:600]}"


@needs_cordis
def test_the_booting_verbs_carry_modules():
    """`revl_load` and `revl_swap` carried `modules` before this fix; they are
    in the table so the table is complete, and checked here on a booted
    composition so a regression in either is caught rather than assumed."""
    loaded = _call("revl_load", {"source": RUNNING})
    assert loaded["ok"] is True, _messages(loaded)
    swapped = _call("revl_swap", {"source": CANDIDATE, "modules": MODULES,
                                  "replacing": ["Old"]})
    assert swapped["ok"] is True, _messages(swapped)
    assert swapped.get("swapped") is True
    assert not any(s in _text(swapped) for s in DROP_SIGNS)

    from revl.mcp.session import Session
    server.SESSION = Session()
    booted = _call("revl_load", {"source": CANDIDATE, "modules": MODULES})
    assert booted["ok"] is True, _messages(booted)
    assert not any(s in _text(booted) for s in DROP_SIGNS)


# =========================================================================== #
# 4. The shape that keeps it positional
# =========================================================================== #

def test_compile_has_no_slot_to_drop_modules_into():
    """`server._compile` takes `modules` positionally with no default. The
    keyword-with-default it used to be is how three verbs dropped it: a call
    that names `source` and `files` and nothing else must not compile."""
    import inspect

    signature = inspect.signature(server._compile)
    modules = signature.parameters["modules"]
    assert modules.default is inspect.Parameter.empty
    assert modules.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    with pytest.raises(TypeError):
        server._compile(CANDIDATE, None)  # type: ignore[call-arg]


def test_every_compile_call_in_the_server_passes_the_whole_candidate():
    """Read out of the source, in the shape of the door guard: every
    `_compile(` call in `server.py` either spreads `_candidate_of(...)` or
    passes three positional arguments. There is no third way to call it."""
    import ast

    tree = ast.parse((ROOT / "src/revl/mcp/server.py").read_text("utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_compile"):
            continue
        spreads_candidate = any(
            isinstance(a, ast.Starred) and isinstance(a.value, ast.Call)
            and isinstance(a.value.func, ast.Name)
            and a.value.func.id == "_candidate_of"
            for a in node.args)
        if spreads_candidate or len(node.args) == 3:
            continue
        offenders.append(f"_compile:{node.lineno}")
    assert not offenders, (
        "these `_compile(` calls hand-pick the candidate's arguments instead "
        "of passing the whole triple: " + ", ".join(offenders))
