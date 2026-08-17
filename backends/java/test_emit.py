"""Java backend tests: IR v1 -> cordis4j (string-level; no JDK required)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import emit  # noqa: E402


def _ir(name: str = "user_cache") -> dict:
    return json.loads((ROOT / "examples" / f"{name}.ir.json").read_text())


def test_user_cache_emits_java_structure():
    src = emit.emit(_ir("user_cache"))
    assert "package revl;" in src
    assert "import io.cordis4j.core.ServiceKey;" in src
    assert "public interface Database" in src
    assert "public interface Cache" in src
    assert "implements Plugin" in src
    assert "ctx.get(Database.class)" in src
    assert "ctx.provide(ServiceKey.of(Cache.class)" in src
    assert "Disposables.composite(" in src
    assert "Disposables.of(() -> store.drop())" in src
    # effectful method body is stubbed, not silently dropped
    assert 'UnsupportedOperationException("effectful method body")' in src


def test_format_emits_string_format():
    ir = {
        "ir_version": 1,
        "services": {"Bus": {"methods": {"send": {
            "params": [{"name": "msg", "type": "Str"}], "returns": None, "emission": True}}}},
        "components": [{
            "name": "Notifier", "requires": {"bus": "Bus"}, "provides": {}, "config": [],
            "body": [{"step": "emit", "expr": {
                "kind": "call", "target": {"kind": "req", "name": "bus"}, "method": "send",
                "args": [{"kind": "format", "template": "hi $0", "args": [{"kind": "name", "id": "x"}]}]}}],
        }],
    }
    src = emit.emit(ir)
    assert 'String.format("hi %s", x)' in src


def test_rejects_v3():
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({"ir_version": 3, "components": [{"name": "X", "body": []}]})
