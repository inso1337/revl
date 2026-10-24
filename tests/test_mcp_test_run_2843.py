"""`revl_test_run`: a selected suite, per-test results (issue #2843).

The verb runs pytest under the server's own interpreter, parses the junit
report it writes to a temp file, deletes the file and answers with per-test
entries, counts, a verdict and a receipt. Every case here runs a tiny suite
written under `tmp_path`, never the real one, and none of them needs the
cordis-py runtime: the verb must answer on an interpreter that cannot boot a
composition.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _call(selection=None, cwd=None, timeout=None):
    arguments = {}
    if selection is not None:
        arguments["selection"] = selection
    if cwd is not None:
        arguments["cwd"] = cwd
    if timeout is not None:
        arguments["timeout"] = timeout
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_test_run",
                                  "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def rooted(tmp_path):
    """The verb confines `cwd` and the selection to the sanctioned roots, so
    the test tree is the sanctioned root for the duration of one case."""
    before = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    server_mod.AUTHORING = before


def _suite(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_a_passing_suite_returns_the_entry_and_the_verdict(rooted):
    suite = _suite(rooted, "test_tiny_pass.py",
                   "def test_ok():\n    assert 1 + 1 == 2\n")
    payload = _call([suite], str(rooted))
    assert payload["ok"] is True
    assert payload["verdict"] == "passed"
    assert payload["counts"] == {"total": 1, "passed": 1, "failed": 0,
                                 "skipped": 0, "errors": 0}
    entry, = payload["tests"]
    assert entry["nodeId"].endswith("test_tiny_pass.py::test_ok")
    assert entry["outcome"] == "passed"
    assert isinstance(entry["duration"], float) and entry["duration"] >= 0
    assert entry["message"] is None
    receipt = payload["receipt"]
    assert receipt["command"][0] == sys.executable
    assert "-m" in receipt["command"] and "pytest" in receipt["command"]
    assert suite in receipt["command"]
    assert receipt["cwd"] == os.path.realpath(str(rooted))
    assert receipt["exitCode"] == 0
    assert receipt["durationSeconds"] >= 0
    # the runner's stdout is never echoed back
    assert "stdout" not in payload


def test_a_failing_suite_carries_the_message_and_a_failed_verdict(rooted):
    suite = _suite(rooted, "test_tiny_fail.py",
                   'def test_bad():\n    assert 1 == 2, "boom-marker"\n')
    payload = _call([suite], str(rooted))
    assert payload["ok"] is True
    assert payload["verdict"] == "failed"
    assert payload["counts"] == {"total": 1, "passed": 0, "failed": 1,
                                 "skipped": 0, "errors": 0}
    entry, = payload["tests"]
    assert entry["nodeId"].endswith("test_tiny_fail.py::test_bad")
    assert entry["outcome"] == "failed"
    assert "boom-marker" in entry["message"]
    assert payload["receipt"]["exitCode"] == 1


def test_an_empty_selection_is_refused(rooted):
    payload = _call([], str(rooted))
    assert payload["ok"] is False
    assert payload["code"] == "EMPTY_SELECTION"
    assert payload["verdict"] == "refused"
    assert "selection" in payload["diagnostics"][0]["message"]


def test_an_all_skipped_selection_is_refused(rooted):
    suite = _suite(rooted, "test_tiny_skip.py",
                   "import pytest\n\n"
                   "pytestmark = pytest.mark.skip(reason=\"not today\")\n\n"
                   "def test_skipped():\n    assert False\n")
    payload = _call([suite], str(rooted))
    assert payload["ok"] is False
    assert payload["code"] == "ALL_SKIPPED"
    assert payload["verdict"] == "refused"
    assert payload["counts"] == {"total": 1, "passed": 0, "failed": 0,
                                 "skipped": 1, "errors": 0}


def test_a_selection_that_collects_nothing_is_refused(rooted):
    suite = _suite(rooted, "test_tiny_empty.py", "# no test here\n")
    payload = _call([suite], str(rooted))
    assert payload["ok"] is False
    assert payload["code"] == "NO_TESTS_COLLECTED"
    assert payload["verdict"] == "refused"


def test_a_long_failure_message_is_bounded(rooted):
    suite = _suite(rooted, "test_tiny_long.py",
                   "def test_long():\n    assert False, \"x\" * 2000\n")
    payload = _call([suite], str(rooted))
    message = payload["tests"][0]["message"]
    assert len(message) <= server_mod._TEST_MESSAGE_LIMIT
    assert message.endswith("...")


def test_a_selection_outside_the_sanctioned_root_is_refused(rooted):
    outside = str(rooted.parent / "test_outside_2843.py")
    payload = _call([outside], str(rooted))
    assert payload["ok"] is False
    assert payload["code"] == "OUTSIDE_ROOT"
    assert payload["verdict"] == "refused"


def test_a_run_that_outlasts_its_timeout_is_refused(rooted):
    suite = _suite(rooted, "test_tiny_slow.py",
                   "import time\n\n"
                   "def test_slow():\n    time.sleep(10)\n")
    payload = _call([suite], str(rooted), timeout=1)
    assert payload["ok"] is False
    assert payload["code"] == "TIMEOUT"
    assert payload["verdict"] == "refused"
    assert payload["receipt"]["timedOut"] is True
    assert payload["receipt"]["exitCode"] is None


def test_the_verb_runs_without_a_cordis_runtime(rooted):
    suite = _suite(rooted, "test_tiny_runtime_free.py",
                   "def test_ok():\n    assert True\n")
    before = server_mod.runtime_available()
    server_mod.set_runtime_available(False)
    try:
        payload = _call([suite], str(rooted))
    finally:
        server_mod.set_runtime_available(before)
    assert payload["ok"] is True, payload
    assert payload["verdict"] == "passed"
