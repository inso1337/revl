"""Issue #2596: `revl verify` must not turn a backend name read out of the
bundle under verification into a path it imports code from.

The backend keys of a bundle's own `runtime-manifest.json` are data inside the
artifact being checked. They are checked against the shipped backend list before
they are used for anything, and a key that is not on it is a MISMATCH on the
emitted-artifact tier that names it: never a lookup, never an import, and never
the quiet "cannot verify" an absent emitter gets.

Each name below is tried in three spellings: a plain unknown name, a relative
form and an absolute form. The two path-shaped ones point at a temporary
directory holding a module that writes a marker file when it is imported, so
"nothing was imported" is read off the filesystem and off `sys.modules`, not
inferred from the verdict.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revl import bundle as B  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from test_bundle import BASE, KEY, _tier, _write, signed_bundle  # noqa: E402,F401
from test_bundle_refusal_1400 import _manifest  # noqa: E402

FORMS = ("plain", "relative", "absolute")


def _planted(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """The three spellings of a name that is not a shipped backend, and the
    marker file that a module imported through the two path-shaped ones would
    write. The directory they name holds a real `emit.py`, so a loader that
    followed them would find something to import."""
    marker = tmp_path / "imported.marker"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "emit.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class EmitError(ValueError):\n    pass\n"
        "def emit(ir):\n    return ''\n",
        encoding="utf-8")
    keys = {
        "plain": "nosuchbackend",
        "relative": os.path.relpath(planted, B._BACKENDS_ROOT),
        "absolute": str(planted),
    }
    return keys, marker


def _record(bundle: Path, section: str, key: str) -> None:
    """Add `key` to the bundle's own manifest, the way a doctored bundle would."""
    manifest = _manifest(bundle)
    manifest.setdefault(section, {})[key] = (
        {"files": []} if section == "backends" else "recorded refusal")
    (bundle / B.RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _revl_modules() -> set[str]:
    return {name for name in sys.modules if name.startswith("revl_")}


# ------------------------------------------------------------ the emitted tier

@pytest.mark.parametrize("form", FORMS)
def test_an_unshipped_backend_key_is_a_mismatch_naming_it(
        form, signed_bundle, tmp_path):  # noqa: F811
    key = _planted(tmp_path)[0][form]
    _record(signed_bundle, "backends", key)

    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))

    tier = _tier(report, f"emitted [{key}]")
    assert tier is not None
    assert tier.status == B.MISMATCH, tier.detail
    assert key in tier.detail
    assert "not a backend this toolchain ships" in tier.detail
    assert not report.ok
    assert _tier(report, "reproducible").status == B.MISMATCH
    # the shipped tiers beside it are judged as they always were
    assert _tier(report, "emitted [python]").status == B.OK


@pytest.mark.parametrize("form", FORMS)
def test_an_unshipped_backend_key_imports_and_reads_nothing(
        form, signed_bundle, tmp_path):  # noqa: F811
    keys, marker = _planted(tmp_path)
    B.verify_bundle(str(signed_bundle), env=dict(KEY))  # the shipped emitters load now
    _record(signed_bundle, "backends", keys[form])
    before = _revl_modules()

    B.verify_bundle(str(signed_bundle), env=dict(KEY))

    assert not marker.exists(), "a module named by the manifest was imported"
    assert _revl_modules() == before


@pytest.mark.parametrize("form", FORMS)
def test_verify_exits_non_zero_for_an_unshipped_backend_key(
        form, signed_bundle, tmp_path, monkeypatch, capsys):  # noqa: F811
    keys, marker = _planted(tmp_path)
    key = keys[form]
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY["REVL_ATTEST_KEY"])
    _record(signed_bundle, "backends", key)

    assert main(["verify", str(signed_bundle)]) == 1
    out = capsys.readouterr().out
    assert key in out
    assert "not a backend this toolchain ships" in out
    assert not marker.exists(), "a module named by the manifest was imported"


# ------------------------------------------------------------ the refused tier

@pytest.mark.parametrize("form", FORMS)
def test_an_unshipped_recorded_refusal_key_is_a_mismatch_naming_it(
        form, signed_bundle, tmp_path):  # noqa: F811
    keys, marker = _planted(tmp_path)
    key = keys[form]
    _record(signed_bundle, "refusedBackends", key)

    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))

    tier = _tier(report, f"refused [{key}]")
    assert tier is not None
    assert tier.status == B.MISMATCH, tier.detail
    assert key in tier.detail
    assert not marker.exists(), "a module named by the manifest was imported"


# ------------------------------------------------------------------ _emitter

@pytest.mark.parametrize("form", FORMS)
def test_the_emitter_loader_refuses_the_name_before_any_path_is_built(
        form, tmp_path, monkeypatch):
    """Every caller goes through `_emitter`, so the refusal lives there, and it
    comes before the name is joined onto anything."""
    keys, marker = _planted(tmp_path)

    class NoPaths:
        def __truediv__(self, other):
            raise AssertionError("a path was built from the backend name")

    monkeypatch.setattr(B, "_BACKENDS_ROOT", NoPaths())
    with pytest.raises(RevlError) as caught:
        B._emitter(keys[form])
    assert keys[form] in caught.value.message
    assert not marker.exists()


def test_building_a_bundle_for_an_unshipped_backend_is_refused_by_name(tmp_path):
    src = _write(tmp_path, "app.rvl", BASE)
    with pytest.raises(RevlError) as caught:
        B.build_bundle([src], str(tmp_path / "out.revlbundle"),
                       backends=("nosuchbackend",), env=dict(KEY))
    assert "nosuchbackend" in caught.value.message


def test_a_refused_build_leaves_an_existing_output_directory_untouched(tmp_path):
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "out.revlbundle"
    out.mkdir()
    kept = out / "kept.txt"
    kept.write_text("an earlier bundle", encoding="utf-8")

    with pytest.raises(RevlError) as caught:
        B.build_bundle([src], str(out), backends=("python", "nosuchbackend"),
                       force=True, env=dict(KEY))

    assert "nosuchbackend" in caught.value.message
    assert kept.read_text(encoding="utf-8") == "an earlier bundle"
    assert sorted(p.name for p in out.iterdir()) == ["kept.txt"]


# ------------------------------------------------------------------- control

def test_a_shipped_backend_name_verifies_as_before(
        signed_bundle, monkeypatch, capsys):  # noqa: F811
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert report.ok, [c.detail for c in report.mismatches]
    for backend in B.DEFAULT_BACKENDS:
        assert _tier(report, f"emitted [{backend}]").status == B.OK, backend
    assert B._emitter("python") is not None

    monkeypatch.setenv("REVL_ATTEST_KEY", KEY["REVL_ATTEST_KEY"])
    assert main(["verify", str(signed_bundle)]) == 0
