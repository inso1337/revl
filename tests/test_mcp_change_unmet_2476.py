"""Issue #2476: a COMMITTED `revl_change` names an unmet requirement too.

#2454 made the speculative (proposal) half report `unmetRequirements`: the
requirement nothing provides, which would leave the component PENDING and the
commit refused. The commit return did not carry it, so the very answer that
says "committed" stayed silent about the one thing the caller now needs to
know — that a component of the composition it just installed did not come up
with everything it requires.

Both halves come from the same helper, `unmet_requirements(ir)`, over the same
compiled IR `ir` the commit swaps in, so they report the same records with the
same keys.
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

UNMET = [{"component": "AuditLog", "key": "Audit", "service": "Audit",
          "fix": FIX}]


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


def _committed(added: str) -> dict:
    """`revl_change` with `commit: true` over a composition whose compiled IR
    has an unmet requirement, without reaching the runtime: the swap is stood
    in for, since what is tested here is the shape of the answer the verb
    builds on top of `edit.admit`'s return — the same swap the real session
    would have to have survived for the answer to exist at all."""
    session = Session()
    session.ir = edit_mod.compile_virtual({"source": KV, "modules": {}})
    session.origin = {"source": KV}
    session._driver = object()  # `loaded`; nothing here reaches the runtime

    def _swap(ir, origin=None, migrate="generational", *, agent_authored):
        session.ir = ir
        return {"swapped": True, "generation": 1}

    session.swap = _swap
    result = edit_mod.admit(session, {"source": KV + added, "modules": {}},
                            edit_mod.running_source(session), [], (),
                            commit=True)
    return change_mod.shape("add", result, None, [], None)


def test_a_committed_change_names_the_requirement_nothing_provides_offline():
    # Before this change the commit return carried no `unmetRequirements`, so
    # `shape` built its guarantees from `unmet=None` and `self_check` omitted
    # the key entirely: the assert below saw a `KeyError`. Afterwards the
    # committed answer carries the record, exactly as the proposal does.
    gone = _committed(AUDIT)
    assert gone["ok"] is True and gone["committed"] is True, gone
    assert gone["verified"]["admission"] == "passed", gone
    guarantees = gone["verified"]["guarantees"]
    # a warning, not a verdict: the key may be met outside the composition
    assert guarantees["admissible"] is True, guarantees
    assert guarantees["unmetRequirements"] == UNMET, guarantees
    assert "would stay PENDING" in guarantees["note"]
    # reported once, inside the guarantees
    assert "unmetRequirements" not in gone


def test_the_committed_and_proposed_shapes_carry_the_same_records():
    # the point of #2476: a caller reads one shape either way
    session = Session()
    session.ir = edit_mod.compile_virtual({"source": KV, "modules": {}})
    session.origin = {"source": KV}
    session._driver = object()
    session.swap = lambda ir, *a, **k: {"swapped": True, "generation": 1}
    proposed = edit_mod.admit(session, {"source": KV + AUDIT, "modules": {}},
                              edit_mod.running_source(session), [], (),
                              commit=False)
    proposed.pop("_proposal")
    proposed = change_mod.shape("add", proposed, None, [], None)
    assert proposed["verified"]["guarantees"]["unmetRequirements"] == UNMET
    assert _committed(AUDIT)["verified"]["guarantees"]["unmetRequirements"] \
        == proposed["verified"]["guarantees"]["unmetRequirements"]


def test_a_committed_change_with_every_requirement_met_lists_none_offline():
    gone = _committed(AUDIT.replace("requires Audit: Audit",
                                    "requires kv: Kv"))
    assert gone["committed"] is True, gone
    guarantees = gone["verified"]["guarantees"]
    assert guarantees["admissible"] is True, guarantees
    assert "unmetRequirements" not in guarantees


@needs_runtime
def test_revl_change_committed_names_the_requirement_nothing_provides():
    # An unmet requirement on a component that was NOT already PENDING is
    # refused by the item-334 health gate, so this call is expected to come
    # back refused rather than committed — and it must name the key, which is
    # what a caller whose component did not come up has to act on.
    assert _call("revl_load", {"source": KV})["ok"] is True
    gone = _call("revl_change", {"add": {"source": AUDIT}, "commit": True})
    assert gone["committed"] is False, gone
    joined = str(gone)
    assert "requires `Audit: Audit`" in joined, gone
    assert FIX in joined, gone
