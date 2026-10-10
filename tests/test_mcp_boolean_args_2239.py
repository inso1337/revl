"""Boolean arguments sent as strings are checked at dispatch (issue #2239).

Every verb declares its boolean arguments as `{"type": "boolean"}`, and the
handlers read them as the singletons. Nothing checked the type before the
handler ran, so `revl_edit {commit: "false"}` committed the edit live (the
default-true read was `is not False`), and `revl_export {overwrite: "true"}`
was refused with "pass `overwrite: true`", the value the caller had passed.

The server now checks each call against the verb's own `inputSchema` once,
before any handler: `"true"`/`"false"` are read as their bools and the
response names the keys it read that way; any other non-bool is refused by
key, expected type and received type, and nothing runs.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime "
           "— install it with `sh backends/python/setup.sh`")

TENANTS = ROOT / "examples" / "tenants.rvl"

#: The issue's edit: remove both TenantA components.
REMOVE_A = {"edits": [{"symbol": "TenantAApp", "remove": True},
                      {"symbol": "TenantAStore", "remove": True}],
            "replacing": ["TenantAApp", "TenantAStore"]}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.proposals = {}
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def loaded(clean_session):
    path = clean_session / "tenants.rvl"
    path.write_text(TENANTS.read_text(encoding="utf-8"), encoding="utf-8")
    out = _call("revl_load", {"files": [str(path)]})
    assert out["ok"] is True, out
    return out


def _running(payload: dict):
    state = payload["sessionState"]
    return state["generation"], sorted(state["components"])


def _message(payload: dict) -> str:
    return " ".join(d.get("message", "") for d in payload.get("diagnostics") or [])


#: `revl_export {path}` writes only an inline composition, and inline source
#: under the untrusted authoring profile may not name a realm, which
#: tenants.rvl does; the export cases use a plain one.
INLINE = ("service S { fn f() -> Int }\n"
          "component C provides s: S { provide s { fn f() = 1 } }\n")

ALL_FOUR = sorted(["TenantAApp", "TenantAStore", "TenantBApp", "TenantBStore"])


# ------------------------------------------------ "false" stages, never swaps

def test_commit_string_false_does_not_swap(loaded):
    before = _running(loaded)
    out = _call("revl_edit", {**REMOVE_A, "commit": "false"})
    assert out.get("swapped") is not True, out
    assert _running(out) == before
    assert before == (1, ALL_FOUR)
    assert out["argumentsCanonicalised"]["keys"] == ["commit"]
    assert "commit" in out["argumentsCanonicalised"]["note"]


def test_commit_string_false_matches_real_false(loaded):
    real = _call("revl_edit", {**REMOVE_A, "commit": False})
    server_mod.SESSION.unload()
    server_mod.SESSION.proposals = {}
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    # fresh load, same edit as a string
    path = Path.cwd() / "tenants.rvl"
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    text = _call("revl_edit", {**REMOVE_A, "commit": "false"})
    for key in ("ok", "swapped", "speculative"):
        assert text.get(key) == real.get(key), (key, real, text)


# --------------------------------------------- a non-bool is refused by name

@pytest.mark.parametrize("value, kind", [(7, "int"), ("no", "the string 'no'"),
                                         (None, "null")])
def test_commit_non_boolean_is_refused(loaded, value, kind):
    before = _running(loaded)
    out = _call("revl_edit", {**REMOVE_A, "commit": value})
    assert out["ok"] is False, out
    message = _message(out)
    assert "`commit`" in message and "boolean" in message, message
    assert kind in message, message
    assert _running(out) == before


def test_nested_declared_boolean_is_checked():
    """`revl_verbs` hatch args reach the named verb's own schema."""
    out = _call("revl_verbs", {"name": "revl_export",
                               "args": {"overwrite": 1}})
    assert out["ok"] is False, out
    assert "`overwrite` must be a boolean" in _message(out)


# ----------------------------------------- "true" means true, on revl_export

def test_export_overwrite_string_true_writes(clean_session):
    assert _call("revl_load", {"source": INLINE})["ok"] is True
    target = clean_session / "out.rvl"
    target.write_text("stale\n", encoding="utf-8")
    out = _call("revl_export", {"path": str(target), "overwrite": "true"})
    assert out["ok"] is True, out
    assert target.read_text(encoding="utf-8") != "stale\n"
    assert out["argumentsCanonicalised"]["keys"] == ["overwrite"]


# ------------------------------------------ a real bool behaves as before

def test_real_false_stages_without_note(loaded):
    out = _call("revl_edit", {**REMOVE_A, "commit": False})
    assert out["ok"] is True, out
    assert out.get("swapped") is False
    assert _running(out) == (1, ALL_FOUR)
    assert "argumentsCanonicalised" not in out


def test_real_true_and_absent_commit(loaded):
    out = _call("revl_edit", {**REMOVE_A, "commit": True})
    assert out["ok"] is True and out.get("swapped") is True, out
    assert _running(out) == (2, ["TenantBApp", "TenantBStore"])
    assert "argumentsCanonicalised" not in out


def test_export_real_overwrite_false_still_refuses(clean_session):
    assert _call("revl_load", {"source": INLINE})["ok"] is True
    target = clean_session / "out.rvl"
    target.write_text("stale\n", encoding="utf-8")
    out = _call("revl_export", {"path": str(target), "overwrite": False})
    assert out["ok"] is False, out
    assert target.read_text(encoding="utf-8") == "stale\n"


def test_handler_default_true_read_fails_closed(loaded):
    """Behind dispatch too: only a real True, or no key, commits."""
    out = server_mod._edit_loaded({**REMOVE_A, "commit": "false"})
    assert out.get("swapped") is not True, out
    after = _call("revl_state", {})
    assert _running(after) == (1, ALL_FOUR)
