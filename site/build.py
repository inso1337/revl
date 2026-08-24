#!/usr/bin/env python3
"""Rebuild the site's generated assets: the compiler wheel and the examples.

Reuses playground/build_wheel.py (the wheel construction is identical), then
copies the wheel into site/vendor/. Run from anywhere:

    python3 site/build.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_cordis_wheel(vendor: Path) -> None:
    """Package the pinned cordis-py clone (pure Python, no deps) as a wheel.

    The playground's live mode boots compositions on the real cordis-py
    runtime under Pyodide; this wheel supplies the `cordis` package beside the
    revl wheel. Source: the `backends/python/setup.sh` clone (override with
    CORDIS_PY), which that script keeps at the tested pin.
    """
    clone = Path(os.environ.get("CORDIS_PY", ROOT / "backends" / "python" / ".cordis-py"))
    src = clone / "src" / "cordis"
    if not src.is_dir():
        raise SystemExit(
            f"cordis-py clone not found at {clone} — run `sh backends/python/setup.sh` "
            "first, or point CORDIS_PY at an existing clone"
        )
    bw = _load("build_wheel_mod", ROOT / "playground" / "build_wheel.py")
    dist_info = "cordis-4.0.0.dist-info"
    wheel_path = vendor / "cordis-4.0.0-py3-none-any.whl"
    import zipfile

    metadata = (
        "Metadata-Version: 2.1\nName: cordis\nVersion: 4.0.0\n"
        "Summary: Pure Python port of cordis (spatiotemporal composability runtime)\n"
        "Requires-Python: >=3.11\n"
    )
    wheel_meta = (
        "Wheel-Version: 1.0\nGenerator: revl-site-build\n"
        "Root-Is-Purelib: true\nTag: py3-none-any\n"
    )
    records: list[str] = []
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
        for path in sorted(src.rglob("*.py")):
            arcname = "cordis/" + path.relative_to(src).as_posix()
            data = path.read_bytes()
            whl.writestr(arcname, data)
            records.append(bw._record_line(arcname, data))
        for arcname, text in ((f"{dist_info}/METADATA", metadata),
                              (f"{dist_info}/WHEEL", wheel_meta)):
            data = text.encode("utf-8")
            whl.writestr(arcname, data)
            records.append(bw._record_line(arcname, data))
        record_name = f"{dist_info}/RECORD"
        records.append(f"{record_name},,")
        whl.writestr(record_name, "\n".join(records) + "\n")
    print(f"wrote site/vendor/{wheel_path.name} ({wheel_path.stat().st_size} bytes)")


def main() -> None:
    build_wheel = _load("build_wheel", ROOT / "playground" / "build_wheel.py")
    build_wheel.main()  # -> playground/vendor/revl-<version>-py3-none-any.whl

    vendor = SITE / "vendor"
    vendor.mkdir(exist_ok=True)
    for whl in (ROOT / "playground" / "vendor").glob("revl-*.whl"):
        shutil.copy2(whl, vendor / whl.name)
        print(f"copied {whl.name} -> site/vendor/")

    build_cordis_wheel(vendor)

    gen = _load("gen_examples", SITE / "gen_examples.py")
    gen.main()


if __name__ == "__main__":
    main()
