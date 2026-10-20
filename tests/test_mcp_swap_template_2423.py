"""Issue #2423: `revl_swap` over MCP on a composition that spawns a template.

A `spawn` target is a template: it is one of the running IR's components but
not one of the linked ROOT manifest's, so `query.withdrawal` answers `unknown
component` and carries no `breaks`. The authoring loop's blast radius indexed
that key unconditionally, so a swap that touched the template raised
`KeyError: 'breaks'` and the caller got an internal error instead of the swap
result.

The fix branches on the withdrawal answer. A name the manifest lists as a
template gets an explicit template entry: an empty cascade and zero breaks,
because its provisions come up per instance, with a precision and note saying
the static composition excludes it. Any other unknown component refuses
instead of reporting zeros, so an inconsistent radius cannot hide a real
withdrawal set. That refusal is a named answer at the MCP boundary, never an
internal fault.

The trust boundary is the other half. An agent-authored successor may not
inherit the running operator template's live instance state, whatever the
migration policy: a default (generational) swap refuses with
STATE_UNDISCLOSED and gen N keeps serving. Authorship is what the compile
reports about the text's origin — inline source, in-memory modules, an edited
buffer, or a file in an export area — never the authoring-trust config, so a
trusted host-code author is still the agent. Only a template the successor
CHANGES trips the gate; one it re-declares unchanged keeps migrating, and an
operator swap still migrates.

The composition and the swap are the legitimate template path of item 334,
driven through the MCP verb the issue names.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import authoring_loop  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

CLEAN = ("service S { fn f() -> Int }\n"
         "component C provides s: S { provide s { fn f() = 1 } }\n")

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")


def _instance_source(version: int) -> str:
    """`Worker` is a template (a `spawn` target) providing `store`; `store`
    lives per instance and never at ROOT. Stateless, so a session-level
    disclosure refusal here is about the instance itself, not its state."""
    return f"""
service Store {{ fn version() -> Int }}
service Admin {{ fn ver() -> Int }}

component Worker provides store: Store {{
  provide store {{ fn version() = {version} }}
}}

component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{ fn ver() = w.store.version() }}
}}
"""


def _stateful_instance_source(version: int) -> str:
    """The item-334 live-instance fixture (tests/test_self_extending_334.py,
    `_instances_source`): `Worker` holds a `Map`, `Supervisor` spawns one and
    exposes seed/read/ver through the spawn handle, so a live `ver()` proves
    the successor's code and a surviving read proves its instance state."""
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
service Admin {{
  fn seed(k: Str, v: Str)
  fn read(k: Str) -> Opt[Str]
  fn ver() -> Int
}}
component Worker provides store: Store {{
  let m = effect Map.new() undo m.drop()
  provide store {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{
    fn seed(k, v) = w.store.put(k, v)
    fn read(k) = w.store.get(k)
    fn ver() = w.store.version()
  }}
}}
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session():
    """The MCP surface shares one module-level session, undo stack and authoring
    trust. Start each test from the trust it found and put it back, so a trusted
    host-code control cannot leak into a refusal test."""
    from revl.mcp import server as server_mod

    saved = server_mod.AUTHORING
    server_mod.UNDO_STACK.clear()
    try:
        yield server_mod
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.AUTHORING = saved
        server_mod.UNDO_STACK.clear()


@needs_runtime
def test_an_untrusted_template_swap_without_a_live_instance_answers(
        _fresh_server_session):
    """Load the template composition, then swap the generation that changes the
    template's body. That swap's preflight looks `Worker` up in the linked ROOT
    graph and finds no entry, which is the shape that used to raise.

    The only live instance is quiesced first: the disclosure gate refuses a
    live instance an agent-authored successor would inherit, and this control
    covers the template answer when there is nothing to inherit."""
    server = _fresh_server_session
    loaded = _call("revl_load", {"source": _instance_source(1)})
    assert loaded["ok"] is True, loaded
    assert loaded["providedKeys"] == ["admin"]  # `store` is per instance

    for instance in list(server.SESSION._driver.runtime.live_instances("Worker")):
        pending = instance.dispose()
        if pending is not None:
            server.SESSION._run(pending)
    assert server.SESSION._driver.runtime.live_instances("Worker") == []

    swapped = _call("revl_swap", {"source": _instance_source(2)})

    # A swap result, not an internal error: the template is in the blast
    # radius as an explicit template answer, not a fabricated exact one, and
    # the successor really is live.
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    entry = swapped["blastRadius"]["components"]["Worker"]
    assert entry["template"] is True
    assert entry["precision"] == "template"
    assert "excluded from the static composition" in entry["precisionNote"]
    assert "per instance" in entry["precisionNote"]
    assert entry["breaks"] == 0
    assert entry["cascade"] == []
    assert swapped["blastRadius"]["breaks"] == 0
    # The response note must not claim an exact answer for a component the
    # static graph does not hold.
    assert "exact, from the running composition" not in swapped["blastRadius"]["note"]
    assert "spawn template" in swapped["blastRadius"]["note"]
    called = _call("revl_call", {"key": "admin", "method": "ver"})
    assert called["result"] == 2, called


@needs_runtime
def test_a_refused_blast_radius_is_a_named_refusal_not_an_internal_fault(
        _fresh_server_session):
    """The refusal `blast_radius` raises for a non-template unknown component
    must cross the MCP boundary as a composition verdict: category, outcome,
    the component, the withdrawal query's error and the names it does know.
    Before the boundary catch it fell into the generic handler and read as
    `internal: BlastRadiusRefused: ...`, indistinguishable from a server
    fault."""
    server = _fresh_server_session
    loaded = _call("revl_load", {"source": CLEAN})
    assert loaded["ok"] is True, loaded

    # The only way this refusal arises from the tool: the running IR holds a
    # component the linked manifest neither lists nor names as a template.
    server.SESSION.ir["components"].append({"name": "Ghost", "provides": {}})

    refused = _call("revl_swap", {"source": CLEAN})

    assert refused["ok"] is False, refused
    diagnostic = refused["diagnostics"][0]
    assert diagnostic["category"] == "blast-radius"
    assert refused["outcome"] == "refused"
    assert refused["component"] == "Ghost"
    assert refused["withdrawalError"] == "unknown component: 'Ghost'"
    assert "C" in refused["known"]
    assert "Ghost" not in refused["known"]
    # gen N still serving: the refusal happened before any swap.
    assert _call("revl_call", {"key": "s", "method": "f"})["result"] == 1


@needs_runtime
def test_an_agent_swap_does_not_inherit_operator_instance_state(
        tmp_path, _fresh_server_session):
    """The operator composition is loaded from a jailed file outside every
    export area, so gen N and the seeded instance state are the operator's.
    The successor arrives inline, which is agent text; a default (generational)
    swap must refuse it with STATE_UNDISCLOSED before teardown, leaving gen N
    serving both its code and its state."""
    server = _fresh_server_session
    operator_file = tmp_path / "instances.rvl"
    operator_file.write_text(_stateful_instance_source(1))
    server.set_authoring_trust(host_code=False, roots=[str(tmp_path)])

    loaded = _call("revl_load", {"files": [str(operator_file)]})
    assert loaded["ok"] is True, loaded
    assert _call("revl_call", {"key": "admin", "method": "seed",
                               "args": ["alice", "42"]})["ok"] is True
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"

    swapped = _call("revl_swap", {"source": _stateful_instance_source(2)})

    assert swapped["ok"] is False, swapped
    assert swapped["code"] == "STATE_UNDISCLOSED"
    assert swapped["diagnostics"][0]["category"] != "internal"
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


@needs_runtime
def test_a_trusted_host_code_agent_swap_refuses_to_inherit_state(
        _fresh_server_session):
    """`--author-trust trusted` is a statement about HOST CODE: the operator
    vouches for the agent's externs. It does not make the agent the operator,
    so inline source is still agent-authored text and a default swap must run
    the item-334 disclosure gate. Before the fix authorship was read off
    `AUTHORING.profile()`, which is none under this trust, so the gate was
    skipped and the seeded operator state rode onto the successor."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True)
    assert server.AUTHORING.profile() is None

    loaded = _call("revl_load", {"source": _stateful_instance_source(1)})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"

    swapped = _call("revl_swap", {"source": _stateful_instance_source(2)})

    assert swapped["ok"] is False, swapped
    assert swapped["code"] == "STATE_UNDISCLOSED"
    assert swapped["diagnostics"][0]["category"] != "internal"
    assert swapped.get("swapped") is not True
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


@needs_runtime
def test_a_trusted_host_code_repair_refuses_to_inherit_state(
        _fresh_server_session):
    """`revl_repair` swaps a candidate too, and its authorship was read off the
    trust config as well. Under trusted host code a transport repair of the
    live template migrated the seeded state with no gate; the compile report
    says it is agent text, so the repair is rejected and gen N keeps serving."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True)
    assert server.AUTHORING.profile() is None

    loaded = _call("revl_load", {"source": _stateful_instance_source(1)})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    repaired = _call("revl_repair", {
        "component": "Worker",
        "candidate": {"source": _stateful_instance_source(2)},
        "selfRepairPolicy": {"eligible": [{"component": "*"}],
                             "mayTouch": ["*"], "ackOnWiden": False}})

    assert repaired["incident"]["status"] == "rejected", repaired["incident"]
    assert repaired["remediation"]["applied"] is False
    assert repaired["remediation"]["code"] == "STATE_UNDISCLOSED"
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


@needs_runtime
def test_a_trusted_host_code_edit_refuses_to_inherit_state(
        _fresh_server_session):
    """The edit door is the same authoring door: a committed patch of the
    running template is transport text whatever the trust says. It used to read
    `AUTHORING.profile()` too, so under trusted host code a `revl_edit` commit
    re-seated the live instance state with no gate."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True)
    assert server.AUTHORING.profile() is None

    loaded = _call("revl_load", {"source": _stateful_instance_source(1)})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    edited = _call("revl_edit", {"edits": [
        {"anchor": "fn version() = 1", "replacement": "fn version() = 2"}]})

    assert edited["ok"] is False, edited
    assert edited["code"] == "STATE_UNDISCLOSED"
    assert edited.get("swapped") is not True
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


def test_a_non_template_unknown_component_refuses_instead_of_zeroing():
    """Only a named template may take the empty shape. Any other component the
    withdrawal query does not know is an inconsistent radius: it must name the
    refusal, never return a zero that would read as an empty cascade."""
    before = compile_source(CLEAN, "clean.rvl")
    after = compile_source(CLEAN, "clean.rvl")
    # A component of the running IR that the linked manifest does not hold and
    # does not name as a template.
    before["components"].append({"name": "Ghost", "provides": {}})
    after["components"].append({"name": "Ghost", "provides": {"ghost": "S"}})

    with pytest.raises(authoring_loop.BlastRadiusRefused) as raised:
        authoring_loop.blast_radius(before, after)

    assert raised.value.component == "Ghost"
    assert raised.value.answer["ok"] is False
    assert "unknown component" in str(raised.value)


def _template_module(version: int) -> str:
    """The version-bearing half as a `use`-reached module: its text is the text
    the compile reads for it, so editing it changes the successor without
    touching a listed file."""
    return f"pub fn template_version() -> Int {{ return {version} }}\n"


def _operator_using_template_module(module: str) -> str:
    """The stateful template composition, with `Worker`'s version read from the
    module at `module`. The only listed file is the operator's own, outside
    every export area."""
    return """
use "__MODULE__" { template_version }

service Store {
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}
service Admin {
  fn seed(k: Str, v: Str)
  fn read(k: Str) -> Opt[Str]
  fn ver() -> Int
}
component Worker provides store: Store {
  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(k) = m.get(k)
    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }
    fn version() = template_version()
  }
}
component Supervisor provides admin: Admin {
  let w = effect spawn Worker undo w.dispose()
  provide admin {
    fn seed(k, v) = w.store.put(k, v)
    fn read(k) = w.store.get(k)
    fn ver() = w.store.version()
  }
}
""".replace("__MODULE__", module)


@needs_runtime
def test_a_files_candidate_reaching_a_changed_export_file_refuses_state(
        tmp_path, _fresh_server_session):
    """B5: a files-only candidate outside every export area that `use`-reaches
    a file inside one still carries the agent's text there, so the server-side
    swap must run the disclosure gate. Before, authorship looked only at the
    files the working set lists, so the reached file's changed text was
    installed, live instance state migrated (`migrated: true`) and the
    successor read alice=42 without the gate ever running."""
    server = _fresh_server_session
    exported = tmp_path / ".revl" / "export" / "extra.rvl"
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_text(_template_module(1), encoding="utf-8")
    operator_file = tmp_path / "instances.rvl"
    operator_file.write_text(
        _operator_using_template_module(".revl/export/extra.rvl"),
        encoding="utf-8")
    server.set_authoring_trust(host_code=False, roots=[str(tmp_path)])

    loaded = _call("revl_load", {"files": [str(operator_file)]})
    assert loaded["ok"] is True, loaded
    assert _call("revl_call", {"key": "admin", "method": "seed",
                               "args": ["alice", "42"]})["ok"] is True
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"

    # The agent's text changes in the export area, not in any listed file.
    exported.write_text(_template_module(2), encoding="utf-8")

    swapped = _call("revl_swap", {})

    assert swapped["ok"] is False, swapped
    assert swapped["code"] == "STATE_UNDISCLOSED"
    assert swapped.get("swapped") is not True
    assert swapped["diagnostics"][0]["category"] != "internal"
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


@needs_runtime
def test_a_files_only_operator_swap_reaching_no_export_text_still_migrates(
        tmp_path, _fresh_server_session):
    """The boundary the B5 fix must not move: a files-only working set whose
    compile reaches a module OUTSIDE every export area carries no agent text, so
    the server-side swap keeps generational migration and the gate does not run,
    even when that reached module's disk text changed."""
    server = _fresh_server_session
    helper = tmp_path / "helper.rvl"
    helper.write_text(_template_module(1), encoding="utf-8")
    operator_file = tmp_path / "instances.rvl"
    operator_file.write_text(_operator_using_template_module("helper.rvl"),
                             encoding="utf-8")
    server.set_authoring_trust(host_code=False, roots=[str(tmp_path)])

    loaded = _call("revl_load", {"files": [str(operator_file)]})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    helper.write_text(_template_module(2), encoding="utf-8")

    swapped = _call("revl_swap", {})

    assert swapped["ok"] is True, swapped
    assert swapped["swapped"] is True
    assert swapped["migration"]["templates"]["Worker"]["migrated"] is True
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 2
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


@needs_runtime
def test_a_trusted_author_reaching_an_export_file_still_refuses_state(
        tmp_path, _fresh_server_session):
    """Export-area text is the agent's by its path, and the trust config does
    not change that. `revl_export` is the only writer under `.revl/export/`, so
    a changed module there is transport text even when no profile is applied.
    The server-side swap must run the disclosure gate and refuse; before the
    fix it treated the reached file as the operator's and migrated."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True, roots=[str(tmp_path)])
    assert server.AUTHORING.profile() is None

    exported = tmp_path / ".revl" / "export" / "extra.rvl"
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_text(_template_module(1), encoding="utf-8")
    operator_file = tmp_path / "instances.rvl"
    operator_file.write_text(
        _operator_using_template_module(".revl/export/extra.rvl"),
        encoding="utf-8")

    loaded = _call("revl_load", {"files": [str(operator_file)]})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    exported.write_text(_template_module(2), encoding="utf-8")

    swapped = _call("revl_swap", {})

    assert swapped["ok"] is False, swapped
    assert swapped["code"] == "STATE_UNDISCLOSED"
    assert swapped.get("swapped") is not True
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


def _stateful_instance_source_with_extra() -> str:
    """`_stateful_instance_source(1)` plus an unrelated component. `Worker`,
    `Supervisor` and `Admin` are byte for byte the same, so a successor built
    from this text changes something other than the live template."""
    return _stateful_instance_source(1) + """
service Ping { fn ping() -> Int }
component Echo provides ping: Ping {
  provide ping { fn ping() = 9 }
}
"""


def _provider_call_source(tag: int) -> str:
    """A stateful template that calls a root provider: `Worker` keeps its live
    `Map` and its `version()` reads the `Tag` provider, so changing only the
    provider changes the code the instance's state is re-seated with, while
    `Worker` itself stays byte for byte the same."""
    return f"""
service Tag {{ fn tag() -> Int }}
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
service Admin {{
  fn seed(k: Str, v: Str)
  fn read(k: Str) -> Opt[Str]
  fn ver() -> Int
}}
component Backend provides tag: Tag {{
  provide tag {{ fn tag() = {tag} }}
}}
component Worker provides store: Store requires tag: Tag {{
  let m = effect Map.new() undo m.drop()
  provide store {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = tag.tag()
  }}
}}
component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{
    fn seed(k, v) = w.store.put(k, v)
    fn read(k) = w.store.get(k)
    fn ver() = w.store.version()
  }}
}}
"""


@needs_runtime
def test_a_provider_only_change_still_refuses_the_live_template(
        _fresh_server_session):
    """A template's own text can be byte-identical while the provider it calls
    changes. The live state still lands on code that can observe it through
    that call, so the template's identity includes the components that provide
    the services it requires; checking only its own entry would migrate the
    seeded state onto the changed provider."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True)

    loaded = _call("revl_load", {"source": _provider_call_source(1)})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1

    swapped = _call("revl_swap", {"source": _provider_call_source(2)})

    assert swapped["ok"] is False, swapped
    assert swapped["code"] == "STATE_UNDISCLOSED"
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


@needs_runtime
def test_an_unrelated_edit_lands_with_the_live_template_untouched(
        tmp_path, _fresh_server_session):
    """The operator loads the stateful template composition from a jailed file
    outside every export area and seeds a live instance. The agent then swaps a
    successor that adds an unrelated component and re-declares `Worker`
    unchanged. The disclosure gate runs (inline source is agent text) but the
    template is not touched, so the edit lands and generational migration still
    carries the operator's instance state. Before the fix EVERY re-declared
    live template was checked, so this unrelated edit was refused."""
    server = _fresh_server_session
    operator_file = tmp_path / "instances.rvl"
    operator_file.write_text(_stateful_instance_source(1), encoding="utf-8")
    server.set_authoring_trust(host_code=False, roots=[str(tmp_path)])

    loaded = _call("revl_load", {"files": [str(operator_file)]})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    swapped = _call("revl_swap", {"source": _stateful_instance_source_with_extra()})

    assert swapped["ok"] is True, swapped
    assert swapped["swapped"] is True
    assert swapped["migration"]["templates"]["Worker"]["migrated"] is True
    assert "ping" in swapped["providedKeys"]
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"
    assert _call("revl_call", {"key": "ping", "method": "ping"})["result"] == 9


@needs_runtime
def test_a_refused_trusted_author_swap_lands_after_the_instances_quiesce(
        _fresh_server_session):
    """The trusted-author refusal is about the live instance, not a permanent
    hold on the successor: quiesce the instances it would have inherited and
    the same agent-authored swap lands, cold."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True)

    loaded = _call("revl_load", {"source": _stateful_instance_source(1)})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    refused = _call("revl_swap", {"source": _stateful_instance_source(2)})
    assert refused["ok"] is False, refused
    assert refused["code"] == "STATE_UNDISCLOSED"

    for instance in list(server.SESSION._driver.runtime.live_instances("Worker")):
        pending = instance.dispose()
        if pending is not None:
            server.SESSION._run(pending)
    assert server.SESSION._driver.runtime.live_instances("Worker") == []

    swapped = _call("revl_swap", {"source": _stateful_instance_source(2)})

    assert swapped["ok"] is True, swapped
    assert swapped["swapped"] is True
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 2
    # quiesced, so there is no instance state to inherit
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] is None


@needs_runtime
def test_a_rollback_and_an_undo_cold_start_with_no_migration(
        _fresh_server_session):
    """Rollback and undo restore the predecessor's CODE only: both are cold
    starts for the instance layer, so migrated state does not ride back (F4).
    The guard stays in place now that agent-authored swaps can land on an
    untouched template with migration."""
    server = _fresh_server_session
    server.set_authoring_trust(host_code=True)

    loaded = _call("revl_load", {"source": _stateful_instance_source(1)})
    assert loaded["ok"] is True, loaded
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})

    swapped = _call("revl_swap",
                    {"source": _stateful_instance_source_with_extra()})
    assert swapped["ok"] is True, swapped
    assert swapped["migration"]["templates"]["Worker"]["migrated"] is True
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"

    rolled = _call("revl_rollback", {})

    assert rolled["ok"] is True, rolled
    assert rolled.get("migration") is None
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] is None

    # `revl_undo` replays an earlier generation through the same respawn swap.
    again = _call("revl_swap", {"source": _stateful_instance_source_with_extra()})
    assert again["ok"] is True, again
    _call("revl_call", {"key": "admin", "method": "seed",
                        "args": ["alice", "42"]})
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"

    undone = _call("revl_undo", {})

    assert undone["ok"] is True and undone["undone"] is True, undone
    assert undone.get("migration") is None
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] is None
