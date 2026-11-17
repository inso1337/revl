"""Host config for a component no composition row places is checked at load
against the component's declared config (issue #3535).

A row-placed component's host table is typed by `composition._check_config`
(issue #3484). A component loaded from a plain module has no row, and its host
table used to reach the runtime unchecked: a wrongly typed field failed later,
at activation, with an `AttributeError` naming neither the field nor the type,
and an undeclared field was silently ignored. Both are now refused before any
runtime starts, naming the component, the field and the declared type.

The refusal tests fail on the base commit (the load gets as far as the
runtime); the two loading tests pin that a well-typed table and no table still
start the component as before.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the component runs in a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SOURCE = """
service Database { fn url() -> Str  fn pool() -> Int }

component SqliteDatabase provides db: Database {
  config { url: Str = "x", pool: Int = 4 }
  provide db {
    fn url() = config.url
    fn pool() = config.pool
  }
}
"""


def _refused(config: dict) -> tuple[str, object]:
    from revl.mcp.session import Session
    session = Session()
    with pytest.raises(RevlError) as caught:
        session.load(compile_source(SOURCE), config)
    # refused at load, before any runtime started: the activation-time
    # `AttributeError` of the issue is no longer reachable from this path
    assert session._driver is None
    assert not isinstance(caught.value.__cause__, AttributeError)
    return str(caught.value), session


def _read(session, method: str):
    return session.call("db", method, [], raw=True)["result"]


def test_a_wrongly_typed_field_is_refused_naming_component_field_and_type():
    message, _ = _refused({"SqliteDatabase": {"pool": "big"}})
    assert "component `SqliteDatabase`" in message
    assert "config field `pool`" in message
    assert "declared `Int`" in message
    assert "AttributeError" not in message


def test_an_undeclared_field_is_refused_naming_it():
    message, _ = _refused({"SqliteDatabase": {"poool": 4}})
    assert "`poool` is not a config field of `SqliteDatabase`" in message


def test_a_correct_table_loads_and_the_provider_sees_its_values():
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(SOURCE),
                 {"SqliteDatabase": {"pool": 9, "url": "sqlite://host"}})
    try:
        assert _read(session, "pool") == 9
        assert _read(session, "url") == "sqlite://host"
    finally:
        session.unload()


def test_no_host_table_keeps_the_declared_defaults():
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(SOURCE), None)
    try:
        assert _read(session, "pool") == 4
        assert _read(session, "url") == "x"
    finally:
        session.unload()


SOURCE_NO_POOL = """
service Database { fn url() -> Str  fn pool() -> Int }

component SqliteDatabase provides db: Database {
  config { url: Str = "x" }
  provide db {
    fn url() = config.url
    fn pool() = 0
  }
}
"""


def test_a_swap_whose_successor_drops_a_host_config_field_is_refused():
    """The session's host config still sets `pool`, so a successor that no
    longer declares it is refused like a fresh load would be, and the running
    composition keeps serving with its config."""
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(SOURCE), {"SqliteDatabase": {"pool": 9}})
    try:
        with pytest.raises(RevlError) as caught:
            session.swap(compile_source(SOURCE_NO_POOL), agent_authored=False)
        assert "`pool` is not a config field of `SqliteDatabase`" in str(
            caught.value)
        assert _read(session, "pool") == 9
    finally:
        session.unload()
