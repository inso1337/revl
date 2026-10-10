"""Issue #2240: `revl_change {add: {component, provide, methods}}` takes
`provide` in the language's own `key: Service` spelling (`kv: Kv`), as well as a
bare key or {key, service}. The string used to be read as a key literally named
`kv: Kv`, and the refusal said the composition does not know it, while `kv` was
in `providedKeys`."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import change  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.change import ChangeError  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session verbs boot a composition and need the cordis-py runtime "
           "- install it with `sh backends/python/setup.sh`")

TENANTS = ROOT / "examples" / "tenants.rvl"
METHODS = {"get": "None", "set": "{ }", "unset": "{ }"}
DICT_FORM = {"key": "kv", "service": "Kv"}
SPELLINGS = ["kv: Kv", "kv:Kv", "  kv  :  Kv  ", "kv: Kv "]


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tenants.rvl").write_text(TENANTS.read_text())
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    server_mod.set_runtime_available(True)
    yield
    server_mod.set_runtime_available(None)
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old
    _call("revl_change", {"discard": True})


def _vs() -> dict:
    return {"source": TENANTS.read_text(), "modules": {}}


def _spec(provide) -> dict:
    return {"component": "ExtraStore", "provide": provide, "methods": METHODS}


@pytest.mark.parametrize("provide", SPELLINGS)
def test_key_colon_service_reads_as_key_and_service(provide):
    assert change._provided(provide, {"kv": "Kv"}) == ("kv", "Kv")
    assert change._provided(provide, {}) == ("kv", "Kv")


@pytest.mark.parametrize("provide", SPELLINGS)
def test_component_source_matches_the_dict_form(provide):
    expected = change.component_source(_vs(), _spec(DICT_FORM))
    assert change.component_source(_vs(), _spec(provide)) == expected


def test_a_bare_key_keeps_inferring_the_service():
    assert change._provided("kv", {"kv": "Kv"}) == ("kv", "Kv")
    with pytest.raises(ChangeError, match="does not know `nowhere` yet"):
        change._provided("nowhere", {"kv": "Kv"})


@pytest.mark.parametrize("provide", [":Kv", "kv:", ":", " : ", "kv: Kv Extra",
                                     "k v: Kv", "kv: Kv:Kv", "1kv: Kv"])
def test_a_malformed_key_service_names_the_accepted_forms(provide):
    with pytest.raises(ChangeError) as excinfo:
        change._provided(provide, {"kv": "Kv"})
    message = str(excinfo.value)
    assert "bare key" in message and "key: Service" in message, message
    assert "kv: Kv" in message and repr(provide) in message, message
    assert "does not know" not in message, message


@needs_runtime
@pytest.mark.parametrize("provide", SPELLINGS)
def test_over_mcp_the_spelling_resolves_like_the_dict_form(provide):
    loaded = _call("revl_load", {"files": ["tenants.rvl"]})
    assert loaded["ok"] is True, loaded
    expected = _call("revl_change", {"add": _spec(DICT_FORM)})
    _call("revl_change", {"discard": True})
    got = _call("revl_change", {"add": _spec(provide)})
    assert got["ok"] == expected["ok"], (got, expected)
    assert got.get("diagnostics") == expected.get("diagnostics"), (got, expected)
    assert not any("does not know" in d.get("message", "")
                   for d in got.get("diagnostics") or []), got


@needs_runtime
@pytest.mark.parametrize("provide", [":Kv", "kv:"])
def test_over_mcp_a_malformed_value_is_refused_with_the_forms(provide):
    loaded = _call("revl_load", {"files": ["tenants.rvl"]})
    assert loaded["ok"] is True, loaded
    result = _call("revl_change", {"add": _spec(provide)})
    assert result["ok"] is False
    message = result["diagnostics"][0]["message"]
    assert "bare key" in message and "key: Service" in message, message
