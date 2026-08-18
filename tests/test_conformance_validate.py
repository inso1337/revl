"""The conformance matrix's second half: emitted code must survive its toolchain.

`tools/conformance.py` alone answers "did the emitter raise?", and that
question has a blind spot with a track record: the rust backend emitted a
provider struct that never captured its `requires` bindings, produced Rust
referencing a free variable, and reported `ok` in the matrix for months. The
TypeScript backend had the *same* bug in a different spelling — `ctx.bus` for
a required service, with `bus` never declared on cordis's `Context` — and it
also reported `ok`. Neither is visible without handing the output to a real
compiler.

These tests do that. A tier whose toolchain is absent skips *loudly*: the
reason is the message, because "nothing checked it" must never be recorded as
"it passed".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import conformance  # noqa: E402
from validate import VALIDATORS  # noqa: E402


# Emitted code that its own toolchain rejects *today*. Every entry is a real
# emitter bug, found the first time that tier was validated against a compiler
# rather than merely run (docs/conformance.md). They are baselined so this
# suite fails on *new* breakage instead of being switched off while they are
# worked through — and the test also fails when a baselined case starts
# passing, so the list can only shrink.
KNOWN_FAILURES: dict[str, set[str]] = {
    # rust is at zero: the host-builtin contract (typecheck.py `_HOST_ARG_SIG`)
    # closed both of its remaining cases, and the `T` -> `Opt[T]` injection
    # closed the third.
    "java": {
        "expr/ADT construct + match",
        "fn/arrow lambda",
    },
}


@pytest.fixture(scope="module")
def artifacts() -> dict[str, list[tuple[str, object]]]:
    """Every case emitted once, per tier, shared across the tier tests."""
    collected: dict[str, list[tuple[str, object]]] = {t: [] for t in conformance.TIERS}
    for index, (group, name, source) in enumerate(conformance.CASES):
        label = f"{group}/{name}"
        try:
            ir = conformance.compile_source(source)
        except Exception:  # noqa: BLE001 — frontend rejection is its own report
            continue
        for tier in conformance.TIERS:
            try:
                collected[tier].append((label, conformance.emitter(tier).emit(
                    ir, **conformance._emit_kwargs(tier, index))))
            except Exception:  # noqa: BLE001 — a refusal has nothing to validate
                pass
    return collected


@pytest.mark.parametrize("tier", conformance.TIERS)
def test_emitted_code_survives_its_toolchain(tier, artifacts):
    validator = VALIDATORS[tier]
    reason = validator.unavailable()
    if reason:
        pytest.skip(f"{tier}: {reason}")

    results = validator.check(artifacts[tier])
    assert results, f"{tier}: nothing was validated"
    failures = {label: detail for label, (status, detail) in results.items()
                if status != "ok"}
    known = KNOWN_FAILURES.get(tier, set())

    regressions = {label: detail for label, detail in failures.items()
                   if label not in known}
    assert not regressions, (
        f"{tier} emitted code its own toolchain rejects ({validator.depth}):\n"
        + "\n".join(f"  {label}: {detail}" for label, detail in regressions.items()))

    repaired = sorted(known - set(failures))
    assert not repaired, (
        f"{tier}: these are in KNOWN_FAILURES but now pass — delete them from "
        f"the baseline so it cannot rot:\n" + "\n".join(f"  {c}" for c in repaired))


# --- regressions for what the first validated run found ----------------------
# String-level, so they hold the line on machines with no toolchain at all.

def _ts(source: str) -> str:
    return conformance.emitter("typescript").emit(conformance.compile_source(source))


def test_required_services_are_declared_on_context():
    """The TypeScript instance of the rust `requires` bug.

    The emitter augments cordis's `Context` so `ctx.<key>` typechecks. It used
    to augment with provisions only, so every component that read a service it
    required emitted `ctx.bus` against a `Context` with no `bus`.
    """
    out = _ts("service Bus { emission fn send(n: Int) -> Int }\n"
              # `f` reaches an emission, so G4 requires it be declared one.
              "service S { emission fn f(x: Int) -> Int }\n"
              "component C requires bus: Bus provides s: S {\n"
              "  provide s { fn f(x) { emit bus.send(x)  return x } }\n"
              "}")
    assert "ctx.bus.send(x)" in out
    assert "    bus: Bus" in out, "required service missing from the augmentation"
    assert "    s: S" in out


def test_iteration_boundary_yields_a_disposable():
    """cordis types a yield as `Disposable<T> = () => T`; `null` is not one."""
    out = _ts("service S { fn f(x: Int) -> Int }\n"
              "component C provides s: S {\n"
              "  await Job.run(\"boot\")\n"
              "  provide s { fn f(x) = x }\n"
              "}")
    assert "yield null" not in out
    assert "yield () => {}  // iteration boundary (A1)" in out


def test_arrow_parameters_are_explicitly_any():
    """Arrows are in the checker's enumerated unchecked remainder, so there is
    no type to emit — and `strict` rejects an *implicit* any, not a written one."""
    out = _ts("fn apply(n: Int) -> Int { let g = v => v + 1  return g(n) }\n"
              "service S { fn f(x: Int) -> Int }\n"
              "component C provides s: S {\n"
              "  provide s { fn f(x) = apply(x) }\n"
              "}")
    assert "((v: any) =>" in out


def test_python_validator_detects_an_uncaptured_binding():
    """A negative control: a validator that cannot fail proves nothing.

    This is the emitted shape of the original rust bug, in Python.
    """
    unbound = VALIDATORS["python"]._unbound(
        "def _c_apply(ctx, config):\n"
        "    class _S:\n"
        "        def f(self, x):\n"
        "            return bus.query(x)\n",
        "synthetic")
    assert unbound == ["f: bus"]


def _java(source: str) -> str:
    return conformance.emitter("java").emit(conformance.compile_source(source))


def test_java_provider_class_captures_required_services():
    """The Java instance of the rust/TypeScript `requires` bug.

    The single-expression provider path emitted `bus.send(x)` inside the
    provider class while `bus` only ever existed as a local of the plugin's
    `apply` — a free name javac rejects with `cannot find symbol`. A required
    service has to be captured the same way a `let-effect` bind already was:
    a final field, filled in by the constructor.
    """
    out = _java("service Bus { emission fn send(n: Int) -> Int }\n"
                "service S { emission fn f(x: Int) -> Int }\n"
                "component C requires bus: Bus provides s: S {\n"
                "  provide s { fn f(x) = emit bus.send(x) }\n"
                "}")
    assert "    private final Bus bus;" in out
    assert "CS(Bus bus) {" in out
    assert "this.bus = bus;" in out
    assert "return this.bus.send(x);" in out
    assert "new CS(bus)" in out


def test_java_user_declared_types_survive_on_both_sides_of_a_signature():
    """The service interface and its implementing class rendered a declared
    type with two different functions: the interface kept the name, the
    implementation collapsed it to `Object`, and javac reported the class as
    not overriding the interface method. Both sides must spell it the same.
    """
    record = _java("type R = { a: Int }\nservice S { fn f(x: R) -> R }\n"
                   "component C provides s: S { provide s { fn f(x) = x } }")
    assert "    R f(R x);" in record
    assert "public R f(R x)" in record

    adt = _java("type O = Found(Int) | Missing\nservice S { fn f(x: O) -> O }\n"
                "component C provides s: S { provide s { fn f(x) = x } }")
    assert "    O f(O x);" in adt
    assert "public O f(O x)" in adt
