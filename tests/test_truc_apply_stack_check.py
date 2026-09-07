"""`truc apply` and `truc stack check` — the composition-layer CLI verbs (426 S6).

426 decision 7 (§7): distribution is truc's and SEMANTICS are the composition's.
These two verbs are truc's distribution front doors onto `revl.composition`:

  truc stack check   resolve the entry composition's declared layer stack
                     HEADER-ONLY and report a collision before anything is
                     admitted (§8; the pure fold never calls the gate, §3.3).
  truc apply         resolve the layer stack and ADMIT it, with every gate
                     firing inside `revl.composition`: the mandatory truc.lock
                     pin (exit test 17) and the vendored-dir jail (exit test 15)
                     at resolution, the untrusted-author confinement profile
                     (exit test 13) at admission. On a clean admit the applied
                     manifest is written to build/assembly.json; on any refusal
                     build/ is untouched (all-or-nothing).

Two layers of coverage. The HOST-BODY tests exercise the Python behind the
externs directly (`revl.truc._host`) and need no runtime — the verbs delegate to
`revl.composition`, so this is the same engine every other 426 test drives. The
CLI tests drive the REAL `truc` binary end to end (the bootstrapped `.rvl`
toolchain), the same discipline as test_truc_add_assemble.py, and are gated on
the cordis-py runtime (they skip when it is absent, never reported as passing).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from revl.truc import _host

ROOT = Path(__file__).resolve().parents[1]
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

_SERVICES = "service Db { fn query(q: Str) -> Str }\n"
_PG_COMPONENT = (
    'use "services.rvl" { }\n'
    "component PgDb provides db: Db {\n"
    "  config { url: Str }\n"
    "  provide db { fn query(q) = q }\n"
    "}\n"
)
_BASE = """composition Demo {
  use "services.rvl"
  row @db from "trucs/pg_database/component.rvl" provides db
    config { url: "postgres://primary:5432/app" }
%(stack)s}
"""

_PINNED = {"lockVersion": 1,
           "trucs": [{"name": "pg_database", "sourceHash": "a" * 64}]}


def _project(tmp_path: Path, *, stack: str = "", lock: dict | None = _PINNED,
             extra_trucs: list[str] | None = None) -> Path:
    """A truc project whose single entry document is a composition vendoring
    `pg_database`. `truc.toml`'s [assembly].entry is what `apply`/`stack check`
    resolve — truc owns which document, the composition owns the semantics."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "services.rvl").write_text(_SERVICES)
    vendored = tmp_path / "trucs" / "pg_database"
    vendored.mkdir(parents=True, exist_ok=True)
    (vendored / "services.rvl").write_text(_SERVICES)
    (vendored / "component.rvl").write_text(_PG_COMPONENT)
    (tmp_path / "base.rvl").write_text(_BASE % {"stack": stack})
    (tmp_path / "truc.toml").write_text(
        '[assembly]\nname = "demo"\nentry = ["base.rvl"]\n')
    if lock is not None:
        rows = list(lock["trucs"]) + [
            {"name": n, "sourceHash": "b" * 64} for n in (extra_trucs or [])]
        (tmp_path / "truc.lock").write_text(
            json.dumps({"lockVersion": lock["lockVersion"], "trucs": rows},
                       indent=2))
    return tmp_path


def _add_metrics_kit(proj: Path) -> None:
    """A vendored layer that adds a second row, reading a component INSIDE its
    own truc directory (the honest jail case)."""
    kit = proj / "trucs" / "metrics_kit"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / "services.rvl").write_text("service Metrics { fn tick() -> Int }\n")
    (kit / "component.rvl").write_text(
        'use "services.rvl" { }\n'
        "component KitMetrics provides metrics: Metrics {\n"
        "  provide metrics { fn tick() = 1 }\n"
        "}\n")
    (kit / "layer.rvl").write_text(
        "layer MetricsKit for Demo {\n"
        '  add row @metrics from "component.rvl" provides metrics\n'
        "}\n")


def _add_evil_layer(proj: Path) -> None:
    """A vendored layer whose `from` climbs out of its own truc directory to
    launder a project source as its own row — the jail escape (§4.1)."""
    (proj / "secret.rvl").write_text(_PG_COMPONENT)
    evil = proj / "trucs" / "evil"
    evil.mkdir(parents=True, exist_ok=True)
    (evil / "layer.rvl").write_text(
        "layer Evil for Demo {\n"
        '  add row @stolen from "../../secret.rvl" provides db\n'
        "}\n")


# --------------------------------------------------------------------------- #
# `truc stack check` — header-only resolution, reported at edit time.
# --------------------------------------------------------------------------- #

def test_stack_check_renders_a_pinned_stack(tmp_path):
    """A clean, pinned layer stack resolves header-only: every row and its
    provenance trail render, no body is lowered, nothing is written."""
    proj = _project(tmp_path, stack='  stack "trucs/metrics_kit/layer.rvl"\n',
                    extra_trucs=["metrics_kit"])
    _add_metrics_kit(proj)
    report = json.loads(_host.stack_check(str(proj)))
    assert report["code"] == 0, report["message"]
    msg = report["message"]
    assert "PgDb" in msg and "KitMetrics" in msg
    assert "add by `MetricsKit`" in msg          # the provenance trail
    assert "no component body was lowered" in msg
    assert report["sources"] == ""               # a check writes nothing


def test_stack_check_refuses_an_unpinned_truc(tmp_path):
    """426 exit test 17, through the verb: a vendored truc the composition
    references with no truc.lock is refused at resolution (the 428 F3 gate)."""
    proj = _project(tmp_path, lock=None)
    report = json.loads(_host.stack_check(str(proj)))
    assert report["code"] == 1
    assert "unpinned truc" in report["message"]
    assert "pg_database" in report["message"]


def test_stack_check_refuses_the_vendored_dir_jail_escape(tmp_path):
    """426 exit test 15 (second half), through the verb: a stack layer whose
    `from` resolves outside its own truc's vendored directory is refused."""
    proj = _project(tmp_path, stack='  stack "trucs/evil/layer.rvl"\n',
                    extra_trucs=["evil"])
    _add_evil_layer(proj)
    report = json.loads(_host.stack_check(str(proj)))
    assert report["code"] == 1
    assert "outside" in report["message"]
    assert "trucs/evil/" in report["message"]


# --------------------------------------------------------------------------- #
# `truc apply` — resolve + admit + write, all-or-nothing.
# --------------------------------------------------------------------------- #

def test_apply_admits_a_pinned_composition_and_writes_the_assembly(tmp_path):
    """A clean, pinned composition admits through the gate and its manifest is
    written to build/assembly.json (the applied composition a reviewer reads)."""
    proj = _project(tmp_path)
    report = json.loads(_host.apply(str(proj), False))
    assert report["code"] == 0, report["message"]
    assert "applied" in report["message"]
    assert "MEASURED" in report["message"]       # confinement basis, not trusted
    # the planner writes the manifest carried on `sources`.
    _host.write_assembly(str(proj), report["sources"])
    assembly = proj / "build" / "assembly.json"
    assert assembly.exists()
    assert "PgDb" in assembly.read_text()


def test_apply_refuses_an_unpinned_truc_and_leaves_build_untouched(tmp_path):
    """All-or-nothing: an unpinned truc is refused and `sources` is "", so
    write_assembly is a no-op and build/ never appears (exit test 17)."""
    proj = _project(tmp_path, lock=None)
    report = json.loads(_host.apply(str(proj), False))
    assert report["code"] == 1
    assert "unpinned truc" in report["message"]
    assert report["sources"] == ""
    _host.write_assembly(str(proj), report["sources"])
    assert not (proj / "build").exists()


def test_apply_threads_trust_host_code_through(tmp_path):
    """`--trust-host-code` reaches `revl.composition` and changes the panel's
    trust basis (§8.8): the applied report leads with CLAIMED, not MEASURED."""
    proj = _project(tmp_path)
    report = json.loads(_host.apply(str(proj), True))
    assert report["code"] == 0, report["message"]
    assert "CLAIMED" in report["message"]
    assert "--trust-host-code" in report["message"]


def test_a_project_with_no_entry_document_is_refused(tmp_path):
    """truc owns which document is applied; a truc.toml naming no entry is a
    refusal, not a crash (the boundary stays total)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "truc.toml").write_text('[assembly]\nname = "demo"\nentry = []\n')
    for report_json in (_host.stack_check(str(tmp_path)),
                        _host.apply(str(tmp_path), False)):
        report = json.loads(report_json)
        assert report["code"] == 1
        assert "no composition document" in report["message"]


# --------------------------------------------------------------------------- #
# The real `truc` CLI, end to end (gated on the cordis-py runtime).
# --------------------------------------------------------------------------- #

_cli = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


@_cli
def test_cli_stack_check_reports_a_pinned_stack(tmp_path):
    proj = _project(tmp_path, stack='  stack "trucs/metrics_kit/layer.rvl"\n',
                    extra_trucs=["metrics_kit"])
    _add_metrics_kit(proj)
    result = _truc(proj, "stack", "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KitMetrics" in result.stdout


@_cli
def test_cli_stack_check_refuses_an_unpinned_truc(tmp_path):
    proj = _project(tmp_path, lock=None)
    result = _truc(proj, "stack", "check")
    assert result.returncode == 1, result.stdout
    assert "unpinned truc" in result.stdout


@_cli
def test_cli_apply_admits_and_writes_the_assembly(tmp_path):
    proj = _project(tmp_path)
    result = _truc(proj, "apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "applied" in result.stdout
    assembly = proj / "build" / "assembly.json"
    assert assembly.exists() and "PgDb" in assembly.read_text()


@_cli
def test_cli_apply_refuses_an_unpinned_truc_and_writes_nothing(tmp_path):
    proj = _project(tmp_path, lock=None)
    result = _truc(proj, "apply")
    assert result.returncode == 1, result.stdout
    assert "unpinned truc" in result.stdout
    assert not (proj / "build").exists()


@_cli
def test_cli_bare_stack_without_check_is_unknown(tmp_path):
    """`truc stack` with no `check` is an unknown command, not a partial verb."""
    proj = _project(tmp_path)
    result = _truc(proj, "stack")
    assert result.returncode == 2, result.stdout
    assert "unknown command" in result.stdout
