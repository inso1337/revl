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
  * `pyproject.toml` declares the console script and maps `backends` into
    `revl/backends`;
  * the actually-built wheel carries the entry point and the emitter tree
    (guarded — skips where the build tooling is unavailable).

GHSA-gj88-cx6q-38r2 added the other half of the question. Item 108 asked "does
the wheel contain enough?"; the advisory asked "does it contain ONLY that?" —
`force-include` shipped the builder's `node_modules`, cargo `target/` and test
key material, and the artifact stopped being a function of its commit. The
static half of that is pinned below; the built-artifact half is
`tools/check_wheel_manifest.py`, which runs in `lint`, in the release dry run
and immediately before the PyPI upload.
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
    """The build hook must map the `backends/` tree into `revl/backends`, else
    the installed package cannot resolve any emitter."""
    sys.path.insert(0, str(_REPO))
    import hatch_build

    assert hatch_build.TREES["backends"] == "revl/backends"
    assert hatch_build.TREES["stdlib"] == "revl/stdlib"
    hook = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["hooks"]["custom"]
    assert hook["path"] == "hatch_build.py"


def test_wheel_target_keeps_packages_so_editable_installs_work():
    """`sources` is the tidier way to place `backends/` inside the wheel and it
    cannot be used: a rewrite that CHANGES a prefix rather than removing one
    makes dev-mode installs impossible ("Dev mode installations are unsupported
    when any path rewrite in the `sources` option changes a prefix"). Eleven CI
    jobs and both setup docs run `pip install -e ".[test]"`, so a `sources`
    entry for `backends` would break every one of them."""
    cfg = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert cfg.get("packages") == ["src/revl"]
    for src in cfg.get("sources", {}):
        assert src == "src/revl", (
            f"a sources rewrite for {src!r} breaks `pip install -e .`; place the "
            "tree through hatch_build.py instead")


def test_pyproject_never_force_includes_again():
    """GHSA-gj88-cx6q-38r2. `backends`/`stdlib` were force-included, and
    hatchling's `force-include` runs AFTER file selection: it is exempt from
    every exclude rule and from the ignore files, so it copied whatever sat on
    the builder's disk. A developer wheel came out at 2933 members / 250 MB
    unpacked against the 523 the commit describes, carrying `node_modules`,
    cargo `target/`, a cloned `.cordis-py`, compiled runner binaries and the
    generated key material `.gitignore` parks under
    `backends/typescript/test-secret-store/` — so the published artifact was a
    function of the builder's filesystem rather than of the revision.

    `tools/check_wheel_manifest.py` is the gate that holds the built wheel to
    `git ls-files`; this is the cheap static half, and it is here rather than
    only in that tool because reaching for `force-include` is the specific
    mistake that reintroduces the defect."""
    cfg = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert not cfg.get("force-include"), (
        "the wheel target force-includes a directory again; hatch_build.py "
        "computes the entry list per file from git ls-files instead, so add "
        "paths there (GHSA-gj88-cx6q-38r2)")


def test_build_excludes_the_artefact_classes():
    """The exclude list states, in the packaging file itself, what may never
    ship. It is deliberately redundant with `.gitignore` — hatchling reads only
    the ROOT ignore file, and this repo has sixteen nested ones — so a relaxed
    ignore rule elsewhere cannot widen the sdist, and it is what
    `hatch_build.py` falls back to when there is no git to ask."""
    exclude = set(_pyproject()["tool"]["hatch"]["build"].get("exclude", []))
    for pattern in (
        "**/node_modules",
        "**/target",
        "**/.cordis-py",
        "backends/typescript/test-secret-store",
    ):
        assert pattern in exclude, f"wheel exclude lost {pattern!r}"


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
