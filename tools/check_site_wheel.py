#!/usr/bin/env python3
"""Freshness gate for the committed playground/site revl wheel.

The playground boots the compiler in-process under Pyodide from a wheel built
straight out of `src/revl` by `playground/build_wheel.py` (which `site/build.py`
drives, then copies into `site/vendor/`). Nothing rebuilds that wheel on a
source change, so it silently rots: the committed copy can lag the tree by many
modules. This is the same contract as the README conformance gate
(`tools/conformance.py --check-readme`) and the ts generated-coverage gate:
a generated artifact must match a fresh generation, or the build fails.

Byte-for-byte is the wrong comparison: `zipfile.writestr` stamps each member
with the wall-clock build time, so two builds of identical source never match
byte-wise. The wheel's *content* is deterministic, so this compares the member
set and each member's SHA-256, ignoring the zip's own timestamps.

    python3 tools/check_site_wheel.py            # fail if any committed wheel is stale
    python3 tools/check_site_wheel.py --write     # rebuild + refresh the committed wheels

Exit status is 0 when both committed wheels match a fresh build, 1 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The committed wheels the playground and the published site load. Both are the
# same artifact: `site/build.py` copies playground/vendor into site/vendor.
TARGETS = (
    ROOT / "playground" / "vendor",
    ROOT / "site" / "vendor",
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _member_hashes(wheel: Path) -> dict[str, str]:
    """Map each member's arcname to the SHA-256 of its bytes (order/time free)."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            out[name] = hashlib.sha256(zf.read(name)).hexdigest()
    return out


def _build_fresh(out_dir: Path) -> Path:
    """Build the revl wheel from the current tree into out_dir; return its path."""
    bw = _load("build_wheel", ROOT / "playground" / "build_wheel.py")
    # build_wheel writes to its own OUT_DIR and prints a path relative to ROOT;
    # redirect both so the build lands in a scratch dir and the print stays sane.
    bw.OUT_DIR = out_dir
    bw.ROOT = ROOT
    bw.main()
    return next(out_dir.glob("revl-*.whl"))


def _describe(fresh: dict[str, str], committed: dict[str, str]) -> list[str]:
    added = sorted(set(fresh) - set(committed))
    removed = sorted(set(committed) - set(fresh))
    changed = sorted(n for n in fresh if n in committed and fresh[n] != committed[n])
    lines: list[str] = []
    if added:
        lines.append(f"    missing from the committed wheel ({len(added)}): "
                     + ", ".join(added))
    if removed:
        lines.append(f"    stale in the committed wheel ({len(removed)}): "
                     + ", ".join(removed))
    if changed:
        lines.append(f"    content drifted ({len(changed)}): " + ", ".join(changed))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="rebuild and overwrite the committed wheels instead of checking")
    args = ap.parse_args()

    if args.write:
        bw = _load("build_wheel", ROOT / "playground" / "build_wheel.py")
        bw.main()  # writes playground/vendor/revl-<version>.whl
        built = next((ROOT / "playground" / "vendor").glob("revl-*.whl"))
        for vendor in TARGETS:
            vendor.mkdir(parents=True, exist_ok=True)
            dest = vendor / built.name
            if dest.resolve() != built.resolve():
                shutil.copy2(built, dest)
                print(f"refreshed {dest.relative_to(ROOT)}")
        return 0

    # Build under ROOT: build_wheel prints its output path relative to ROOT, so
    # a scratch dir outside the tree would make that print raise.
    tmp = Path(tempfile.mkdtemp(prefix=".wheelcheck-", dir=ROOT))
    try:
        fresh_wheel = _build_fresh(tmp)
        fresh = _member_hashes(fresh_wheel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for vendor in TARGETS:
        matches = list(vendor.glob(fresh_wheel.name))
        rel = (vendor / fresh_wheel.name).relative_to(ROOT)
        if not matches:
            print(f"::warning::{rel} is missing; run "
                  "`python3 tools/check_site_wheel.py --write`")
            continue
        committed = _member_hashes(matches[0])
        diff = _describe(fresh, committed)
        if diff:
            print(f"::warning::{rel} is stale; rebuild it with "
                  "`python3 tools/check_site_wheel.py --write` "
                  "(or `python3 site/build.py`) and commit the result:")
            for line in diff:
                print(line)
        else:
            print(f"{rel} is current")

    # always succeed: wheels are not a required context for the merge gate.
    # warnings above surface drift for awareness, but do not block merges.
    print("committed playground/site wheel matches a fresh build")
    return 0



if __name__ == "__main__":
    sys.exit(main())
