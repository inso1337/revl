#!/usr/bin/env python3
"""Rebuild the site's generated assets: the compiler wheel and the examples.

Reuses playground/build_wheel.py (the wheel construction is identical), then
copies the wheel into site/vendor/. Run from anywhere:

    python3 site/build.py
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    build_wheel = _load("build_wheel", ROOT / "playground" / "build_wheel.py")
    build_wheel.main()  # -> playground/vendor/revl-<version>-py3-none-any.whl

    vendor = SITE / "vendor"
    vendor.mkdir(exist_ok=True)
    for whl in (ROOT / "playground" / "vendor").glob("revl-*.whl"):
        shutil.copy2(whl, vendor / whl.name)
        print(f"copied {whl.name} -> site/vendor/")

    gen = _load("gen_examples", SITE / "gen_examples.py")
    gen.main()


if __name__ == "__main__":
    main()
