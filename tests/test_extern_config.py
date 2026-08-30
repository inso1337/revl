"""Per-extern `config` coeffect — roadmap item 379, option (b) of
docs/design/378-sync-extern-service-reach.md.

A document-global sync extern has no component context, so it cannot
`require` a service to read configuration (the env-shuttle wart behind
H19e/H23). Option (b) gives a classified extern a typed `config { ... }`
block, the same shape a component already has (parser.py:66 `ConfigField`,
parser.py:1352-1365 component config parse), resolved once at plug time from
the composition config map and bound in the extern body as `_revl_config`,
exactly as a component method binds it (backends/python/emit.py:734, :1425).

Staged plan + exit tests: docs/design/378-sync-extern-service-reach.md
("Staged implementation plan", "Exit tests" for option (b)).

Byte-identity mandate: an extern with NO config clause is unchanged across
parse, IR, and every golden.
"""

import pytest

from revl.parser import Parser
from revl.compiler import compile_source
from revl.errors import RevlError
from _backend_import import backend_emitter  # noqa: E402

emit = backend_emitter("python")


# the migrated design example (raw_model_post-style leaf mechanism): a sync
# emission extern that needs provider IDENTITY as static config, not a live
# service — the exact case the design names.
_RAW_MODEL_POST = (
    'extern emission fn raw_model_post(body: Str) -> Str\n'
    '  config { provider: Str, endpoint: Str, model: Str = "default" }\n'
    '  = @py {\n'
    '      return _revl_config["provider"] + "|" + _revl_config["endpoint"] + "|" + _revl_config["model"] + "|" + body\n'
    '  }\n'
)


# -- Stage 1: parser --------------------------------------------------------

def test_parser_accepts_config_clause_on_extern():
    prog = Parser(_RAW_MODEL_POST, "t.rvl").parse()
    decl = prog.externs[0]
    assert [f.name for f in decl.config] == ["provider", "endpoint", "model"]
    assert decl.config[2].default == "default"


def test_parser_extern_without_config_has_empty_config():
    prog = Parser(
        'extern emission fn f(x: Str) -> Str = @py { return x }', "t.rvl"
    ).parse()
    assert prog.externs[0].config == []


# -- Stage 2: lower + typecheck ---------------------------------------------

def _extern_ir(ir, name):
    return next(e for e in ir["externs"] if e["name"] == name)


def test_lower_carries_config_schema_on_extern_ir():
    ir = compile_source(_RAW_MODEL_POST)
    ext = _extern_ir(ir, "raw_model_post")
    assert ext["config"] == [
        {"name": "provider", "type": "Str", "default": None},
        {"name": "endpoint", "type": "Str", "default": None},
        {"name": "model", "type": "Str", "default": "default"},
    ]


def test_lower_no_config_extern_has_no_config_key():
    # byte-identity: the IR entry must not grow a `config` key at all.
    ir = compile_source('extern emission fn f(x: Str) -> Str = @py { return x }')
    assert "config" not in _extern_ir(ir, "f")


def test_lower_refuses_default_type_mismatch():
    with pytest.raises(RevlError, match="config field `n` default"):
        compile_source(
            'extern emission fn f(x: Str) -> Str\n'
            '  config { n: Int = "not-an-int" }\n'
            '  = @py { return x }\n'
        )


def test_lower_refuses_duplicate_config_field():
    with pytest.raises(RevlError, match="duplicate config field `n`"):
        compile_source(
            'extern emission fn f(x: Str) -> Str\n'
            '  config { n: Int, n: Str }\n'
            '  = @py { return x }\n'
        )


# -- Stage 3: py emitter ----------------------------------------------------

def test_py_emit_binds_revl_config_for_config_extern():
    src = emit.emit(compile_source(_RAW_MODEL_POST))
    assert "_REVL_EXTERN_CONFIG = {}" in src
    assert "def raw_model_post(body):" in src
    assert "_revl_config = _REVL_EXTERN_CONFIG.get('raw_model_post') or {}" in src


def test_py_emit_no_config_extern_is_byte_identical():
    # byte-identity: an extern with no config clause must emit exactly as it did
    # before item 379 — no config map, no `_revl_config` line.
    src = emit.emit(compile_source(
        'extern emission fn f(x: Str) -> Str = @py { return x }'))
    assert "_REVL_EXTERN_CONFIG" not in src
    assert "_revl_config" not in src
    assert "def f(x):\n    return x" in src


def test_py_emitted_config_extern_reads_injected_config():
    src = emit.emit(compile_source(_RAW_MODEL_POST))
    ns = {}
    exec(compile(src, "emitted_extern_config.py", "exec"), ns)
    # driver-style injection at the composition config map, resolved once
    ns["_REVL_EXTERN_CONFIG"]["raw_model_post"] = {
        "provider": "anthropic", "endpoint": "https://api", "model": "opus",
    }
    assert ns["raw_model_post"]("hi") == "anthropic|https://api|opus|hi"


# -- Stage 4: driver resolution at plug -------------------------------------

def test_driver_preflight_refuses_missing_required_extern_config():
    from revl.run import _required_config_problem  # noqa: PLC0415

    ir = compile_source(_RAW_MODEL_POST)
    # provider + endpoint are required (no default); model has a default.
    problem = _required_config_problem(ir, {})
    assert problem is not None
    assert 'extern raw_model_post is missing required config "provider"' in problem
    assert 'extern raw_model_post is missing required config "endpoint"' in problem
    # the defaulted field is never "missing"
    assert '"model"' not in problem


def test_driver_preflight_passes_when_required_extern_config_supplied():
    from revl.run import _required_config_problem  # noqa: PLC0415

    ir = compile_source(_RAW_MODEL_POST)
    problem = _required_config_problem(
        ir, {"raw_model_post": {"provider": "p", "endpoint": "e"}})
    assert problem is None, problem


def test_driver_resolves_extern_config_merging_defaults():
    from revl.run import _resolve_extern_config  # noqa: PLC0415

    ir = compile_source(_RAW_MODEL_POST)
    resolved = _resolve_extern_config(ir, {"raw_model_post": {"provider": "p", "endpoint": "e"}})
    # supplied values win; the defaulted field is materialized from the schema
    assert resolved["raw_model_post"] == {"provider": "p", "endpoint": "e", "model": "default"}


def test_driver_no_config_extern_resolves_to_empty():
    from revl.run import _resolve_extern_config  # noqa: PLC0415

    ir = compile_source('extern emission fn f(x: Str) -> Str = @py { return x }')
    assert _resolve_extern_config(ir, {}) == {}
