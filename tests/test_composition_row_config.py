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
  * a value the host supplies at load wins over the row's, field by field, is
    typed against the declared config like a row value, and is recorded in the
    rows IR and the manifest with its provenance (426 §3.1 level 3);
  * a `--set @db.pool=...` invocation overlay reaches the running component;
  * applying a plan that re-loads the row's component, and swapping to another
    admitted document, start it with the rows in force (undo of a generation
    booted from a composition document does not re-admit it yet: an xfail);
  * two rows placing one component are refused, never blended.

The row-value tests fail on the base commit (the component reads its declared
default). `test_a_host_supplied_field_wins_over_the_rows` pins the precedence
and passes on base too, since the host table was all the runtime read.
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
    (tmp_path / "services.rvl").write_text(SERVICES, encoding="utf-8")
    (tmp_path / "sqlite.rvl").write_text(SQLITE, encoding="utf-8")
    clauses = ""
    if stack:
        (tmp_path / "layers").mkdir()
        (tmp_path / "layers" / "tune.rvl").write_text(TUNE, encoding="utf-8")
        clauses = '  stack "layers/tune.rvl"\n'
    doc = tmp_path / "base.rvl"
    doc.write_text(BASE % clauses, encoding="utf-8")
    return doc


def _started(tmp_path: Path, config: dict | None = None, stack: bool = False,
             overlay: dict | None = None):
    from revl.mcp.session import Session
    doc = _project(tmp_path, stack)
    document = admit_composition(str(doc), str(tmp_path), overlay)
    session = Session()
    session.load(document, config, origin={"files": [str(doc)]})
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


def test_a_host_field_is_recorded_in_the_rows_with_its_provenance(tmp_path):
    session = _started(tmp_path, {"SqliteDatabase": {"url": "sqlite://host"}})
    try:
        for table in (session.ir["rows"], session.ir["manifest"]["rows"]):
            (row,) = table["rows"]
            assert row["config"] == {"url": "sqlite://host"}
            assert row["provenance"][-1] == {"level": 3, "layer": "<host>",
                                             "op": "configure"}
    finally:
        session.unload()


def test_a_wrongly_typed_host_field_does_not_load(tmp_path):
    from revl.errors import RevlError
    from revl.mcp.session import Session
    document = admit_composition(str(_project(tmp_path)), str(tmp_path))
    session = Session()
    with pytest.raises(RevlError) as caught:
        session.load(document, {"SqliteDatabase": {"pool": "big"}})
    assert "config field `pool`" in str(caught.value)
    assert "declared `Int`" in str(caught.value)
    assert session._driver is None


def test_a_set_overlay_value_reaches_the_running_component(tmp_path):
    session = _started(tmp_path, overlay={("db", "pool"): 16})
    try:
        assert _read(session, "url") == "sqlite://row"
        assert _read(session, "pool") == 16
    finally:
        session.unload()


SQLITE_V2 = SQLITE.replace("fn url() = config.url",
                           'fn url() = "v2:" + config.url')


def test_an_applied_plan_reloads_the_component_with_its_row_config(tmp_path):
    from revl.apply import build_artifact
    from revl.plan import plan
    session = _started(tmp_path)
    try:
        (tmp_path / "sqlite_v2.rvl").write_text(SQLITE_V2, encoding="utf-8")
        result = plan(files=[str(tmp_path / "sqlite_v2.rvl")],
                      manifest=session.ir, include_ir=True)
        report = session.apply(build_artifact(result, session.ir))
        assert report["applied"] is True
        assert _read(session, "url") == "v2:sqlite://row"
        assert _read(session, "pool") == 4
    finally:
        session.unload()


def _swap_to_second(session, tmp_path: Path) -> None:
    second = tmp_path / "second"
    second.mkdir()
    doc = _project(second)
    doc.write_text(doc.read_text(encoding="utf-8").replace("sqlite://row", "sqlite://two"))
    session.swap(admit_composition(str(doc), str(second)), agent_authored=False)


def test_a_swap_starts_with_the_successor_documents_rows(tmp_path):
    session = _started(tmp_path)
    try:
        _swap_to_second(session, tmp_path)
        assert _read(session, "url") == "sqlite://two"
    finally:
        session.unload()


@pytest.mark.xfail(strict=True, reason=(
    "undo re-admits a generation through `persist._recompile`, which compiles "
    "its files as modules; a composition document compiles to no components "
    "there, so the restored generation is empty (rows and components alike)"))
def test_undo_restores_the_earlier_documents_rows(tmp_path):
    session = _started(tmp_path)
    try:
        _swap_to_second(session, tmp_path)
        session.undo()
        assert _read(session, "url") == "sqlite://row"
    finally:
        session.unload()


def test_two_rows_placing_one_component_are_refused(tmp_path):
    from revl.errors import RevlError
    from revl.mcp.session import Session
    document = admit_composition(str(_project(tmp_path)), str(tmp_path))
    (row,) = document["rows"]["rows"]
    twin = {**row, "label": "db2", "qualified": ".::@db2",
            "config": {"url": "sqlite://twin"}}
    document["rows"] = {**document["rows"], "rows": [row, twin]}
    session = Session()
    with pytest.raises(RevlError) as caught:
        session.load(document)
    assert "placed by 2 rows" in str(caught.value)
    assert session._driver is None
