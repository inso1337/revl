"""Issue #2552: `truc add` and `truc assemble` admit vendored text as an
untrusted author.

A vendored row is registry text, not the project's own source, so the gate
compiles it under the same untrusted-author profile the MCP server applies by
default: no host body, no self-chosen realm, no self-minted declassifier. A
host body is admitted only behind `--trust-host-code`, the flag `truc apply`
already takes, given on the command line for that one command. A flagged
`add` records the lift in the row's lock entry, bound to the pinned bytes, so
a later `assemble` admits exactly those bytes and nothing else; a lock row
that records no admission is re-admitted under the current rule.

Two layers. The gate half drives `_host.admit_all`, the function every truc
verb hands its ordered rows to, and needs no runtime, so it always runs. The
console half drives the real `truc` command against a throwaway local
registry and skips without the cordis-py runtime (never reported as passing).
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from revl.truc import _host

ROOT = Path(__file__).resolve().parents[1]
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

FLAG = "--trust-host-code"
#: Spellings an operator might reach for instead of the flag. None of them is
#: read: the flag is a command-line decision for one command, nothing else.
ENV_SPELLINGS = ("TRUC_TRUST_HOST_CODE", "REVL_TRUST_HOST_CODE",
                 "TRUST_HOST_CODE")


def _host_body(marker: Path) -> str:
    """A component whose provider reaches a raw `@py` host body. Admission
    compiles it; nothing here runs it (the marker proves that)."""
    return ("service Ops { emission[notify] fn stash(p: Str) }\n"
            "extern emission[notify] fn notify(p: Str) = @py { open("
            + json.dumps(str(marker)) + ", 'w').write(p) }\n"
            "component OpsProvider provides ops: Ops {\n"
            "  provide ops { fn stash(p) { emit notify(p) } }\n"
            "}\n")


PLAIN = ("service Notes { fn count() -> Int }\n"
         "component NotesProvider provides notes: Notes {\n"
         "  provide notes { fn count() = 0 }\n"
         "}\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _row(name: str, source: str) -> dict:
    return {"path": f"/__truc_candidate__/{name}/component.rvl",
            "source": source, "name": name}


def _lock(name: str, source: str, admitted: dict) -> str:
    return json.dumps({"lockVersion": 0, "registryIndexVersion": "2",
                       "trucs": [{"name": name, "registry": "local",
                                  "sourceHash": _sha(source),
                                  "admitted": admitted}]})


# ================================================================ the gate half

def test_vendored_host_body_is_refused_without_the_flag(tmp_path):
    """The defect: on base this row is admitted under operator trust."""
    marker = tmp_path / "ran"
    verdict = json.loads(_host.admit_all(json.dumps(
        [_row("pkg", _host_body(marker))])))
    assert verdict["ok"] is False, verdict
    assert verdict["failed"] == "pkg"
    assert FLAG in verdict["diagnostic"]
    assert verdict["admitted"] == []
    assert not marker.exists()


def test_the_flag_admits_the_row_and_names_it_on_stderr(tmp_path, capsys):
    marker = tmp_path / "ran"
    verdict = json.loads(_host.admit_all(
        json.dumps([_row("pkg", _host_body(marker))]), "", True))
    assert verdict["ok"] is True, verdict["diagnostic"]
    assert verdict["admitted"] == ["pkg"]
    assert verdict["trusted"] == ["pkg"]
    err = capsys.readouterr().err
    assert FLAG in err and '"pkg"' in err
    assert not marker.exists()


def test_a_plain_vendored_row_admits_with_no_flag(capsys):
    """Positive control: passes on base and after. A row with no host body is
    admitted under the profile, and nothing is reported as trusted."""
    verdict = json.loads(_host.admit_all(json.dumps([_row("notes", PLAIN)])))
    assert verdict["ok"] is True, verdict["diagnostic"]
    assert verdict["admitted"] == ["notes"]
    assert verdict.get("trusted", []) == []
    assert FLAG not in capsys.readouterr().err


def test_the_projects_own_entry_keeps_operator_trust(tmp_path):
    """Control: an entry row (no name) is the project's own source and is
    first-party, so its host body admits with no flag, before and after."""
    marker = tmp_path / "ran"
    entry = {"path": str(tmp_path / "main.rvl"),
             "source": _host_body(marker), "name": ""}
    verdict = json.loads(_host.admit_all(json.dumps([entry])))
    assert verdict["ok"] is True, verdict["diagnostic"]
    assert not marker.exists()


def test_a_lock_row_admitted_by_flag_admits_those_bytes(tmp_path, capsys):
    source = _host_body(tmp_path / "ran")
    lock = _lock("pkg", source, {"at": "2026-10-18T00:00:00Z",
                                 "indexVersion": "2",
                                 "admission": "trusted-host-code"})
    verdict = json.loads(_host.admit_all(json.dumps([_row("pkg", source)]),
                                         lock, False))
    assert verdict["ok"] is True, verdict["diagnostic"]
    assert verdict["trusted"] == ["pkg"]
    assert '"pkg"' in capsys.readouterr().err


def test_a_lock_row_with_no_admission_record_is_refused(tmp_path):
    """A row stamped `admitted` with no record of how is weaker than the
    current rule: it is re-admitted under the profile and refused."""
    source = _host_body(tmp_path / "ran")
    lock = _lock("pkg", source, {"at": "2026-10-18T00:00:00Z",
                                 "indexVersion": "2"})
    verdict = json.loads(_host.admit_all(json.dumps([_row("pkg", source)]),
                                         lock, False))
    assert verdict["ok"] is False
    assert verdict["failed"] == "pkg"


def test_a_lock_row_admitted_profiled_does_not_trust_a_host_body(tmp_path):
    source = _host_body(tmp_path / "ran")
    lock = _lock("pkg", source, {"at": "2026-10-18T00:00:00Z",
                                 "indexVersion": "2",
                                 "admission": "profiled"})
    verdict = json.loads(_host.admit_all(json.dumps([_row("pkg", source)]),
                                         lock, False))
    assert verdict["ok"] is False
    assert verdict["failed"] == "pkg"


def test_lock_trust_covers_only_the_pinned_bytes(tmp_path):
    """The record trusts the bytes the flag was given for. Other bytes under
    the same name are admitted as an untrusted author again."""
    trusted = _host_body(tmp_path / "ran")
    other = _host_body(tmp_path / "elsewhere")
    lock = _lock("pkg", trusted, {"at": "2026-10-18T00:00:00Z",
                                  "indexVersion": "2",
                                  "admission": "trusted-host-code"})
    verdict = json.loads(_host.admit_all(json.dumps([_row("pkg", other)]),
                                         lock, False))
    assert verdict["ok"] is False
    assert verdict["failed"] == "pkg"


def test_the_flag_is_not_read_from_the_environment(tmp_path, monkeypatch):
    for name in ENV_SPELLINGS:
        monkeypatch.setenv(name, "1")
    verdict = json.loads(_host.admit_all(json.dumps(
        [_row("pkg", _host_body(tmp_path / "ran"))])))
    assert verdict["ok"] is False
    assert verdict["failed"] == "pkg"


# ============================================================= the console half

_cli = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _truc(project: Path, *args: str, env_extra: dict | None = None
          ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_extra or {})
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


def _registry(tmp_path: Path, entries: dict[str, str]) -> Path:
    """A local registry directory holding `entries` ({name: source})."""
    reg = tmp_path / "registry"
    index = {"indexVersion": "2", "components": {}}
    for name, source in entries.items():
        base = reg / "components" / name
        base.mkdir(parents=True)
        (base / "component.rvl").write_text(source)
        (base / "manifest.json").write_text("{}\n")
        index["components"][name] = {
            "sourceHash": _sha(source), "manifestHash": _sha("{}\n"),
            "version": "1.0.0", "provides": {}, "requires": {},
            "capabilities": [], "emissions": 0}
    (reg / "index.json").write_text(json.dumps(index, indent=2))
    return reg


def _project(tmp_path: Path, registry: Path, assembly_extra: str = "") -> Path:
    proj = tmp_path / "app"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.rvl").write_text("// the project's own entry.\n")
    (proj / "truc.toml").write_text(
        '[assembly]\n'
        'name = "demo"\n'
        'entry = ["src/main.rvl"]\n'
        f'{assembly_extra}\n'
        '[registries]\n'
        f'local = {{ path = "{registry}" }}\n\n'
        '[trucs]\n')
    return proj


def _lock_row(proj: Path, name: str) -> dict:
    lock = json.loads((proj / "truc.lock").read_text(encoding="utf-8"))
    return next(r for r in lock["trucs"] if r["name"] == name)


@_cli
def test_cli_add_refuses_a_host_body_and_names_it(tmp_path):
    marker = tmp_path / "ran"
    proj = _project(tmp_path, _registry(tmp_path, {"pkg": _host_body(marker)}))
    r = _truc(proj, "add", "pkg")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stdout and '"pkg"' in r.stdout
    assert FLAG in r.stdout
    assert not (proj / "trucs" / "pkg").exists()
    assert not (proj / "truc.lock").exists()
    assert not marker.exists()


@_cli
def test_cli_add_with_the_flag_admits_and_records_it(tmp_path):
    marker = tmp_path / "ran"
    proj = _project(tmp_path, _registry(tmp_path, {"pkg": _host_body(marker)}))
    r = _truc(proj, "add", "pkg", FLAG)
    assert r.returncode == 0, r.stdout + r.stderr
    assert FLAG in r.stderr and '"pkg"' in r.stderr
    assert _lock_row(proj, "pkg")["admitted"]["admission"] == "trusted-host-code"
    assert not marker.exists()

    # the record carries the lift to a later assemble, with no flag
    r = _truc(proj, "assemble")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (proj / "build" / "assembly.json").exists()


@_cli
def test_cli_add_of_a_plain_component_needs_no_flag(tmp_path):
    proj = _project(tmp_path, _registry(tmp_path, {"notes": PLAIN}))
    r = _truc(proj, "add", "notes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lock_row(proj, "notes")["admitted"]["admission"] == "profiled"
    assert FLAG not in r.stderr
    r = _truc(proj, "assemble")
    assert r.returncode == 0, r.stdout + r.stderr


@_cli
def test_cli_assemble_refuses_a_row_with_no_admission_record(tmp_path):
    """A lock written before the rule (stamped `admitted`, no record of how)
    holding a host body: assemble refuses it, and only the flag lifts that."""
    source = _host_body(tmp_path / "ran")
    registry = _registry(tmp_path, {"pkg": source})
    proj = _project(tmp_path, registry)
    shutil.copytree(registry / "components" / "pkg", proj / "trucs" / "pkg")
    with (proj / "truc.toml").open("a", encoding="utf-8") as handle:
        handle.write('pkg = { registry = "local" }\n')
    (proj / "truc.lock").write_text(_lock(
        "pkg", source, {"at": "2026-10-18T00:00:00Z", "indexVersion": "2"}))

    r = _truc(proj, "assemble")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stdout and "pkg" in r.stdout
    assert not (proj / "build").exists()

    r = _truc(proj, "assemble", FLAG)
    assert r.returncode == 0, r.stdout + r.stderr
    assert FLAG in r.stderr and '"pkg"' in r.stderr


@_cli
def test_cli_flag_is_not_read_from_env_or_truc_toml(tmp_path):
    marker = tmp_path / "ran"
    registry = _registry(tmp_path, {"pkg": _host_body(marker)})
    proj = _project(tmp_path, registry,
                    assembly_extra='trust-host-code = true\n'
                                   'trust_host_code = true\n')
    r = _truc(proj, "add", "pkg",
              env_extra={name: "1" for name in ENV_SPELLINGS})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stdout and '"pkg"' in r.stdout
    assert not (proj / "truc.lock").exists()
