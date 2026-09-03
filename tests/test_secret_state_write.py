"""A `Secret[T]` stays marked across a component-state write (item 256 §7, G9).

A component state world (`let store = effect Map.new() …`) becomes tainted two
ways, and only one of them is the binding:

    let store = effect Map.new()             # the BINDING carries the taint
    effect store.insert("k", config.token)   # a CALL writes INTO the world

The write is what a `Map`-shaped world almost always gets — the binding is a
clean `Map.new()` and is never rebound — and it can be spelled in the ACTIVATION
BODY as easily as in a provide method. Where it is spelled is not a
confidentiality boundary, so the two spellings have to agree: read the key back
and hand it to a log, and both must be refused.

They did not agree. The state-world seed read the activation body's VALUE
environment, which a write-into never touches, so a world written only there was
seeded CLEAN — and `store.get("k")` handed back a `Secret[Str]` config field with
its `confidential` origin gone, straight into an emission, with no `endorse` and
nothing on the G8 audit surface.

The refusals asserted here are observable compiler behaviour, and the
disclosure-path text is part of it: an author needs to be told WHICH value is
disclosing, not merely that something is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

_HEAD = (
    "service Log { emission fn write(line: Str) -> Int }\n"
    "service Cache { emission fn leak() }\n"
)

# The write is in the ACTIVATION BODY — the spelling that used to compile.
_ACTIVATION_WRITE = _HEAD + (
    "component C requires log: Log provides cache: Cache {\n"
    "  config { token: Secret[Str] }\n"
    "  let store = effect Map.new() undo store.drop()\n"
    '  effect store.insert("k", config.token) undo store.remove("k")\n'
    "  provide cache {\n"
    '    fn leak() { emit log.write(store.get("k") ?? "none") }\n'
    "  }\n}\n"
)

# The same program with the write moved into a provide method — the control that
# was always refused. Both must be refused, and for the same reason.
_METHOD_WRITE = _HEAD + (
    "component C requires log: Log provides cache: Cache {\n"
    "  config { token: Secret[Str] }\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache {\n"
    "    fn leak() {\n"
    '      effect store.insert("k", config.token) undo store.remove("k")\n'
    '      emit log.write(store.get("k") ?? "none")\n'
    "    }\n"
    "  }\n}\n"
)

# A NON-secret config field written the same way, read back the same way. This is
# the false-positive guard: the fix joins the activation body's writes into the
# state world, and joining a CLEAN write must stay clean.
_ORDINARY_CONFIG = _HEAD + (
    "component C requires log: Log provides cache: Cache {\n"
    "  config { label: Str }\n"
    "  let store = effect Map.new() undo store.drop()\n"
    '  effect store.insert("k", config.label) undo store.remove("k")\n'
    "  provide cache {\n"
    '    fn leak() { emit log.write(store.get("k") ?? "none") }\n'
    "  }\n}\n"
)


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "secret_state.rvl")
    return excinfo.value


def test_a_secret_written_into_state_in_the_activation_body_is_refused():
    err = _refusal(_ACTIVATION_WRITE)
    assert err.code == "G-SECRET-FLOW"
    assert "config.token" in str(err), (
        "the refusal must name the disclosing value, not just report a leak")


def test_the_write_position_does_not_change_the_verdict():
    """The activation body and a provide method are the same program. A `Secret[T]`
    that survives one spelling and not the other is a confidentiality boundary
    nobody declared."""
    assert _refusal(_ACTIVATION_WRITE).code == _refusal(_METHOD_WRITE).code


def test_an_ordinary_config_field_still_compiles():
    """The seed now folds in the activation body's writes; folding a CLEAN one
    must not invent a taint. Without this, the fix would refuse every component
    that seeds a `Map` from its own config."""
    compile_source(_ORDINARY_CONFIG, "secret_state.rvl")


def test_a_declared_secret_receiver_still_admits_the_value():
    """The fence is positional, not a ban on the value: routed to a receiver that
    declares `Secret[T]`, the same state read crosses (§7b)."""
    src = (
        "service Vault { emission fn stash(t: Secret[Str]) -> Int }\n"
        "service Cache { emission fn keep() }\n"
        "component C requires vault: Vault provides cache: Cache {\n"
        "  config { token: Secret[Str] }\n"
        "  let store = effect Map.new() undo store.drop()\n"
        '  effect store.insert("k", config.token) undo store.remove("k")\n'
        "  provide cache {\n"
        '    fn keep() { emit vault.stash(store.get("k") ?? "none") }\n'
        "  }\n}\n"
    )
    compile_source(src, "secret_state.rvl")
