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
