"""An untrusted repair sent only as an export-area file may not delete a realm
placement (issue #2627).

A file inside an export directory is the author's text by its path (issue
#2524): `revl_repair` compiles it through `server.compile_under_authoring`,
which reads it under the authoring profile exactly as if it had been sent
inline. Issue #2520 drove the realm-removal refusal through the repair door with
inline source; this module sends the same candidate as an export-area file in
`files`, and nothing else.

Before this fix the trust condition asked only whether `source` or `modules` was
present, so a candidate sent this way was compiled as untrusted and then never
compared with the running generation for realm placements: the provision moved
into the shared realm and the repair came back `repaired`.

Controls. The same file placed outside the export area is the operator's own and
repairs in, which issue #2487 requires and
`test_untrusted_repair_realm_2520.py::test_repair_from_jailed_files_alone_is_admitted`
already pins. The same repair sent from the export area under a trusted operator
is admitted here, because there is no profile for the file to be compiled under,
so the file is not agent text. That control is what keeps the fix from refusing
an operator a repair this door used to allow.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.export_area import EXPORT_DIR, in_export_area  # noqa: E402
from revl.mcp import repair as repair_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the repair loop boots a composition and needs the cordis-py "
           "runtime: install it with `sh backends/python/setup.sh`")

CLAUSE = 'isolate kv in realm("tenant_a")'
REFUSAL = "cannot remove the realm placement of `Vault`"

VAULT = '''service Kv {
  fn get(k: Str) -> Opt[Str]
  fn set(k: Str, v: Str)
  fn unset(k: Str)
}

component Vault provides kv: Kv {
  isolate kv in realm("tenant_a")
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
    fn unset(k) = store.remove(k)
  }
}
'''
WITHOUT_CLAUSE = VAULT.replace("  " + CLAUSE + "\n", "")

POLICY = {"eligible": [{"component": "*"}], "mayTouch": ["*"],
          "ackOnWiden": False}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _repair_args(candidate: dict, **extra) -> dict:
    return {"component": "Vault", "candidate": candidate,
            "selfRepairPolicy": POLICY, **extra}


def _messages(result: dict) -> str:
    return " ".join(str(d.get("message", "")) + " " + str(d.get("hint", ""))
                    for d in result.get("diagnostics") or [])


def _realms(component: str) -> dict:
    for comp in server_mod.SESSION.ir["components"]:
        if comp["name"] == component:
            return dict(comp.get("isolate") or {})
    raise AssertionError(f"{component} is not running")


def _exported(base: Path, text: str) -> Path:
    """`text` as a file inside the root's export directory."""
    path = base / EXPORT_DIR / "vault.rvl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    session = server_mod.SESSION
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def loaded(clean_session):
    path = clean_session / "vault.rvl"
    path.write_text(VAULT)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _realms("Vault") == {"kv": "tenant_a"}
    return path


def _assert_refused(result: dict) -> None:
    assert result["incident"]["status"] == repair_mod.STATUS_REJECTED, result
    assert result["incident"]["swapped"] is False, result
    assert REFUSAL in _messages(result), result
    assert any(d.get("code") == "G9" for d in result["diagnostics"]), result
    assert _realms("Vault") == {"kv": "tenant_a"}


def test_untrusted_repair_sent_only_as_an_exported_file_is_refused(loaded):
    """The candidate is one file, it is inside the export area, and it leaves
    the clause out: refused by name, and the running composition untouched."""
    exported = _exported(loaded.parent, WITHOUT_CLAUSE)
    assert in_export_area(str(exported))  # really in the export area
    result = _call("revl_repair", _repair_args({"files": [str(exported)]}))
    _assert_refused(result)
    assert result["remediation"]["applied"] is False, result
    assert result["remediation"]["blockedBy"] == "realm", result


def test_trusted_repair_sent_only_as_an_exported_file_is_admitted(loaded):
    """With no profile there is nothing the file compiled under, so it stays the
    operator's own and the door admits it, as it admitted it before #2627."""
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None  # the trusted operator
    exported = _exported(loaded.parent, WITHOUT_CLAUSE)
    assert in_export_area(str(exported))  # really in the export area
    result = _call("revl_repair", _repair_args({"files": [str(exported)]}))
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms("Vault") == {}
