"""A component started from a composition row starts with that row's config
(issue #3484).

A row's `config { ... }` is checked against the component's declared config at
admission and carried into the admitted document's rows IR and its manifest.
The py runtime used to start the component with its own declared defaults and
nothing else, so a value written on the row never reached the running
component. What this suite pins, each through a live py session over the
admitted document:

  * a row's config value is what the component reads, and a field the row does
    not name keeps the component's declared default;
  * a value a stack layer sets with `configure` (a configure-only delta)
    reaches the running component;
  * a value the host supplies at load wins over the row's, field by field.

Every test fails on the base commit: the component reads its declared default
in place of the row's value.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import admit_composition  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the component runs in a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SERVICES = """
service Database { fn url() -> Str  fn pool() -> Int }
"""

SQLITE = """
use "services.rvl" { }
component SqliteDatabase provides db: Database {
  config { url: Str = "default://", pool: Int = 4 }
  provide db {
    fn url() = config.url
    fn pool() = config.pool
  }
}
"""

BASE = """
composition Demo {
  use "services.rvl"
  row @db from "sqlite.rvl" provides db
    config { url: "sqlite://row" }
    open   { url, pool }
%s}
"""

TUNE = """
layer Tune for Demo { configure @db with { pool: 32 } }
"""


def _project(tmp_path: Path, stack: bool = False) -> Path:
    (tmp_path / "services.rvl").write_text(SERVICES)
    (tmp_path / "sqlite.rvl").write_text(SQLITE)
    clauses = ""
    if stack:
        (tmp_path / "layers").mkdir()
        (tmp_path / "layers" / "tune.rvl").write_text(TUNE)
        clauses = '  stack "layers/tune.rvl"\n'
    doc = tmp_path / "base.rvl"
    doc.write_text(BASE % clauses)
    return doc


def _started(tmp_path: Path, config: dict | None = None, stack: bool = False):
    from revl.mcp.session import Session
    document = admit_composition(str(_project(tmp_path, stack)), str(tmp_path))
    session = Session()
    session.load(document, config)
    return session


def _read(session, method: str):
    return session.call("db", method, [], raw=True)["result"]


def test_a_row_config_value_is_what_the_component_reads(tmp_path):
    session = _started(tmp_path)
    try:
        assert _read(session, "url") == "sqlite://row"
        # a field the row does not name keeps the declared default
        assert _read(session, "pool") == 4
    finally:
        session.unload()


def test_a_configure_layer_value_reaches_the_running_component(tmp_path):
    session = _started(tmp_path, stack=True)
    try:
        assert _read(session, "url") == "sqlite://row"
        assert _read(session, "pool") == 32
    finally:
        session.unload()


def test_a_host_supplied_field_wins_over_the_rows(tmp_path):
    session = _started(tmp_path, {"SqliteDatabase": {"url": "sqlite://host"}})
    try:
        assert _read(session, "url") == "sqlite://host"
        assert _read(session, "pool") == 4
    finally:
        session.unload()
