"""Issue #3595: the host config encoding for a variant config value.

A field declared as a variant (`type Colour = Red | Green`) takes the wire tag
`{"$kind": "Green"}` (`"$value"` added for a payload case). The load-time check
(`composition._row_value_type` via `_variant_wire_type`) accepts exactly the
declared cases and refuses any other shape naming the field and the declared
type; at load the driver (`run._decode_wire_config`) rebuilds the case instance
from the emitted module, so `config.c == Green` compares like with like. The
config trace line carries the wire form (`runtime._flush_config_trace`).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.composition import configure_rows_from_host  # noqa: E402
from revl.errors import RevlError  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — run this file under "
           "`backends/python/.venv/bin/pytest`",
)

COLOUR = """\
type Colour = Red | Green
service S { fn green() -> Bool }
component C provides s: S {
  config { c: Colour }
  provide s { fn green() = config.c == Green }
}
"""

SHAPE = """\
type Shape = Point(Int) | Empty
service S { fn area() -> Int }
component C provides s: S {
  config { s: Shape }
  provide s {
    fn area() = match config.s {
      Point(n) => n
      Empty => 0
    }
  }
}
"""


def _call(tool: str, arguments: dict) -> dict:
    from revl.mcp.server import handle
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


# ------------------------------------------------------- load-time check (pure)


def _host_document():
    """A minimal admitted-document shape: one row placing `C`, whose config
    field `c` is declared `Colour` (a variant), plus the document's type
    table — what `configure_rows_from_host` reads."""
    return {
        "types": {"Colour": {"params": [], "kind": "variant", "cases": [
            {"name": "Red", "payload": None}, {"name": "Green", "payload": None}]}},
        "components": [{"name": "C", "provides": ["s"],
                        "config": [{"name": "c", "type": "Colour",
                                    "default": None}]}],
        "rows": {"rows": [{"label": "c", "component": "C", "config": {},
                           "source": "c.rvl"}]},
        "manifest": {"loadOrder": ["C"]},
    }


def test_host_config_accepts_the_wire_tag_for_a_variant_field():
    out = configure_rows_from_host(_host_document(),
                                   {"C": {"c": {"$kind": "Green"}}})
    row = out["rows"]["rows"][0]
    assert row["config"]["c"] == {"$kind": "Green"}


def test_host_config_refuses_an_unknown_case_naming_field_and_type():
    with pytest.raises(RevlError) as excinfo:
        configure_rows_from_host(_host_document(),
                                 {"C": {"c": {"$kind": "Blue"}}})
    err = excinfo.value
    assert "config field `c`" in err.message and "`Colour`" in err.message


def test_host_config_refuses_a_plain_string_for_a_variant_field():
    with pytest.raises(RevlError) as excinfo:
        configure_rows_from_host(_host_document(), {"C": {"c": "Green"}})
    assert "declared `Colour`" in excinfo.value.message
    assert "supplies `Str`" in excinfo.value.message


def test_host_config_refuses_the_old_tag_shape():
    with pytest.raises(RevlError) as excinfo:
        configure_rows_from_host(_host_document(),
                                 {"C": {"c": {"tag": "Green"}}})
    assert "declared `Colour`" in excinfo.value.message


# ------------------------------------------------------------- runtime (live)


@needs_runtime
def test_a_variant_config_value_loads_and_matches():
    loaded = _call("revl_load", {"source": COLOUR,
                                 "config": {"C": {"c": {"$kind": "Green"}}}})
    assert loaded["ok"] is True, loaded
    assert _call("revl_call", {"key": "s", "method": "green"})["result"] is True


@needs_runtime
def test_the_other_case_matches_false():
    loaded = _call("revl_load", {"source": COLOUR,
                                 "config": {"C": {"c": {"$kind": "Red"}}}})
    assert loaded["ok"] is True, loaded
    assert _call("revl_call", {"key": "s", "method": "green"})["result"] is False


@needs_runtime
def test_a_payload_case_decodes_its_value():
    loaded = _call("revl_load", {"source": SHAPE,
                                 "config": {"C": {"s": {"$kind": "Point",
                                                        "$value": 7}}}})
    assert loaded["ok"] is True, loaded
    assert _call("revl_call", {"key": "s", "method": "area"})["result"] == 7


@needs_runtime
def test_a_wrongly_typed_variant_config_is_refused_at_load():
    loaded = _call("revl_load", {"source": COLOUR,
                                 "config": {"C": {"c": {"$kind": "Blue"}}}})
    assert loaded["ok"] is False
    assert "declared `Colour`" in str(loaded)
