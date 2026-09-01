"""Cross-tier extern config coeffect (roadmap item 378, Stage 5).

Item 378 landed the extern `config { ... }` coeffect PY-ONLY (option (b) of
docs/design/378-sync-extern-service-reach.md): a document-global sync extern
carries a typed config schema, resolved once at plug time from the composition
config map and bound in the @py body as `_revl_config`, so the body reads
static config instead of env vars. Stage 5 grows the same seam to the other
tiers.

These tests pin the cross-tier behavior:

- ts / go / java / rust each emit a module/class-global config map
  (`_REVL_EXTERN_CONFIG` / `_revl_extern_config_store`) plus a fail-loud lookup,
  and bind `_revl_config` as the first line of a config extern's body.
- a no-config extern stays byte-identical on every tier (the additivity mandate:
  no config map, no `_revl_config`, emitted exactly as before).
- wasm stays a genuine conformance gap (raw WAT, scalar-only spawn-time config
  import, no plug-time config dict), refused at compile.
- the rust seam is string-valued, so a non-`Str` config field on a @rs body is
  refused LOUDLY rather than silently narrowed.

The py reference lives in tests/test_extern_config.py and
tests/test_extern_config_tier_gate.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from _backend_import import backend_emitter  # noqa: E402


def _str_config_extern(tier: str, read: str) -> str:
    # a Str-only config extern, portable to any seamful tier: provider is
    # required, model is defaulted.
    return (
        f'extern emission fn author(body: Str) -> Str\n'
        f'  config {{ provider: Str, model: Str = "opus" }}\n'
        f'  = @{tier} {{ {read} }}\n'
    )


def _no_config_extern(tier: str, body: str) -> str:
    return f'extern emission fn plain(x: Str) -> Str = @{tier} {{ {body} }}\n'


def _emit(backend: str, src: str) -> str:
    ir = compile_source(src)
    return backend_emitter(backend).emit(ir)


# -- ts ---------------------------------------------------------------------

def test_ts_config_extern_binds_revl_config():
    out = _emit("typescript",
                _str_config_extern("ts", 'return _revl_config["provider"] as string'))
    assert "export const _REVL_EXTERN_CONFIG" in out
    assert "function _revlExternConfig(" in out
    assert ('const _revl_config = _revlExternConfig("author", '
            '["provider"], {"model": "opus"});') in out
    assert "throw new Error(" in out  # fail-loud


def test_ts_no_config_extern_is_byte_identical():
    with_seam = _emit("typescript", _no_config_extern("ts", "return x"))
    assert "_REVL_EXTERN_CONFIG" not in with_seam
    assert "_revl_config" not in with_seam
    assert "export function plain(x: string): string {" in with_seam


# -- go ---------------------------------------------------------------------

def test_go_config_extern_binds_revl_config():
    out = _emit("go",
                _str_config_extern("go", 'return _revl_config["provider"].(string)'))
    assert "var _REVL_EXTERN_CONFIG = map[string]map[string]any{}" in out
    assert "func _revlExternConfig(" in out
    assert ('_revl_config := _revlExternConfig("author", []string{"provider"}, '
            'map[string]any{"model": "opus"})') in out
    assert "panic(" in out


def test_go_no_config_extern_is_byte_identical():
    out = _emit("go", _no_config_extern("go", "return x"))
    assert "_REVL_EXTERN_CONFIG" not in out
    assert "_revl_config" not in out


# -- java -------------------------------------------------------------------

def test_java_config_extern_binds_revl_config():
    out = _emit("java",
                _str_config_extern("java", 'return (String) _revl_config.get("provider");'))
    assert "_REVL_EXTERN_CONFIG = new java.util.HashMap<>();" in out
    assert "static java.util.Map<String, Object> _revlExternConfig(" in out
    assert 'java.util.Map<String, Object> _revl_config = _revlExternConfig(' in out
    assert "throw new RuntimeException(" in out


def test_java_no_config_extern_is_byte_identical():
    out = _emit("java", _no_config_extern("java", "return x;"))
    assert "_REVL_EXTERN_CONFIG" not in out
    assert "_revl_config" not in out


# -- rust -------------------------------------------------------------------

def test_rust_config_extern_binds_revl_config():
    out = _emit("rust",
                _str_config_extern("rs", 'format!("{}", _revl_config["provider"])'))
    assert "fn _revl_extern_config_store()" in out
    assert "fn _revl_extern_config(" in out
    assert ('let _revl_config = _revl_extern_config("author", &["provider"], '
            '&[("model", "opus")]);') in out
    assert "panic!(" in out


def test_rust_no_config_extern_is_byte_identical():
    out = _emit("rust", _no_config_extern("rs", "x"))
    assert "_revl_extern_config" not in out
    assert "_revl_config" not in out


def test_rust_non_str_config_field_is_refused():
    # the rust seam is string-valued; a non-Str field has no faithful home yet.
    src = (
        'extern emission fn f(body: Str) -> Str\n'
        '  config { n: Int }\n'
        '  = @rs { body }\n'
    )
    with pytest.raises(Exception, match="only `Str` config fields"):
        _emit("rust", src)


# -- wasm: still a genuine conformance gap ----------------------------------

def test_wasm_config_extern_is_refused_at_compile():
    src = (
        'extern emission fn f(body: Str) -> Str\n'
        '  config { provider: Str }\n'
        '  = @wasm { return body }\n'
    )
    with pytest.raises(RevlError, match="@wasm tier"):
        compile_source(src)
