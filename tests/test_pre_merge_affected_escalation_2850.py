"""The affected local gate never escalates to FULL without an explicit run (#2850).

The selector's machine receipt is the evidence for a targeted or refused run.
If it asks for FULL, fails, or is unavailable, `--affected` must stop before
the gate commands and tell the contributor how to deliberately run `make
pre-merge` instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "pre_merge.sh"

_TARGETED = """\
FULL 0
REASON changed documentation selects only its examples
PYTEST tests/test_doc_examples.py
BACKENDS
GATES
"""
_FULL = """\
FULL 1
REASON unmapped path requires the full selection
PYTEST tests/
BACKENDS python go rust wasm java
GATES conformance site-wheel ruff
"""


def _run_selection(tmp_path: Path, selector: str | None,
                   *, selector_status: int = 0, python_available: bool = True):
    """Execute the real affected-selection block with a controlled selector."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("# --affected: the FAST inner-loop gate.")
    end = source.index("\nfail=0\n", start)
    block = source[start:end]

    shim = tmp_path / "bin"
    shim.mkdir()
    if python_available:
        python3 = shim / "python3"
        payload = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = -c ]; then exit 1; fi\n"
            "if [ \"${1:-}\" = tools/affected_tests.py ]; then\n"
            f"  printf '%s\\n' { _shell_quote(selector or '') }\n"
            f"  exit {selector_status}\n"
            "fi\n"
            "exit 1\n"
        )
        python3.write_text(payload, encoding="utf-8")
        python3.chmod(0o755)
        path = f"{shim}:/usr/bin:/bin"
    else:
        # Keep the usual shell utilities available but intentionally hide both
        # Python executable names, so the unavailable-selector path is real.
        for name in ("sed", "wc", "tr"):
            target = Path("/usr/bin") / name
            if not target.exists():
                target = Path("/bin") / name
            (shim / name).symlink_to(target)
        path = str(shim)

    shell = (
        f"root={_shell_quote(str(ROOT))}\n"
        "PYTHON=\n"
        "set -- --affected\n"
        f"{block}\n"
        "printf 'RUN_ALL=%s\\n' \"${RUN_ALL:-unset}\"\n"
    )
    env = {"PATH": path, "REVL_PREMERGE_WITH": ""}
    return subprocess.run(["/bin/sh", "-c", shell], cwd=ROOT, env=env,
                          text=True, capture_output=True, timeout=10)


def _shell_quote(value: str) -> str:
    """Single-quote a fixture value for the tiny shell shim."""
    return "'" + value.replace("'", "'\\''") + "'"


def test_targeted_selection_prints_the_machine_receipt_and_stays_targeted(tmp_path):
    proc = _run_selection(tmp_path, _TARGETED)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "selector receipt:" in proc.stdout
    assert "  FULL 0" in proc.stdout
    assert "  REASON changed documentation selects only its examples" in proc.stdout
    assert "RUN_ALL=0" in proc.stdout
    assert "no full gate was run" not in proc.stdout


def test_full_selection_is_refused_with_receipt_and_explicit_escalation(tmp_path):
    proc = _run_selection(tmp_path, _FULL)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "  FULL 1" in proc.stdout
    assert "  REASON unmapped path requires the full selection" in proc.stdout
    assert "refusing to run the FULL gate automatically" in proc.stdout
    assert "no full gate was run" in proc.stdout
    assert "run 'make pre-merge'" in proc.stdout
    assert "RUN_ALL=" not in proc.stdout


def test_selector_failure_is_reported_without_running_the_full_gate(tmp_path):
    proc = _run_selection(tmp_path, "selector failed: no merge-base",
                          selector_status=7)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "selector receipt:" in proc.stdout
    assert "selector failed: no merge-base" in proc.stdout
    assert "selector exited with status 7" in proc.stdout
    assert "no full gate was run" in proc.stdout
    assert "RUN_ALL=" not in proc.stdout


def test_empty_selector_output_is_reported_without_running_the_full_gate(tmp_path):
    proc = _run_selection(tmp_path, "")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "selector receipt:" in proc.stdout
    assert "  (no output)" in proc.stdout
    assert "no usable FULL line" in proc.stdout
    assert "no full gate was run" in proc.stdout
    assert "RUN_ALL=" not in proc.stdout


def test_missing_python_reports_unavailable_without_escalating(tmp_path):
    proc = _run_selection(tmp_path, None, python_available=False)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "selector unavailable (no python3 or python on PATH)" in proc.stdout
    assert "no full gate was run" in proc.stdout
    assert "run 'make pre-merge'" in proc.stdout
    assert "RUN_ALL=" not in proc.stdout
