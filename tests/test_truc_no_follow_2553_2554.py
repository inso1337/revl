"""truc refuses symlinks when it vendors (#2553) and when it writes project
files (#2554).

`commit_add` copies `component.rvl`, `manifest.json` and `dossier.json` out of
a registry entry, and `truc.lock`, `build/assembly.json` and `truc.toml` are
written by `commit_add`, `commit_rm` and `write_assembly`. Each of those paths
followed a link: an entry file that links outside the registry was copied into
the project, and a project file or `build/` that links outside the project made
truc create or overwrite a file on the operator's machine.

These drive the host entry points the `truc` console command calls (the same
ones `test_truc_name_jail.py` drives), against a local fixture registry and a
throwaway project, with no network and no cordis runtime.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from revl.truc import _host

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "readonly_database"
OUTSIDE_TEXT = "OUTSIDE-ORIGINAL\n"
OUTSIDE_JSON = '{"lockVersion": 0, "trucs": []}\n'
PLAN = json.dumps({"lockAdd": {"name": ENTRY, "registry": "local"}})
RM_PLAN = json.dumps({"name": ENTRY})


def _registry(tmp_path: Path) -> Path:
    reg = tmp_path / "reg"
    (reg / "components").mkdir(parents=True)
    shutil.copy(ROOT / "registry" / "index.json", reg / "index.json")
    shutil.copytree(ROOT / "registry" / "components" / ENTRY,
                    reg / "components" / ENTRY)
    (reg / "components" / ENTRY / "dossier.json").write_text('{"dossier": 1}\n')
    return reg


def _project(tmp_path: Path, reg: Path) -> Path:
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "truc.toml").write_text(
        '[assembly]\nname = "demo"\nentry = ["src/main.rvl"]\n\n'
        f'[registries]\nlocal = {{ path = "{reg}" }}\n\n[trucs]\n')
    return proj


@pytest.fixture
def world(tmp_path):
    reg = _registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    return tmp_path, reg, _project(tmp_path, reg), outside


def _vendored(proj: Path) -> bool:
    return (proj / "trucs" / ENTRY).exists()


def _refused_add(proj: Path, match: str = "symlink"):
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.commit_add(str(proj), PLAN)
    assert match in str(raised.value)
    return str(raised.value)


# ------------------------------------------------ #2553: vendored sources

@pytest.mark.parametrize("fname", ["manifest.json", "component.rvl",
                                   "dossier.json"])
def test_add_refuses_a_registry_file_that_links_outside(world, fname):
    tmp_path, reg, proj, outside = world
    secret = outside / "secret.txt"
    secret.write_text(OUTSIDE_TEXT)
    entry_file = reg / "components" / ENTRY / fname
    entry_file.unlink()
    entry_file.symlink_to(secret)

    message = _refused_add(proj)

    assert fname in message and "registry" in message
    assert not _vendored(proj), "part of the entry was vendored"
    assert not (proj / "truc.lock").exists()
    assert secret.read_text(encoding="utf-8") == OUTSIDE_TEXT


def test_add_refuses_a_dangling_registry_file_link(world):
    tmp_path, reg, proj, outside = world
    entry_file = reg / "components" / ENTRY / "dossier.json"
    entry_file.unlink()
    entry_file.symlink_to(outside / "gone.json")

    _refused_add(proj)

    assert not _vendored(proj)


def test_add_refuses_a_registry_entry_directory_that_is_a_link(world):
    tmp_path, reg, proj, outside = world
    real = tmp_path / "elsewhere"
    (reg / "components" / ENTRY).rename(real)
    (reg / "components" / ENTRY).symlink_to(real, target_is_directory=True)

    _refused_add(proj)

    assert not _vendored(proj)


def test_add_refuses_a_components_directory_that_is_a_link(world):
    tmp_path, reg, proj, outside = world
    real = tmp_path / "elsewhere"
    (reg / "components").rename(real)
    (reg / "components").symlink_to(real, target_is_directory=True)

    message = _refused_add(proj)

    assert "components" in message
    assert not _vendored(proj)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no os.mkfifo here")
def test_add_refuses_a_registry_source_that_is_not_a_regular_file(world):
    tmp_path, reg, proj, outside = world
    entry_file = reg / "components" / ENTRY / "dossier.json"
    entry_file.unlink()
    os.mkfifo(entry_file)

    message = _refused_add(proj, "not a regular file")

    assert "dossier.json" in message
    assert not _vendored(proj)


# --------------------------------------------- #2554: project file writes

def test_add_refuses_a_lock_that_links_outside(world):
    tmp_path, reg, proj, outside = world
    target = outside / "victim.lock"
    target.write_text(OUTSIDE_JSON)
    (proj / "truc.lock").symlink_to(target)

    _refused_add(proj, "truc.lock")

    assert target.read_text(encoding="utf-8") == OUTSIDE_JSON
    assert not _vendored(proj), "vendored before the lock was refused"


def test_add_refuses_a_dangling_lock_and_creates_nothing(world):
    tmp_path, reg, proj, outside = world
    target = outside / "never-created.lock"
    (proj / "truc.lock").symlink_to(target)

    _refused_add(proj, "truc.lock")

    assert not target.exists(), "the link's target was created outside"
    assert not _vendored(proj)


def test_add_refuses_a_truc_toml_that_links_outside(world):
    tmp_path, reg, proj, outside = world
    target = outside / "victim.toml"
    shutil.move(proj / "truc.toml", target)
    (proj / "truc.toml").symlink_to(target)
    before = target.read_text(encoding="utf-8")

    _refused_add(proj, "truc.toml")

    assert target.read_text(encoding="utf-8") == before
    assert not _vendored(proj)
    assert not (proj / "truc.lock").exists()


def test_rm_refuses_a_lock_that_links_outside(world):
    tmp_path, reg, proj, outside = world
    _host.commit_add(str(proj), PLAN)
    lock = proj / "truc.lock"
    target = outside / "victim.lock"
    shutil.move(lock, target)
    lock.symlink_to(target)
    before = target.read_text(encoding="utf-8")

    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.commit_rm(str(proj), RM_PLAN)

    assert "truc.lock" in str(raised.value)
    assert target.read_text(encoding="utf-8") == before
    assert _vendored(proj), "un-vendored before the lock was refused"


def test_rm_refuses_a_truc_toml_that_links_outside(world):
    tmp_path, reg, proj, outside = world
    _host.commit_add(str(proj), PLAN)
    toml = proj / "truc.toml"
    target = outside / "victim.toml"
    shutil.move(toml, target)
    toml.symlink_to(target)
    before = target.read_text(encoding="utf-8")

    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.commit_rm(str(proj), RM_PLAN)

    assert "truc.toml" in str(raised.value)
    assert target.read_text(encoding="utf-8") == before
    assert _vendored(proj)


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX modes only")
def test_the_lock_gets_the_mode_write_text_would_give_it(world):
    """mkstemp creates 0600; the renamed lock must be 0666 under the umask,
    which is what the plain write it replaces produced."""
    tmp_path, reg, proj, outside = world
    for umask, expected in ((0o077, 0o600), (0o022, 0o644)):
        lock = proj / "truc.lock"
        lock.unlink(missing_ok=True)
        old = os.umask(umask)
        try:
            _host.commit_add(str(proj), PLAN)
        finally:
            os.umask(old)
        assert lock.stat().st_mode & 0o777 == expected, oct(umask)
        _host.commit_rm(str(proj), RM_PLAN)


MANIFEST = json.dumps({"layers": []})


def test_assembly_refuses_a_build_directory_that_is_a_link(world):
    tmp_path, reg, proj, outside = world
    (proj / "build").symlink_to(outside, target_is_directory=True)

    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.write_assembly(str(proj), MANIFEST)

    assert "build" in str(raised.value) and "symlink" in str(raised.value)
    assert list(outside.iterdir()) == []


def test_assembly_refuses_a_dangling_assembly_json_link(world):
    tmp_path, reg, proj, outside = world
    (proj / "build").mkdir()
    target = outside / "never-created.json"
    (proj / "build" / "assembly.json").symlink_to(target)

    with pytest.raises(_host.TrucNameRefusal):
        _host.write_assembly(str(proj), MANIFEST)

    assert not target.exists()


# ------------------------------------------------------- positive control

def test_a_normal_add_assembly_and_rm_still_work(world):
    tmp_path, reg, proj, outside = world

    assert _host.commit_add(str(proj), PLAN) == "committed"
    vendored = proj / "trucs" / ENTRY
    for fname in ("component.rvl", "manifest.json", "dossier.json"):
        assert (vendored / fname).read_bytes() == \
            (reg / "components" / ENTRY / fname).read_bytes()
    lock = json.loads((proj / "truc.lock").read_text(encoding="utf-8"))
    assert [row["name"] for row in lock["trucs"]] == [ENTRY]
    assert f'{ENTRY} = {{ registry = "local" }}' in \
        (proj / "truc.toml").read_text(encoding="utf-8")
    assert not (proj / "truc.lock").is_symlink()
    assert [p.name for p in proj.iterdir() if p.name.startswith(".")] == [], \
        "the lock's temp file was left behind"

    assert _host.write_assembly(str(proj), MANIFEST) == "written"
    assert json.loads((proj / "build" / "assembly.json").read_text(encoding="utf-8")) == \
        json.loads(MANIFEST)
    assert _host.write_assembly(str(proj), MANIFEST) == "written"  # overwrite

    assert _host.commit_rm(str(proj), RM_PLAN) == "removed"
    assert not _vendored(proj)
    assert json.loads((proj / "truc.lock").read_text(encoding="utf-8"))["trucs"] == []
