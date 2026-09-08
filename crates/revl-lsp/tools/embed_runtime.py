#!/usr/bin/env python3
"""Pin and pack a private runtime for embedding into `revl-lsp` (item 336, the
one-file bundling slice, issue #102).

`build.rs` bakes an archive into the binary when the build sets
`REVL_LSP_EMBED_RUNTIME` (its bytes) and, optionally,
`REVL_LSP_EMBED_RUNTIME_PIN` (the versioned-cache key `runtime.rs` extracts it
under). This is the build glue that produces those two values from a real
runtime: a distributor hands it a `python-build-standalone` tree (or an archive
of one) with the `revl` wheel frozen into its own site, and it

  * validates the runtime has a `bin/python3` at the shape `runtime.rs`
    extracts (at the archive root, or under a single container directory),
  * normalizes it into the `runtime.tar` the resolver expects,
  * computes the archive's sha256 and derives a content-addressed pin
    (`pbs-<sha256[:12]>`) unless one is given, and
  * records `{pin, sha256, bytes, source}` in a `runtime.lock.json` beside the
    archive — the documented pin, so a rebuild bakes the SAME bytes under the
    SAME key and a mis-pairing is legible.

It then prints the two build-environment values, so a distribution build is:

    eval "$(embed_runtime.py --runtime <pbs-tree> --out dist/)"
    cargo build --release --manifest-path crates/revl-lsp/Cargo.toml

DELIBERATELY it does NOT fetch. Downloading a `python-build-standalone` release
and freezing the `revl` wheel into it is the distribution step (roadmap 338);
this tool pins and packs the bytes the distributor already has, so it stays
offline, reproducible and safe to run in CI against a stand-in runtime. A bare
`cargo build` with neither environment value set embeds nothing and is
unaffected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def _interpreter_in(directory: Path) -> Path | None:
    """The launcher inside a runtime directory: `bin/python3`, else `bin/python`
    — the same two names, in the same order, `runtime.rs::interpreter_in` accepts."""
    for name in ("python3", "python"):
        candidate = directory / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def _runtime_root(tree: Path) -> Path:
    """The runtime root inside an extracted tree, mirroring
    `runtime.rs::runtime_root`: `bin/python3` at the top level, or under a single
    container directory. Anything else is rejected rather than guessed at."""
    if _interpreter_in(tree) is not None:
        return tree
    subdirs = [child for child in tree.iterdir() if child.is_dir()]
    if len(subdirs) == 1 and _interpreter_in(subdirs[0]) is not None:
        return subdirs[0]
    raise SystemExit(
        f"the runtime under {tree} has no bin/python3 at its root or under a "
        "single container directory (is this a python-build-standalone tree?)"
    )


def _tar_tree(root: Path, dest: Path) -> None:
    """Pack `root`'s CONTENTS (its `bin/`, `lib/`, ...) at the archive root, so
    the produced `runtime.tar` unpacks to a `bin/python3` at the top level — the
    layout `runtime.rs` resolves without a container-directory hop."""
    status = subprocess.run(
        ["tar", "-cf", str(dest), "-C", str(root), "."],
        check=False,
    )
    if status.returncode != 0:
        raise SystemExit(f"`tar` failed to pack the runtime at {root}")


def _normalize_to_archive(runtime: Path, out_archive: Path) -> None:
    """Produce `out_archive` (a `runtime.tar`) from `runtime`, whether it is an
    already-extracted tree or an archive of one. Either way the result is
    validated to carry a `bin/python3` at the root the resolver reads."""
    if runtime.is_dir():
        root = _runtime_root(runtime)
        _tar_tree(root, out_archive)
        return
    if runtime.is_file():
        with tempfile.TemporaryDirectory(prefix="revl-lsp-embed-") as staging_name:
            staging = Path(staging_name)
            status = subprocess.run(
                ["tar", "-xf", str(runtime), "-C", str(staging)],
                check=False,
            )
            if status.returncode != 0:
                raise SystemExit(f"`tar` failed to unpack {runtime}")
            root = _runtime_root(staging)
            _tar_tree(root, out_archive)
        return
    raise SystemExit(f"--runtime {runtime} is neither a directory nor a file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pin and pack a private runtime for embedding into revl-lsp.",
    )
    parser.add_argument(
        "--runtime",
        required=True,
        type=Path,
        help="a python-build-standalone runtime: an extracted tree with "
        "bin/python3, or an archive of one.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="output directory for runtime.tar and runtime.lock.json.",
    )
    parser.add_argument(
        "--pin",
        default=None,
        help="the versioned-cache pin to key this runtime under. Defaults to a "
        "content-addressed pbs-<sha256[:12]>.",
    )
    args = parser.parse_args(argv)

    runtime: Path = args.runtime
    if not runtime.exists():
        raise SystemExit(f"--runtime {runtime} does not exist")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    archive = out / "runtime.tar"
    _normalize_to_archive(runtime, archive)

    sha = _sha256(archive)
    pin = args.pin or f"pbs-{sha[:12]}"
    lock = {
        "pin": pin,
        "sha256": sha,
        "bytes": archive.stat().st_size,
        "source": os.fspath(runtime),
    }
    (out / "runtime.lock.json").write_text(json.dumps(lock, indent=2) + "\n")

    # The two values a `cargo build` bakes in. Emitted as shell exports so the
    # caller can `eval` them; the archive path is absolute so the build finds it
    # from any cwd.
    print(f"export REVL_LSP_EMBED_RUNTIME={shlex.quote(os.fspath(archive.resolve()))}")
    print(f"export REVL_LSP_EMBED_RUNTIME_PIN={shlex.quote(pin)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
