"""Config is static data, not a capability (roadmap item 378).

docs/design/378-sync-extern-service-reach.md asserts "Config is static data,
not a capability, so the capability gate is untouched". `config_block` parsed a
field type with the full type grammar, and an extern's config schema was never
wellformed-checked, so `config { handler: (Str) -> Str }` (or a `service` field)
compiled and a provider invoking `config.handler(x)` reached host emission with
no ticket and no capability check. A live callable arrived through
spawn-with / load-with / the embedding API. These tests make the assertion a
check: a config field's declared type must be built, transitively, out of data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _ok(src: str):
    return compile_source(src, "t.rvl")


def _refused(src: str) -> RevlError:
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    return ei.value


# -- the hole: a config field may not carry a live callable -----------------

def test_component_arrow_config_field_refused():
    err = _refused("""
service Greet { fn greet(x: Str) -> Str }
component Worker provides g: Greet {
  config { handler: (Str) -> Str }
  provide g { fn greet(x: Str) -> Str = config.handler(x) }
}
""")
    assert "must be static data" in str(err)
    assert "arrow" in str(err)


def test_extern_arrow_config_field_refused():
    err = _refused(
        'extern pure fn thing(x: Str) -> Str\n'
        '  config { handler: (Str) -> Str }\n'
        '  = @py { return _revl_config["handler"](x) }\n'
    )
    assert "must be static data" in str(err)
    assert "extern `thing`" in str(err)


def test_service_config_field_refused():
    err = _refused("""
service Greet { fn greet(x: Str) -> Str }
component Worker provides g: Greet {
  config { p: Greet }
  provide g { fn greet(x: Str) -> Str = x }
}
""")
    assert "service `Greet`" in str(err)
    assert "must be static data" in str(err)


def test_record_of_arrow_config_field_refused_transitively():
    err = _refused("""
service Greet { fn greet(x: Str) -> Str }
type Callbacks = { on: (Str) -> Str }
component Worker provides g: Greet {
  config { cbs: Callbacks }
  provide g { fn greet(x: Str) -> Str = x }
}
""")
    assert "must be static data" in str(err)


def test_opt_of_arrow_config_field_refused():
    err = _refused("""
service Greet { fn greet(x: Str) -> Str }
component Worker provides g: Greet {
  config { h: ((Str) -> Str)? }
  provide g { fn greet(x: Str) -> Str = x }
}
""")
    assert "must be static data" in str(err)


# -- no over-refusal: honest data config still compiles ---------------------

def test_data_config_still_compiles():
    _ok("""
service Greet { fn greet(x: Str) -> Str }
type Options = { verbose: Bool, tag: Str }
component Worker provides g: Greet {
  config { url: Str, retries: Int = 3, opts: Options }
  provide g { fn greet(x: Str) -> Str = x }
}
""")


def test_extern_data_config_still_compiles():
    _ok(
        'extern emission fn raw_model_post(body: Str) -> Str\n'
        '  config { provider: Str, endpoint: Str, model: Str = "default" }\n'
        '  = @py { return _revl_config["provider"] + "|" + body }\n'
    )


# -- the producer seam: a spawn config value is type-checked ----------------

_WORKER = """
service Counter { fn value() -> Int }
component Worker provides counter: Counter {
  config { tag: Str }
  provide counter { fn value() = 0 }
}
"""


def test_spawn_config_value_correct_type_compiles():
    _ok(_WORKER + 'component Sup { let w = effect spawn Worker with { tag: "x" } undo w.dispose() }')


def test_spawn_config_value_wrong_type_refused():
    err = _refused(
        _WORKER + 'component Sup { let w = effect spawn Worker with { tag: 42 } undo w.dispose() }')
    assert "config field `tag`" in str(err)
