"""Conformance matrix: every language construct against every backend.

    python3 tools/conformance.py [--json] [--validate] [--check-toolchains]

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



def _emit_kwargs(tier: str, index: int) -> dict:
    """Per-tier emitter options needed to validate many cases side by side.

    Only java needs one: every case emits a class named `Components`, so a
    single `javac` invocation over all of them would see 47 duplicates of
    `revl.Components` rather than 47 programs. `package_name` is a normal
    emitter parameter, so what gets validated is still real emitter output.
    """
    return {"package_name": f"case_{index}"} if tier == "java" else {}


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

        row = {"case": label, "ir_version": ir.get("ir_version"), "tiers": {}}
        for tier in TIERS:
            try:
                artifacts[tier].append(
                    (label, emitter(tier).emit(ir, **_emit_kwargs(tier, index))))
                row["tiers"][tier] = "ok"
            except Exception as exc:  # noqa: BLE001 — any refusal is the datum
                message = str(exc).splitlines()[0]
                row["tiers"][tier] = message
                report["gaps"].setdefault(tier, []).append(
                    {"case": label, "message": message})
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
    from validate import VALIDATORS  # noqa: PLC0415 — resolved next to this file

    status = {}
    for tier in TIERS:
        validator = VALIDATORS[tier]
        status[tier] = {"depth": validator.depth, "reason": validator.unavailable()}

    missing = [t for t, v in status.items() if v["reason"]]
    if as_json:
        print(json.dumps({"toolchains": status, "missing": missing}, indent=2))
    else:
        print("validator toolchains:\n")
        for tier in TIERS:
            entry = status[tier]
            if entry["reason"]:
                print(f"  {tier:<11} UNAVAILABLE — {entry['reason']}")
            else:
                print(f"  {tier:<11} ready ({entry['depth']})")
        if missing:
            print(f"\n{len(missing)} tier(s) cannot be validated here: "
                  f"{', '.join(missing)}.\nTheir emitted code would go "
                  f"unchecked while the run still reported success.")
        else:
            print("\nall tiers validatable — no silent gaps")
    return 1 if missing else 0


def _matrix(report: dict, cell) -> None:
    width = max(len(row["case"]) for row in report["cases"]) + 2
    print(f"{'case'.ljust(width)}" + "".join(t[:6].ljust(8) for t in TIERS))
    print("-" * (width + 8 * len(TIERS)))
    for row in report["cases"]:
        cells = "".join(cell(row, t).ljust(8) for t in TIERS)
        print(f"{row['case'].ljust(width)}{cells}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate", action="store_true",
                        help="also compile/typecheck the emitted code with each "
                             "tier's real toolchain (slower; skips tiers whose "
                             "toolchain is absent, and says which)")
    parser.add_argument("--check-toolchains", action="store_true",
                        help="report which tiers' validators can run and exit "
                             "non-zero if any cannot — for CI, where a missing "
                             "toolchain is a broken job, not a fact of life")
    args = parser.parse_args()

    if args.check_toolchains:
        return _check_toolchains(as_json=args.json)

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
