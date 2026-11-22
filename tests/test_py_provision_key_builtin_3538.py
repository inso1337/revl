"""Issue #3538: a provision key that spells a name the python emitter emits
bare (`next`, `len`, ...) is published under its host key, not the
builtin-escaped python identifier, so the component loads and the runtime
lookup (`root.get(key)`) finds it. Only the python names derived from the key
(the provide class) are escaped; the key itself is a string handed to the
runtime."""

from __future__ import annotations

import importlib.util
import keyword
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load_by_path import load_by_path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading a live composition needs cordis-py",
)


def _emit():
    return load_by_path("revl_py_emit", _BACKEND / "emit.py")


def _keys() -> list[str]:
    emit = _emit()

    return sorted(k for k in emit._EMITTED_BUILTINS
                  if k.isidentifier() and not keyword.iskeyword(k)
                  and k[0].islower())


def _source(key: str) -> str:
    return (f"service V {{ fn value() -> Str }}\n"
            f"component Impl provides {key}: V {{\n"
            f"  provide {key} {{ fn value() = \"x\" }}\n}}\n")


@pytest.fixture
def session():
    from revl.mcp.session import Session  # noqa: PLC0415

    live = Session()
    try:
        yield live
    finally:
        if live.loaded:
            live.unload()


def test_builtin_spelled_keys_are_collected():
    keys = _keys()
    assert "next" in keys and "len" in keys


@pytest.mark.parametrize("key", _keys())
def test_builtin_spelled_key_loads_and_is_published_under_its_key(session, key):
    try:
        ir = compile_source(_source(key), "app.rvl")
    except Exception as exc:  # the checker refuses this spelling: not a provision key
        pytest.skip(f"checker refuses {key!r}: {exc}")
    emit = _emit()

    if key in emit._CONTEXT_MEMBERS:
        # `_revl_ctx.set(...)` is a cordis Context method: refused by name
        with pytest.raises(Exception, match="collides with a cordis-py"):
            session.load(ir)
        return
    state = session.load(ir)
    assert state["providedKeys"] == [key]
    assert session._driver.root.get(key) is not None


def _consumer_source(key: str) -> str:
    return (_source(key)
            + "service W { fn twice() -> Str }\n"
            + f"component User requires {key}: V provides out: W {{\n"
            + f"  provide out {{ fn twice() = {key}.value() + {key}.value() }}\n}}\n")


@pytest.mark.parametrize("key", _keys())
def test_builtin_spelled_key_is_required_and_read_under_its_key(session, key):
    emit = _emit()

    if key in emit._CONTEXT_MEMBERS:
        pytest.skip("a Context member is refused as a provision key")
    try:
        ir = compile_source(_consumer_source(key), "app.rvl")
    except Exception as exc:
        pytest.skip(f"checker refuses {key!r}: {exc}")
    state = session.load(ir)
    assert sorted(state["providedKeys"]) == sorted([key, "out"])
    assert session._driver.root.get("out") is not None
