"""Issue #1692: `revl mcp serve` never leaves the runtime verbs silently dead.

Under an interpreter that cannot import `cordis`, no composition can be
loaded, so every verb that acts on a live composition was dead, and each one
failed on its own: `revl_load` with an import error, the rest with "nothing
is loaded". An agent benchmark lost 11 of 51 verbs for a whole series that
way, with nothing at startup saying so.

Now the server re-executes under the repository's runtime venv when that venv
has cordis. Otherwise it announces the unavailable verbs (stderr and the
`initialize` instructions), and each of them answers with a named refusal
whose `next` field is the fix. `revl doctor` reports which of the three the
server will do.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import doctor  # noqa: E402
from revl.doctor import ProbeResult  # noqa: E402
from revl.mcp import runtime_gate as gate  # noqa: E402
from revl.mcp import server  # noqa: E402

SOURCE = ("service S { fn f() -> Int }\n"
          "component C provides s: S {\n  provide s {\n    fn f() = 1\n  }\n}\n")


@pytest.fixture
def no_runtime():
    server.set_runtime_available(False)
    yield
    server.set_runtime_available(None)


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------------------ the verb lists

def test_the_verb_lists_name_real_verbs_and_do_not_overlap():
    tools = {tool["name"] for tool in server.TOOLS}
    assert gate.RUNTIME_VERBS <= tools, sorted(gate.RUNTIME_VERBS - tools)
    assert set(gate.LIMITED_VERBS) <= tools, sorted(set(gate.LIMITED_VERBS) - tools)
    assert not gate.RUNTIME_VERBS & set(gate.LIMITED_VERBS)
    assert "revl_load" in gate.RUNTIME_VERBS


# ------------------------------------------------------- the announced state

def test_initialize_names_every_unavailable_verb_and_the_fix(no_runtime):
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}})["result"]
    text = result["instructions"]
    for verb in gate.RUNTIME_VERBS:
        assert verb in text, verb
    assert "setup.sh" in text and "not importable" in text


def test_initialize_says_nothing_extra_when_the_runtime_is_there():
    server.set_runtime_available(True)
    try:
        text = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {}})["result"]["instructions"]
    finally:
        server.set_runtime_available(None)
    assert "not importable" not in text


@pytest.mark.parametrize("verb", sorted(gate.RUNTIME_VERBS))
def test_each_runtime_verb_answers_with_the_named_refusal(no_runtime, verb):
    payload = _call(verb, {})
    assert payload["ok"] is False and payload["refused"] is True, payload
    assert payload["unavailable"] == "cordis-py runtime"
    assert "setup.sh" in payload["next"]
    diagnostic = payload["diagnostics"][0]
    assert diagnostic["category"] == "runtime"
    assert f"`{verb}` needs the cordis-py runtime" in diagnostic["message"]


def test_load_is_refused_by_name_not_by_an_import_error(no_runtime):
    payload = _call("revl_load", {"source": SOURCE})
    assert payload["refused"] is True
    assert "ModuleNotFoundError" not in json.dumps(payload)


def test_ship_is_refused_only_when_it_would_apply(no_runtime):
    assert _call("revl_ship", {"source": SOURCE, "apply": True})["refused"] is True
    assert "unavailable" not in _call("revl_ship", {"source": SOURCE})


def test_static_verbs_still_answer_without_the_runtime(no_runtime):
    assert _call("revl_check", {"source": SOURCE})["ok"] is True
    assert _call("revl_query_dependents", {"target": "S", "source": SOURCE})["ok"] is True


@pytest.mark.skipif(importlib.util.find_spec("cordis") is not None,
                    reason="exercises a real cordis-less interpreter")
def test_a_real_cordis_less_server_announces_and_refuses():
    """The exit test, end to end: this interpreter has no cordis, and the
    re-exec is switched off, so the server must announce and refuse."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "revl_load", "arguments": {"source": SOURCE}}},
    ]
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
               **{gate.NO_REEXEC_ENV: "1"})
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "mcp", "serve"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, env=env, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "revl_load" in proc.stderr and "not importable" in proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert "revl_load" in responses[0]["result"]["instructions"]
    load = responses[1]["result"]["structuredContent"]
    assert load["refused"] is True and "setup.sh" in load["next"]


# ------------------------------------------------------------ the re-exec

def _fake_venv(tmp_path: Path, exit_code: int) -> Path:
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    python.chmod(0o755)
    return python


def test_reexec_targets_the_venv_when_it_can_import_cordis(tmp_path, monkeypatch):
    python = _fake_venv(tmp_path, 0)
    monkeypatch.setattr(gate, "cordis_importable", lambda: False)
    monkeypatch.setattr(gate, "venv_python", lambda: python)
    monkeypatch.delenv(gate.REEXEC_ENV, raising=False)
    monkeypatch.delenv(gate.NO_REEXEC_ENV, raising=False)
    assert gate.reexec_target() == python


@pytest.mark.parametrize("why", ["has-cordis", "venv-lacks-cordis",
                                 "already-reexeced", "switched-off"])
def test_reexec_stays_put(tmp_path, monkeypatch, why):
    python = _fake_venv(tmp_path, 1 if why == "venv-lacks-cordis" else 0)
    monkeypatch.setattr(gate, "cordis_importable", lambda: why == "has-cordis")
    monkeypatch.setattr(gate, "venv_python", lambda: python)
    monkeypatch.delenv(gate.REEXEC_ENV, raising=False)
    monkeypatch.delenv(gate.NO_REEXEC_ENV, raising=False)
    if why == "already-reexeced":
        monkeypatch.setenv(gate.REEXEC_ENV, "1")
    if why == "switched-off":
        monkeypatch.setenv(gate.NO_REEXEC_ENV, "1")
    assert gate.reexec_target() is None


def test_reexec_replaces_the_process_and_says_so(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(gate.os, "execve",
                        lambda path, argv, env: calls.append((path, argv, env)))
    python = tmp_path / "python"
    gate.reexec(python, ["mcp", "serve", "--root", "x"])
    (path, argv, env), = calls
    assert path == str(python)
    assert argv == [str(python), "-P", "-m", "revl", "mcp", "serve", "--root", "x"]
    assert env[gate.REEXEC_ENV] == "1"
    assert "re-executing under" in capsys.readouterr().err


# ------------------------------------------------------------- revl doctor

class _Prober:
    def __init__(self, cordis: bool, venv_ok: bool):
        self.cordis, self.venv_ok = cordis, venv_ok

    def module_available(self, name):
        return self.cordis and name == "cordis"

    def run(self, argv, timeout=None, stdin=""):
        return ProbeResult(found=self.venv_ok, returncode=0 if self.venv_ok else 1)


@pytest.mark.parametrize("cordis,venv_ok,status,needle", [
    (True, False, doctor.OK, "every MCP verb is available"),
    (False, True, doctor.OK, "re-executes under"),
    (False, False, doctor.WARN, "would refuse"),
])
def test_doctor_reports_what_mcp_serve_will_do(cordis, venv_ok, status, needle):
    check = doctor.check_mcp_runtime(_Prober(cordis, venv_ok), ROOT / "backends")
    assert check.status is status and needle in check.detail, check
    if status is doctor.WARN:
        assert "revl_load" in check.detail and "setup.sh" in check.detail
