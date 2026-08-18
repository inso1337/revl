"""One source, every tier, actually *run* — the semantic floor.

tests/test_cross_tier.py holds the portability floor by checking that every
emitter accepts a construct. That catches a tier which refuses; it cannot
catch a tier which accepts and then means something different. Structural
equality was exactly that hole: `{a: 1} == {a: 1}` emitted cleanly on all
five tiers, was true on python and **false** on TypeScript (JS `===` is
identity for objects), and did not compile at all on rust (no `PartialEq`
derive). syntax-2.0 §3.4 promises one structural equality and says "no
backend can diverge", so this was a silent wrong answer against an explicit
guarantee.

The lesson is the project's own recurring one, one level up: "the emitter did
not raise" never implied "the code is right", and "every emitter agreed on a
shape" never implied "every tier agrees on a value".

python and TypeScript execute here because both are fast. rust and java are
gated behind REVL_CROSS_TIER_SLOW=1 (cargo and javac make the default suite
minutes rather than seconds); their regression guards below are static and
cheap, and CI runs the full matrix.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

# Each probe is a semantic claim the language makes, written so that a tier
# which merely *compiles* it still fails when it means the wrong thing.
PROBES = {
    "structural equality": """
type Row = { id: Int, name: Str }
pub fn rec_eq() -> Bool { return { id: 1, name: "a" } == { id: 1, name: "a" } }
pub fn rec_ne() -> Bool { return { id: 1, name: "a" } == { id: 2, name: "a" } }
pub fn list_eq() -> Bool { return [1, 2] == [1, 2] }
pub fn list_ne() -> Bool { return [1, 2] == [2, 1] }
pub fn nested() -> Bool { return [{ id: 1, name: "a" }] == [{ id: 1, name: "a" }] }

test "records compare by value" { assert rec_eq() }
test "different records are not equal" { assert !rec_ne() }
test "lists compare by value" { assert list_eq() }
test "list order matters" { assert !list_ne() }
test "nesting composes" { assert nested() }
test "inequality is the negation" { assert { id: 1, name: "a" } != { id: 2, name: "a" } }
""",
}

FAST_TIERS = ("py", "ts")
SLOW_TIERS = ("rust", "java")


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "cross_tier_exec.rvl"))


@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_probe_executes_identically(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed {name!r}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_probe_executes_identically_slow(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed {name!r}: {message}"


# --------------------------------------------------- cheap static guards

def _emit(backend: str, source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}_xtierexec", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "cross_tier_exec.rvl"))


def test_typescript_equality_is_not_js_identity():
    """`===` on two records is `false` in JavaScript. The emitted code must
    not contain it for an equality the language says is structural."""
    emitted = _emit("typescript", PROBES["structural equality"])
    assert "revlEq(" in emitted
    assert "function revlEq" in emitted, "the helper must travel with the module"


def test_rust_records_derive_partial_eq():
    """Without the derive, rustc refuses `==` outright (E0369) and legal revl
    simply does not build on this tier."""
    emitted = _emit("rust", PROBES["structural equality"])
    assert "PartialEq" in emitted
    for line in emitted.splitlines():
        if line.startswith("pub struct ") or line.startswith("pub enum "):
            break
    assert "#[derive(Clone, Debug, PartialEq" in emitted


def test_java_equality_goes_through_objects_equals():
    """Java records have a structural `equals`; `==` would be identity. This
    tier was already correct and should stay that way."""
    emitted = _emit("java", PROBES["structural equality"])
    assert "java.util.Objects.equals(" in emitted
