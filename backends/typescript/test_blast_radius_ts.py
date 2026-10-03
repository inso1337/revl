"""The blast-radius benchmark's TypeScript renderings type-check (issue #1702).

`bench/blast_radius/ts/` holds the ts emitter's rendering of each benchmark
composition, so the same task can be handed to an agent working in TypeScript.
`tests/test_blast_radius_bench.py` checks the renderings are current; this
checks they type-check, each as its own program under the options this tier
checks its emitted modules with. It needs node and this tier's `npm ci`, which
is why it lives here: the typescript CI job has both.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
SCRIPT = ROOT / "bench" / "blast_radius" / "typecheck.mjs"
RENDERINGS = sorted((ROOT / "bench" / "blast_radius" / "ts").glob("*.ts"))

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
    pytest.mark.skipif(not (BACKEND / "node_modules" / "typescript").exists(),
                       reason="run `npm ci` in backends/typescript"),
]


def _typecheck(*files: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(SCRIPT), *map(str, files)], cwd=ROOT,
                          capture_output=True, text=True, timeout=600)


def test_every_rendering_type_checks():
    assert len(RENDERINGS) >= 2
    result = _typecheck()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (f"{len(RENDERINGS)} rendering(s) checked, 0 with errors"
            in result.stdout)


def test_a_type_error_in_a_rendering_is_reported(tmp_path):
    source = RENDERINGS[0].read_text(encoding="utf-8")
    broken = source.replace("return ctx.kv.read(k)", "return ctx.kv.read(42)", 1)
    assert broken != source
    # Same directory as the real renderings, so the relative runtime import
    # resolves and the only error is the one planted here.
    target = RENDERINGS[0].with_name(f"zz_broken_{tmp_path.name}.ts")
    target.write_text(broken, encoding="utf-8")
    try:
        result = _typecheck(target)
    finally:
        target.unlink()
    assert result.returncode == 1
    assert "1 rendering(s) checked, 1 with errors" in result.stdout
