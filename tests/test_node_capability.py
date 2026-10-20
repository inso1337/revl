"""Unit tests for the shared node `.ts` capability probe.

The probe itself runs under the host's node, so its decision is exercised with
an injected runner here: the node-present-but-incapable case is the one these
gates used to run and fail on, and it must be reproducible without installing
an old node.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _node_capability import PROBE_SOURCE, node_ts_skip_reason

_ABSENT = "node is required to run the emitted ts fixture"


class _FakeRunner:
    """Stands in for `subprocess.run`, recording the argv of both calls."""

    def __init__(self, *, version="v24.1.0", probe_code=0,
                 probe_stdout="42", error=None, probe_error=None):
        self.version = version
        self.probe_code = probe_code
        self.probe_stdout = probe_stdout
        self.error = error
        self.probe_error = probe_error
        self.calls = []
        self.probe_path = None
        self.probe_source = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        if argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, f"{self.version}\n", "")
        if self.probe_error is not None:
            raise self.probe_error
        self.probe_path = Path(argv[1])
        self.probe_source = self.probe_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, self.probe_code, self.probe_stdout, "")


def test_no_node_returns_the_gates_original_reason():
    assert node_ts_skip_reason(_ABSENT, which=lambda name: None) == _ABSENT


def test_a_capable_node_does_not_skip_and_is_probed_on_a_real_ts_file():
    runner = _FakeRunner()
    assert node_ts_skip_reason(
        _ABSENT, which=lambda name: "/fake/node", runner=runner) is None
    assert [call[0] for call in runner.calls] == ["/fake/node", "/fake/node"]
    assert runner.calls[1][1].endswith(".ts")
    assert runner.probe_source == PROBE_SOURCE
    assert len(runner.probe_source.splitlines()) == 2


def test_an_incapable_node_names_its_version_and_the_type_stripping_need():
    reason = node_ts_skip_reason(
        _ABSENT, node="/fake/node",
        runner=_FakeRunner(version="v22.2.0", probe_code=1, probe_stdout=""))
    assert reason is not None
    assert "v22.2.0" in reason
    assert "runs .ts directly" in reason
    assert "type stripping" in reason
    assert _ABSENT not in reason


def test_a_zero_exit_with_the_wrong_value_is_not_capability():
    reason = node_ts_skip_reason(
        _ABSENT, node="/fake/node",
        runner=_FakeRunner(probe_code=0, probe_stdout="41"))
    assert reason is not None
    assert "v24.1.0" in reason


def test_a_node_that_cannot_be_executed_is_named_by_path_not_reported_absent():
    reason = node_ts_skip_reason(
        _ABSENT, node="/fake/node",
        runner=_FakeRunner(error=FileNotFoundError("/fake/node")))
    assert reason is not None
    assert "/fake/node" in reason
    assert "could not be probed" in reason
    assert "type stripping" in reason
    assert _ABSENT not in reason


def test_a_probe_that_times_out_keeps_the_version_already_read():
    reason = node_ts_skip_reason(
        _ABSENT, node="/fake/node",
        runner=_FakeRunner(probe_error=subprocess.TimeoutExpired(["node"], 30)))
    assert reason is not None
    assert "v24.1.0" in reason
    assert "could not be probed" in reason
    assert _ABSENT not in reason


def test_a_wrapper_that_exits_nonzero_is_judged_incapable(tmp_path):
    wrapper = tmp_path / "node"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo v22.2.0; exit 0; fi\n"
        "echo 'TypeError [ERR_UNKNOWN_FILE_EXTENSION]' >&2\n"
        "exit 1\n",
        encoding="utf-8")
    wrapper.chmod(0o755)
    reason = node_ts_skip_reason(_ABSENT, node=str(wrapper))
    assert reason is not None
    assert "v22.2.0" in reason
    assert "type stripping" in reason
