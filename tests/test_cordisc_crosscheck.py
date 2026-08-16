"""Cross-toolchain verification: cordisc (the independent TS-side checker,
~/Projects/cordisc) must accept the code the revl TS backend emits.

Two independently built verifiers agreeing on the same artifact is defense
in depth: cordisc rediscovers the component graph from the *emitted*
TypeScript with no knowledge of revl, so a lowering bug that broke the
inject/provide surface would surface here even if revl's own tests missed it.

Skips when cordisc is not installed next to this repo (CI without it).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORDISC = Path.home() / "Projects" / "cordisc" / "dist" / "cli.js"


@pytest.mark.skipif(
    not CORDISC.exists() or shutil.which("node") is None,
    reason="cordisc (or node) not available",
)
def test_cordisc_accepts_emitted_typescript():
    result = subprocess.run(
        ["node", str(CORDISC), "check", "-p",
         str(ROOT / "backends" / "typescript" / "tsconfig.json"), "--json"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"cordisc reported errors:\n{result.stdout}\n{result.stderr}"
    report = json.loads(result.stdout)
    errors = [d for d in report.get("diagnostics", []) if d.get("severity") == "error"]
    assert errors == []
    names = {c.get("name") for c in report.get("components", [])}
    assert {"PgDatabase", "UserCache"} <= names, \
        f"cordisc failed to rediscover the compiled components: {names}"
