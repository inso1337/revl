"""truc's name jail — GHSA-4pxr-phgp-rfq8, the end-to-end half.

Drives the real `truc` console command through the backend's own venv, against
a throwaway registry, to show the guard holds where it actually matters: at the
write. The unit half is `test_truc_name_jail.py`; these are the cases that
escaped the process.

Both cases below are reproduced from the advisory's PoC, and both are *silent*
failures without the guard — `add` and `rm` report success while the project has
done nothing it owns, and the TOML write leaves `truc.toml` unparseable, so not
even the remediating `rm` can read the project back.

Without the runtime these skip (never reported as passing). Set it up with
`sh backends/python/setup.sh`.
"""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

#: A lone provider: admits on its own and needs nobody, so a refusal in a test
#: below can only be the name guard and never the admission gate.
ENTRY = "readonly_database"

pytestmark = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


def _new_project(tmp_path: Path, registry: Path = REGISTRY,
                 extra_trucs: str = "") -> Path:
    proj = tmp_path / "app"
    (proj / "src").mkdir(parents=True)
    (proj / "trucs").mkdir()
    (proj / "src" / "main.rvl").write_text("// the project's own entry.\n")
    (proj / "truc.toml").write_text(
        '[assembly]\n'
        'name = "demo"\n'
        'entry = ["src/main.rvl"]\n\n'
        '[registries]\n'
        f'local = {{ path = "{registry}" }}\n\n'
        '[trucs]\n'
        f'{extra_trucs}')
    return proj


def _parses(proj: Path) -> bool:
    try:
        tomllib.loads((proj / "truc.toml").read_text())
    except tomllib.TOMLDecodeError:
        return False
    return True


# ------------------------------------------------ recall: a name is not a path

def test_rm_of_a_traversal_name_is_refused_and_deletes_nothing(tmp_path):
    """`rm ../../victim-dir` un-vendored a directory the project does not own.

    Note where the name comes from: the project's OWN `truc.toml` names the key,
    so this is reachable by editing a file truc does not author — and at HEAD the
    directory is gone before anything has read the name.
    """
    proj = _new_project(tmp_path, extra_trucs='"../../deep/victim-dir" = '
                                            '{ registry = "local" }\n')
    victim = tmp_path / "deep" / "victim-dir"
    victim.mkdir(parents=True)
    (victim / "KEEPME.txt").write_text("KEEPME\n")

    r = _truc(proj, "rm", "../../deep/victim-dir")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "victim-dir" in r.stderr
    assert (victim / "KEEPME.txt").read_text() == "KEEPME\n"
    assert sorted(p.name for p in (proj / "trucs").iterdir()) == []
    assert _parses(proj)


# ------------------------------------------- add: a name is not a destination

def _escaping_registry(tmp_path: Path) -> Path:
    """A registry whose index names an entry outside its own `components/` dir.

    `components/../../escaped` is the OS's path, not truc's: the registry reads
    `tmp_path/reg/escaped`, while the project would vendor into
    `tmp_path/escaped` — two different directories, neither of them the
    project's. The entry is real, so nothing on the content side notices.
    """
    reg = tmp_path / "reg" / "registry"
    (reg / "components").mkdir(parents=True)
    index = json.loads((REGISTRY / "index.json").read_text())
    index["components"] = {"../../escaped": dict(index["components"][ENTRY])}
    (reg / "index.json").write_text(json.dumps(index, indent=2))
    escaped = tmp_path / "reg" / "escaped"
    escaped.mkdir()
    for f in sorted((REGISTRY / "components" / ENTRY).iterdir()):
        if f.is_file():
            shutil.copy(f, escaped / f.name)
    return reg


def test_add_of_a_name_that_vendors_outside_the_project_is_refused(tmp_path):
    registry = _escaping_registry(tmp_path)
    proj = _new_project(tmp_path, registry=registry)
    outside = tmp_path / "escaped"

    r = _truc(proj, "add", "../../escaped")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "escaped" in r.stderr
    assert not outside.exists(), "the destination was outside the project"
    assert sorted(p.name for p in (proj / "trucs").iterdir()) == []
    # At HEAD this line did not parse: the bare key write left the project
    # unreadable, so `truc rm` could not have cleaned it up either.
    assert _parses(proj)
    assert "../../escaped" not in (proj / "truc.toml").read_text()


def test_add_refuses_a_trucs_directory_that_is_a_symlink(tmp_path):
    """`mkdir(exist_ok=True)` and `write_text` both follow a link, so a linked
    `trucs/` turns "vendor a truc" into a write into someone else's tree — with
    a name that is perfectly legitimate."""
    proj = _new_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(proj / "trucs")
    (proj / "trucs").symlink_to(outside, target_is_directory=True)

    r = _truc(proj, "add", ENTRY)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "symlink" in r.stderr
    assert sorted(p.name for p in outside.iterdir()) == []


# --------------------------------- the destination FILES are part of the jail
#
# `_vendor_dir` says the destination DIRECTORY is the project's own; it says
# nothing about the entries inside it, and `write_text` follows a link. With a
# legitimate name and a real `trucs/<name>/`, a `component.rvl` planted as a
# symlink was written through to its target with `rc=0` and "vendored into
# trucs/" on stdout: a containment claim the code never derived.

def test_add_refuses_a_symlinked_component_and_leaves_the_target_alone(tmp_path):
    """THE BLOCKING CASE. Existing target: without the guard the registry
    source lands in `victim-existing.txt`, outside the project, and `add`
    reports success."""
    proj = _new_project(tmp_path)
    victim = tmp_path / "victim-existing.txt"
    victim.write_text("ORIGINAL-VICTIM\n")
    (proj / "trucs" / ENTRY).mkdir()
    (proj / "trucs" / ENTRY / "component.rvl").symlink_to(victim)

    r = _truc(proj, "add", ENTRY)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "symlink" in r.stderr
    assert victim.read_text() == "ORIGINAL-VICTIM\n"
    assert not (proj / "truc.lock").exists(), "the lock was written anyway"
    assert f'{ENTRY} = {{ registry = "local" }}' not in \
        (proj / "truc.toml").read_text()
    assert _parses(proj)


def test_add_refuses_a_dangling_symlinked_manifest_and_creates_nothing(tmp_path):
    """The dangling half: `exists()` is False through a broken link, so the
    guard has to be `is_symlink()` (or `O_NOFOLLOW`), never `exists()`. Without
    it `write_text` CREATES `victim-dangling.txt`, outside the project."""
    proj = _new_project(tmp_path)
    victim = tmp_path / "victim-dangling.txt"
    (proj / "trucs" / ENTRY).mkdir()
    (proj / "trucs" / ENTRY / "manifest.json").symlink_to(victim)

    r = _truc(proj, "add", ENTRY)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "symlink" in r.stderr
    assert not victim.exists(), "the link's target was created outside the project"
    # Every destination is checked before the first write, so the sibling that
    # is NOT a link was not vendored either: no half-vendored truc dir.
    assert not (proj / "trucs" / ENTRY / "component.rvl").exists()
    assert not (proj / "truc.lock").exists()
    assert _parses(proj)


def test_add_refuses_a_name_that_is_a_regular_file(tmp_path):
    """`trucs/<name>` as a regular file is not a destination this project owns.
    Left alone, `mkdir` dies on it and `add` reports an `OSError` traceback
    rather than a refusal."""
    proj = _new_project(tmp_path)
    squatter = proj / "trucs" / ENTRY
    squatter.write_text("not a directory\n")

    r = _truc(proj, "add", ENTRY)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "not a directory" in r.stderr
    assert squatter.read_text() == "not a directory\n"
    assert not (proj / "truc.lock").exists()
    assert _parses(proj)


# ------------------------------------------------------------ the happy path

def test_a_plain_add_then_rm_still_round_trips(tmp_path):
    """The guard must cost nothing: an ordinary name still vendors, still lands
    in `trucs/`, is still pinned, and the TOML keeps the spelling projects
    already contain."""
    proj = _new_project(tmp_path)

    r = _truc(proj, "add", ENTRY)
    assert r.returncode == 0, r.stderr or r.stdout
    assert (proj / "trucs" / ENTRY / "component.rvl").exists()
    assert f'{ENTRY} = {{ registry = "local" }}' in \
        (proj / "truc.toml").read_text()
    assert _parses(proj)

    r = _truc(proj, "rm", ENTRY)
    assert r.returncode == 0, r.stderr or r.stdout
    assert not (proj / "trucs" / ENTRY).exists()
    assert ENTRY not in (proj / "truc.toml").read_text()
    assert _parses(proj)
