"""Keep the admission-latency claim and MCP walkthrough tied to their sources."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

RUNNING_SOURCE = """\
service Store { fn get(key: Str) -> Int }
component StoreImpl provides store: Store {
  provide store { fn get(key) = 1 }
}
"""

BAD_CANDIDATE = """\
service Store { fn get(key: Str) -> Int }
service Worker { fn run() -> Int }
component WorkerImpl provides worker: Worker {
  provide worker { fn run() = store.get("boot") }
}
"""

CORRECTED_CANDIDATE = """\
service Store { fn get(key: Str) -> Int }
service Worker { fn run() -> Int }
component WorkerImpl requires store: Store provides worker: Worker {
  provide worker { fn run() = store.get("boot") }
}
"""


@pytest.fixture
def fresh_server(tmp_path, monkeypatch):
    previous_authoring = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    server.set_authoring_trust(host_code=False, granted=None, providers=None,
                              roots=(str(tmp_path),))
    yield
    server.AUTHORING = previous_authoring


def _call(tool: str, arguments: dict) -> dict:
    response = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    return response["result"]["structuredContent"]


def test_readme_quotes_the_scoped_committed_measurement():
    benchmark = (ROOT / "bench/results/admission-latency.md").read_text(
        encoding="utf-8")
    row = next(line for line in benchmark.splitlines()
               if "**compile + admit (round-trip)**" in line)
    match = re.search(r"\*\*([0-9.]+ ms)\*\*", row)
    assert match is not None

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(
        rf"median of\s+\*\*{re.escape(match.group(1))}\*\*", readme)
    assert "Python gate" in readme
    assert "in-process" in readme
    flat = " ".join(readme.split())
    assert "not MCP transport latency" in flat
    assert "tests/test_admission_latency.py" in flat
    assert (ROOT / "tests/test_admission_latency.py").exists()
    assert "20 ms ceiling, which is only a regression guard" in flat


def test_walkthrough_matches_mcp_refusal_and_admission(fresh_server):
    page = (ROOT / "docs/mcp-admission-walkthrough.md").read_text(
        encoding="utf-8")
    assert RUNNING_SOURCE.strip() in page
    assert BAD_CANDIDATE.strip() in page
    assert CORRECTED_CANDIDATE.strip() in page

    refused = _call("revl_check", {"source": BAD_CANDIDATE})
    assert refused["ok"] is False
    diagnostic = refused["diagnostics"][0]
    assert diagnostic["code"] == "G1"
    assert diagnostic["guarantee"] == (
        "declared access: a component reads only what it requires")
    assert diagnostic["fix"] == (
        "add the key to the component's `requires` clause, or drop the access")
    displayed = {key: diagnostic[key] for key in
                 ("code", "message", "hint", "guarantee", "fix")}
    output_block = "```json\n" + json.dumps(
        displayed, ensure_ascii=False, indent=2) + "\n```"
    assert output_block in page

    checked = _call("revl_check", {"source": CORRECTED_CANDIDATE})
    assert checked["ok"] is True
    assert checked["selfCheck"]["admissible"] is True

    running_manifest = compile_source(RUNNING_SOURCE, "running.rvl")
    admitted = _call("revl_admit", {
        "source": CORRECTED_CANDIDATE,
        "manifest": running_manifest,
    })
    assert admitted["ok"] is True
    assert admitted["admitted"] is True
    assert "revl_admit({ source: correctedCandidate, manifest: runningManifest })" in page
    assert "manifest input is required" in page
