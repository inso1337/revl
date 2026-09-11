"""The declared-shape half of the `Secret[T]` redaction walk (item 421 F6).

Every tier walks a declared `Secret[T]` value and remembers its leaves so a
later sink can scrub them. The walk's container rule is that a record's KEYS
are field names the author wrote and are skipped, while its VALUES are walked.

The rule was applied to a `Map` too, and that is where it is wrong: a `Map`'s
keys are the CALLER's data, exactly as confidential as the values they map to.
On the ts, go and java tiers the map branch is map-specific and each tier walks
records in a SEPARATE branch beside it, so the record rationale was inapplicable
at the site it was written -- and on python a record reaches the runtime as a
`dict`, the same representation a `Map` uses, so one branch had to serve both
and the values-only rule silently covered the record case only.

The consequence was uniform: a declared `Secret[Map[K, V]]` registered every
value and no key, and a key the caller chose crossed the operator console trace,
the WAL record render and the seam failure text verbatim.

The fix is the declared TYPE, not a different value rule. A `Map` and a record
are both a `dict`/`object` at runtime, so the value alone cannot say which the
author declared; the emitter knows, so it spells the type out. Where it does not
(`_type`/`_declared` absent) the walk keeps its previous values-only behavior
exactly, which is what makes this a closed gap rather than a rewrite of the
container rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

import confidential  # noqa: E402

REDACTED = "<redacted:secret>"

# A declared `Secret[Map[Str, Str]]` extern return: the origin the audit read the
# key off. Long enough to clear `_MIN_MARKABLE`.
MAP_KEY = "KEY-secret-abcdefghijklmnop"
MAP_VALUE = "VAL-secret-qrstuvwxyz012345"

# A record whose FIELD is a `Map`: the case the declared type has to be threaded
# through to reach, since the record branch walks values by declared field type.
NESTED_KEY = "NESTED-secret-abcdefghijklmnop"
NESTED_VALUE = "N-secret-qrstuvwxyz012345"

_MAP_DOC = f'''
extern emission[store.mint] fn mint(u: Str) -> Secret[Map[Str, Str]]
  = @py {{ return {{"{MAP_KEY}": "{MAP_VALUE}"}} }}

service Store {{ emission fn mint(u: Str) -> Secret[Map[Str, Str]] }}
'''

_RECORD_DOC = f'''
pub type Cred = {{ user: Str, tokens: Map[Str, Str] }}

extern emission[store.wrap] fn wrap(u: Str) -> Secret[Cred]
  = @py {{ return {{"user": "u", "tokens": {{"{NESTED_KEY}": "{NESTED_VALUE}"}}}} }}

service Wrap {{ emission fn wrap(u: Str) -> Secret[Cred] }}
'''

# A declared secret that reaches no `Map`: the emission must not change.
_SCALAR_DOC = '''
extern emission[store.mint] fn mint(u: Str) -> Secret[Str]
  = @py { return "SEKRIT-CANARY-SCALAR-abcdefgh" }

service Store { emission fn mint(u: Str) -> Secret[Str] }
'''

# The same declared `Secret[Map[Str, Str]]` origin with a body for every tier, so
# each emitter that reads the document can be handed one it accepts. The walk is
# a per-tier artifact gated on the document declaring a secret, so it takes a
# real secret-bearing document to reach it -- and on the go tier the funnel is
# emitted with a PLACEMENT, which `_GO_SECRET_DOC` below supplies.
_CROSS_TIER_DOC = f'''
extern emission[store.mint] fn mint(u: Str) -> Secret[Map[Str, Str]]
  = @py {{ return {{"{MAP_KEY}": "{MAP_VALUE}"}} }}
  = @ts {{ return new Map([["{MAP_KEY}", "{MAP_VALUE}"]]) }}
  = @go {{ return map[string]string{{"{MAP_KEY}": "{MAP_VALUE}"}} }}
  = @java {{ return java.util.Map.of("{MAP_KEY}", "{MAP_VALUE}"); }}

service Store {{ emission fn mint(u: Str) -> Secret[Map[Str, Str]] }}

component Impl {{ }}
'''

# A placement that declares a `Secret[T]`, the shape the go tier emits its
# confidentiality funnel for.
_GO_SECRET_DOC = f'''
service Vault {{ fn show(user: Str) -> Str }}

component Impl provides vault: Vault {{
  config {{ token: Secret[Str] = "{MAP_KEY}" }}

  provide vault {{
    fn show(user) {{ return "shown " + user }}
  }}
}}
'''


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is a per-process global; each test starts empty."""
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


# ---------------------------------------------------------------------------
# the walk itself: the declared type is what separates a Map from a record
# ---------------------------------------------------------------------------


def test_a_declared_map_registers_its_keys_beside_its_values():
    """The reported gap, at the funnel. Before the declared shape was threaded
    the `dict` branch read `value.values()` only, so the key was never in the
    registry and no sink could scrub it."""
    confidential.register_secret_tree({MAP_KEY: MAP_VALUE}, None, "Map[Str, Str]")
    assert MAP_KEY in confidential._secret_values
    assert MAP_VALUE in confidential._secret_values


def test_a_declared_map_key_is_scrubbed_from_the_console_trace():
    """The sink the audit read the key off: the operator console trace."""
    confidential.register_secret_tree({MAP_KEY: MAP_VALUE}, None, "Map[Str, Str]")
    rendered = confidential.redact_text(f"host trace: mint() -> {{{MAP_KEY!r}: {MAP_VALUE!r}}}")
    assert MAP_KEY not in rendered
    assert MAP_VALUE not in rendered
    assert rendered.count(REDACTED) == 2


def test_a_record_keeps_skipping_its_field_names():
    """The half of the rule that was right, and the reason the fix threads the
    type instead of simply registering every key: a record's keys ARE field
    names, and erasing them destroys the diagnostic's shape."""
    confidential.declare_secret_types({"Cred": {"user": "Str", "tokens": "Str"}})
    confidential.register_secret_tree(
        {"user": "u", "tokens": "VAL-secret-qrstuvwxyz012345"}, None, "Cred")
    assert "user" not in confidential._secret_values
    assert "tokens" not in confidential._secret_values
    assert "VAL-secret-qrstuvwxyz012345" in confidential._secret_values


def test_a_map_inside_a_record_field_is_reached_as_a_map():
    """The record branch walks each field with its DECLARED type, so a `Map`
    field is not mistaken for another record. Without the field type the walk
    reaches the inner dict with no type and applies the record rule again."""
    confidential.declare_secret_types(
        {"Cred": {"user": "Str", "tokens": "Map[Str, Str]"}})
    confidential.register_secret_tree(
        {"user": "u", "tokens": {NESTED_KEY: NESTED_VALUE}}, None, "Cred")
    assert NESTED_KEY in confidential._secret_values
    assert NESTED_VALUE in confidential._secret_values
    # ...and the record's own field names are still not registered.
    assert "user" not in confidential._secret_values
    assert "tokens" not in confidential._secret_values


def test_a_map_in_a_list_element_is_reached_as_a_map():
    """`Secret[List[Map[Str, Str]]]`: the element type is threaded too."""
    confidential.register_secret_tree(
        [{MAP_KEY: MAP_VALUE}], None, "List[Map[Str, Str]]")
    assert MAP_KEY in confidential._secret_values
    assert MAP_VALUE in confidential._secret_values


def test_without_a_declared_type_the_values_only_rule_is_unchanged():
    """The backward-compatibility half. Every caller that does not know its
    declared type -- and every nested node whose type was not threaded -- keeps
    the previous behavior exactly, so this is a closed gap and not a change to
    what the walk does to an untyped value."""
    confidential.register_secret_tree({MAP_KEY: MAP_VALUE})
    assert MAP_KEY not in confidential._secret_values
    assert MAP_VALUE in confidential._secret_values


def test_a_map_key_shorter_than_the_markable_minimum_is_not_registered():
    """`register_secret_value`'s minimum still applies to a key: a short key is
    a word that would shred unrelated diagnostics for no confidentiality gain."""
    confidential.register_secret_tree({"ab": "VAL-secret-qrstuvwxyz012345"},
                                      None, "Map[Str, Str]")
    assert "ab" not in confidential._secret_values
    assert "VAL-secret-qrstuvwxyz012345" in confidential._secret_values


def test_a_self_referential_map_still_terminates():
    """The cycle guard is on the path, and the new key leg must not defeat it:
    a key that is the map itself is a back-edge, not an infinite descent."""
    value: dict = {}
    value[MAP_KEY] = value
    confidential.register_secret_tree(value, None, "Map[Str, Str]")
    assert MAP_KEY in confidential._secret_values


# ---------------------------------------------------------------------------
# the emitted program: the declared shape has to reach the runtime
# ---------------------------------------------------------------------------


def test_the_python_emitter_spells_out_a_declared_map_return():
    code = backend_emitter("python").emit(compile_source(_MAP_DOC))
    assert "@_revl_secret_result(_declared='Map[Str, Str]')" in code


def test_the_python_emitter_emits_the_record_field_types_a_map_needs():
    """A `Map` in a record field needs the record's declared field types, so the
    emitter registers them once for the module."""
    code = backend_emitter("python").emit(compile_source(_RECORD_DOC))
    assert "@_revl_secret_result(_declared='Cred')" in code
    assert "_revl_declare_secret_types({'Cred': {'tokens': 'Map[Str, Str]'" in code


def test_a_secret_that_reaches_no_map_emits_exactly_what_it_did_before():
    """The change is inert for a document with no declared secret `Map`."""
    code = backend_emitter("python").emit(compile_source(_SCALAR_DOC))
    assert "@_revl_secret_result\n" in code
    assert "_declared=" not in code
    assert "declare_secret_types" not in code


def test_the_ts_tier_registers_a_maps_keys():
    """The ts walk lives in the hand-written runtime the tier ships.

    `rememberSecret` is not generated per program on this tier, so the artifact
    that carries the rule is `backends/typescript/runtime.ts` itself.
    """
    runtime = (ROOT / "backends" / "typescript" / "runtime.ts").read_text()
    assert "for (const key of value.keys()) rememberSecret(key, seen)" in runtime
    assert "for (const item of value.values()) rememberSecret(item, seen)" in runtime


def test_the_ts_tier_compiles_the_declared_map_origin():
    """The runtime rule is reachable: the tier accepts the origin that needs it."""
    code = backend_emitter("typescript").emit(compile_source(_CROSS_TIER_DOC))
    assert "mint" in code


def test_the_go_tier_registers_a_maps_keys():
    """The go walk is generated with the composition's confidentiality funnel.

    That funnel (`revlRegisterValue` and friends) is emitted by `emit_placement`,
    only for a document that actually declares a `Secret[T]`, so the document
    here is a placement carrying one.
    """
    code = backend_emitter("go").emit_placement(compile_source(_GO_SECRET_DOC), "emitted")
    assert "revlRegisterValue(iter.Key(), seen)" in code
    assert "revlRegisterValue(iter.Value(), seen)" in code


def test_the_java_tier_registers_a_maps_keys():
    code = backend_emitter("java").emit(compile_source(_CROSS_TIER_DOC))
    assert "for (Object key : mapping.keySet()) {" in code
    assert "revlRememberSecret(key, seen);" in code
