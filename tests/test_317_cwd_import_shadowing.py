"""`python -m revl` must not import bare names out of the working directory.

Issue #317. `-m` makes CPython put the process's working directory at
`sys.path[0]`, ahead of site-packages, and nothing scrubbed it. Every bare-name
import the CLI makes after that resolved there first, so a `cordis.py` or
`yaml.py` sitting next to a composition was imported INSTEAD of the real
module: host Python running before any admission check, with the `.rvl` never
having to compile.

Covered:
  * `drop_cwd_entry()` removes the `-m`-style head entry, in both the `""` and
    the absolute-path spellings, and leaves an unrelated head alone;
  * a `yaml.py` in the working directory does not run, and the REAL PyYAML is
    what `revl import openapi` parses a `.yaml` document with;
  * a `cordis.py` next to a composition does not run under `revl run`;
  * every `-m` entry point the package ships calls the scrub.

The subprocess tests are the non-vacuity anchor: revert `drop_cwd_entry()` in
`src/revl/__main__.py` and both of them fail with the marker file present.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl._safepath import drop_cwd_entry  # noqa: E402

#: a shadow module that records that it was imported and then stops the
#: process, so a hijack cannot be mistaken for an ordinary failure
SHADOW = (
    "import pathlib\n"
    "pathlib.Path(__file__).with_name('HIJACKED').write_text('{name}')\n"
    "raise SystemExit(99)\n"
)

MINIMAL_OPENAPI = """\
openapi: 3.0.0
info:
  title: Shadowed
  version: 1.0.0
paths:
  /ping:
    get:
      operationId: ping
      responses:
        '200':
          description: ok
"""


def _run_module(module: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # The entry point under test is `-m`, so src/ has to reach the child some
    # other way than sys.path[0] — PYTHONPATH lands AFTER the injected working
    # directory, exactly as site-packages does for a real install.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.pop("PYTHONSAFEPATH", None)
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180)


@pytest.fixture
def saved_path():
    original = list(sys.path)
    yield
    sys.path[:] = original


def test_drop_cwd_entry_removes_the_empty_head(saved_path):
    sys.path.insert(0, "")
    assert drop_cwd_entry() is True
    assert sys.path[0] != ""


def test_drop_cwd_entry_removes_an_absolute_cwd_head(saved_path):
    # CPython writes `""` for `-m` on some versions and the absolute working
    # directory on others; both spellings must go.
    sys.path.insert(0, os.getcwd())
    assert drop_cwd_entry() is True
    assert sys.path[0] != os.getcwd()


def test_drop_cwd_entry_leaves_an_unrelated_head_alone(saved_path):
    sys.path.insert(0, str(ROOT / "src"))
    assert drop_cwd_entry() is False
    assert sys.path[0] == str(ROOT / "src")


def test_drop_cwd_entry_is_idempotent(saved_path):
    sys.path.insert(0, "")
    assert drop_cwd_entry() is True
    # A second call has nothing left to remove and must not eat a real entry.
    head = sys.path[0]
    drop_cwd_entry()
    assert sys.path[0] == head


def test_yaml_in_the_working_directory_does_not_shadow_the_real_pyyaml(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "yaml.py").write_text(SHADOW.format(name="yaml"), encoding="utf-8")
    (tmp_path / "spec.yaml").write_text(MINIMAL_OPENAPI, encoding="utf-8")

    proc = _run_module("revl", "import", "openapi", "spec.yaml", "-o", "out.rvl",
                       cwd=tmp_path)

    assert not (tmp_path / "HIJACKED").exists(), (
        f"the working directory's yaml.py ran: {proc.stdout}\n{proc.stderr}")
    # Positive half: the REAL PyYAML is what parsed the document, so the
    # importer got as far as emitting revl source from it.
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "ping" in (tmp_path / "out.rvl").read_text(encoding="utf-8")


def test_cordis_next_to_a_composition_does_not_run(tmp_path):
    (tmp_path / "cordis.py").write_text(SHADOW.format(name="cordis"), encoding="utf-8")
    (tmp_path / "app.rvl").write_text(
        (ROOT / "examples" / "counter_pair.rvl").read_text(encoding="utf-8"),
        encoding="utf-8")

    proc = _run_module("revl", "run", "app.rvl", "--once", cwd=tmp_path)

    assert not (tmp_path / "HIJACKED").exists(), (
        f"the working directory's cordis.py ran: {proc.stdout}\n{proc.stderr}")
    assert proc.returncode != 99, "the shadow module's exit code reached the caller"


def test_every_dash_m_entry_point_scrubs_the_working_directory():
    # A new `__main__.py` that forgets the call reopens the hole silently, so
    # the set of entry points is checked rather than a fixed list of three.
    entries = sorted((ROOT / "src" / "revl").rglob("__main__.py"))
    assert entries, "no `-m` entry points found — the glob is wrong"
    missing = [str(p.relative_to(ROOT)) for p in entries
               if "drop_cwd_entry()" not in p.read_text(encoding="utf-8")]
    assert not missing, f"entry points that never scrub sys.path[0]: {missing}"


def test_console_scripts_are_declared_so_a_no_window_invocation_exists():
    # `-m` cannot cover names resolved while `import revl` itself runs; the
    # console script has no window at all because it never puts the caller's
    # directory on sys.path. It must stay declared.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'revl = "revl.__main__:main"' in pyproject
