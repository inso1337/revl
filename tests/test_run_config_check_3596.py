"""`revl run --config` checks the host config table the way a Session load does,
and a missing required field is refused at load on both paths (issue #3596).

`Session.load` folds and types its host table through
`composition.configure_rows_from_host`; `revl run` read `--config` straight
into the runtime, so a wrongly typed field failed inside activation and an
undeclared one was ignored. A required field (no default) of a component no
composition row places was refused by `revl run`'s preflight but loaded in a
session, and then failed at activation with a bare `AttributeError`.

The refusal and folding tests fail on the base commit. Three pass there and
pin what stays: a correct table still plans, `revl run`'s own required-config
preflight keeps its message, and a config-less session load still loads.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source, run as run_mod  # noqa: E402
from revl.errors import RevlError  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the component runs in a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

MODULE = """
service Database { fn url() -> Str  fn pool() -> Int }

component SqliteDatabase provides db: Database {
  config { url: Str, pool: Int = 4 }
  provide db {
    fn url() = config.url
    fn pool() = config.pool
  }
}
"""


def _run(tmp_path, capsys, config_json: str, files=None, plan=True):
    module = tmp_path / "m.rvl"
    module.write_text(MODULE, encoding="utf-8")
    cfg = tmp_path / "host.json"
    cfg.write_text(config_json, encoding="utf-8")
    args = types.SimpleNamespace(files=files or [str(module)],
                                 config=str(cfg), plan=plan, backend="py")
    code = run_mod.run_command(args)
    return code, capsys.readouterr().err


# (a) and (b): the module path, no composition row ---------------------------

def test_run_refuses_a_wrongly_typed_field_at_load(tmp_path, capsys):
    code, err = _run(tmp_path, capsys,
                     '{"SqliteDatabase": {"url": "u", "pool": "big"}}')
    assert code != 0
    assert "config field `pool` of component `SqliteDatabase`" in err
    assert "declared `Int`" in err


def test_run_refuses_an_undeclared_field_at_load(tmp_path, capsys):
    code, err = _run(tmp_path, capsys,
                     '{"SqliteDatabase": {"url": "u", "poool": 4}}')
    assert code != 0
    assert "`poool` is not a config field of `SqliteDatabase`" in err


def test_run_still_plans_a_correct_table(tmp_path, capsys):
    code, err = _run(tmp_path, capsys,
                     '{"SqliteDatabase": {"url": "u", "pool": 7}}')
    assert code == 0, err


# (c): a row-placed component's host table is folded into the row ------------

SERVICES = "service Database { fn url() -> Str  fn pool() -> Int }\n"

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
}
"""


def _admitted(tmp_path):
    from revl.composition import admit_composition
    (tmp_path / "services.rvl").write_text(SERVICES, encoding="utf-8")
    (tmp_path / "sqlite.rvl").write_text(SQLITE, encoding="utf-8")
    doc = tmp_path / "base.rvl"
    doc.write_text(BASE, encoding="utf-8")
    return admit_composition(str(doc), str(tmp_path))


def _run_document(tmp_path, capsys, monkeypatch, config_json: str):
    """`revl run` over an admitted composition document (an IR carrying
    `rows`), capturing the IR the plan is printed from.

    `compile_files` is patched to return the admitted document because no
    `revl run` CLI path yields an IR with `rows` today (`compile_files` never
    admits a composition, src/revl/run.py `run_command`); this pins what
    `run_command` does with one when it gets one."""
    document = _admitted(tmp_path)
    monkeypatch.setattr(run_mod, "compile_files", lambda files: document)
    seen = {}
    real = run_mod._print_plan

    def capture(ir, config, backend):
        seen["ir"], seen["config"] = ir, config
        return real(ir, config, backend)

    monkeypatch.setattr(run_mod, "_print_plan", capture)
    cfg = tmp_path / "host.json"
    cfg.write_text(config_json, encoding="utf-8")
    args = types.SimpleNamespace(files=[str(tmp_path / "base.rvl")],
                                 config=str(cfg), plan=True, backend="py")
    code = run_mod.run_command(args)
    return code, capsys.readouterr(), seen


def test_run_folds_a_host_table_into_the_rows_config(tmp_path, capsys,
                                                     monkeypatch):
    code, out, seen = _run_document(tmp_path, capsys, monkeypatch,
                                    '{"SqliteDatabase": {"pool": 9}}')
    assert code == 0, out.err
    (row,) = seen["ir"]["rows"]["rows"]
    assert row["config"] == {"url": "sqlite://row", "pool": 9}
    assert row["provenance"][-1] == {"level": 3, "layer": "<host>",
                                     "op": "configure"}
    assert run_mod.component_config(seen["ir"], seen["config"],
                                    "SqliteDatabase") == {
        "url": "sqlite://row", "pool": 9}


def test_run_refuses_a_wrongly_typed_field_for_a_row(tmp_path, capsys,
                                                     monkeypatch):
    code, out, _ = _run_document(tmp_path, capsys, monkeypatch,
                                 '{"SqliteDatabase": {"pool": "big"}}')
    assert code != 0
    assert "config field `pool` of row `@db`" in out.err
    assert "declared `Int`" in out.err


# (d): a missing required field is refused at load, in run and in a session --

def test_run_refuses_a_missing_required_field_naming_component_field_type(
        tmp_path, capsys):
    code, err = _run(tmp_path, capsys, '{"SqliteDatabase": {"pool": 5}}',
                     plan=False)
    assert code != 0
    assert 'SqliteDatabase is missing required config "url" (Str)' in err


@needs_cordis
def test_session_refuses_a_missing_required_field_at_load():
    """The host table names the component but leaves `url` out."""
    from revl.mcp.session import Session
    session = Session()
    with pytest.raises(RevlError) as caught:
        session.load(compile_source(MODULE), {"SqliteDatabase": {"pool": 5}})
    assert session._driver is None
    message = str(caught.value)
    assert ("component `SqliteDatabase` is missing required config "
            "`url` (`Str`)") in message
    assert "AttributeError" not in message


@needs_cordis
def test_a_config_less_session_load_with_a_required_field_still_loads():
    """A host config that does not name the component keeps today's behaviour:
    config-less callers (erase_report, change, interop) load such a module."""
    from revl.mcp.session import Session
    source = (ROOT / "examples" / "user_cache.rvl").read_text(encoding="utf-8")
    ir = compile_source(source, filename=str(ROOT / "examples" / "user_cache.rvl"))
    assert any(f.get("default") is None
               for c in ir.get("components") or []
               for f in c.get("config") or [])
    session = Session()
    session.load(ir, None)
    try:
        assert session._driver is not None
    finally:
        session.unload()


@needs_cordis
@pytest.mark.parametrize("config, expected", [
    ({"SqliteDatabase": {"pool": 5}},
     "component `SqliteDatabase` is missing required config `url` (`Str`)"),
    ({"SqliteDatabase": {"url": "u", "pool": "big"}},
     "config field `pool` of component `SqliteDatabase` is declared `Int`"),
])
def test_the_gauntlet_grades_a_refused_host_config_instead_of_raising(
        config, expected):
    """`gauntlet.run` promises a graded dossier, never a raised error: a host
    config the scratch load refuses is reported as an unavailable lifecycle."""
    from revl.mcp import gauntlet
    from revl.mcp.session import Session
    dossier = gauntlet.run(Session(), {"source": MODULE, "config": config})
    lifecycle = dossier["tested"]["lifecycle"]
    assert lifecycle["status"] == "unavailable"
    assert lifecycle["ran"] is False
    assert expected in lifecycle["reason"]
