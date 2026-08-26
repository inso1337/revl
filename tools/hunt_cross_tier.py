#!/usr/bin/env python3
"""Hunt driver around tools/fuzz_cross_tier.py (roadmap item 292).

Runs the differential fuzzer across many seeds, aggregating DISTINCT
divergences over the whole campaign (the fuzzer's own dedup only spans a
single run/seed). Prints, per distinct (tier, kind, signature): the count of
seeds that hit it and one representative shrunk program. Writes no fixtures —
that decision is left to a deliberate final `fuzz_cross_tier.py` run once the
NEW bugs are identified.

    python3 tools/hunt_cross_tier.py --seeds 0-19 --count 80 --tiers py,go,wasm,ts
"""
from __future__ import annotations

import argparse
import importlib.util
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("fuzz_cross_tier", ROOT / "tools" / "fuzz_cross_tier.py")
fz = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fz  # register so @dataclass can resolve cls.__module__
spec.loader.exec_module(fz)


def parse_seeds(spec_str: str):
    out = []
    for part in spec_str.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=str, default="0-9")
    ap.add_argument("--count", type=int, default=80)
    ap.add_argument("--tiers", type=str, default="py,go,wasm,ts")
    ap.add_argument("--max-shrink", type=int, default=120)
    args = ap.parse_args(argv)

    requested = [t.strip() for t in args.tiers.split(",")]
    available, reasons = fz.detect_tiers(requested, slow=("rust" in requested))
    others = [t for t in available if t != fz.REFERENCE]
    print(f"hunt: reference={fz.REFERENCE} comparison={others}")
    for t, why in reasons.items():
        print(f"  tier {t}: not executed — {why}")

    seeds = parse_seeds(args.seeds)
    # aggregated across the whole campaign
    found = {}                       # key -> representative Divergence
    seed_hits = defaultdict(set)     # key -> set of seeds
    refusals_total = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)

    for seed in seeds:
        rng = random.Random(seed)
        per_seed_seen = set()
        for i in range(args.count):
            totals["generated"] += 1
            gen = fz.Generator(random.Random(rng.random()))
            try:
                prog = gen.program()
                src = prog.render()
            except Exception:
                continue
            try:
                fz.compile_source(src)
            except fz.RevlError:
                totals["rejected"] += 1
                continue
            totals["admitted"] += 1
            try:
                value = fz.reference_value(src)
            except fz.ReferenceFault:
                totals["ref_fault"] += 1
                continue
            aug = fz.assertion_source(src, prog.probe_ret, value)
            if aug is None:
                totals["ref_fault"] += 1
                continue
            oc, _m, _o = fz._run_tier(fz.REFERENCE, fz.compile_source(aug))
            if oc != "pass":
                totals["ref_fault"] += 1
                continue
            totals["ran"] += 1
            for tier in others:
                outcome, tmsg, tout = fz.check_tier(tier, aug)
                if outcome in ("pass", "skip"):
                    continue
                if outcome == "refusal":
                    sig = tmsg.split(":", 2)[-1].strip()[:60]
                    refusals_total[tier][sig] += 1
                    continue
                kind = outcome
                sig = fz.divergence_signature(tmsg, tout)
                key = (tier, kind, sig)
                seed_hits[key].add(seed)
                if key in per_seed_seen:
                    continue
                per_seed_seen.add(key)
                if key not in found:
                    decls, body, reduced = fz.shrink(prog, tier, prog.probe_ret, value, args.max_shrink)
                    _oc, rmsg, rout = fz.check_tier(tier, reduced)
                    found[key] = fz.Divergence(
                        tier=tier, kind=kind,
                        signature=fz.divergence_signature(rmsg, rout),
                        py_msg="reference passed", tier_msg=rmsg,
                        tier_stdout=rout, source=reduced)
        print(f"  seed {seed}: cumulative distinct={len(found)} ran={totals['ran']}")

    print("\n" + "=" * 70)
    print(f"generated={totals['generated']} admitted={totals['admitted']} "
          f"rejected={totals['rejected']} ref_fault={totals['ref_fault']} "
          f"ran={totals['ran']}")
    print(f"tiers: {others}   seeds: {seeds[0]}..{seeds[-1]} ({len(seeds)})")
    print(f"DISTINCT divergences: {len(found)}\n")
    for key, div in sorted(found.items()):
        tier, kind, sig = key
        hits = len(seed_hits[key])
        print(f"### {tier} / {kind} / seeds_hit={hits}")
        print(f"    sig: {div.signature}")
        print(f"    msg: {div.tier_msg.strip()[:200]}")
        for ln in div.source.strip().splitlines():
            print("      " + ln)
        print()
    print("--- refusal (capability boundary) totals ---")
    for tier, d in refusals_total.items():
        tot = sum(d.values())
        print(f"  {tier}: {tot} refusals, {len(d)} kinds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
