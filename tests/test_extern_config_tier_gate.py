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


# -- Change 1: compile-time tier gate ---------------------------------------

def test_config_extern_with_ts_body_is_refused_at_compile():
    with pytest.raises(RevlError) as exc:
        compile_source(_TS_CONFIG_EXTERN)
    msg = str(exc.value)
    # names the offending tier and redirects to option (c).
    assert "@ts" in msg
    assert "option (c)" in msg
