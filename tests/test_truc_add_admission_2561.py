"""Issues #2561 and #2556 (item 1): what truc reads before it admits.

A `[registries]` entry with `url` used to be read as a filesystem path: the
string was handed on as if it named a registry directory, so the source truc
read was not the one the operator wrote. Fetching a registry from a url is not
shipped, so a `url` entry is refused by name, before anything at that string is
touched, in both places truc reads `[registries]`: the manifest every verb
starts from, and the target registry `truc ship` publishes to. A `path` entry
is unchanged.

truc's own stage-0 lock pins every root it compiles, but `externs.rvl`, the
host surface every root imports, was unpinned. It is now pinned the same way,
as an import (it is not compiled as a root), and drift in it refuses to boot.

`truc rm` admits the remainder of the assembly, and a lock row written before
the admission record existed could only be lifted by `--trust-host-code`, which
`rm` did not take. It now takes the flag, for that one command, like `add` and
`assemble`.

The gate half drives `revl.truc._host` and `revl.truc._launcher` directly and
needs no runtime. The console half drives the real `truc` command and skips
without the cordis-py runtime (never reported as passing).
"""

import hashlib
import json
import os
import pathlib
import re
import shutil

import pytest

from revl.truc import _host
from test_truc_admit_profile_2552 import (CORDIS_PY, FLAG, PLAIN, _host_body,
                                          _registry, _sha, _truc)
from test_truc_bootstrap import TRUC, package  # noqa: F401

URL_REFUSAL = "fetching a registry from a url is not shipped"


def _planted(tmp_path: pathlib.Path) -> pathlib.Path:
    """A working registry, plus a ship policy, at the string a `url` entry
    names, so the base reads it as a registry."""
    planted = _registry(tmp_path / "planted", {"pkg": PLAIN})
    (planted / "policy.json").write_text(json.dumps({"ship": "none"}))
    return planted


def _toml_project(tmp_path: pathlib.Path, registry_line: str,
                  ship: str = "") -> pathlib.Path:
    proj = tmp_path / "app"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.rvl").write_text("// the project's own entry.\n")
    (proj / "truc.toml").write_text(
        '[assembly]\nname = "demo"\nentry = ["src/main.rvl"]\n\n'
        f"{ship}"
        f"[registries]\n{registry_line}\n\n[trucs]\n")
    return proj


# ======================================================= #2556 item 1: `url`

def test_a_url_registry_is_refused_by_name(tmp_path):
    """On the base the url string comes back as the `local` registry path."""
    planted = _planted(tmp_path)
    proj = _toml_project(tmp_path, f'local = {{ url = "{planted}" }}')
    with pytest.raises(_host.TrucNameRefusal) as excinfo:
        _host.toml_manifest(str(proj))
    text = str(excinfo.value)
    assert "'local'" in text
    assert URL_REFUSAL in text
    assert "it was not read" in text


def test_ship_refuses_a_url_registry_before_reading_it(tmp_path):
    """The second reader of `[registries]`: the registry `truc ship` targets.
    On the base the planted `policy.json` is read as that registry's policy."""
    planted = _planted(tmp_path)
    proj = _toml_project(tmp_path, f'community = {{ url = "{planted}" }}',
                         ship='[ship]\nregistry = "community"\n\n')
    with pytest.raises(_host.TrucNameRefusal) as excinfo:
        _host.ship_context(str(proj))
    text = str(excinfo.value)
    assert "'community'" in text
    assert URL_REFUSAL in text
    assert "it was not read" in text


def test_a_path_registry_resolves_as_before(tmp_path):
    """Control, passes on the base and after: a `path` entry resolves to the
    absolute directory it names, relative to the project."""
    proj = _toml_project(tmp_path, 'local = { path = "../registry" }')
    manifest = json.loads(_host.toml_manifest(str(proj)))
    expected = os.path.abspath(os.path.join(str(proj), "../registry"))
    assert manifest["registries"] == {"local": expected}
    assert manifest["registry"] == expected


# =================================== #2561 follow-up 3: externs.rvl is pinned

def _imports(lock: dict) -> dict[str, str]:
    return {row["path"]: row["sha256"] for row in lock.get("imports") or []}


def test_the_lock_pins_externs_rvl():
    lock = json.loads((TRUC / "truc.lock").read_text())
    pins = _imports(lock)
    assert "externs.rvl" in pins, lock.get("imports")
    digest = hashlib.sha256((TRUC / "externs.rvl").read_bytes()).hexdigest()
    assert pins["externs.rvl"] == digest
    # pinned as an import, not compiled as a ninth root
    assert "externs.rvl" not in [row["path"] for row in lock["files"]]


_USE_DECL = re.compile(r'^\s*use\s+"([^"]+)"', re.MULTILINE)


def test_every_use_in_trucs_components_is_pinned():
    """Static, no compile: each `use "<target>"` in a component, resolved
    relative to the importing file as the compiler resolves it, names a
    path the lock pins as a root (`files`) or as an import (`imports`)."""
    lock = json.loads((TRUC / "truc.lock").read_text())
    pins = ({row["path"]: row["sha256"] for row in lock["files"]}
            | _imports(lock))
    unpinned = []
    for component in sorted((TRUC / "components").rglob("*.rvl")):
        for target in _USE_DECL.findall(component.read_text()):
            rel = pathlib.PurePath(os.path.relpath(
                os.path.normpath(component.parent / target), TRUC)).as_posix()
            # A target the lock merely names is not pinned: the pin has to be
            # the sha256 of the bytes the compiler would load, which is what
            # the launcher checks at boot. A blank or drifting pin fails here.
            try:
                expected = hashlib.sha256(
                    (TRUC / rel).read_bytes()).hexdigest()
            except FileNotFoundError:
                unpinned.append(f"{component.name}: use {target!r} -> {rel}"
                                " is not a file truc ships")
                continue
            if pins.get(rel) != expected:
                unpinned.append(f"{component.name}: use {target!r} -> {rel}")
    assert unpinned == []


def test_drifting_externs_refuse_to_boot(package):  # noqa: F811
    from revl.truc import _launcher

    (package / "externs.rvl").write_text("// tampered\n")
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "hash drift" in str(excinfo.value)
    assert "externs.rvl" in str(excinfo.value)


# ============================================================= the console half

_cli = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


@_cli
def test_cli_add_from_a_url_registry_is_refused(tmp_path):
    """On the base the planted directory is read as the registry and `pkg` is
    vendored from it."""
    planted = _planted(tmp_path)
    proj = _toml_project(tmp_path, f'local = {{ url = "{planted}" }}')
    r = _truc(proj, "add", "pkg")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "'local'" in r.stderr
    assert URL_REFUSAL in r.stderr
    assert not (proj / "trucs").exists()
    assert not (proj / "truc.lock").exists()


@_cli
def test_cli_rm_takes_the_flag(tmp_path):
    """#2561 follow-up 1. The remainder after `rm notes` holds a host body
    whose lock row records no admission, so it admits only with the flag.
    Without it `rm` is refused and names the flag; with it `rm` succeeds."""
    source = _host_body(tmp_path / "ran")
    registry = _registry(tmp_path, {"pkg": source, "notes": PLAIN})
    proj = _toml_project(tmp_path, f'local = {{ path = "{registry}" }}')
    for name in ("pkg", "notes"):
        shutil.copytree(registry / "components" / name, proj / "trucs" / name)
    with (proj / "truc.toml").open("a") as handle:
        handle.write('pkg = { registry = "local" }\n'
                     'notes = { registry = "local" }\n')
    (proj / "truc.lock").write_text(json.dumps({
        "lockVersion": 0, "registryIndexVersion": "2",
        "trucs": [
            {"name": "pkg", "registry": "local", "sourceHash": _sha(source),
             "admitted": {"at": "2026-10-18T00:00:00Z", "indexVersion": "2"}},
            {"name": "notes", "registry": "local", "sourceHash": _sha(PLAIN)},
        ]}))

    r = _truc(proj, "rm", "notes")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stdout and FLAG in r.stdout
    assert (proj / "trucs" / "notes").exists()

    r = _truc(proj, "rm", "notes", FLAG)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (proj / "trucs" / "notes").exists()
    assert (proj / "trucs" / "pkg").exists()
    assert not (tmp_path / "ran").exists()
