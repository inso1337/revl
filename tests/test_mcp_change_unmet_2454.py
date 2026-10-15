"""Issue #2454: a speculative `revl_change` names an unmet requirement.

`revl_check` lists a `requires` key nothing provides in `unmetRequirements`
(issue #2386). A `revl_change` that is proposed without `commit` verifies the
candidate with the same self-check, but computed it with no unmet list, so a
change that leaves a requirement without a provider read as a clean pass even
though committing it would be refused as PENDING.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import change as change_mod  # noqa: E402
from revl.mcp import edit as edit_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.session import Session  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

KV = ("service Kv { fn get(k: Str) -> Str }\n"
      "component Store provides kv: Kv {\n"
      "  provide kv { fn get(k) = k }\n"
      "}\n")
# the service name used as the requirement key, on the component that
# provides that service itself: nothing provides the key `Audit`
AUDIT = ("service Audit { fn record(msg: Str) -> Str\n fn count() -> Int }\n"
         "component AuditLog requires Audit: Audit provides audit: Audit {\n"
         "  provide audit {\n"
         "    fn record(msg) = msg\n"
         "    fn count() = 0\n"
         "  }\n"
         "}\n")
needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="revl_load boots the composition; install the runtime with "
           "`sh backends/python/setup.sh`")

FIX = ("`AuditLog` already provides `Audit` as `audit`, and a component does "
       "not require the service it provides, so drop `requires Audit`")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(ROOT), str(tmp_path)))
    server_mod.UNDO_STACK.clear()
    yield tmp_path
    _call("revl_change", {"discard": True})
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.UNDO_STACK.clear()
    server_mod.AUTHORING = old


def _proposed(added: str) -> dict:
    """What a speculative `revl_change` makes of adding `added` to `KV`,
    without booting anything: a session that holds the compiled composition,
    the same `edit.admit` the verb runs, and the verb's own `shape`."""
    session = Session()
    session.ir = edit_mod.compile_virtual({"source": KV, "modules": {}})
    session.origin = {"source": KV}
    session._driver = object()  # `loaded`; nothing here reaches the runtime
    result = edit_mod.admit(session, {"source": KV + added, "modules": {}},
                            edit_mod.running_source(session), [], (),
                            commit=False)
    result.pop("_proposal")
    return change_mod.shape("add", result, None, [], None)


def test_a_speculative_change_names_the_requirement_nothing_provides_offline():
    proposed = _proposed(AUDIT)
    assert proposed["ok"] is True and proposed["committed"] is False, proposed
    assert proposed["verified"]["admission"] == "passed", proposed
    guarantees = proposed["verified"]["guarantees"]
    assert guarantees["admissible"] is True, guarantees
    assert guarantees["unmetRequirements"] == [{
        "component": "AuditLog", "key": "Audit", "service": "Audit",
        "fix": FIX}], guarantees
    # reported once, inside the guarantees
    assert "unmetRequirements" not in proposed


def test_a_speculative_change_with_every_requirement_met_lists_none_offline():
    proposed = _proposed(AUDIT.replace("requires Audit: Audit",
                                       "requires kv: Kv"))
    guarantees = proposed["verified"]["guarantees"]
    assert guarantees["admissible"] is True, guarantees
    assert "unmetRequirements" not in guarantees


@needs_runtime
def test_revl_change_names_the_requirement_nothing_provides():
    assert _call("revl_load", {"source": KV})["ok"] is True
    proposed = _call("revl_change", {"add": {"source": AUDIT}})
    assert proposed["ok"] is True and proposed["committed"] is False, proposed
    assert proposed["verified"]["admission"] == "passed", proposed
    guarantees = proposed["verified"]["guarantees"]
    # a warning, not a verdict: the key may come from outside the composition
    assert guarantees["admissible"] is True, guarantees
    assert guarantees["unmetRequirements"] == [{
        "component": "AuditLog", "key": "Audit", "service": "Audit",
        "fix": FIX}], guarantees
    assert "would stay PENDING" in guarantees["note"]


@needs_runtime
def test_revl_change_with_every_requirement_met_lists_none():
    assert _call("revl_load", {"source": KV})["ok"] is True
    met = AUDIT.replace("requires Audit: Audit", "requires kv: Kv")
    proposed = _call("revl_change", {"add": {"source": met}})
    assert proposed["ok"] is True, proposed
    guarantees = proposed["verified"]["guarantees"]
    assert guarantees["admissible"] is True, guarantees
    assert "unmetRequirements" not in guarantees
