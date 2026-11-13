"""`revl run --policy` refuses what `revl audit --policy` refuses (issue #3185).

Before this, `args.policy` was read on the wasm tier only (and there through
the allow-list alone), so on py, rust, ts, java and go a composition the audit
refused booted and ran its host bodies. These drive the real CLI as a
subprocess: the refusal must be the audit's own report, on every tier, before
any runtime is imported, and the host body must never run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.run_ts import ts_runtime_reason  # noqa: E402

MARK = "SENDEMAIL-BODY-RAN"

APP = f"""\
extern emission fn sendEmail(to: Str) -> Str
  = @py {{
    print("{MARK} to=" + to)
    return "ok"
  }}
  = @ts {{
    console.log("{MARK} to=" + to);
    return "ok";
  }}
component Mailer {{
  emit sendEmail("victim@example.invalid")
}}
"""

DENY = "component * may not reach sendEmail\n"
ALLOW_OTHER = "component Mailer may reach other\n"
UNRELATED = "component * may not reach other\n"


def _have_cordis_py() -> bool:
    try:
        import cordis  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001 - absent or broken: not runnable here
        return False
    return True


def _write(tmp_path: Path, policy: str) -> tuple[str, str]:
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    pol = tmp_path / "p.policy"
    pol.write_text(policy, encoding="utf-8")
    return str(app), str(pol)


def _cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")
    return subprocess.run([sys.executable, "-m", "revl", *args],
                          capture_output=True, text=True, input="", env=env,
                          check=False, cwd=ROOT)


@pytest.mark.parametrize("policy", [DENY, ALLOW_OTHER])
def test_audit_refuses_the_fixture(tmp_path, policy):
    # the target: what the audit gate refuses for this source and policy
    app, pol = _write(tmp_path, policy)
    result = _cli("audit", app, "--policy", pol)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "admission REFUSED" in result.stdout


@pytest.mark.parametrize("backend", ["py", "ts", "rust", "java", "go", "wasm"])
@pytest.mark.parametrize("policy", [DENY, ALLOW_OTHER])
def test_run_refuses_what_audit_refuses_on_every_tier(tmp_path, backend, policy):
    app, pol = _write(tmp_path, policy)
    result = _cli("run", app, "--backend", backend, "--policy", pol, "--once")
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "boundary policy: 1 violation(s)" in result.stderr, out
    assert "admission REFUSED" in result.stderr, out
    assert "lifecycle stage: admission" in result.stderr, out
    assert MARK not in out


def test_run_refuses_under_placement(tmp_path):
    app, pol = _write(tmp_path, DENY)
    placement = tmp_path / "p.toml"
    placement.write_text('[components.Mailer]\nbackend = "py"\n',
                         encoding="utf-8")
    result = _cli("run", app, "--placement", str(placement), "--policy", pol,
                  "--once")
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "admission REFUSED" in result.stderr, out
    assert MARK not in out


def test_unloadable_policy_refuses_instead_of_running(tmp_path):
    app, _ = _write(tmp_path, DENY)
    result = _cli("run", app, "--policy", str(tmp_path / "missing.policy"),
                  "--once")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot load policy" in result.stderr
    assert MARK not in result.stdout


@pytest.mark.skipif(not _have_cordis_py(), reason="needs the cordis-py runtime")
def test_py_control_a_clean_policy_still_runs_the_body(tmp_path):
    # the negative control: the gate refuses violations, not every policy
    app, pol = _write(tmp_path, UNRELATED)
    result = _cli("run", app, "--policy", pol, "--once")
    assert result.returncode == 0, result.stdout + result.stderr
    assert MARK in result.stdout


@pytest.mark.skipif(ts_runtime_reason() is not None,
                    reason=f"needs node + cordis-ts: {ts_runtime_reason()}")
def test_ts_control_a_clean_policy_still_runs_the_body(tmp_path):
    app, pol = _write(tmp_path, UNRELATED)
    result = _cli("run", app, "--backend", "ts", "--policy", pol, "--once")
    assert result.returncode == 0, result.stdout + result.stderr
    assert MARK in result.stdout


def test_watch_reload_check_rejects_an_edit_the_policy_refuses(tmp_path):
    # `--watch`: the reload check refuses an edit the boot would have refused
    from revl import compile_source  # noqa: PLC0415
    from revl.policy import parse_policy  # noqa: PLC0415
    from revl.run import _both_checks, _policy_check  # noqa: PLC0415

    check = _both_checks(lambda ir: None, _policy_check(parse_policy(DENY)))
    assert "admission REFUSED" in check(compile_source(APP))
    clean = _both_checks(None, _policy_check(parse_policy(UNRELATED)))
    assert clean(compile_source(APP)) is None
    assert _both_checks(None, _policy_check(None)) is None


@pytest.mark.parametrize("which", ["evaluate", "approval_admission"])
def test_a_policy_error_during_evaluation_is_a_clean_refusal(
        tmp_path, monkeypatch, capsys, which):
    # a RevlError raised while the policy is evaluated refuses the run with a
    # stage-named error, never a traceback and never a boot
    import revl.policy as policy_mod  # noqa: PLC0415
    from revl.__main__ import main  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    def boom(*_args, **_kwargs):
        raise RevlError("p.policy", 1, "policy evaluation broke")

    monkeypatch.setattr(policy_mod, which, boom)
    app, pol = _write(tmp_path, DENY)
    code = main(["run", app, "--policy", pol, "--once"])
    err = capsys.readouterr()
    assert code == 1
    assert "cannot apply policy" in err.err
    assert "policy evaluation broke" in err.err
    assert "lifecycle stage: config" in err.err
    assert MARK not in err.out


def test_placement_compile_error_is_reported_at_the_compile_stage(tmp_path):
    app = tmp_path / "app.rvl"
    app.write_text("component Broken {\n", encoding="utf-8")
    pol = tmp_path / "p.policy"
    pol.write_text(DENY, encoding="utf-8")
    placement = tmp_path / "p.toml"
    placement.write_text('[components.Broken]\nbackend = "py"\n',
                         encoding="utf-8")
    result = _cli("run", str(app), "--placement", str(placement),
                  "--policy", str(pol), "--once")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "lifecycle stage: compile" in result.stderr, result.stderr
    assert "cannot apply policy" not in result.stderr
