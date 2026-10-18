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
    assert secret.read_text() == OUTSIDE_TEXT


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
