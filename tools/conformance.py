"""Conformance matrix: every language construct against every backend.

    python3 tools/conformance.py [--json] [--all]

Backend divergence is this project's recurring bug class, and it is always
the same shape: a construct lands, some emitters take it, one does not, and
nobody notices until that tier is targeted by hand. `tests/test_cross_tier.py`
holds the floor for a few known-portable constructs; this walks the *whole*
surface and prints what each tier does with it.

Every case is a minimal source. A case that the frontend itself rejects is
reported as such (a language-level limit, not a backend gap). Otherwise each
of the five emitters runs and the result is OK or the refusal it raised.
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

TIERS = ("python", "typescript", "rust", "java", "wasm")

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
    ("service", "async op",
     _component("  provide s { async fn f(x) { return x } }",
                services="service S { async fn f(x: Int) -> Int }\n")),
    ("service", "commutative op",
     _component("  provide s { fn f(x) = x }",
                services="service S { commutative fn f(x: Int) -> Int }\n")),
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
     _component("  await Job.run(1)\n  provide s { fn f(x) = x }")),
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
                "  provide s { fn f(x) { effect m.insert(x, x)  undo m.remove(x)  return x } }")),
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

    # ---- expressions (in a method body, the position that has diverged)
    ("expr", "arithmetic", _component("  provide s { fn f(x) = x + 1 * 2 }")),
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
     "extern pure fn h(n: Int) -> Int = @py { return n } = @ts { return n }\n"
     + _component("  provide s { fn f(x) = h(x) }")),
    ("extern", "acquire extern",
     "extern acquire fn open_(n: Int) -> Int undo close_(handle)\n"
     "  = @py { return n } = @ts { return n }\n"
     "extern pure fn close_(h: Int) -> Int = @py { return h } = @ts { return h }\n"
     + _component("  provide s { fn f(x) = x }")),
    ("extern", "emission extern",
     "extern emission fn ship(n: Int) -> Int = @py { return n } = @ts { return n }\n"
     + _component("  provide s { fn f(x) = emit ship(x) }",
                  services="service S { emission fn f(x: Int) -> Int }\n")),
    ("test", "test block",
     "fn inc(n: Int) -> Int { return n + 1 }\n"
     'test "inc" { assert inc(1) == 2 }\n'
     + _component("  provide s { fn f(x) = inc(x) }")),
]


def run(all_cases: bool = False) -> dict:
    report: dict = {"cases": [], "frontend_rejected": [], "gaps": {}}
    for group, name, source in CASES:
        label = f"{group}/{name}"
        try:
            ir = compile_source(source)
        except RevlError as error:
            report["frontend_rejected"].append(
                {"case": label, "message": str(error).splitlines()[0]})
            continue

        row = {"case": label, "ir_version": ir.get("ir_version"), "tiers": {}}
        for tier in TIERS:
            try:
                emitter(tier).emit(ir)
                row["tiers"][tier] = "ok"
            except Exception as exc:  # noqa: BLE001 — any refusal is the datum
                message = str(exc).splitlines()[0]
                row["tiers"][tier] = message
                report["gaps"].setdefault(tier, []).append(
                    {"case": label, "message": message})
        report["cases"].append(row)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    width = max(len(row["case"]) for row in report["cases"]) + 2
    print(f"{'case'.ljust(width)}" + "".join(t[:6].ljust(8) for t in TIERS))
    print("-" * (width + 8 * len(TIERS)))
    for row in report["cases"]:
        cells = "".join(("ok" if row["tiers"][t] == "ok" else "FAIL").ljust(8)
                        for t in TIERS)
        print(f"{row['case'].ljust(width)}{cells}")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
