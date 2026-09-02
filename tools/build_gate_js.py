#!/usr/bin/env python3
"""Package the revl gate component for JavaScript — roadmap item 335 slice 4,
design `docs/design/335-wasm-edge-gate.md` §6 ("the JS harness + the demo").

Slices 0-2 landed the artifact: `tools/build_gate_wasm.py` generates
`crates/revl-gate-wasm`, which compiles to a `revl:gate@1.0.0` WASI-P2
component with an EMPTY import section. `wasmtime` loads that component
directly. A browser and an edge worker do not; they need the component
transpiled to JS + core wasm, which is what `jco transpile` produces and what
this tool drives.

    crates/revl-gate-wasm -> (build_gate_wasm) -> revl_gate.wasm
      -> (jco transpile) -> dist/revl_gate.js + dist/*.core.wasm

Why this is a separate tool
---------------------------
`tools/build_gate_wasm.py` GENERATES the committed crate, and its own bytes are
a digest input for that crate's drift gate. Transpilation generates nothing
committed, so putting it here keeps the crate's provenance digest untouched by
JS-lane churn. This tool imports the generator rather than restating any of it,
so there is exactly one definition of "how the component is built".

Nothing this tool produces is committed
---------------------------------------
For the same reason the `.wasm` is not committed: the transpiled JS is a
function of an exact `jco` version and an exact rustc, and a committed copy
would red on every bump while proving nothing. What is committed is the crate
source (drift-gated), the consumer example that imports the transpiled module,
and a test that transpiles freshly and runs it.

The property that must survive transpilation
--------------------------------------------
The gate has no admitting arm. `admitted` is `false` on every arm of the WIT
`verdict` record, the real arm travels in `kind`, and a consumer has exactly
two safe readings: REJECT on `refused`, ESCALATE on `no-objection` and
`outside-frontier`.

Transpilation WIDENS that, and this tool narrows it back. WIT has no singleton
type, so the world can only say `admitted: bool` and `jco` faithfully emits
`admitted: boolean` in the `.d.ts` a JS consumer programs against. A consumer
handed `boolean` writes `if (v.admitted) run(candidate)` and TypeScript agrees
that branch is reachable. It is not. `narrow_declarations` rewrites the field
to the literal `false`, so `v.admitted === true` stops type-checking, and
`--check-surface` fails the build if the narrowing did not take. An npm-shaped
gate whose types let a browser read a non-refusal as an admission would be the
worst possible outcome of this item; the type is where that gets closed.

Usage
-----
    python3 tools/build_gate_js.py --out DIR     # build + transpile + measure
    python3 tools/build_gate_js.py --check-surface --out DIR
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "crates" / "revl-gate-wasm"
EXAMPLE = ROOT / "examples" / "ecosystem-consumer-js"

# The transpiled module's name, so the example's import specifier is fixed.
JS_NAME = "revl_gate"


def _generator():
    """`tools/build_gate_wasm.py` as a module. Imported, never restated: the
    component this tool transpiles must be the one that tool builds."""
    path = ROOT / "tools" / "build_gate_wasm.py"
    spec = importlib.util.spec_from_file_location("revl_build_gate_wasm_js", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revl_build_gate_wasm_js"] = module
    spec.loader.exec_module(module)
    return module


GEN = _generator()


# ------------------------------------------------------------------ toolchain

def node_binary() -> str | None:
    """`node`, from `REVL_NODE` or the path."""
    override = os.environ.get("REVL_NODE")
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("node")


def jco_binary() -> str | None:
    """`jco`, in the order a consumer would find it.

    `REVL_JCO` first (a machine that installed it out of band), then a local
    `node_modules/.bin/jco` under the example project, then the path. `npx` is
    deliberately NOT probed: it would install from the network mid-test, and a
    test that silently reaches the network is not a test of what is installed.
    """
    override = os.environ.get("REVL_JCO")
    if override:
        return override if Path(override).is_file() else None
    local = EXAMPLE / "node_modules" / ".bin" / "jco"
    if local.is_file():
        return str(local)
    return shutil.which("jco")


def js_toolchain_reason() -> str | None:
    """Why the JS package cannot be produced here, or None when it can.

    The wasm reason comes first and verbatim from the component builder: there
    is no JS lane without an artifact to transpile.
    """
    wasm = GEN.toolchain_reason()
    if wasm is not None:
        return wasm
    if node_binary() is None:
        return "node not found (set REVL_NODE)"
    if jco_binary() is None:
        return ("jco not found (npm i @bytecodealliance/jco in "
                "examples/ecosystem-consumer-js, or set REVL_JCO)")
    return None


def jco_version() -> str | None:
    """The transpiler's version. Recorded with every measurement, because the
    transpiled bytes are a function of it."""
    binary = jco_binary()
    if binary is None:
        return None
    done = subprocess.run([binary, "--version"], capture_output=True, text=True,
                          timeout=300, check=False)
    return done.stdout.strip() or None if done.returncode == 0 else None


# ----------------------------------------------------------------- transpile

def transpile(component: Path, out_dir: Path, *, name: str = JS_NAME) -> Path:
    """`jco transpile` the component into `out_dir`. Returns the JS entry point.

    No `--optimize` and no `--minify`: this lane is measured, and a measurement
    of a size the default build does not produce would be a number nobody can
    reproduce from the committed instructions.
    """
    binary = jco_binary()
    if binary is None:
        raise RuntimeError("jco not found")
    out_dir.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [binary, "transpile", str(component), "-o", str(out_dir), "--name", name],
        capture_output=True, text=True, timeout=1800, check=False)
    if done.returncode != 0:
        raise RuntimeError("jco transpile failed:\n"
                           + (done.stderr or done.stdout or "")[-4000:])
    entry = out_dir / f"{name}.js"
    if not entry.is_file():
        raise RuntimeError(f"jco produced no {entry.name} in {out_dir}")
    return entry


def build_js(out_dir: Path) -> dict:
    """Build the component and transpile it. Returns the measured provenance.

    The component is built into `out_dir` too, so one directory holds the whole
    JS lane and a caller can hand it to a static server as-is.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    component = GEN.build_component(CRATE, out_dir / f"{JS_NAME}.wasm")
    entry = transpile(component, out_dir)
    narrowed = narrow_declarations(out_dir)
    files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    core = [p for p in files if p.suffix == ".wasm" and p.name != component.name]
    return {
        "component": str(component),
        "component_bytes": component.stat().st_size,
        "entry": str(entry),
        "js_bytes": sum(p.stat().st_size for p in files if p.suffix == ".js"),
        "core_wasm_bytes": sum(p.stat().st_size for p in core),
        "total_bytes": sum(p.stat().st_size for p in files
                           if p.name != component.name),
        "files": [p.relative_to(out_dir).as_posix() for p in files],
        "jco": jco_version(),
        "imports": GEN.component_imports(component),
        "narrowed_admitted": narrowed,
        "issues_admissions": False,
    }


# --------------------------------------------------- the no-admission surface

# WIT has no singleton type, so `admitted: bool` is the strongest thing the
# world can say, and `jco` faithfully renders it `admitted: boolean`. That is a
# WIDENING on the one field that must not widen: the gate has no admitting arm
# at all, `admitted` is the constant `false`, and a TypeScript consumer handed
# `boolean` writes `if (v.admitted) run(candidate)` and gets a branch the type
# system says is reachable. It is not reachable, and this is the packaging step
# that says so in the type a consumer actually programs against.
#
# Narrowing here rather than in the WIT is deliberate: the WIT boundary must
# keep a field the crate ABI and the component ABI can both carry, and this is
# the JS lane's own packaging obligation, held by `check_surface` below and by
# `tests/test_gate_consumer_example_js.py`.
_ADMITTED_WIDE = "admitted: boolean,"
_ADMITTED_NARROW = """/**
   * ALWAYS `false`, on every arm, and narrowed here from the `boolean` the
   * component ABI is obliged to carry (WIT has no singleton type). This gate
   * decides the composition/guarantee layer and NOT the reference type layer,
   * so it has no admission to give: `if (v.admitted)` is dead code and
   * `v.admitted === true` does not type-check. The real signal is `kind`, and
   * a consumer has exactly two safe readings of it: REJECT on "refused",
   * ESCALATE on "no-objection" and "outside-frontier".
   */
  admitted: false,"""


def narrow_declarations(out_dir: Path) -> bool:
    """Narrow `admitted` to the literal `false` in the emitted `.d.ts`.

    Returns True when a declaration was rewritten. Idempotent: a future jco that
    emits the literal itself leaves nothing to do and this returns False.
    """
    changed = False
    for path in sorted(out_dir.rglob("*.d.ts")):
        text = path.read_text(encoding="utf-8")
        if _ADMITTED_WIDE not in text:
            continue
        path.write_text(text.replace(_ADMITTED_WIDE, _ADMITTED_NARROW),
                        encoding="utf-8")
        changed = True
    return changed


def check_surface(out_dir: Path) -> list[str]:
    """Problems with the packaged JS surface, empty when it is sound.

    Read off the emitted declarations rather than the WIT, because what a JS
    consumer programs against is what shipped, not what we wrote.
    """
    problems: list[str] = []
    types = sorted(out_dir.rglob("*.d.ts"))
    if not types:
        return ["jco emitted no TypeScript declarations to check "
                "(was --no-typescript passed?)"]
    blob = "\n".join(p.read_text(encoding="utf-8") for p in types)
    if "admitted: false," not in blob:
        problems.append(
            "the packaged surface does not type `admitted` as the literal "
            "`false`; a JS consumer could read it as sometimes-true, which is "
            "the false admission this gate has no arm for")
    if _ADMITTED_WIDE in blob:
        problems.append(
            "the packaged surface still carries a widened `admitted: boolean`")
    for name in ("admit", "admitJson", "gateVersion", "admitArtifact"):
        if name not in blob:
            problems.append(f"the packaged surface is missing `{name}`")
    return problems


# ---------------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Transpile the revl gate component for JavaScript hosts.")
    parser.add_argument("--out", type=Path, required=True,
                        help="output directory for the JS package (not committed)")
    parser.add_argument("--check-surface", action="store_true",
                        help="also assert the transpiled surface keeps the "
                             "no-admission shape")
    args = parser.parse_args(argv[1:])

    reason = js_toolchain_reason()
    if reason is not None:
        print(f"cannot build the JS package: {reason}", file=sys.stderr)
        return 2

    info = build_js(args.out)
    print(json.dumps(info, indent=2, sort_keys=True))

    if args.check_surface:
        problems = check_surface(args.out)
        if problems:
            print("transpiled surface PROBLEMS:", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            return 1
        print("transpiled surface: `admitted` is the literal false, "
              "all four exports present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
