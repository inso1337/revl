"""Issue #2556, item 2: the backend name `truc reproduce` imports.

The emitted-artifact tier reads the backend names out of the registry entry's
`artifacts.json` and imports `backends.<name>.emit` for each. The name is the
registry's text, not the operator's, so it is checked against the backends the
product ships before any import: an unknown or path-shaped name is a refusal
that names the backend and the truc, and no module is imported for it.

These drive the real `reproduce()` over a throwaway copy of the repo registry,
the same way tests/test_truc_reproduce.py does. The positive controls pass on
the base as well: a shipped name is still imported and still reproduces.
"""

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revl.compiler import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.truc import reproduce as R  # noqa: E402

REGISTRY = ROOT / "registry"
COMPONENT = "user_cache"

# Not shipped (a near miss of a real name), and two path-shaped spellings that
# would be spliced into the dotted module path. Each ends up as an import of a
# `backends.<junk>.emit` module that does not exist, so on the base they read
# as an absent emitter (cannot verify, exit 0) instead of a refusal.
REFUSED_NAMES = ["py2", "../x", "backends.py"]

# The directory names under backends/, the names an entry records.
SHIPPED_NAMES = ["python", "typescript", "rust", "java", "go", "wasm"]


@pytest.fixture
def registry(tmp_path):
    dst = tmp_path / "registry"
    shutil.copytree(REGISTRY, dst)
    return dst


def _entry(registry: Path) -> Path:
    return registry / "components" / COMPONENT


def _record(registry: Path, name: str, digest: str = "00" * 32) -> None:
    _entry(registry).joinpath("artifacts.json").write_text(
        json.dumps({"backends": {name: {"sourceSha256": digest}}}))


def _tier(report, label: str):
    for c in report.checks:
        if c.tier == label:
            return c
    return None


def _artifact_checks(report):
    return [c for c in report.checks if c.tier.startswith(R.TIER_ARTIFACT)]


@pytest.fixture
def import_log(monkeypatch):
    """Every `backends.*` import `reproduce` attempts, recorded and passed
    through to the real import."""
    calls: list[str] = []
    real = importlib.import_module

    def spy(name, *args, **kwargs):
        if str(name).startswith("backends."):
            calls.append(str(name))
        return real(name, *args, **kwargs)

    monkeypatch.setattr(R.importlib, "import_module", spy)
    return calls


def _backend_modules() -> set[str]:
    return {m for m in sys.modules if m.startswith("backends.")}


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_an_unshipped_backend_name_is_refused_by_name(registry, import_log, name):
    """The refusal names the backend and the truc, and the tier is a MISMATCH
    (a recorded claim this toolchain cannot honour), not `cannot verify`."""
    _record(registry, name)
    before = _backend_modules()

    report = R.reproduce(COMPONENT, registry=str(registry))

    arts = _artifact_checks(report)
    assert len(arts) == 1, [(c.tier, c.status) for c in arts]
    art = arts[0]
    assert art.status == R.MISMATCH, (art.tier, art.status, art.detail)
    assert name in art.detail and COMPONENT in art.detail, art.detail
    assert not report.ok
    assert import_log == [], import_log
    assert _backend_modules() == before


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_the_cli_exits_non_zero_for_an_unshipped_backend(
        registry, import_log, name, capsys):
    _record(registry, name)
    code = R.run([COMPONENT, "--registry", str(registry)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "MISMATCH" in out and name in out
    assert import_log == [], import_log


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_the_emitter_loader_refuses_an_unshipped_name_without_importing(
        registry, import_log, name):
    """The loader itself, not only the tier above it: an unshipped name raises
    a refusal naming the backend before any import is attempted."""
    ir = compile_files([str(_entry(registry) / "component.rvl")])
    with pytest.raises(RevlError) as caught:
        R._emit_backend_source(name, ir)
    assert name in caught.value.message
    assert import_log == [], import_log


@pytest.mark.parametrize("name", SHIPPED_NAMES)
def test_a_shipped_name_is_still_imported(registry, monkeypatch, name):
    """Positive control, passes on the base: a shipped name reaches the import
    exactly as `backends.<name>.emit`, and an absent emitter there is still
    `cannot verify`, not a refusal."""
    seen: list[str] = []
    real = importlib.import_module

    def absent(module, *args, **kwargs):
        if not str(module).startswith("backends."):
            return real(module, *args, **kwargs)
        seen.append(module)
        raise ImportError(module)

    monkeypatch.setattr(R.importlib, "import_module", absent)
    _record(registry, name)

    report = R.reproduce(COMPONENT, registry=str(registry))

    art = _tier(report, f"{R.TIER_ARTIFACT} [{name}]")
    assert art is not None and art.status == R.UNVERIFIED, art
    assert seen == [f"backends.{name}.emit"]
    assert report.ok


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_an_unshipped_name_with_no_recorded_hash_is_still_refused(
        registry, import_log, name, capsys):
    """The name is checked before the record is read: an entry that records an
    unshipped backend with no `sourceSha256` is a refusal too, not a quiet
    `cannot verify` with exit 0."""
    _entry(registry).joinpath("artifacts.json").write_text(
        json.dumps({"backends": {name: {}}}))

    report = R.reproduce(COMPONENT, registry=str(registry))

    arts = _artifact_checks(report)
    assert len(arts) == 1, [(c.tier, c.status) for c in arts]
    assert arts[0].status == R.MISMATCH, (arts[0].status, arts[0].detail)
    assert name in arts[0].detail and COMPONENT in arts[0].detail
    assert not report.ok
    assert R.run([COMPONENT, "--registry", str(registry)]) == 1
    assert "MISMATCH" in capsys.readouterr().out
    assert import_log == [], import_log
