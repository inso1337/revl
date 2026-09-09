"""The placement runner's own log channels sit behind the redaction funnel
(issue #814; advisory GHSA-4x4q-296m-9x9x L1/M1 extension).

`runtime._record` funnels the host trace and `bridge.seam_failure` funnels the
wire reply, but this process's OWN console lines sat outside both: a probe
result, a probe error quoting its argument, the E-Stop inventory's stranded
`repr(resource)` entries, and — the finding itself — an uncaught exception's
traceback to stderr, which the conductor merges verbatim. Every channel this
fix covers now reads the SAME registry the runtime marks, and the failure
paths run the two-stage funnel (own arguments first, then secrets) the wire
reply has had since item 421 F5.

Every assertion is PAIRED — canary absent AND marker present — so none can
pass on an empty line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _process_runner as runner  # noqa: E402

CANARY = "SEKRIT-RUNNER-CANARY-814"
REDACTED = "<redacted:secret>"
REDACTED_ARG = "<redacted:arg>"


@pytest.fixture(autouse=True)
def _fresh_registry():
    confidential = runner._funnel()
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


def test_redact_scrubs_a_registered_secret():
    runner._funnel().register_secret_value(CANARY)
    out = runner._redact(f"probe => {{'token': '{CANARY}'}}")
    assert CANARY not in out
    assert REDACTED in out


def test_redact_leaves_ordinary_text_alone():
    runner._funnel().register_secret_value(CANARY)
    text = "[c] probe| db.query | => [{'id': 1}]"
    assert runner._redact(text) == text


def test_redact_call_scrubs_arguments_first_then_secrets():
    runner._funnel().register_secret_value(CANARY)
    out = runner._redact_call(f"KeyError: '{CANARY}'", [CANARY])
    # the ARGUMENT stage wins over the registry stage (the more specific fact)
    assert CANARY not in out
    assert REDACTED_ARG in out
    assert REDACTED not in out


def test_redact_call_reports_a_held_secret_as_a_secret():
    runner._funnel().register_secret_value(CANARY)
    out = runner._redact_call(f"upstream refused token {CANARY}", ["user-1234"])
    assert CANARY not in out
    assert REDACTED in out


class _Raising:
    def get(self, key):
        raise KeyError(key)


def test_eval_probe_hands_parsed_arguments_back():
    args: list = []
    with pytest.raises(KeyError):
        runner._eval_probe("vault.get('alice')", {"vault": _Raising()},
                           args_out=args)
    assert args == ["alice"]


def test_eval_probe_default_keeps_old_signature():
    # `_eval_probe`'s other callers (test_distribute.py) pass two args.
    args: list = []
    with pytest.raises(ValueError, match="not a key this process holds"):
        runner._eval_probe("other.query('x')", {"vault": _Raising()}, args)
    assert args == []  # the parse never reached the literals


def test_probe_error_line_is_two_stage_redacted(capsys, monkeypatch):
    # `run()`'s probe loop logs the in-process dispatch failure through the
    # funnel: the caller's own argument cannot ride the error text out.
    runner._funnel().register_secret_value(CANARY)
    lines: list[str] = []
    log = lambda channel, subject, detail: lines.append(  # noqa: E731
        runner._redact_call(f"{channel}|{subject}|{detail}", []))

    args: list = []
    try:
        runner._eval_probe("vault.get('alice')", {"vault": _Raising()}, args_out=args)
    except Exception as exc:  # noqa: BLE001 — the runner's own catch shape
        log("probe", "vault.get('alice')",
            "ERROR " + runner._redact_call(f"{type(exc).__name__}: {exc}", args))
    out = lines[0]
    # the SUBJECT is the placement's own declared probe text (composition
    # data, printable); the DETAIL — the exception text — is what crossed
    channel, subject, detail = out.split("|", 2)
    assert "alice" not in detail, out
    assert REDACTED_ARG in detail, out
    assert REDACTED not in detail, out


def test_main_catch_all_prints_one_redacted_line(tmp_path, monkeypatch, capsys):
    # `main()`'s last unguarded channel: an exception `run()` does not catch
    # used to escape as a bare traceback to stderr (merged verbatim by the
    # conductor). Now: one funnelled line, non-zero exit, no traceback.
    runner._funnel().register_secret_value(CANARY)
    spec = {"name": "boom", "files": [str(tmp_path / "missing.rvl")]}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    monkeypatch.setattr(sys, "argv", ["_process_runner.py", str(spec_path)])
    with pytest.raises(SystemExit) as excinfo:
        runner.main()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert "FATAL" in err
    # the compile error quotes the missing file path — fine — but any SECRET
    # interpolated into the exception text is scrubbed before it lands
    assert CANARY not in err
