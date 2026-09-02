"""Packaging invariants for the published wheel (roadmap item 108).

The wheel used to pack `src/revl` alone, so every backend-touching command
(`revl run`, cross-tier `revl test`, placement, fault tests) broke once
installed from PyPI: they resolve emitters/runtimes under `backends/`, a path
that does not exist under site-packages. There was also no console script, so
`pip install revl` produced no `revl` command though the docs use one.

These tests pin the fix so it cannot silently regress:

  * `revl._backends_root()` resolves the emitters under BOTH layouts — the
    source checkout (sibling `backends/`) and the installed wheel (packaged
    `revl/backends/`);
  * `pyproject.toml` declares the console script and force-includes `backends`;
  * the actually-built wheel carries the entry point and the emitter tree
    (guarded — skips where the build tooling is unavailable).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import revl
from revl import _paths

_REPO = Path(__file__).resolve().parents[1]
_TIERS = ("python", "rust", "wasm", "go", "java", "typescript")


def test_backends_root_checkout_layout():
    """In a checkout, the resolver returns the real sibling `backends/`."""
    root = revl._backends_root()
    assert root == (_REPO / "backends")
    assert root.is_dir()
    for tier in _TIERS:
        assert (root / tier / "emit.py").is_file(), f"missing emitter for {tier}"


def test_backends_root_prefers_checkout_over_packaged(tmp_path, monkeypatch):
    """When both a checkout sibling and a packaged copy exist, checkout wins —
    a dev tree behaves exactly as it did before packaging."""
    pkg = tmp_path / "src" / "revl"
    pkg.mkdir(parents=True)
    (pkg / "backends").mkdir()              # packaged copy
    checkout = tmp_path / "backends"
    checkout.mkdir()                         # checkout sibling
    monkeypatch.setattr(_paths, "_PKG_DIR", pkg)
    assert _paths.backends_root() == checkout


def test_backends_root_falls_back_to_packaged(tmp_path, monkeypatch):
    """Installed wheel layout: no checkout sibling, so the packaged
    `revl/backends` under site-packages is returned."""
    pkg = tmp_path / "site-packages" / "revl"
    pkg.mkdir(parents=True)
    (pkg / "backends").mkdir()
    monkeypatch.setattr(_paths, "_PKG_DIR", pkg)
    # sibling of `revl` (…/site-packages/backends) does not exist
    assert _paths.backends_root() == pkg / "backends"


def _pyproject() -> dict:
    return tomllib.loads((_REPO / "pyproject.toml").read_text())


def test_pyproject_declares_console_script():
    """`pip install revl` must yield a `revl` command matching the docs."""
    scripts = _pyproject().get("project", {}).get("scripts", {})
    assert scripts.get("revl") == "revl.__main__:main"


def test_pyproject_declares_a_stability_posture():
    """Roadmap item 338: the packaging metadata must state a Development
    Status, so a consumer sees the stability posture from `pip show revl` /
    PyPI itself, not only a design note buried in the checkout."""
    classifiers = _pyproject()["project"].get("classifiers", [])
    assert any(c.startswith("Development Status ::") for c in classifiers), (
        "pyproject.toml [project.classifiers] must include a "
        "'Development Status ::' entry (roadmap 338)")


def test_pyproject_states_the_gate_api_stability_contract():
    """The promised dependency surface (`revl.gate`) and where its full
    contract lives must be discoverable directly from pyproject.toml, since
    that is the one file every consumer's tooling actually fetches — a
    design note under docs/design/ is not (docs/gate-dependency-contract.md
    is the consumer-facing version this comment must point at)."""
    text = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "revl.gate" in text
    assert "docs/gate-dependency-contract.md" in text


def test_pyproject_ships_backends_in_wheel():
    """The wheel target must force-include the `backends/` tree, else the
    installed package cannot resolve any emitter."""
    cfg = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    force = cfg.get("force-include", {})
    assert force.get("backends") == "revl/backends"


def test_entry_point_target_is_callable():
    """The console-script target `revl.__main__:main` must exist and be
    callable, so the generated launcher is not a dangling reference."""
    from revl.__main__ import main
    assert callable(main)


@pytest.mark.skipif(
    importlib.util.find_spec("build") is None
    or importlib.util.find_spec("hatchling") is None,
    reason="wheel build tooling (build + hatchling) not installed",
)
def test_built_wheel_carries_entrypoint_and_backends(tmp_path):
    """Build the real wheel and assert the artifact exposes the console script
    AND packs the emitter tree — the two failures item 108 fixes."""
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(tmp_path)],
        cwd=str(_REPO), capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"build failed:\n{proc.stdout}\n{proc.stderr}"
    wheels = list(tmp_path.glob("revl-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
        entry = next(n for n in names if n.endswith("entry_points.txt"))
        assert "revl = revl.__main__:main" in zf.read(entry).decode()
        for tier in _TIERS:
            assert f"revl/backends/{tier}/emit.py" in names, \
                f"wheel is missing the {tier} emitter"


def test_installed_layout_resolves_emitter(tmp_path, monkeypatch):
    """Simulate a site-packages install: a `revl` package dir with a packaged
    `backends/` and no checkout sibling. The resolver must find the emitter —
    the exact path that was broken pre-fix."""
    site = tmp_path / "revl"
    (site / "backends" / "python").mkdir(parents=True)
    (site / "backends" / "python" / "emit.py").write_text("# emitter\n")
    monkeypatch.setattr(_paths, "_PKG_DIR", site)
    root = _paths.backends_root()
    assert (root / "python" / "emit.py").is_file()
