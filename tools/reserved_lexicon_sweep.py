"""Conformance sweep for issue #320: A3 says "every name is safe verbatim on
both hosts", so a legal revl identifier must compile AND run on every tier no
matter what host keyword / builtin / predeclared global / import / emitter
helper it happens to spell.

This module generates one-name-per-program probes over a candidate lexicon and
runs each through the real per-tier toolchains (via `tools/validate.py`), in the
identifier POSITIONS a name can occupy: a function parameter, a function name,
and a value-binding local. The result is the (name x tier x position) matrix
the issue asks for.

`UNION` is the frontend-owned reserved lexicon (mirrors
`src/revl/lower.py::_HOST_PREDECLARED`). The sweep proves every member is safe
verbatim on every tier whose toolchain is present.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from tools import validate as V  # noqa: E402
from tools.conformance import emitter, _emit_kwargs  # noqa: E402

# The candidate lexicon swept. Mirror of src/revl/lower.py::_HOST_PREDECLARED
# plus the keyword families items 160/165/406 already cover (kept here so the
# sweep is a total statement about "safe verbatim", not only the remainder).
try:
    from revl.lower import _HOST_PREDECLARED as UNION_FROM_FRONTEND
except Exception:  # noqa: BLE001
    UNION_FROM_FRONTEND = set()

# Names called out in issue #320 (the remainder items 160/165/406 missed).
ISSUE_NAMES = {
    # python builtins the emitter emits verbatim
    "len", "str", "isinstance", "int", "float", "bool", "list", "dict",
    "set", "range", "print", "repr", "abs", "min", "max", "sum", "sorted",
    "bytes", "object",
    # ts globals / emitter helpers
    "Number", "String", "Object", "JSON", "Array", "Map", "Pool", "Job",
    "TypeError", "revlLen", "revlEq", "revlStrLen", "eval", "arguments",
    "expect", "it",
    # go predeclared / imports / emitter helpers
    "strconv", "float64", "int64", "panic", "testing", "init",
    # wasm emitter helpers
    "alloc", "f64_to_str", "memory",
    # adt constructors as identifiers
    "Some", "Ok", "Err", "None",
}

UNION = sorted(set(UNION_FROM_FRONTEND) | ISSUE_NAMES)

# The identifier positions a name can occupy. Each maps a name -> a small
# self-contained revl program that is well-typed for any legal identifier and
# whose ANSWER is fixed, so a tier that renames correctly runs it and a tier
# that leaks the name into host scope breaks (compile or runtime).
POSITIONS = {
    "param": lambda n: (
        f"fn use_{{i}}({n}: Int) -> Int {{ return {n} + 1 }}\n"
        f'test "t{{i}}" {{ assert use_{{i}}(2) == 3 }}\n'
    ),
    "fnname": lambda n: (
        f"fn {n}(x: Int) -> Int {{ return x + 1 }}\n"
        f'test "t{{i}}" {{ assert {n}(2) == 3 }}\n'
    ),
    "local": lambda n: (
        f'test "t{{i}}" {{ let {n} = 2  assert {n} + 1 == 3 }}\n'
    ),
}

# Tiers whose local toolchain runs the program (best signal: catches the python
# runtime shadowing that a compile-only check misses). Others compile-check.
EXEC_TIERS = ("python", "go", "typescript")
COMPILE_TIERS = ("java", "rust", "wasm")
ALL_TIERS = ("python", "typescript", "go", "java", "rust", "wasm")


def _probe(position: str, name: str, i: int) -> str:
    return POSITIONS[position](name).replace("{i}", str(i))


def sweep(names=UNION, positions=tuple(POSITIONS)):
    """Return {(name, position, tier): (outcome, detail)}.

    outcome in {"pass", "fail", "unavailable", "frontend-reject"}.
    """
    result: dict = {}

    # Which tiers can we actually check here?
    avail: dict[str, str | None] = {}
    for tier in ALL_TIERS:
        if tier in EXEC_TIERS:
            avail[tier] = V.EXECUTORS[tier].unavailable()
        else:
            avail[tier] = V.VALIDATORS[tier].unavailable()

    for position in positions:
        # Batch compile-tier artifacts across all names into one toolchain call.
        artifacts: dict[str, list] = {t: [] for t in COMPILE_TIERS}
        exec_items: dict[str, list] = {t: [] for t in EXEC_TIERS}
        labels: list[str] = []
        for i, name in enumerate(names):
            label = f"{position}:{name}"
            labels.append((label, name))
            src = _probe(position, name, i)
            # Emit for compile tiers
            try:
                ir = compile_source(src)
            except RevlError as err:
                for tier in ALL_TIERS:
                    result[(name, position, tier)] = (
                        "frontend-reject", str(err).splitlines()[0])
                continue
            for tier in COMPILE_TIERS:
                if avail[tier] is not None:
                    result[(name, position, tier)] = ("unavailable", avail[tier])
                    continue
                try:
                    artifacts[tier].append(
                        (label, emitter(tier).emit(ir, **_emit_kwargs(tier, i))))
                except Exception as exc:  # noqa: BLE001
                    result[(name, position, tier)] = (
                        "fail", f"emit: {str(exc).splitlines()[0]}")
            for tier in EXEC_TIERS:
                if avail[tier] is not None:
                    result[(name, position, tier)] = ("unavailable", avail[tier])
                    continue
                exec_items[tier].append((label, src))

        # Run the batched compile tiers.
        for tier in COMPILE_TIERS:
            if not artifacts[tier]:
                continue
            checked = V.VALIDATORS[tier].check(artifacts[tier])
            for (label, name) in labels:
                if (name, position, tier) in result:
                    continue
                outcome = checked.get(label)
                if outcome is None:
                    continue
                ok, detail = outcome
                result[(name, position, tier)] = (
                    "pass" if ok == V.OK else "fail", detail)

        # Run the exec tiers (per program; py is in-process/fast).
        for tier in EXEC_TIERS:
            for (label, src) in exec_items[tier]:
                name = label.split(":", 1)[1]
                out, detail = V._run_program(tier, src)
                result[(name, position, tier)] = (
                    "pass" if out == "pass" else out, detail)

    return result


def failures(result):
    return {k: v for k, v in result.items() if v[0] == "fail"}


if __name__ == "__main__":
    res = sweep()
    fails = failures(res)
    tiers_seen = sorted({t for (_, _, t) in res})
    print(f"swept {len(UNION)} names x {len(POSITIONS)} positions x "
          f"{len(tiers_seen)} tiers")
    unavail = sorted({t for (_, _, t), v in res.items() if v[0] == "unavailable"})
    if unavail:
        print(f"unavailable tiers (not checked here): {', '.join(unavail)}")
    if not fails:
        print("PASS: every swept name is safe verbatim on every available tier")
    else:
        print(f"FAIL: {len(fails)} (name,position,tier) cells break:")
        for (name, pos, tier), (_, detail) in sorted(fails.items()):
            print(f"  {tier:11} {pos:7} {name:12} {detail[:90]}")
