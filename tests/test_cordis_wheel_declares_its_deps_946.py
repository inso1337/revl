"""The vendored cordis wheel must declare the dependencies it imports (#946).

`site/vendor/cordis-4.0.0-py3-none-any.whl` is the runtime the playground's
live mode boots compositions on under Pyodide, installed beside the revl wheel.
It shipped with a METADATA carrying only Metadata-Version/Name/Version/Summary/
Requires-Python: no `Requires-Dist`, no `Provides-Extra`. So it installed
cleanly and then failed on first use, because two of its modules import things
that are not there:

  * `cordis/include.py` imports `yaml` at module scope (its whole plugin-tree
    config surface is YAML), so `pyyaml` is a hard requirement;
  * `cordis/__init__.py` imports `.hmr` at module scope, so `watchdog` is one
    too, despite `hmr.py` documenting `cordis[hmr]` as an extra with an
    mtime-polling fallback: its `except ImportError` branch sets
    `FileSystemEventHandler = None` for a module-level
    `class _WatchHandler(FileSystemEventHandler)` to subclass, so the fallback
    raises `TypeError: NoneType takes no arguments`. Both requirements are
    therefore declared hard, and the extra is declared empty so that upstream's
    documented install form is at least not a pip warning.

`backends/python/setup.sh` already installs `pyyaml watchdog` literally, so the
requirement was known to the dev setup and simply absent from the artifact.

The declarations were settled by installing the wheel into a bare venv, not by
reading upstream's intent: without pyyaml `import cordis` dies in `include.py`,
with pyyaml and without watchdog it dies in `hmr.py`, with both it imports.
That is the loop this file pins, minus the venv.
The METADATA is a hardcoded string in `site/build.py` rather than read from the
clone's pyproject, which is how the two drifted apart with nothing to notice.

This file is that notice. It is static — stdlib `zipfile` over a committed
artifact, no toolchain, no PyYAML — so it rides the `frontend` job's plain
`pytest tests/ -q` and costs a PR nothing.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEEL = ROOT / "site" / "vendor" / "cordis-4.0.0-py3-none-any.whl"
DIST_INFO = "cordis-4.0.0.dist-info"
BUILD = ROOT / "site" / "build.py"
SETUP = ROOT / "backends" / "python" / "setup.sh"


def _build_module():
    spec = importlib.util.spec_from_file_location("_site_build_946", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _metadata() -> str:
    with zipfile.ZipFile(WHEEL) as whl:
        return whl.read(f"{DIST_INFO}/METADATA").decode("utf-8")


def _fields(text: str) -> dict[str, list[str]]:
    """Split an RFC 822 metadata block into field name -> repeated values."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.strip() or line[0] in " \t" or ":" not in line:
            continue
        name, _, value = line.partition(":")
        out.setdefault(name.strip(), []).append(value.strip())
    return out


def test_the_committed_wheel_declares_what_build_py_says_it_does():
    # The point of the fix: one source of truth. If someone edits the string in
    # site/build.py, the committed artifact has to be re-emitted in the same
    # change — the failure mode #946 was, with the drift going the other way.
    build = _build_module()
    assert _metadata() == build.CORDIS_METADATA
    with zipfile.ZipFile(WHEEL) as whl:
        assert whl.read(f"{DIST_INFO}/WHEEL").decode("utf-8") == build.CORDIS_WHEEL_META


def test_pyyaml_is_a_hard_requirement():
    fields = _fields(_metadata())
    # Unconditional: the value is exactly "pyyaml", with no marker on the line.
    assert [d for d in fields.get("Requires-Dist", []) if d == "pyyaml"] == ["pyyaml"], (
        fields.get("Requires-Dist")
    )


def test_watchdog_is_declared_hard_and_the_documented_extra_exists():
    fields = _fields(_metadata())
    # Hard, not conditional: `cordis/__init__.py` imports `.hmr` at module
    # scope, so an install without watchdog cannot even `import cordis`.
    # Declaring it under the extra instead is the plausible-looking version of
    # this fix that leaves the wheel broken; that is why this is asserted.
    assert [d for d in fields.get("Requires-Dist", []) if d == "watchdog"] == ["watchdog"], (
        fields.get("Requires-Dist")
    )
    assert not [
        d for d in fields.get("Requires-Dist", []) if d.startswith("watchdog") and "extra" in d
    ], fields.get("Requires-Dist")

    # `hmr.py` tells users to install `cordis[hmr]`. An undeclared extra is a
    # pip warning on every one of those installs; an empty one is not, and it
    # is where watchdog goes once upstream's fallback works.
    assert "hmr" in fields.get("Provides-Extra", []), fields.get("Provides-Extra")


def test_the_zero_dependency_install_upstream_documents_cannot_work_yet():
    # The evidence for declaring watchdog hard rather than as an extra, read
    # out of the wheel. If upstream makes the fallback real, this fails — and
    # that failure is the signal to move watchdog under the extra, which is
    # what `hmr.py`'s own docstring says it should be.
    with zipfile.ZipFile(WHEEL) as whl:
        init = whl.read("cordis/__init__.py").decode("utf-8")
        hmr = whl.read("cordis/hmr.py").decode("utf-8")

    assert re.search(r"(?m)^from \.hmr import ", init), (
        "__init__.py no longer imports hmr at module scope?"
    )
    assert re.search(r"(?m)^\s*FileSystemEventHandler = None$", hmr), (
        "hmr.py no longer has an ImportError fallback?"
    )
    # The fallback assigns None and the class statement subclasses it at module
    # scope, so the ImportError path raises instead of degrading to polling.
    assert re.search(r"(?m)^class _WatchHandler\(FileSystemEventHandler\):", hmr), (
        "_WatchHandler no longer subclasses the watchdog base at module scope?"
    )


def test_the_declared_dependencies_are_the_ones_the_wheel_actually_imports():
    # The evidence for the two lines above, read out of the wheel itself rather
    # than restated: if upstream ever makes the yaml import lazy or drops the
    # watchdog watcher, this is where the declaration gets revisited.
    with zipfile.ZipFile(WHEEL) as whl:
        include = whl.read("cordis/include.py").decode("utf-8")
        hmr = whl.read("cordis/hmr.py").decode("utf-8")

    assert re.search(r"(?m)^import yaml$", include), "include.py no longer needs pyyaml?"
    assert "cordis[hmr]" in hmr, "hmr.py no longer names its extra?"
    assert re.search(r"(?m)^\s*from watchdog\.", hmr), "hmr.py no longer needs watchdog?"


def test_the_dev_setup_and_the_artifact_agree_on_the_requirements():
    # setup.sh installs the two names literally (they are deliberately not
    # revl's `test` extra, per the comment there). That list is what the wheel
    # was missing; keep the two in step.
    setup = SETUP.read_text(encoding="utf-8")
    install = next(
        (ln for ln in setup.splitlines() if "uv pip install" in ln and "pyyaml" in ln),
        None,
    )
    assert install is not None, "setup.sh no longer installs pyyaml literally"
    assert "watchdog" in install, install

    fields = _fields(_metadata())
    declared = " ".join(fields.get("Requires-Dist", []))
    assert "pyyaml" in declared
    assert "watchdog" in declared


def test_the_record_matches_the_members_it_lists():
    # A wheel whose RECORD hashes disagree with its members fails to install
    # (`hash mismatch`). This one is edited in place, so the check earns its
    # keep: every member except RECORD must be listed once, with a correct
    # sha256 and byte count.
    with zipfile.ZipFile(WHEEL) as whl:
        names = whl.namelist()
        data = {name: whl.read(name) for name in names}
    record = data[f"{DIST_INFO}/RECORD"].decode("utf-8").splitlines()

    listed = [line.split(",")[0] for line in record]
    assert sorted(listed) == sorted(names), (set(names) ^ set(listed))
    assert listed.count(f"{DIST_INFO}/RECORD") == 1
    assert record[listed.index(f"{DIST_INFO}/RECORD")] == f"{DIST_INFO}/RECORD,,"

    for line in record:
        arcname, digest, size = line.split(",")
        if not digest:
            continue
        raw = data[arcname]
        assert int(size) == len(raw), arcname
        want = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        assert digest == f"sha256={want}", arcname


def test_the_filename_and_the_dist_info_agree():
    # `site/build.py` hardcodes both the dist-info directory and the output
    # filename, so a version bump that misses one produces a wheel pip cannot
    # read. Cheap to pin; free to keep pinned.
    fields = _fields(_metadata())
    assert fields["Name"] == ["cordis"]
    assert fields["Version"] == ["4.0.0"]
    assert WHEEL.name.startswith(f"cordis-{fields['Version'][0]}-")
    assert DIST_INFO == f"cordis-{fields['Version'][0]}.dist-info"
