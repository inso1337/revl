"""Run the wasm codegen bench.

    PYTHONPATH=<repo>/src python bench/codegen/wasm/run.py [--quick]

Reports, per program: emitted module size, how much of the emitted prelude
is unreachable, and the exact wasmtime fuel one canonical call burns --
baseline versus each hand-written variant standing in for a proposed
emitter fix.

Fuel is a deterministic count of executed wasm operations, so everything
here is reproducible under machine load; no wall clock is reported on
purpose. Two derived numbers are the ones to quote:

* ``delta`` -- fuel saved per call, baseline minus variant. Exact: the
  ``--invoke`` instantiation charge is identical across variants of one
  program, so it cancels.
* ``slope`` -- fuel per input byte, from a two-point sweep. Exact:
  instantiation does not scale with the argument.

A raw fuel TOTAL also contains instantiation, which for a module with
`(data ...)` segments is ~16385 fuel. Ratios of totals are therefore a
LOWER BOUND on the per-call ratio; the slope ratio is the honest one.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402

#: program -> service, canonical export, WAVE call template, arg sizes to
#: sweep, and which variants to A/B against the baseline.
CASES = {
    "echo": dict(
        service="Echoer",
        export="revl:exported/echoer#echo",
        call='echo("{0}")',
        sizes=[16, 256, 4096],
        variants=["memcopy_lift", "zerocopy_lift", "prune_dead",
                  "static_return_area", "combined"],
    ),
    "greet": dict(
        service="Greeter",
        export="revl:exported/greeter#greet",
        call='greet("{0}")',
        sizes=[16, 4096],
        variants=["memcopy_lift", "zerocopy_lift", "fused_concat",
                  "prune_dead", "combined"],
    ),
    "concat6": dict(
        service="Banner",
        export="revl:exported/banner#banner",
        call='banner("{0}", "{0}")',
        sizes=[16, 1024],
        variants=["memcopy_lift", "zerocopy_lift", "fused_concat", "combined"],
    ),
    "listid": dict(
        service="Ids",
        export="revl:exported/ids#ids",
        call="ids({0})",
        sizes=[4, 1024],
        variants=["list_bulk", "prune_dead", "combined"],
    ),
    "constfold": dict(
        service="Scaler",
        export="revl:exported/scaler#scaled",
        call="scaled(3)",
        sizes=[0],
        variants=["const_fold", "prune_dead", "combined"],
    ),
    "scalar": dict(
        service="Doubler",
        export="revl:exported/doubler#dbl",
        call="dbl(21)",
        sizes=[0],
        variants=["prune_dead", "combined"],
    ),
}


def _expr(case: dict, n: int) -> str:
    if case["export"].endswith("#ids"):        # a WAVE list literal
        return case["call"].format("[" + ", ".join("1" for _ in range(n)) + "]")
    return case["call"].format("a" * n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="static metrics only, no wasmtime execution")
    ap.add_argument("--json", type=pathlib.Path, default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    ok, why = harness.have_toolchain()
    if not ok and not args.quick:
        print(f"toolchain unavailable ({why}); falling back to --quick")
        args.quick = True

    report: dict = {"programs": {}}
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="revl-wasm-bench-"))
    try:
        for program, case in CASES.items():
            if args.only and args.only != program:
                continue
            emitted = harness.emit(
                harness.PROGRAMS / f"{program}.revl", case["service"])
            base_wat = emitted["core_wat"]
            names = ["baseline", *case["variants"]]
            wats = {"baseline": base_wat}
            for name in case["variants"]:
                wats[name] = harness.VARIANTS[name](base_wat)

            entry: dict = {
                "service": case["service"],
                "static": harness.path_costs(base_wat, case["export"]),
                "modules": {},
                "fuel": {},
            }
            for name in names:
                info = harness.reachability(wats[name])
                rec = {
                    "wat_bytes": len(wats[name].encode("utf-8")),
                    "funcs_defined": info["defined"],
                    "funcs_reachable": info["reachable"],
                    "dead_funcs": len(info["dead"]),
                    "dead_names": info["dead"],
                }
                if not args.quick:
                    rec["core_wasm_bytes"] = harness.core_wasm_bytes(
                        wats[name], tmp / program / name, name)
                entry["modules"][name] = rec

            if not args.quick:
                # Correctness gate: a variant that is faster but wrong is not
                # a finding. Every variant must return exactly what the
                # baseline returns for the same argument.
                answers = {}
                for name in names:
                    comp = harness.build(
                        wats[name], emitted["wit"], emitted["world"],
                        tmp / program / name, name)
                    ok_c, _oof, answers[name] = harness.invoke(
                        comp, _expr(case, case["sizes"][0]))
                    if not ok_c:
                        raise RuntimeError(f"{program}/{name} does not run")
                    if answers[name] != answers["baseline"]:
                        raise RuntimeError(
                            f"{program}/{name} disagrees with baseline: "
                            f"{answers[name]!r} != {answers['baseline']!r}")
                    entry["fuel"][name] = {
                        str(n): harness.fuel_cost(comp, _expr(case, n))
                        for n in case["sizes"]}
                sizes = case["sizes"]
                for name in names:
                    f = entry["fuel"][name]
                    if len(sizes) >= 2:
                        lo, hi = sizes[0], sizes[-1]
                        entry["fuel"][name]["slope_per_byte"] = round(
                            (f[str(hi)] - f[str(lo)]) / (hi - lo), 4)
                base = entry["fuel"]["baseline"]
                entry["delta"] = {
                    name: {str(n): base[str(n)] - entry["fuel"][name][str(n)]
                           for n in sizes}
                    for name in names[1:]}
                entry["agrees_with_baseline"] = sorted(answers)
            report["programs"][program] = entry
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    _print(report, quick=args.quick)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


def _print(report: dict, *, quick: bool) -> None:
    for program, entry in report["programs"].items():
        print(f"\n=== {program} (service {entry['service']}) ===")
        s = entry["static"]
        print(f"  static path: allocs={s['alloc_calls']} "
              f"memory.copy={s['memory_copy_sites']} "
              f"str_concat={s['str_concat_calls']} "
              f"funcs_on_path={s['funcs_on_path']}")
        for name, rec in entry["modules"].items():
            size = rec.get("core_wasm_bytes")
            size_s = f"{size:6d} B core.wasm" if size else " " * 18
            print(f"  {name:20s} {size_s}  funcs "
                  f"{rec['funcs_reachable']:2d}/{rec['funcs_defined']:2d} live"
                  f"  ({rec['dead_funcs']} dead)")
        dead = entry["modules"]["baseline"]["dead_names"]
        if dead:
            print(f"    dead in baseline: {', '.join(dead)}")
        if quick or not entry["fuel"]:
            continue
        for name, f in entry["fuel"].items():
            cells = "  ".join(
                f"n={k}:{v}" for k, v in f.items() if k != "slope_per_byte")
            slope = f.get("slope_per_byte")
            slope_s = f"   {slope:8.3f} fuel/byte" if slope is not None else ""
            print(f"  fuel {name:20s} {cells}{slope_s}")
        for name, d in entry.get("delta", {}).items():
            cells = "  ".join(f"n={k}:-{v}" for k, v in d.items())
            print(f"  saved {name:19s} {cells}")


if __name__ == "__main__":
    raise SystemExit(main())
