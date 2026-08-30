"""Two safety changes a Fable review flagged as REQUIRED before item 378's
remaining Stage 5 (ts/go/rust/java config injection). New roadmap item 395.

Item 378 landed option (b) PY-ONLY: an extern `config { ... }` coeffect,
resolved once at plug and bound as `_revl_config` in the @py body. The ts/go/
rust/java emitters have NO config-injection seam, so today a config extern
carrying a non-py host body emits with `_revl_config` UNBOUND -> a late,
mis-attributed runtime ReferenceError (ts) / compile error of the emitted
artifact (go/rust/java). Two changes close the hazard:

1. COMPILE-TIME TIER GATE (`src/revl/lower.py` `_lower_externs`): a config
   extern that carries a host body for a backend whose emitter LACKS the
   config-injection seam (everything but @py today) is REFUSED at compile with
   a redirect to option (c) [a home component that `requires` the service],
   instead of silently emitting an unbound `_revl_config`. A py-only config
   extern still works.

2. FAIL-LOUD PY LOOKUP (`backends/python/emit.py` `_emit_externs`): the
   `_REVL_EXTERN_CONFIG.get(name) or {}` fallback is replaced with a lookup
   that RAISES a clear "config extern `<name>` called before plug-time
   configuration was installed" when a REQUIRED (non-defaulted) field is
   absent, instead of silently handing the body `{}`. A defaults-only extern
   keeps a resolved-defaults dict.

See docs/design/378-sync-extern-service-reach.md (option (b), Stage 5).
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from _backend_import import backend_emitter  # noqa: E402

emit = backend_emitter("python")


# a config extern whose ONLY host body is on a seam-less tier (@ts).
_TS_CONFIG_EXTERN = (
    'extern emission fn author_ts(body: Str) -> Str\n'
    '  config { provider: Str }\n'
    '  = @ts {\n'
    '      return _revl_config["provider"] + body\n'
    '  }\n'
)


# a config extern that needs a required field (no default) plus a defaulted one,
# py-only — the case the fail-loud lookup guards.
_REQUIRED_CONFIG_EXTERN = (
    'extern emission fn need_provider(body: Str) -> Str\n'
    '  config { provider: Str, model: Str = "default" }\n'
    '  = @py { return _revl_config["provider"] + "|" + _revl_config["model"] + "|" + body }\n'
)

# a defaults-ONLY config extern, py-only — allowed to resolve driver-free.
_DEFAULTS_ONLY_EXTERN = (
    'extern emission fn only_defaults(body: Str) -> Str\n'
    '  config { model: Str = "opus" }\n'
    '  = @py { return _revl_config["model"] + "|" + body }\n'
)


def _exec(src):
    ns = {}
    exec(compile(src, "emitted.py", "exec"), ns)  # noqa: S102
    return ns


# -- Change 1: compile-time tier gate ---------------------------------------

def test_config_extern_with_ts_body_is_refused_at_compile():
    with pytest.raises(RevlError) as exc:
        compile_source(_TS_CONFIG_EXTERN)
    msg = str(exc.value)
    # names the offending tier and redirects to option (c).
    assert "@ts" in msg
    assert "option (c)" in msg


@pytest.mark.parametrize("tier", ["ts", "go", "rs", "java", "wasm"])
def test_config_extern_refused_on_every_seamless_tier(tier):
    src = (
        f'extern emission fn f(body: Str) -> Str\n'
        f'  config {{ provider: Str }}\n'
        f'  = @{tier} {{ return body }}\n'
    )
    with pytest.raises(RevlError, match=f"@{tier} tier"):
        compile_source(src)


def test_config_extern_with_py_and_ts_bodies_is_refused():
    # even when a valid @py body is present, an accompanying seam-less body is
    # the hazard the gate closes.
    src = (
        'extern emission fn f(body: Str) -> Str\n'
        '  config { provider: Str }\n'
        '  = @py { return _revl_config["provider"] + body }\n'
        '  = @ts { return _revl_config["provider"] + body }\n'
    )
    with pytest.raises(RevlError, match="@ts tier"):
        compile_source(src)


def test_py_only_config_extern_still_compiles():
    # the whole point: option (b) py-only is untouched.
    ir = compile_source(_REQUIRED_CONFIG_EXTERN)
    ext = next(e for e in ir["externs"] if e["name"] == "need_provider")
    assert ext["config"][0] == {"name": "provider", "type": "Str", "default": None}


def test_non_config_extern_with_ts_body_is_untouched():
    # byte-identity mandate: the gate fires ONLY for a config extern. A plain
    # multi-tier extern with no config block still compiles.
    ir = compile_source(
        'extern emission fn f(x: Str) -> Str\n'
        '  = @py { return x }\n'
        '  = @ts { return x }\n'
    )
    assert "config" not in next(e for e in ir["externs"] if e["name"] == "f")


# -- Change 2: fail-loud py lookup ------------------------------------------

def test_required_config_absent_fails_loud_at_call():
    # emitted module used OUTSIDE the run.py driver: config never installed.
    src = emit.emit(compile_source(_REQUIRED_CONFIG_EXTERN))
    assert "_REVL_EXTERN_CONFIG.get('need_provider') or {}" not in src  # old fallback gone
    ns = _exec(src)
    with pytest.raises(RuntimeError) as exc:
        ns["need_provider"]("hi")
    msg = str(exc.value)
    assert "config extern `need_provider` called before plug-time" in msg
    assert "provider" in msg


def test_required_config_installed_resolves_and_merges_defaults():
    src = emit.emit(compile_source(_REQUIRED_CONFIG_EXTERN))
    ns = _exec(src)
    # driver-style install of only the required field; the default is merged in.
    ns["_REVL_EXTERN_CONFIG"]["need_provider"] = {"provider": "anthropic"}
    assert ns["need_provider"]("hi") == "anthropic|default|hi"


def test_partial_install_missing_required_field_fails_loud():
    # a config dict installed but missing a required field still fails loud.
    src = emit.emit(compile_source(_REQUIRED_CONFIG_EXTERN))
    ns = _exec(src)
    ns["_REVL_EXTERN_CONFIG"]["need_provider"] = {"model": "opus"}  # provider missing
    with pytest.raises(RuntimeError, match="missing required config"):
        ns["need_provider"]("hi")


def test_defaults_only_config_extern_resolves_driver_free():
    # a defaults-only extern has NO required field, so it resolves to its
    # defaults even when configuration was never installed — no raise.
    src = emit.emit(compile_source(_DEFAULTS_ONLY_EXTERN))
    ns = _exec(src)
    assert ns["only_defaults"]("hi") == "opus|hi"
