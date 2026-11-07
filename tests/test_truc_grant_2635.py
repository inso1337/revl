"""truc bounds a vendored row's reach to what its `[trucs]` entry grants (#2635).

`truc apply` confines a non-first-party row to the services its composition
grant allows. `truc add` had no bound at all: the add plan carries only
`{path, source, name}` and the vendored row compiled with the reach allowlist
off. This slice gives the operator one declaration per row in `truc.toml`:

    [trucs]
    user_cache = { registry = "local", grant = ["db"] }

`grant` names the row's own `requires` KEYS, the vocabulary `apply`'s grant
uses. The bound is fail-closed: an entry that grants nothing (or has no
`grant` key) admits nothing that requires a service, so the missing
declaration is refused rather than admitted unbounded. `truc add` admits the
candidate under that bound before anything is vendored, `truc assemble` and
`truc rm` re-apply the same declaration, and `--trust-host-code` trusts a
row's host code without widening what it may reach.

The host half drives `_host.admit_all` directly and needs no runtime; the CLI
half drives the real `truc` command and skips without the cordis-py runtime
(never reported as passing).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from revl.truc import _host

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

#: `user_cache` requires `db` (`registry/components/user_cache/component.rvl`),
#: so it is the row the bound can refuse.
REQUIRING = "user_cache"

ENTRY_SOURCE = """
service Database { fn ping() -> Bool }

component Db provides db: Database {
  provide db {
    fn ping() = true
  }
}
"""

CACHE_SOURCE = """
service Cache { fn get(k: Str) -> Opt[Str] }

component CacheKit requires db: Database provides cache: Cache {
  provide cache {
    fn get(k) = if (db.ping()) { Some(k) } else { None }
  }
}
"""


def _ordered(grant: list[str], name: str = "cache") -> str:
    """The two rows the gate admits: the project's own provider, then the
    vendored candidate requiring `db`."""
    return json.dumps([
        {"path": "/project/src/main.rvl", "source": ENTRY_SOURCE,
         "name": "", "grant": []},
        {"path": f"/__truc_candidate__/{name}/component.rvl",
         "source": CACHE_SOURCE, "name": name, "grant": grant},
    ])


# ================================================================ host half

def test_a_requiring_row_with_no_grant_is_refused():
    verdict = json.loads(_host.admit_all(_ordered([])))
    assert verdict["ok"] is False, verdict
    assert verdict["failed"] == "cache"
    assert "not in the granted set" in verdict["diagnostic"]
    assert "db" in verdict["diagnostic"]


def test_a_requiring_row_with_its_key_granted_admits():
    verdict = json.loads(_host.admit_all(_ordered(["db"])))
    assert verdict["ok"] is True, verdict["diagnostic"]


def test_trust_host_code_does_not_widen_the_grant():
    """The flag trusts a host body; it must not also erase the reach bound. A
    row whose host code needs the flag still gets only its granted keys."""
    verdict = json.loads(_host.admit_all(_ordered([]), trust_host_code=True))
    assert verdict["ok"] is False, verdict
    assert "not in the granted set" in verdict["diagnostic"]


def test_a_row_that_requires_nothing_needs_no_grant():
    """Fail-closed bounds reach, it does not refuse every vendored row: a pure
    row declares nothing to bound and admits under an empty grant."""
    ordered = json.dumps([
        {"path": "/project/src/main.rvl", "source": ENTRY_SOURCE,
         "name": "", "grant": []},
        {"path": "/__truc_candidate__/pure/component.rvl",
         "source": "service Ping { fn go() -> Int }\n"
                   "component Pure provides ping: Ping {\n"
                   "  provide ping { fn go() = 1 }\n"
                   "}\n",
         "name": "pure", "grant": []},
    ])
    verdict = json.loads(_host.admit_all(ordered))
    assert verdict["ok"] is True, verdict["diagnostic"]


# ================================================================ CLI half

def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


def _project(tmp_path: Path, entry: str) -> Path:
    """A fresh project whose `[trucs]` table holds `entry` verbatim."""
    proj = tmp_path / "app"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.rvl").write_text(
        "// the project's own composition entry (empty for this demo).\n")
    (proj / "truc.toml").write_text(
        '[assembly]\n'
        'name = "demo"\n'
        'entry = ["src/main.rvl"]\n\n'
        '[registries]\n'
        f'local = {{ path = "{REGISTRY}" }}\n\n'
        '[trucs]\n'
        f"{entry}\n")
    return proj


def _lock_row(proj: Path) -> dict:
    lock = json.loads((proj / "truc.lock").read_text(encoding="utf-8"))
    return next(r for r in lock["trucs"] if r["name"] == REQUIRING)


@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed "
                           "(run `sh backends/python/setup.sh`)")
def test_cli_add_with_an_empty_grant_is_refused_and_writes_nothing(tmp_path):
    proj = _project(tmp_path, f'{REQUIRING} = {{ registry = "local", grant = [] }}')

    r = _truc(proj, "add", REQUIRING)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stdout
    assert "not in the granted set" in r.stdout
    assert "db" in r.stdout
    # the refusal names the operator's fix
    assert "grant" in r.stdout and "truc.toml" in r.stdout
    # admitted before it joins: nothing is written
    assert not (proj / "trucs" / REQUIRING).exists()
    assert not (proj / "truc.lock").exists()


@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed "
                           "(run `sh backends/python/setup.sh`)")
def test_cli_add_without_a_grant_key_is_refused_fail_closed(tmp_path):
    proj = _project(tmp_path, f'{REQUIRING} = {{ registry = "local" }}')

    r = _truc(proj, "add", REQUIRING)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "not in the granted set" in r.stdout
    assert not (proj / "trucs" / REQUIRING).exists()


@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed "
                           "(run `sh backends/python/setup.sh`)")
def test_cli_add_with_the_granted_key_records_the_bound(tmp_path):
    proj = _project(tmp_path,
                    f'{REQUIRING} = {{ registry = "local", grant = ["db"] }}')

    r = _truc(proj, "add", REQUIRING)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "added" in r.stdout and REQUIRING in r.stdout
    assert (proj / "trucs" / REQUIRING / "component.rvl").exists()
    assert _lock_row(proj)["granted"] == ["db"]

    r = _truc(proj, "assemble")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "assembled" in r.stdout


@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed "
                           "(run `sh backends/python/setup.sh`)")
def test_cli_assemble_refuses_a_grant_removed_after_add(tmp_path):
    """The bound travels with the project, not only with the moment of add:
    narrowing the declaration later is enforced on the next assemble."""
    proj = _project(tmp_path,
                    f'{REQUIRING} = {{ registry = "local", grant = ["db"] }}')
    assert _truc(proj, "add", REQUIRING).returncode == 0
    assert _truc(proj, "assemble").returncode == 0

    toml = proj / "truc.toml"
    toml.write_text(toml.read_text(encoding="utf-8")
                    .replace('grant = ["db"]', "grant = []"))

    r = _truc(proj, "assemble")
    assert r.returncode == 1, r.stdout
    assert "not in the granted set" in r.stdout
    assert "db" in r.stdout
