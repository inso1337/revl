#!/usr/bin/env python3
"""Build a pure-Python wheel for revl, straight from the in-tree source.

The playground loads the compiler *in-process* under Pyodide (micropip
installs this wheel), so it needs a wheel but the environment's pip/build
may be unusable. revl is pure Python, so a wheel is just a zip with a
`.dist-info` — this constructs it directly from `src/revl`, no build
backend required.

Run from anywhere:  python3 playground/build_wheel.py
Output:             playground/vendor/revl-<version>-py3-none-any.whl
"""
from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "revl"
OUT_DIR = Path(__file__).resolve().parent / "vendor"


def _tracked(tree: Path) -> set[str] | None:
    """The repo-relative paths git tracks under `tree`, or None without git.

    GHSA-gj88-cx6q-38r2 was the PyPI wheel force-including whatever sat on the
    builder's disk. This wheel is a different artifact with a different
    distribution path (it is committed, and `tools/check_site_wheel.py` gates it
    against a fresh build), but the selection had the same shape: `rglob("*.py")`
    is a question about the filesystem, not about the commit, so a developer's
    untracked scratch module under `src/revl/` — or a stray `__pycache__/x.py` —
    rode into the wheel the playground serves. Asking git instead makes this
    wheel a function of the commit too.

    Returning None (no git, not a checkout) leaves the caller on its glob, which
    is what this file did before; the committed-wheel drift gate is the backstop
    there.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", str(tree.relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    paths = {p for p in proc.stdout.split("\0") if p}
    return paths or None


def _version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{arcname},sha256={digest.decode()},{len(data)}"


def main() -> None:
    version = _version()
    dist_info = f"revl-{version}.dist-info"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wheel_path = OUT_DIR / f"revl-{version}-py3-none-any.whl"

    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: revl\n"
        f"Version: {version}\n"
        "Summary: A research language for spatiotemporal composability (Cordis paradigm)\n"
        "Requires-Python: >=3.11\n"
    )
    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: revl-playground-build_wheel\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )

    src_tracked = _tracked(SRC)
    records: list[str] = []
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
        for path in sorted(SRC.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if src_tracked is not None and rel not in src_tracked:
                continue
            arcname = "revl/" + path.relative_to(SRC).as_posix()
            data = path.read_bytes()
            whl.writestr(arcname, data)
            records.append(_record_line(arcname, data))

        # The py-tier runtime glue (emit/runtime/replay/...), packaged where
        # `_paths.backends_root()` finds it in an installed wheel
        # (revl/backends) — the same layout pyproject's wheel target ships,
        # scoped to the one tier the in-browser session can boot. With the
        # cordis wheel installed beside it, `revl.mcp.session.Session` runs
        # load/call/swap/unload entirely client-side.
        py_backend = ROOT / "backends" / "python"
        backend_tracked = _tracked(py_backend)
        for path in sorted(py_backend.glob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if backend_tracked is not None and rel not in backend_tracked:
                continue
            arcname = "revl/backends/python/" + path.name
            data = path.read_bytes()
            whl.writestr(arcname, data)
            records.append(_record_line(arcname, data))

        for arcname, text in (
            (f"{dist_info}/METADATA", metadata),
            (f"{dist_info}/WHEEL", wheel_meta),
        ):
            data = text.encode("utf-8")
            whl.writestr(arcname, data)
            records.append(_record_line(arcname, data))

        record_name = f"{dist_info}/RECORD"
        records.append(f"{record_name},,")
        whl.writestr(record_name, "\n".join(records) + "\n")

    print(f"wrote {wheel_path.relative_to(ROOT)} ({wheel_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
