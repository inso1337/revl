"""Conformance matrix: every language construct against every backend.

    python3 tools/conformance.py [--json] [--validate] [--check-toolchains]

Backend divergence is this project's recurring bug class, and it is always
the same shape: a construct lands, some emitters take it, one does not, and
nobody notices until that tier is targeted by hand. `tests/test_cross_tier.py`
holds the floor for a few known-portable constructs; this walks the *whole*
surface and prints what each tier does with it.

Every case is a minimal source. A case that the frontend itself rejects is
reported as such (a language-level limit, not a backend gap). Otherwise each
of the six emitters runs and the result is OK or the refusal it raised.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

TIERS = ("python", "typescript", "rust", "java", "wasm", "go")

_EMITTERS: dict = {}


def emitter(tier: str):
    if tier not in _EMITTERS:
        spec = importlib.util.spec_from_file_location(
            f"revl_{tier}_emit", ROOT / "backends" / tier / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _EMITTERS[tier] = module
    return _EMITTERS[tier]


# --------------------------------------------------------------------------
# the corpus: (group, name, source). Keep each minimal and independent.
# --------------------------------------------------------------------------

def _component(body: str, *, services: str = "", requires: str = "",
               provides: str = "provides s: S") -> str:
    head = services or "service S { fn f(x: Int) -> Int }\n"
    return f"{head}component C {requires} {provides} {{\n{body}\n}}"


CASES: list[tuple[str, str, str]] = [
    # ---- service declarations
    ("service", "plain op", _component("  provide s { fn f(x) = x }")),
    ("service", "emission op",
     _component("  provide s { fn f(x) = 1 }",
                services="service S { emission fn f(x: Int) -> Int }\n")),
    # capability-scoped emission (docs/capabilities.md): the scope is a
    # checker/audit artefact, so every tier must emit the call unchanged
    ("service", "capability-scoped emission op",
     "service D { emission fn w(x: Int) -> Int }\n"
     "service S { emission[d] fn f(x: Int) -> Int }\n"
     "component C requires d: D provides s: S {\n"
     "  provide s { fn f(x) { return emit d.w(x) } }\n"
     "}"),
    ("service", "async op",
     _component("  provide s { async fn f(x) { return x } }",
                services="service S { async fn f(x: Int) -> Int }\n")),
    ("service", "commutative op",
     _component("  provide s { fn f(x) = x }",
                services="service S { commutative fn f(x: Int) -> Int }\n")),
    # delivery semantics (roadmap item 44): `idempotent` is the sibling of
    # `commutative` — an algebraic property declared on an emission; tiers
    # render it as metadata/comment (the py tier consumes it for retries)
    ("service", "idempotent emission op",
     _component("  provide s { fn f(x) = 1 }",
                services="service S { emission idempotent fn f(x: Int) -> Int }\n")),
    ("service", "void return",
     _component("  provide s { fn f(x) { return } }",
                services="service S { fn f(x: Int) }\n")),

    # ---- component bodies
    ("component", "config field",
     _component("  config { n: Int = 1 }\n  provide s { fn f(x) = x }")),
    ("component", "effect + undo",
     _component("  let m = effect Map.new() undo m.drop()\n"
                "  provide s { fn f(x) = x }")),
    ("component", "block effect setup",
     _component("  let m = effect { let k = 1  Map.new() } undo m.drop()\n"
                "  provide s { fn f(x) = x }")),
    ("component", "await (A1 boundary)",
     _component("  await Job.run(\"boot\")\n  provide s { fn f(x) = x }")),
    ("component", "fail (A8)",
     _component("  config { n: Int = 1 }\n  if (config.n < 1) { fail \"bad\" }\n"
                "  provide s { fn f(x) = x }")),
    ("component", "emit + compensate",
     _component("  emit bus.send(1) compensate bus.send(0)\n"
                "  provide s { fn f(x) = x }",
                services="service Bus { emission fn send(n: Int) -> Int }\n"
                         "service S { fn f(x: Int) -> Int }\n",
                requires="requires bus: Bus")),
    ("component", "isolate (realm)",
     _component("  isolate s in realm(\"t\")\n  provide s { fn f(x) = x }")),
    ("component", "intercept",
     _component("  intercept bus with { limit: 1 }\n  provide s { fn f(x) = x }",
                services="service Bus { fn ping(n: Int) -> Int }\n"
                         "service S { fn f(x: Int) -> Int }\n",
                requires="requires bus: Bus")),

    # ---- method bodies
    ("method", "let binding", _component("  provide s { fn f(x) { let y = x  return y } }")),
    ("method", "var + assign",
     _component("  provide s { fn f(x) { var y = x  y = 2  return y } }")),
    ("method", "method-time effect",
     _component("  let m = effect Map.new() undo m.drop()\n"
                "  provide s { fn f(x) { effect m.insert(\"k\", \"v\")  undo m.remove(\"k\")  return x } }")),
    ("method", "emit in method",
     _component("  provide s { fn f(x) { emit bus.send(x)  return x } }",
                services="service Bus { emission fn send(n: Int) -> Int }\n"
                         "service S { emission fn f(x: Int) -> Int }\n",
                requires="requires bus: Bus")),
    ("method", "emit as value",
     _component("  provide s { fn f(x) = emit bus.send(x) }",
                services="service Bus { emission fn send(n: Int) -> Int }\n"
                         "service S { emission fn f(x: Int) -> Int }\n",
                requires="requires bus: Bus")),
    # FR-1 (roadmap 77a): an arrow literal in a provide-method body binds its
    # parameters in the method's scope — the pure-helper + callback-arrow
    # escape (docs/expressible-iteration.md). The ts tier used to refuse the
    # arrow's own parameter as an unbound name; the corpus had no arrow-in-
    # method case, so the divergence was invisible to the matrix.
    ("method", "arrow param binds in method scope (FR-1)",
     "fn apply2(n: Int, f: (Int) -> Int) -> Int { return f(n) }\n"
     + _component("  provide s { fn f(x) { return apply2(x, v => v + 1) } }")),

    # ---- expressions (in a method body, the position that has diverged)
    ("expr", "arithmetic", _component("  provide s { fn f(x) = x + 1 * 2 }")),
    # Int32 bitwise operators (item 366, docs/arithmetic.md): `& ^ ~ | << >>`
    # in one kernel, plus the `.to_int32()` narrow and `.to_int()` widen that
    # bracket it. Every tier lowers these to native bit ops with the same
    # value (proved by execution in tests/test_cross_tier_execution.py); the
    # matrix pins that each one still *emits* on all six.
    ("expr", "Int32 bitwise",
     "fn mask(a: Int32, b: Int32, n: Int32) -> Int32 {\n"
     "  return (((a & b) ^ ~a) | (a << n)) >> 1.to_int32()\n"
     "}\n"
     + _component("  provide s { fn f(x) { let a = x.to_int32()\n"
                  "    return mask(a, a, 2.to_int32()).to_int() } }")),
    # `/` and Float `==` (item 433 riders R1 and R2). The corpus's only
    # arithmetic case was `x + 1 * 2`, so nothing here ever divided or
    # compared. This matrix asks only "did the emitter raise?", and
    # tests/test_conformance_validate.py asks "does the emitted code compile?".
    # Neither question can see a tier that emits cleanly and then computes a
    # different number, which is what java did for `Int / Int` (Java's
    # `long / long` is integer division) and for `==` on Float
    # (`Objects.equals` boxes to Double and compares `doubleToLongBits`). The
    # VALUES are asserted by execution in tests/test_cross_tier_execution.py's
    # "true division and IEEE float equality" probe; these two cases pin that
    # both constructs still emit on all six tiers and that what they emit is
    # accepted by each tier's own compiler.
    ("expr", "true division",
     "fn realdiv(a: Int, b: Int) -> Float { return a / b }\n"
     + _component("  provide s { fn f(x) { let q = realdiv(x, 2)  return x } }")),
    ("expr", "float equality",
     "fn feq(a: Float, b: Float) -> Bool { return a == b }\n"
     + _component("  provide s { fn f(x) { let b = feq(1.5, 1.5)  return x } }")),
    ("expr", "comparison", _component("  provide s { fn f(x) { let b = x > 1  return x } }")),
    ("expr", "unary", _component("  provide s { fn f(x) = -x }")),
    ("expr", "ternary", _component("  provide s { fn f(x) = x > 0 ? 1 : 0 }")),
    ("expr", "call a pure fn",
     "fn double(n: Int) -> Int { return n * 2 }\n"
     + _component("  provide s { fn f(x) = double(x) }")),
    ("expr", "list literal",
     _component("  provide s { fn f(x) { let xs = [1, 2]  return x } }")),
    ("expr", "record literal",
     "type R = { a: Int }\n"
     + _component("  provide s { fn f(x) { let r = { a: x }  return r.a } }")),
    ("expr", "index",
     _component("  provide s { fn f(x) { let xs = [1, 2]  return xs[0] } }")),
    ("expr", "stdlib method",
     _component("  provide s { fn f(x) { let xs = [1]  return xs.length() } }")),
    ("expr", "template string",
     _component("  provide s { fn f(x) = bus.note(`n=${x}`) }",
                services="service Bus { fn note(m: Str) -> Int }\n"
                         "service S { fn f(x: Int) -> Int }\n",
                requires="requires bus: Bus")),
    ("expr", "nullish ??",
     _component("  provide s { fn f(x) = bus.maybe(x) ?? 0 }",
                services="service Bus { fn maybe(n: Int) -> Opt[Int] }\n"
                         "service S { fn f(x: Int) -> Int }\n",
                requires="requires bus: Bus")),
    ("expr", "ADT construct + match",
     "type Outcome = Found(Int) | Missing\n"
     + _component("  provide s { fn f(x) { let o = Found(x)  "
                  "return match o { Found(v) => v, Missing => 0 } } }")),
    ("expr", "Opt Some/None",
     _component("  provide s { fn f(x) = 1 }",
                services="service S { fn f(x: Int) -> Opt[Int] }\n")),

    # ---- pure functions (top level)
    ("fn", "pure fn", "fn add(a: Int, b: Int) -> Int { return a + b }\n"
     + _component("  provide s { fn f(x) = add(x, 1) }")),
    ("fn", "while loop",
     "fn count(n: Int) -> Int { var i = 0  while (i < n) { i += 1 }  return i }\n"
     + _component("  provide s { fn f(x) = count(x) }")),
    ("fn", "for-of loop",
     "fn total(xs: List[Int]) -> Int { var t = 0  for (v of xs) { t += v }  return t }\n"
     + _component("  provide s { fn f(x) { let xs = [1, 2]  return total(xs) } }")),
    ("fn", "recursion",
     "fn fib(n: Int) -> Int { if (n < 2) { return n }  return fib(n - 1) + fib(n - 2) }\n"
     + _component("  provide s { fn f(x) = fib(x) }")),
    ("fn", "arrow lambda",
     "fn apply(n: Int) -> Int { let g = v => v + 1  return g(n) }\n"
     + _component("  provide s { fn f(x) = apply(x) }")),
    ("fn", "verified fn",
     "verified fn inc(n: Int) -> Int { return n + 1 }\n"
     + _component("  provide s { fn f(x) = inc(x) }")),

    # ---- types
    ("type", "Str service", "service S { fn f(x: Str) -> Str }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "Bool service", "service S { fn f(x: Bool) -> Bool }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "Float service", "service S { fn f(x: Float) -> Float }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "List service", "service S { fn f(x: List[Int]) -> List[Int] }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "Opt service", "service S { fn f(x: Opt[Int]) -> Opt[Int] }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "Result service",
     "service S { fn f(x: Int) -> Result[Int, Str] }\n"
     "component C provides s: S { provide s { fn f(x) = Ok(x) } }"),
    ("type", "record in signature",
     "type R = { a: Int }\nservice S { fn f(x: R) -> R }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "ADT in signature",
     "type O = Found(Int) | Missing\nservice S { fn f(x: O) -> O }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),
    ("type", "Map service", "service S { fn f(x: Map[Str, Int]) -> Map[Str, Int] }\n"
     "component C provides s: S { provide s { fn f(x) = x } }"),

    # ---- host blocks and tests
    ("extern", "pure extern",
     "extern pure fn h(n: Int) -> Int = @py { return n } = @ts { return n } = @go { return n } = @rs { return n } = @java { return n; } = @wasm { (local.get $p_n) }\n"
     + _component("  provide s { fn f(x) = h(x) }")),
    ("extern", "acquire extern",
     "extern acquire fn open_(n: Int) -> Int undo close_(handle)\n"
     "  = @py { return n } = @ts { return n } = @go { return n } = @rs { return n } = @java { return n; }\n"
     "extern pure fn close_(h: Int) -> Int = @py { return h } = @ts { return h } = @go { return h } = @rs { return h } = @java { return h; }\n"
     + _component("  provide s { fn f(x) = x }")),
    ("extern", "emission extern",
     "extern emission fn ship(n: Int) -> Int = @py { return n } = @ts { return n } = @go { return n } = @rs { return n } = @java { return n; } = @wasm { (local.get $p_n) }\n"
     + _component("  provide s { fn f(x) = emit ship(x) }",
                  services="service S { emission fn f(x: Int) -> Int }\n")),
    ("test", "test block",
     "fn inc(n: Int) -> Int { return n + 1 }\n"
     'test "inc" { assert inc(1) == 2 }\n'
     + _component("  provide s { fn f(x) = inc(x) }")),

    # ---- slice-then-method chaining (FR-7)
    # A slice's result is used immediately by another stdlib call; its static
    # kind must survive, or the emitted TS reads a `string | T[]` union and
    # `.split`/`.join`/`.push`/return-assignability all fail tsc. The corpus
    # missed this class because no case chained a method onto a slice — the
    # harness hit `rest.split(" ")` after `resp.slice(10, len)` with 4 errors.
    ("slice", "Str then split",
     'fn parse(rest: Str) -> List[Str] { return rest.slice(0, 10).split(" ") }\n'
     + _component('  provide s { fn f(x) { let p = parse("ab cd")  return p.length() } }')),
    ("slice", "Str then length",
     "fn words(s: Str) -> Int { return s.slice(1, 5).length() }\n"
     + _component('  provide s { fn f(x) = words("hello") }')),
    ("slice", "List then join",
     'fn firsts(xs: List[Str]) -> Str { return xs.slice(0, 2).join(", ") }\n'
     + _component('  provide s { fn f(x) = firsts(["a", "b"]).length() }')),
    ("slice", "List then push",
     "fn keep(xs: List[Int]) -> List[Int] { return xs.slice(0, 2).push(9) }\n"
     + _component("  provide s { fn f(x) = keep([1, 2, 3])[0] }")),
    ("slice", "List returned",
     "fn take3(xs: List[Int]) -> List[Int] { return xs.slice(0, 3) }\n"
     + _component("  provide s { fn f(x) = take3([1, 2, 3, 4])[0] }")),
    ("slice", "List then index",
     "fn head(xs: List[Int]) -> Int { let ys = xs.slice(1, 3)  return ys[0] }\n"
     + _component("  provide s { fn f(x) = head([1, 2, 3]) }")),
]


# --------------------------------------------------------------------------
# the answer each case computes — the difference between "it compiled" and
# "it means the same thing" (issue #244)
# --------------------------------------------------------------------------
#
# Everything above this line certifies COMPILATION. `run()` asks "did the
# emitter raise?"; `tools/validate.py`'s six validators ask "does that output
# survive its own toolchain?". Neither can see a tier that emits cleanly, type-
# checks cleanly, and then computes a different number — which is exactly what
# java did for `Int / Int` (item 433 rider R1) and what TypeScript did for
# `{a: 1} == {a: 1}`. A matrix of `ok` cells over two different claim strengths
# is how that stayed invisible, so the corpus now says, per case, WHICH claim
# it makes.
#
# A case is EXECUTED when its construct has an answer a pure call can reach.
# `PROBES` gives that call and the answer it must produce:
#
#     label -> (probe declaration, the answer as a revl literal)
#
# The probe is APPENDED to the case source, so the case itself is unchanged and
# still emitted and validated exactly as before. The executed program is
# `case + probe + test "conformance_answer" { assert probe() == <answer> }`,
# and EVERY executed tier asserts the SAME literal. That makes this a
# differential, not a per-tier smoke test: a tier that builds and runs but
# computes something else is the only thing that can fail here, and a tier that
# quietly agrees with a wrong answer is caught too, because the answer is
# authored rather than read off whichever tier happened to run first.
#
# Answers are chosen to be unambiguous and to actually exercise the construct.
# `expr/Int32 bitwise` is the sharpest of them: `mask(6, 6, 2)` is -1, which
# separates an arithmetic `>>` from a logical `>>>` and a 32-bit lane from a
# 64-bit one — three things the six tiers spell differently.

PROBES: dict[str, tuple[str, str]] = {
    "method/arrow param binds in method scope (FR-1)":
        ("pub fn probe() -> Int { return apply2(41, v => v + 1) }", "42"),
    # -1: `(((6 & 6) ^ ~6) | (6 << 2)) >> 1` in a 32-bit lane with an
    # ARITHMETIC right shift. A logical shift, or a 64-bit `~`, answers
    # differently — which is the point of executing it.
    "expr/Int32 bitwise":
        ("pub fn probe() -> Int { let a = 6.to_int32()\n"
         "  return mask(a, a, 2.to_int32()).to_int() }", "-1"),
    # 3.5, not 3: `Int / Int` is true division (docs/arithmetic.md). java
    # emitted `7L / 2L` here and answered 3 while typechecking clean.
    "expr/true division":
        ("pub fn probe() -> Float { return realdiv(7, 2) }", "3.5"),
    "expr/float equality":
        ("pub fn probe() -> Bool { return feq(1.5, 1.5) }", "true"),
    "expr/call a pure fn":
        ("pub fn probe() -> Int { return double(21) }", "42"),
    "fn/pure fn":
        ("pub fn probe() -> Int { return add(20, 22) }", "42"),
    "fn/while loop":
        ("pub fn probe() -> Int { return count(7) }", "7"),
    "fn/for-of loop":
        ("pub fn probe() -> Int { return total([1, 2, 3]) }", "6"),
    "fn/recursion":
        ("pub fn probe() -> Int { return fib(10) }", "55"),
    "fn/arrow lambda":
        ("pub fn probe() -> Int { return apply(41) }", "42"),
    "fn/verified fn":
        ("pub fn probe() -> Int { return inc(41) }", "42"),
    # the one probe whose answer travels through a host block on every tier
    "extern/pure extern":
        ("pub fn probe() -> Int { return h(42) }", "42"),
    "test/test block":
        ("pub fn probe() -> Int { return inc(41) }", "42"),
    "slice/Str then split":
        ('pub fn probe() -> List[Str] { return parse("ab cd ef") }',
         '["ab", "cd", "ef"]'),
    "slice/Str then length":
        ('pub fn probe() -> Int { return words("hello") }', "4"),
    "slice/List then join":
        ('pub fn probe() -> Str { return firsts(["a", "b", "c"]) }', '"a, b"'),
    "slice/List then push":
        ("pub fn probe() -> List[Int] { return keep([1, 2, 3]) }", "[1, 2, 9]"),
    "slice/List returned":
        ("pub fn probe() -> List[Int] { return take3([1, 2, 3, 4]) }", "[1, 2, 3]"),
    "slice/List then index":
        ("pub fn probe() -> Int { return head([1, 2, 3]) }", "2"),
}

# Why the rest carry the weaker claim. These are not "not done yet" — they are
# cases whose answer a pure call cannot reach, and saying so is the whole point
# of the claim column.
COMPILE_ONLY_REASONS = {
    "signature": ("declares a type or service shape and computes no value — "
                  "there is no answer to compare"),
    "activation": ("its value is produced inside a component provide-method (or "
                   "during activation), so reaching it means booting the "
                   "composition on each tier's runtime, not calling a pure fn"),
}


def claim(label: str) -> str:
    """`exec` or `compile-only` — which kind of claim this row's cells make."""
    return "exec" if label in PROBES else "compile-only"


def compile_only_reason(label: str, group: str) -> str | None:
    """The reason key for a compile-only case, or None when it is executed.

    Keyed on the case's group rather than a 43-entry table: a `type/*` case
    declares a shape and evaluates nothing, and every other unprobed case puts
    its value behind a provide-method. A new case therefore gets a reason for
    free, and a new EXECUTABLE case has to be given a probe to lose one.
    """
    if label in PROBES:
        return None
    return "signature" if group == "type" else "activation"


def probe_program(label: str, source: str) -> str | None:
    """The case source plus its probe and the assertion every tier must satisfy.

    None for a compile-only case. The assertion literal is the corpus's own
    declared answer, identical on every tier — that identity is what makes the
    execution pass a cross-tier comparison rather than a per-tier smoke test.
    """
    entry = PROBES.get(label)
    if entry is None:
        return None
    declaration, answer = entry
    return (f"{source}\n{declaration}\n"
            f'test "conformance_answer" {{ assert probe() == {answer} }}\n')


def executable_cases() -> list[tuple[str, str]]:
    """(label, probe program) for every case whose answer can be executed."""
    out = []
    for group, name, source in CASES:
        label = f"{group}/{name}"
        program = probe_program(label, source)
        if program is not None:
            out.append((label, program))
    return out

def _emit_kwargs(tier: str, index: int) -> dict:
    """Per-tier emitter options needed to validate many cases side by side.

    java and go both need one: every case emits into a package/class that
    would otherwise collide across the corpus (java's `revl.Components`, go's
    `package emitted`), so a single `javac`/`go build` over all of them would
    see 47 duplicates rather than 47 programs. `package_name` is a normal
    emitter parameter, so what gets validated is still real emitter output.
    """
    return {"package_name": f"case_{index}"} if tier in ("java", "go") else {}


def run(all_cases: bool = False, validate: bool = False) -> dict:
    report: dict = {"cases": [], "frontend_rejected": [], "gaps": {}}
    # Emitted artifacts per tier, kept for the validation pass: emitting twice
    # would be wasteful and could not be trusted to produce the same text.
    artifacts: dict[str, list[tuple[str, object]]] = {t: [] for t in TIERS}

    for index, (group, name, source) in enumerate(CASES):
        label = f"{group}/{name}"
        try:
            ir = compile_source(source)
        except RevlError as error:
            report["frontend_rejected"].append(
                {"case": label, "message": str(error).splitlines()[0]})
            continue

        row = {"case": label, "ir_version": ir.get("ir_version"),
               "tiers": {}, "emit_kind": {}}
        for tier in TIERS:
            try:
                artifacts[tier].append(
                    (label, emitter(tier).emit(ir, **_emit_kwargs(tier, index))))
                row["tiers"][tier] = "ok"
                row["emit_kind"][tier] = "ok"
            except Exception as exc:  # noqa: BLE001 — any refusal is the datum
                message = str(exc).splitlines()[0]
                # A tier's own EmitError is a *deliberate* refusal — a named
                # tier limit the emitter chose to raise rather than fall through
                # (the wasm i32 boundary, java's lack of anonymous function
                # types). Any other exception is an unhandled crash: the emitter
                # had no case for a construct it should express, which is a real
                # gap. This is the automatable form of docs/conformance.md's
                # deliberate-vs-gap split, keyed on how the refusal was raised.
                deliberate = isinstance(exc, getattr(emitter(tier), "EmitError", ()))
                row["tiers"][tier] = message
                row["emit_kind"][tier] = "limit" if deliberate else "gap"
                report["gaps"].setdefault(tier, []).append(
                    {"case": label, "message": message, "deliberate": deliberate})
        report["cases"].append(row)

    if validate:
        report["validation"] = _validate(artifacts)
    return report


def _validate(artifacts: dict[str, list[tuple[str, object]]]) -> dict:
    """Hand each tier's emitted artifacts to that tier's real toolchain.

    A tier whose toolchain is missing reports `unavailable` with the reason —
    never `ok`. "Nothing checked it" and "it passed" are different answers and
    this matrix exists because conflating them hid a bug for months.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from validate import VALIDATORS  # noqa: PLC0415 — resolved next to this file

    out: dict = {}
    for tier in TIERS:
        validator = VALIDATORS[tier]
        entry: dict = {"depth": validator.depth, "results": {}}
        reason = validator.unavailable()
        if reason:
            entry["status"] = "unavailable"
            entry["reason"] = reason
        else:
            try:
                results = validator.check(artifacts[tier])
            except Exception as exc:  # noqa: BLE001 — a broken harness is a datum too
                entry["status"] = "error"
                entry["reason"] = str(exc).splitlines()[0]
            else:
                entry["results"] = {label: {"status": status, "detail": detail}
                                    for label, (status, detail) in results.items()}
                failures = [k for k, v in results.items() if v[0] != "ok"]
                entry["status"] = "fail" if failures else "ok"
                entry["failures"] = failures
        out[tier] = entry
    return out


def execute(report: dict | None = None) -> dict:
    """Run every executable case on every executed tier and compare ANSWERS.

    The compile validators answer "does this output survive its toolchain?".
    This answers "do the tiers that ran agree on what it evaluates to?" — the
    question `conformance` is named for and did not ask (issue #244).

    Returns

        {"tiers": {tier: {status, depth, results|reason}},
         "cases": {label: {tier: "agree"|"differs"|"-"}},
         "agreed": [label, ...], "diverged": {label: {tier: detail}},
         "executed": N, "compile_only": {reason: [label, ...]}}

    where a case is `agree` on a tier when that tier ran the program and the
    corpus's declared answer held. Because every tier asserts the SAME literal,
    the set of `agree` tiers for a case is a cross-tier agreement, not N
    independent smoke tests.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from validate import EXECUTORS  # noqa: PLC0415 — resolved next to this file

    cases = executable_cases()
    admitted = {row["case"] for row in (report or run())["cases"]}
    out: dict = {"tiers": {}, "cases": {label: {} for label, _ in cases},
                 "executed": len(cases), "agreed": [], "diverged": {},
                 "compile_only": compile_only_index(admitted)}
    for tier, executor in EXECUTORS.items():
        entry: dict = {"depth": executor.depth}
        reason = executor.unavailable()
        if reason:
            entry["status"] = "unavailable"
            entry["reason"] = reason
            out["tiers"][tier] = entry
            for label, _ in cases:
                out["cases"][label][tier] = "-"
            continue
        try:
            results = executor.check(cases)
        except Exception as exc:  # noqa: BLE001 — a broken harness is a datum too
            entry["status"] = "error"
            entry["reason"] = str(exc).splitlines()[0]
            out["tiers"][tier] = entry
            for label, _ in cases:
                out["cases"][label][tier] = "-"
            continue
        expected = {label for label, _ in cases}
        missing = sorted(expected - set(results))
        extra = sorted(set(results) - expected)
        if missing or extra:
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if extra:
                details.append("unexpected: " + ", ".join(extra))
            entry["status"] = "error"
            entry["reason"] = (
                "executor returned an incomplete result set ("
                + "; ".join(details) + ")"
            )
            out["tiers"][tier] = entry
            for label, _ in cases:
                out["cases"][label][tier] = "-"
            continue
        failures = {}
        for label, (status, detail) in results.items():
            agreed = status == "ok"
            out["cases"][label][tier] = "agree" if agreed else "differs"
            if not agreed:
                failures[label] = detail
                out["diverged"].setdefault(label, {})[tier] = detail
        entry["status"] = "fail" if failures else "ok"
        entry["failures"] = failures
        out["tiers"][tier] = entry

    for label, _ in cases:
        verdicts = out["cases"][label]
        ran = [t for t, v in verdicts.items() if v != "-"]
        if ran and all(verdicts[t] == "agree" for t in ran):
            out["agreed"].append(label)
    if report is not None:
        report["execution"] = out
    return out


def compile_only_index(labels: set[str] | None = None) -> dict[str, list[str]]:
    """reason key -> the cases carrying it. The weaker half of the matrix,
    enumerated rather than left to look like the executed half.

    `labels` restricts the count to the cases that actually reached the tiers —
    a case the frontend rejects is neither executed nor compile-only, it is a
    language-level limit reported on its own line.
    """
    index: dict[str, list[str]] = {}
    for group, name, _ in CASES:
        label = f"{group}/{name}"
        if labels is not None and label not in labels:
            continue
        reason = compile_only_reason(label, group)
        if reason:
            index.setdefault(reason, []).append(label)
    return index


def _check_toolchains(*, as_json: bool = False) -> int:
    """Which tiers can actually be validated — and is that all of them?

    `--validate` degrades gracefully: a tier whose compiler is absent reports
    `unavailable` and the run still exits 0. That is right for a laptop and
    wrong for CI, where "no toolchain" means the job is misconfigured and the
    tier's emitted code went unchecked while the build read green. This is the
    same hazard as the wasm tier's tests skipping because nobody installed
    wasmtime; it wants the same loud answer.

    Exits non-zero if any tier's validator cannot run.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from validate import EXECUTORS, VALIDATORS  # noqa: PLC0415 — next to this file

    status = {}
    for tier in TIERS:
        validator = VALIDATORS[tier]
        status[tier] = {"depth": validator.depth, "reason": validator.unavailable()}
    # The executed tiers are a second, stronger claim and need their own
    # runtimes (a vitest install, a go toolchain), which are NOT implied by the
    # compile validators being ready. A missing one here means the matrix
    # silently falls back to compile-only for that tier — the exact conflation
    # issue #244 is about — so it is a broken job here just the same.
    runtimes = {}
    for tier, executor in EXECUTORS.items():
        runtimes[tier] = {"depth": executor.depth, "reason": executor.unavailable()}

    missing = [t for t, v in status.items() if v["reason"]]
    missing_runtimes = [t for t, v in runtimes.items() if v["reason"]]
    if as_json:
        print(json.dumps({"toolchains": status, "missing": missing,
                          "runtimes": runtimes,
                          "missing_runtimes": missing_runtimes}, indent=2))
    else:
        print("validator toolchains (compile depth):\n")
        for tier in TIERS:
            entry = status[tier]
            if entry["reason"]:
                print(f"  {tier:<11} UNAVAILABLE — {entry['reason']}")
            else:
                print(f"  {tier:<11} ready ({entry['depth']})")
        print("\nexecuted tiers (answer depth):\n")
        for tier in EXECUTORS:
            entry = runtimes[tier]
            if entry["reason"]:
                print(f"  {tier:<11} UNAVAILABLE — {entry['reason']}")
            else:
                print(f"  {tier:<11} ready ({entry['depth']})")
        if missing:
            print(f"\n{len(missing)} tier(s) cannot be validated here: "
                  f"{', '.join(missing)}.\nTheir emitted code would go "
                  f"unchecked while the run still reported success.")
        if missing_runtimes:
            print(f"\n{len(missing_runtimes)} tier(s) cannot be EXECUTED here: "
                  f"{', '.join(missing_runtimes)}.\nTheir answers would go "
                  f"uncompared while the run still reported success.")
        if not missing and not missing_runtimes:
            print("\nall tiers validatable and every executed tier runnable — "
                  "no silent gaps")
    return 1 if (missing or missing_runtimes) else 0


# --------------------------------------------------------------------------
# the revl-native column: the self-host compiler compiling itself
# --------------------------------------------------------------------------
#
# The six tiers above are *host* runtimes. revl also has a native path, and the
# matrix should show revl conforming to itself, not only to its hosts. Two
# facts stand for that native path:
#
#   * wasm is revl's *first-party* runtime (the cordis-wasm substrate) — it is
#     one of the six columns, marked as first-party in the rendered legend.
#   * the self-host compiler (`selfhost/compile.rvl`) compiles revl source to
#     target code with no reference compiler in the chain (docs/selfhost-
#     compile.md). Running every conformance construct through it, and checking
#     the result byte-for-byte against the reference emitter, answers "does revl
#     compile *itself* on this construct?".
#
# That check is the `revl` column below. It is cheap (the native pipeline loads
# once and runs the whole corpus in well under a second) and deterministic (a
# byte comparison), so it belongs in the regenerated matrix rather than a bench.

SELFHOST_TIER = "revl"


def _load_selfhost():
    """Load `selfhost/compile.rvl` as the reference python backend runs it.

    Mirrors `tests/test_selfhost_compile.py`'s harness exactly: compile the
    native driver with the reference frontend, emit it to python through the
    reference python backend, and exec it under a lazy `runtime` stub the pure
    functions never touch. Returns `(compile_to, reference_py_emit, compile_source)`.
    """
    import types  # noqa: PLC0415

    from revl import compile_files  # noqa: PLC0415

    pyemit = emitter("python")  # backends/python/emit.py — the reference emitter
    ir = compile_files([str(ROOT / "selfhost" / "compile.rvl")])
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(pyemit.emit(ir), "selfhost_compile.py", "exec"), namespace)  # noqa: S102
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["compile_to"], pyemit.emit, compile_source


def selfhost_column() -> dict[str, str] | None:
    """Per-case verdict for the revl-native self-compile, or None if it cannot load.

    ok    — the native pipeline's output is byte-identical to the reference
            emitter's: revl compiles itself correctly for this construct.
    limit — the native pipeline runs but its output is not yet byte-exact, or
            its own gate refuses the construct. This is the documented self-host
            frontier (docs/selfhost-compile.md), not a regression.
    gap   — the native pipeline crashed. A real defect.
    """
    try:
        compile_to, reference_emit, compile_src = _load_selfhost()
    except Exception:  # noqa: BLE001 — a self-host that will not even load is its own signal
        return None

    verdicts: dict[str, str] = {}
    for group, name, source in CASES:
        label = f"{group}/{name}"
        try:
            produced = compile_to(source, "py")
        except Exception:  # noqa: BLE001 — an unexpected crash is the datum
            verdicts[label] = "gap"
            continue
        if produced.startswith(("REFUSED", "UNKNOWN_TIER")):
            verdicts[label] = "limit"  # the native gate declined — a frontier edge
            continue
        try:
            expected = reference_emit(compile_src(source))
        except Exception:  # noqa: BLE001 — no oracle to compare against; call it a frontier edge
            verdicts[label] = "limit"
            continue
        verdicts[label] = "ok" if produced == expected else "limit"
    return verdicts


# --------------------------------------------------------------------------
# the markdown matrix + its README embedding (roadmap item 328)
# --------------------------------------------------------------------------

_SHORT = {"python": "py", "typescript": "ts"}
_GLYPH = {"ok": "ok", "limit": "lim", "gap": "**GAP**"}

README_START = "<!-- CONFORMANCE-MATRIX:START -->"
README_END = "<!-- CONFORMANCE-MATRIX:END -->"
_README_ANCHOR = "## The toolchain is the developer surface"

# The per-tier emit-sweep summary (issue #233). It was authored by hand from a
# one-off run and then rotted: rust read 3 refusals against a measured 0 and
# java 3 against a measured 1, for months, while the tool that answers the
# question exactly sat in this file. Same fix as the matrix above — generate it
# and let the staleness gate diff it — so the counts cannot outlive their run.
SWEEP_START = "<!-- CONFORMANCE-SWEEP:START -->"
SWEEP_END = "<!-- CONFORMANCE-SWEEP:END -->"


def _short(tier: str) -> str:
    return _SHORT.get(tier, tier)


def _perf_headline() -> str | None:
    """The committed admission round-trip median from the bench result.

    Read from `bench/results/admission-latency.md`, which is a committed
    artifact (a re-run overwrites it deliberately), so the number is stable
    across machines — unlike a live timing, which could never sit in a block a
    staleness gate diffs. Live emit timings are a `--markdown` readout instead.
    """
    path = ROOT / "bench" / "results" / "admission-latency.md"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if "compile + admit" in line and line.count("|") >= 3:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[1]:
                return cells[1]
    return None


def _summary_rows(report: dict, selfhost: dict[str, str] | None) -> list[tuple[str, int, int, int]]:
    """(tier, ok, limit, gap) per host tier, then the revl self-host row."""
    rows = []
    for tier in TIERS:
        kinds = [row["emit_kind"][tier] for row in report["cases"]]
        rows.append((_short(tier), kinds.count("ok"),
                     kinds.count("limit"), kinds.count("gap")))
    if selfhost is not None:
        verdicts = [selfhost.get(row["case"], "gap") for row in report["cases"]]
        rows.append((f"{SELFHOST_TIER} (self-host)", verdicts.count("ok"),
                     verdicts.count("limit"), verdicts.count("gap")))
    return rows


def _markdown(report: dict, selfhost: dict[str, str] | None) -> str:
    """The construct x tier matrix as a deterministic markdown block.

    Emit-only (pure Python, no toolchain), so it regenerates byte-identically
    on any machine — which is what lets a CI gate diff the committed copy
    against a fresh generation. The `--validate` second question (does the
    emitted code survive its real compiler?) needs every toolchain present and
    is gated separately by `tests/test_conformance_validate.py`.
    """
    columns = list(TIERS) + ([SELFHOST_TIER] if selfhost is not None else [])
    cases = report["cases"]
    out: list[str] = []

    out.append("_Generated by `python3 tools/conformance.py --write-readme`. "
               "Do not edit by hand._")
    out.append("")
    out.append("`ok` the emitter produces code · `lim` a deliberate tier limit "
               "· **GAP** a real gap (emitter has no case). "
               "`wasm` is revl's first-party runtime; `revl` is the self-host "
               "pipeline compiling itself, where `ok` means byte-identical to "
               "the reference emitter and `lim` is the self-host frontier.")
    out.append("")
    # The claim column exists because the table used to be uniform over two
    # different claim strengths, which is how the missing execution tier stayed
    # invisible (issue #244). It is derived from the corpus (pure data), so it
    # regenerates deterministically with the rest of the block; whether the
    # executed rows AGREED is a `--execute` run, gated separately.
    out.append("**claim** says how strong each row is. `exec` the tiers RUN the "
               "construct and assert one declared answer, so a tier that "
               "compiles and then computes something else fails "
               "(`python3 tools/conformance.py --execute`; python, typescript "
               "and go execute today). `compile-only` nothing runs it: the "
               "`ok`/`lim` cells on that row mean the emitter produced code its "
               "own toolchain accepts, and say nothing about what it evaluates "
               "to.")
    out.append("")

    out.append("| construct | claim | " + " | ".join(_short(t) for t in columns) + " |")
    out.append("|" + "|".join(["---"] * (len(columns) + 2)) + "|")
    for row in cases:
        cells = [_GLYPH[row["emit_kind"][t]] for t in TIERS]
        if selfhost is not None:
            cells.append(_GLYPH.get(selfhost.get(row["case"], "gap"), "**GAP**"))
        out.append("| " + row["case"] + " | " + claim(row["case"]) + " | "
                   + " | ".join(cells) + " |")

    out.append("")
    out.append(f"**Per tier** ({len(cases)} constructs emitted; "
               f"{len(report['frontend_rejected'])} rejected by the frontend, below):")
    out.append("")
    out.append("| tier | ok | deliberate limit | real gap |")
    out.append("|---|---|---|---|")
    for tier, ok, limit, gap in _summary_rows(report, selfhost):
        gap_cell = f"**{gap}**" if gap else "0"
        out.append(f"| {tier} | {ok} | {limit} | {gap_cell} |")

    executed = [row["case"] for row in cases if row["case"] in PROBES]
    index = compile_only_index({row["case"] for row in cases})
    out.append("")
    out.append(f"**Per claim** ({len(executed)} executed, "
               f"{sum(len(v) for v in index.values())} compile-only):")
    out.append("")
    out.append("| claim | cases | what a green row proves |")
    out.append("|---|---|---|")
    out.append(f"| `exec` | {len(executed)} | every executed tier ran the "
               f"construct and agreed on one declared answer |")
    for reason in sorted(index):
        out.append(f"| `compile-only` ({reason}) | {len(index[reason])} | "
                   f"the emitter produced code its toolchain accepts; "
                   f"{COMPILE_ONLY_REASONS[reason]} |")

    if report["frontend_rejected"]:
        out.append("")
        rejected = ", ".join(f"`{item['case']}`" for item in report["frontend_rejected"])
        out.append(f"Rejected by the frontend (a language-level limit, not a "
                   f"backend gap): {rejected}.")

    perf = _perf_headline()
    if perf:
        out.append("")
        out.append(f"**Performance.** In-memory admission round-trip "
                   f"(compile + gate) median **{perf}** per candidate component "
                   f"([bench/results/admission-latency.md]"
                   # The block is spliced into docs/conformance.md, so the href
                   # is relative to docs/ — without the `../` it resolved to a
                   # docs/bench/ that has never existed.
                   f"(../bench/results/admission-latency.md)). This is the "
                   f"per-candidate cost an agent loop pays at the v3.0 gate.")

    return "\n".join(out)


# --------------------------------------------------------------------------
# the per-tier emit sweep (issue #233)
# --------------------------------------------------------------------------

# Why a refusal happened, in the words of the emitter that raised it. Each rule
# is (matcher, label); the first that matches wins, so order is significance
# order. A refusal no rule recognises falls back to its case label rather than
# to a bucket named "other" — an unnamed cause should read as conspicuous in
# the committed table, not as a tidy count.
_REFUSAL_RULES: tuple[tuple[str, str], ...] = (
    ("config block", "config block"),
    ("host builtin", "host builtin"),
    ("arrow values are not lowerable", "function-typed signature"),
    ("declared function type", "function-typed signature"),
)

_EXTERN_BODY = "has no @"
_TYPE_REFUSAL = " is not lowerable"


def refusal_reason(message: str) -> str:
    """Classify one emitter refusal into the cause the summary table names.

    Keyed on the emitter's own message, which is the only place the cause is
    stated. A type refusal names the type (`Float`, `Map[Str, Int]`) and an
    extern refusal names the missing body tag (`@rs`), so both are read back
    out rather than hard-coded per tier — a new refusing type or tier needs no
    edit here.
    """
    for needle, label in _REFUSAL_RULES:
        if needle in message:
            return label

    if _EXTERN_BODY in message:
        tag = message.split(_EXTERN_BODY, 1)[1].split()[0]
        return f"extern with no `@{tag}` body"

    if "type '" in message and _TYPE_REFUSAL in message:
        named = message.split("type '", 1)[1].split("'", 1)[0]
        base = named.split("[", 1)[0]
        return f"`{base}` signature"

    return message


def _deliberate_cell(items: list[dict]) -> str:
    """The `deliberate` column: each cause with how many cases hit it.

    Ordered by count then label so two runs of the same tree render the same
    string — the staleness gate diffs this text byte for byte.
    """
    if not items:
        return "—"
    counts: dict[str, int] = {}
    for item in items:
        reason = refusal_reason(item["message"])
        counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{reason} ({count})" for reason, count in ordered)


def _sweep_markdown(report: dict) -> str:
    """The per-tier refusal summary, generated from the same walk as the matrix.

    Emit-only and therefore deterministic, exactly like `_markdown`. The three
    columns answer three different questions and must not be collapsed: how
    many constructs the tier refused, how many of those are *gaps* (an emitter
    with no case for a construct it should express), and what the deliberate
    ones are.

    Scope, deliberately: every cell here is about *emit*, and `--validate`'s
    second question (does the emitted code survive its real compiler?) is
    still only a compile. Issue #244 is that the validators never *execute*
    what they compile, so a row that reads clean proves compilation and not
    behaviour. When that lands, this table wants a fourth column separating
    executed rows from compile-only ones — a count that means "ran and agreed"
    must not render the same as one that means "parsed". The gate is already
    shaped for it: add the column here and the staleness diff carries it.
    """
    out: list[str] = []
    out.append("_Generated by `python3 tools/conformance.py --write-readme`. "
               "Do not edit by hand._")
    out.append("")
    out.append("| tier | refusals | real gaps | deliberate |")
    out.append("|---|---|---|---|")
    for tier in TIERS:
        items = report["gaps"].get(tier) or []
        gaps = [item for item in items if not item["deliberate"]]
        deliberate = [item for item in items if item["deliberate"]]
        gap_cell = f"**{len(gaps)}**" if gaps else "0"
        out.append(f"| {_short(tier)} | {len(items)} | {gap_cell} | "
                   f"{_deliberate_cell(deliberate)} |")

    clean = [_short(t) for t in TIERS if not (report["gaps"].get(t) or [])]
    refusing = [_short(t) for t in TIERS if (report["gaps"].get(t) or [])]
    any_gap = any(not item["deliberate"]
                  for items in report["gaps"].values() for item in items)
    if clean:
        out.append("")
        out.append(f"**{_and_list(clean)} are at zero"
                   + ("" if not refusing else
                      f"; every remaining refusal on {_and_list(refusing)} is "
                      + ("a deliberate tier limit" if not any_gap
                         else "listed above, and the bold count is a real gap"))
                   + ".**")
    return "\n".join(out)


def _and_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def sweep_block(report: dict) -> str:
    return f"{SWEEP_START}\n{_sweep_markdown(report)}\n{SWEEP_END}"


def _print_emit_timings(report: dict) -> None:
    """A live per-tier emit-cost readout for the operator running `--markdown`.

    Deliberately NOT part of the embedded block: a wall-clock number could never
    survive the staleness diff. It times only the emit step (each construct's IR
    lowered by each tier), which is the cheap measurement the walk already does;
    the compile/typecheck cost is the `--validate` question, gated separately.
    """
    import time  # noqa: PLC0415

    irs = []
    for group, name, source in CASES:
        try:
            irs.append((f"{group}/{name}", compile_source(source)))
        except RevlError:
            pass

    print("\n_Live per-tier emit timing (local, not embedded — timings are "
          "non-deterministic):_\n")
    print("| tier | emit (ms) | per construct |")
    print("|---|---|---|")
    for tier in TIERS:
        module = emitter(tier)
        started = time.perf_counter()
        emitted = 0
        for index, (label, ir) in enumerate(irs):
            try:
                module.emit(ir, **_emit_kwargs(tier, index))
                emitted += 1
            except Exception:  # noqa: BLE001 — a refusal is not timed work
                pass
        elapsed = (time.perf_counter() - started) * 1000
        per = elapsed / emitted if emitted else 0.0
        print(f"| {_short(tier)} | {elapsed:.1f} | {per:.2f} ms |")


def readme_block(report: dict, selfhost: dict[str, str] | None) -> str:
    return f"{README_START}\n{_markdown(report, selfhost)}\n{README_END}"


def _splice_readme(text: str, block: str) -> str:
    """Replace the marked block in README text, inserting a section if absent."""
    if README_START in text and README_END in text:
        pre = text[:text.index(README_START)]
        post = text[text.index(README_END) + len(README_END):]
        return pre + block + post
    section = f"## Conformance matrix\n\n{block}\n\n"
    if _README_ANCHOR in text:
        return text.replace(_README_ANCHOR, section + _README_ANCHOR, 1)
    return text.rstrip("\n") + "\n\n" + section


def _splice_marked(text: str, start: str, end: str, block: str) -> str:
    """Replace an already-marked block. The markers must exist.

    Unlike `_splice_readme` there is no fallback that invents a section: this
    block replaces prose that was there before it, so a missing marker means
    someone deleted the generated table rather than that it has yet to land,
    and inventing a new section elsewhere in the file would hide that.
    """
    if start not in text or end not in text:
        raise SystemExit(
            f"docs/conformance.md is missing the {start}/{end} markers — the "
            "generated block cannot be spliced. Restore them.")
    pre = text[:text.index(start)]
    post = text[text.index(end) + len(end):]
    return pre + block + post


def _write_readme(*, check_only: bool) -> int:
    """Regenerate the generated blocks in docs/conformance.md.

    `check_only` gates staleness for CI. Returns 0 when the committed blocks
    already match a fresh generation (or were written), 1 when `check_only`
    finds either stale — the same shape as the generated-artifact gates the
    rest of the tree uses.

    Two blocks, one walk: the construct x tier matrix and the per-tier emit
    sweep are two views of the same `run()`, so generating them together costs
    nothing extra and, more to the point, makes it impossible for one to be
    regenerated while the other rots (issue #233 — that is exactly what had
    happened to the sweep, which had been authored by hand).
    """
    report = run()
    selfhost = selfhost_column()
    block = readme_block(report, selfhost)
    # The full construct matrix lives in docs/conformance.md, not the README —
    # the README carries only a short qualitative summary, so the front page
    # stays a front page. This block is still generated + gated, never authored.
    doc = ROOT / "docs" / "conformance.md"
    text = doc.read_text(encoding="utf-8")
    updated = _splice_readme(text, block)
    updated = _splice_marked(updated, SWEEP_START, SWEEP_END, sweep_block(report))

    if check_only:
        if updated == text:
            print("docs/conformance.md matrix and emit sweep are up to date.")
            return 0
        print("docs/conformance.md matrix or emit sweep is STALE — regenerate "
              "it with `python3 tools/conformance.py --write-readme` (or "
              "`make matrix`) and commit the result.", file=sys.stderr)
        return 1

    if updated == text:
        print("docs/conformance.md matrix and emit sweep already up to date.")
    else:
        doc.write_text(updated, encoding="utf-8")
        print("docs/conformance.md matrix and emit sweep regenerated.")
    return 0


def _matrix(report: dict, cell) -> None:
    width = max(len(row["case"]) for row in report["cases"]) + 2
    print(f"{'case'.ljust(width)}" + "".join(t[:6].ljust(8) for t in TIERS))
    print("-" * (width + 8 * len(TIERS)))
    for row in report["cases"]:
        cells = "".join(cell(row, t).ljust(8) for t in TIERS)
        print(f"{row['case'].ljust(width)}{cells}")


def _execute_command(*, as_json: bool, require: bool) -> int:
    """`--execute`: the differential. Prints per-case agreement across the
    executed tiers, and what the compile-only rows are NOT claiming."""
    result = execute()
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        tiers = list(result["tiers"])
        width = max((len(label) for label in result["cases"]), default=10) + 2
        print("execute — do the tiers that RAN agree on the answer?")
        print("  agree = ran and matched the declared answer   "
              "differs = ran and disagreed   - = no runtime here\n")
        print("case".ljust(width) + "".join(t[:10].ljust(12) for t in tiers))
        print("-" * (width + 12 * len(tiers)))
        for label in result["cases"]:
            cells = "".join(result["cases"][label][t].ljust(12) for t in tiers)
            print(label.ljust(width) + cells)

        print("\nper tier:")
        for tier, entry in result["tiers"].items():
            if entry["status"] in ("unavailable", "error"):
                tag = "unavailable" if entry["status"] == "unavailable" else "HARNESS ERROR"
                print(f"  {tier:<11} {tag} — {entry['reason']}")
                continue
            failures = entry.get("failures") or {}
            total = result["executed"]
            print(f"  {tier:<11} {total - len(failures)}/{total} answers agreed "
                  f"({entry['depth']})")
            for label, detail in failures.items():
                print(f"      {label}: {detail[:160]}")

        index = result["compile_only"]
        compile_only = sum(len(v) for v in index.values())
        print(f"\n{result['executed']} of {result['executed'] + compile_only} "
              f"cases are executable; {compile_only} are compile-only by nature:")
        for reason in sorted(index):
            print(f"  {reason:<11} {len(index[reason]):>3} — "
                  f"{COMPILE_ONLY_REASONS[reason]}")

    unavailable = [t for t, e in result["tiers"].items()
                   if e["status"] == "unavailable"]
    broken = [t for t, e in result["tiers"].items() if e["status"] == "error"]
    if result["diverged"] or broken:
        return 1
    if require and unavailable:
        print(f"\n{len(unavailable)} executed tier(s) had no runtime here: "
              f"{', '.join(unavailable)}. Their answers went uncompared.",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate", action="store_true",
                        help="also compile/typecheck the emitted code with each "
                             "tier's real toolchain (slower; skips tiers whose "
                             "toolchain is absent, and says which)")
    parser.add_argument("--execute", action="store_true",
                        help="run every executable case on every tier that has "
                             "a runtime here and compare the ANSWERS; exits "
                             "non-zero on a disagreement")
    parser.add_argument("--require-execution", action="store_true",
                        help="with --execute, also fail when an executed tier's "
                             "runtime is absent — for CI, where an unrun tier "
                             "is a broken job, not a fact of life")
    parser.add_argument("--check-toolchains", action="store_true",
                        help="report which tiers' validators can run and exit "
                             "non-zero if any cannot — for CI, where a missing "
                             "toolchain is a broken job, not a fact of life")
    parser.add_argument("--markdown", action="store_true",
                        help="print the construct x tier matrix as markdown "
                             "(emit-only, deterministic), plus a revl self-host "
                             "column and a live per-tier emit-timing readout")
    parser.add_argument("--write-readme", action="store_true",
                        help="regenerate the matrix block between the "
                             "CONFORMANCE-MATRIX and CONFORMANCE-SWEEP "
                             "markers in docs/conformance.md in place")
    parser.add_argument("--check-readme", action="store_true",
                        help="exit non-zero if either committed block in "
                             "docs/conformance.md (the matrix or the per-tier "
                             "emit sweep) differs from a fresh generation — "
                             "the CI staleness gate")
    args = parser.parse_args()

    if args.check_toolchains:
        return _check_toolchains(as_json=args.json)

    if args.execute:
        return _execute_command(as_json=args.json,
                                require=args.require_execution)

    if args.write_readme or args.check_readme:
        return _write_readme(check_only=args.check_readme)

    if args.markdown:
        report = run()
        selfhost = selfhost_column()
        print(_markdown(report, selfhost))
        print()
        print(_sweep_markdown(report))
        _print_emit_timings(report)
        return 0

    report = run(validate=args.validate)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("emit — did the backend produce code?\n")
    _matrix(report, lambda row, t: "ok" if row["tiers"][t] == "ok" else "FAIL")

    if report["frontend_rejected"]:
        print("\nrejected by the frontend (language-level, not a backend gap):")
        for item in report["frontend_rejected"]:
            print(f"  {item['case']}: {item['message']}")

    print("\ngaps per tier:")
    for tier in TIERS:
        items = report["gaps"].get(tier) or []
        print(f"  {tier:<11} {len(items)}")
        for item in items:
            print(f"      {item['case']}: {item['message'][:90]}")

    if not args.validate:
        print("\n(emit only — pass --validate to also compile the emitted code)")
        return 0

    validation = report["validation"]

    def cell(row, tier):
        entry = validation[tier]
        if entry["status"] in ("unavailable", "error"):
            return "-"
        result = entry["results"].get(row["case"])
        if result is None:
            return "."          # the emitter refused; nothing to validate
        return "ok" if result["status"] == "ok" else "FAIL"

    print("\n\nvalidate — does that code hold up in the real toolchain?")
    print("  ok = accepted   FAIL = rejected   . = not emitted   - = no toolchain\n")
    _matrix(report, cell)

    print("\nper tier:")
    for tier in TIERS:
        entry = validation[tier]
        checked = len(entry["results"])
        if entry["status"] == "unavailable":
            print(f"  {tier:<11} unavailable — {entry['reason']}")
            continue
        if entry["status"] == "error":
            print(f"  {tier:<11} HARNESS ERROR — {entry['reason']}")
            continue
        failures = entry.get("failures") or []
        print(f"  {tier:<11} {checked - len(failures)}/{checked} accepted "
              f"({entry['depth']})")
        for label in failures:
            detail = entry["results"][label]["detail"]
            print(f"      {label}: {detail[:120]}")

    return 1 if any(validation[t]["status"] in ("fail", "error") for t in TIERS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
