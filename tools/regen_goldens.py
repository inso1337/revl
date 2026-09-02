#!/usr/bin/env python3
"""Regenerate every checked-in golden, and check them all for drift.

    python3 tools/regen_goldens.py                 # list every target
    python3 tools/regen_goldens.py --check         # drift check, all targets
    python3 tools/regen_goldens.py --check rust    # drift check, one target
    python3 tools/regen_goldens.py rust wasm       # regenerate those targets
    python3 tools/regen_goldens.py --all           # regenerate everything

Why this exists
---------------
The goldens are SNAPSHOT tests, not a freeze (docs/conformance.md, "Golden
policy: snapshot, not freeze"). The invariant is "emitter output never changes
*unreviewed*", so regenerating plus reviewing the diff is always an acceptable
resolution. That policy only works if regenerating is mechanical, and it was
not: six backends carried six different answers to "how do I regenerate this?"
— a python script here, a shell script there, and for three tiers no script at
all, only an emit recipe buried in a test. An agent facing a red golden had to
reverse-engineer the recipe, and the cheap way out is to bend the emitter back,
which is exactly what the policy forbids.

This tool is the single answer. Every target names the files it owns and knows
how to produce their bytes, so `--check` can say precisely which file drifted
and which command fixes it.

Isolation
---------
Each target runs in its own subprocess (`--worker`). The six backends' emitters
are all called `emit.py` and several import siblings by bare name, so loading
two of them into one interpreter collides. One process per target sidesteps it
and costs nothing measurable.

Adding a target
---------------
Append a `Target` to `TARGETS`. `produce` returns `{repo-relative path: text}`
and must be pure (no writes) — the driver writes on regen and compares in
memory on check. `commands` are for producers that need a shell (gofmt, a
tier's own regen.sh); the driver snapshots and restores their files to check
them, so they must be deterministic. A target must declare every file it owns:
an undeclared file is an unchecked file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Worker exit codes. 1 is drift, 2 is a broken producer, 3 is a loud skip: the
# machine lacks a tool the producer needs, which is not a stale golden.
SKIPPED = 3


def _load(name: str, path: Path):
    """Load a module by path under an explicit name, the way the backends' own
    tests load their emitters."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _frontend():
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    import revl  # noqa: PLC0415

    return revl


def _reference_ir() -> dict:
    return json.loads((ROOT / "examples" / "user_cache.ir.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------- producers
#
# One per tier. Each returns {repo-relative path: text} and writes nothing.


def produce_python() -> dict[str, str]:
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    emit = _load("emit", ROOT / "backends" / "python" / "emit.py")
    return {"backends/python/golden/user_cache.py": emit.emit(_reference_ir())}


def produce_typescript() -> dict[str, str]:
    backend = ROOT / "backends" / "typescript"
    sys.path.insert(0, str(backend))
    # `emit_temporal.py` does `from emit import ...`, so the emitter has to be
    # under its canonical name for the temporal target to share one EmitError.
    emit = _load("emit", backend / "emit.py")
    fixtures = backend / "tests" / "fixtures"
    out = {"backends/typescript/golden/user_cache.ts": emit.emit(_reference_ir())}

    # The fixture-driven goldens. Each is `tsc`-validated by `npm run typecheck`
    # (tsconfig includes golden/**), which is what pins the type-level claims
    # each one exists for: `Any` -> `any` (item 79), `Promise<T>` at awaited
    # call sites (item 80), async fn-coloring (item 90), async function values
    # (item 92).
    for stem, golden in (("fr3_json", "fr3_json.ts"),
                         ("async_http", "async_http.ts"),
                         ("async_agent_loop", "async_agent_loop.ts"),
                         ("async_fn_values", "async_fn_values.ts")):
        ir = json.loads((fixtures / f"{stem}.ir.json").read_text(encoding="utf-8"))
        out[f"backends/typescript/golden/{golden}"] = emit.emit(ir)

    # The Temporal rendering mode (item 253): a different target off the same
    # emitter, from a committed source rather than an inline string, so this
    # tool and backends/typescript/test_temporal_target.py cannot disagree.
    src = (fixtures / "booktrip.revl").read_text(encoding="utf-8")
    ir = _frontend().compile_source(src, "booktrip.revl")
    out["backends/typescript/golden/temporal_booktrip.ts"] = emit.emit(ir, target="temporal")
    return out


def produce_rust() -> dict[str, str]:
    backend = ROOT / "backends" / "rust"
    emit = _load("emit", backend / "emit.py")
    revl = _frontend()
    jsonwire = revl.compile_files([str(backend / "scenarios" / "jsonwire.rvl")])
    return {
        "backends/rust/golden/user_cache.rs": emit.emit(_reference_ir()),
        "backends/rust/golden/jsonwire.rs": emit.emit(jsonwire),
    }


def produce_java() -> dict[str, str]:
    emit = _load("emit", ROOT / "backends" / "java" / "emit.py")
    return {"backends/java/golden/user_cache.java": emit.emit(_reference_ir())}


def produce_wasm() -> dict[str, str]:
    backend = ROOT / "backends" / "wasm"
    golden = backend / "golden"
    sys.path.insert(0, str(backend))
    emit = _load("emit", backend / "emit.py")
    revl = _frontend()
    out: dict[str, str] = {}

    # v3 pure functions: the typed-core surface (records, lists, variants,
    # branches) over top-level fns.
    src = (golden / "functions.revl").read_text(encoding="utf-8")
    out["backends/wasm/golden/functions.wat"] = emit.emit(revl.compile_source(src))["functions"]

    # The component-tier modules, from the shipped examples.
    beacon = emit.emit(revl.compile_files([str(ROOT / "examples" / "beacon.rvl")]))
    for name in ("Beacon", "Auditor"):
        out[f"backends/wasm/golden/{name}.wat"] = beacon[name]
    pulse = emit.emit(revl.compile_files([str(ROOT / "examples" / "pulse.rvl")]))
    out["backends/wasm/golden/Pulse.wat"] = pulse["Pulse"]

    # The canonical-ABI component goldens (item 41 slice-3): the Str-only
    # fixture, the aggregate follow-on, and the same value surface presented
    # from a component's `provide` methods.
    canonical = _load("canonical", backend / "canonical.py")
    for stem, service in (("canonical_echoer", "Echoer"),
                          ("canonical_aggregates", "Registry"),
                          ("canonical_service", "Registry")):
        text = (golden / f"{stem}.revl").read_text(encoding="utf-8")
        res = canonical.emit_component(revl.compile_source(text), service=service)
        out[f"backends/wasm/golden/{stem}.core.wat"] = res["core_wat"]
        out[f"backends/wasm/golden/{stem}.wit"] = res["wit"]
    return out


# ----------------------------------------------------------------- registry


@dataclass(frozen=True)
class Target:
    name: str
    what: str
    files: tuple[str, ...]
    produce: object = None                       # () -> {path: text}, pure
    commands: tuple[tuple[str, ...], ...] = ()   # argv, run from ROOT
    check_command: tuple[str, ...] | None = None  # its own drift gate, if any
    gate: str = ""                               # the test that reds on drift
    unstable: tuple[str, ...] = ()               # declared, but not byte-reproducible
    requires: tuple[str, ...] = ()               # tools that must be on PATH
    notes: tuple[str, ...] = field(default_factory=tuple)


def _glob(*patterns: str) -> tuple[str, ...]:
    out: list[str] = []
    for pattern in patterns:
        out += sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern))
    return tuple(out)


TARGETS: tuple[Target, ...] = (
    Target(
        name="python",
        what="reference-IR golden for the python (cordis-py) tier",
        files=("backends/python/golden/user_cache.py",),
        produce=produce_python,
        gate="pytest tests/test_goldens.py backends/python/tests/test_emitter.py",
        notes=("backends/python/golden/fork_report_compensate_false.json is NOT here: "
               "tests/test_session_fork.py compares it as PARSED json, not bytes, and "
               "its formatting is authored. Edit it by hand.",),
    ),
    Target(
        name="typescript",
        what="reference-IR, fixture and Temporal goldens for the typescript tier",
        files=("backends/typescript/golden/user_cache.ts",
               "backends/typescript/golden/fr3_json.ts",
               "backends/typescript/golden/async_http.ts",
               "backends/typescript/golden/async_agent_loop.ts",
               "backends/typescript/golden/async_fn_values.ts",
               "backends/typescript/golden/temporal_booktrip.ts"),
        produce=produce_typescript,
        gate="pytest tests/test_goldens.py backends/typescript/test_temporal_target.py",
        notes=("golden/activities.ts and golden/temporal-sdk.d.ts are hand-written "
               "support files that the emitted workflow is typechecked against, not "
               "emitter output. They are not regenerated.",),
    ),
    Target(
        name="rust",
        what="reference-IR and jsonwire goldens plus the crashproof scenario",
        files=("backends/rust/golden/user_cache.rs",
               "backends/rust/golden/jsonwire.rs",
               "backends/rust/scenarios/crashproof/src/lib.rs"),
        produce=produce_rust,
        commands=(("sh", "backends/rust/scenarios/crashproof/regen.sh"),),
        gate="pytest tests/test_goldens.py backends/rust/test_emit_rust.py",
    ),
    Target(
        name="java",
        what="reference-IR golden plus the crashproof scenario",
        files=("backends/java/golden/user_cache.java",
               "backends/java/scenarios/crashproof/revl/Components.java"),
        produce=produce_java,
        commands=(("sh", "backends/java/scenarios/crashproof/regen.sh"),),
        gate="pytest tests/test_goldens.py backends/java/test_emit_java.py",
        unstable=("backends/java/scenarios/crashproof/revl/Components.java",),
        notes=("Components.java is regenerated but NOT drift-checked: the java emitter "
               "names a witnessed step's temporary from the AST node's `id()`, so two "
               "runs of the same input differ (`_revl_wit4419035648` vs "
               "`_revl_wit4345361728`). It is compiled by the backend-java job, never "
               "byte-compared. Making that gensym deterministic would let it be "
               "checked like every other target.",),
    ),
    Target(
        name="wasm",
        what="v3 functions, component modules and canonical-ABI goldens",
        files=("backends/wasm/golden/functions.wat",
               "backends/wasm/golden/Beacon.wat",
               "backends/wasm/golden/Auditor.wat",
               "backends/wasm/golden/Pulse.wat",
               "backends/wasm/golden/canonical_echoer.core.wat",
               "backends/wasm/golden/canonical_echoer.wit",
               "backends/wasm/golden/canonical_aggregates.core.wat",
               "backends/wasm/golden/canonical_aggregates.wit",
               "backends/wasm/golden/canonical_service.core.wat",
               "backends/wasm/golden/canonical_service.wit",
               "backends/wasm/scenarios/crashproof/crashproof.ir.json"),
        produce=produce_wasm,
        commands=(("sh", "backends/wasm/scenarios/crashproof/regen.sh"),),
        gate=("pytest tests/test_goldens.py tests/test_wasm_backend.py "
              "backends/wasm/test_v3_emit.py backends/wasm/test_canonical_abi.py"),
    ),
    Target(
        name="go",
        what="the emitted go under scenarios/emitted and v3 (needs gofmt)",
        files=_glob("backends/go/scenarios/emitted/*/gen*.go",
                    "backends/go/v3/*/gen*.go",
                    "backends/go/scenarios/crashproof/gen_crash_recovery_test.go"),
        commands=(("sh", "backends/go/regen.sh"),
                  ("sh", "backends/go/scenarios/crashproof/regen.sh")),
        gate="pytest backends/go/test_emit_go.py",
        requires=("gofmt",),
        notes=("Without gofmt the emitted go is written unformatted, so this target "
               "loud-skips rather than reporting a drift that is really a missing "
               "tool. CI's backend-go job is the real gate.",),
    ),
    Target(
        name="gate-crate",
        what="crates/revl-gate, the committed rust gate crate (item 332)",
        files=("crates/revl-gate/",),
        commands=(("python3", "tools/build_gate_crate.py"),),
        check_command=("python3", "tools/build_gate_crate.py", "--check"),
        gate="pytest tests/test_gate_crate_drift.py",
        notes=("The crate embeds emitted rust, so ANY change to backends/rust/emit.py "
               "or to selfhost/*.rvl rewrites it. Regenerate it in the same commit as "
               "the emitter change; a PR that does not is red on drift alone.",),
    ),
)

BY_NAME = {t.name: t for t in TARGETS}


# -------------------------------------------------------------------- worker


def _snapshot(target: Target) -> dict[str, bytes | None]:
    snap: dict[str, bytes | None] = {}
    for rel in target.files:
        path = ROOT / rel
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    snap[str(child.relative_to(ROOT))] = child.read_bytes()
        else:
            snap[rel] = path.read_bytes() if path.exists() else None
    return snap


def _restore(snap: dict[str, bytes | None]) -> None:
    for rel, blob in snap.items():
        path = ROOT / rel
        if blob is None:
            path.unlink(missing_ok=True)
        elif not path.exists() or path.read_bytes() != blob:
            path.write_bytes(blob)


def _run_commands(target: Target) -> None:
    """Run the target's shell producers. Their output is kept quiet unless one
    fails: the driver's own per-file report is the authoritative one, and a
    script echoing "regenerated X" for a file it rewrote byte-identically reads
    as a contradiction next to it."""
    for argv in target.commands:
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            raise subprocess.CalledProcessError(proc.returncode, argv)


def worker(target: Target, check: bool) -> int:
    """Regenerate (or check) one target, in a process of its own."""
    drifted: list[str] = []

    missing = [tool for tool in target.requires if shutil.which(tool) is None]
    if missing:
        # Loud, never silent, and never a drift report: a missing tool would
        # make the producer write different bytes, which is a broken machine
        # rather than a stale golden.
        print(f"SKIP   {target.name}: needs {', '.join(missing)} on PATH")
        return SKIPPED

    if target.produce is not None:
        produced: dict[str, str] = target.produce()
        undeclared = sorted(set(produced) - set(target.files))
        if undeclared:
            print(f"regen-goldens: {target.name} produces undeclared files: "
                  f"{', '.join(undeclared)}", file=sys.stderr)
            return 2
        for rel, text in sorted(produced.items()):
            path = ROOT / rel
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current == text:
                continue
            drifted.append(rel)
            if not check:
                path.write_text(text, encoding="utf-8")
                print(f"  regenerated {rel}")

    if target.check_command is not None and check:
        # The target brings its own drift gate (tempdir-based, no mutation).
        if subprocess.run(target.check_command, cwd=ROOT).returncode != 0:
            drifted.append(target.files[0])
    elif target.commands:
        # A shell producer: snapshot, run it, diff, and put the files back when
        # this is only a check.
        snap = _snapshot(target)
        try:
            _run_commands(target)
        except subprocess.CalledProcessError as exc:
            if check:
                _restore(snap)
            print(f"regen-goldens: {target.name} regeneration failed: {exc}", file=sys.stderr)
            return 2
        after = _snapshot(target)
        for rel in sorted(set(snap) | set(after)):
            if snap.get(rel) == after.get(rel):
                continue
            if rel in target.unstable:
                if check:
                    print(f"unstable {target.name}: {rel} is not byte-reproducible; "
                          f"not drift-checked")
                    continue
            drifted.append(rel)
            if not check:
                print(f"  regenerated {rel}")
        if check:
            _restore(snap)

    if not check:
        if not drifted:
            print(f"  {target.name}: already current")
        return 0

    if drifted:
        print(f"DRIFT  {target.name}: {len(drifted)} file(s) differ from a fresh generation")
        for rel in drifted:
            print(f"         {rel}")
        print(f"       fix: python3 tools/regen_goldens.py {target.name}   "
              f"(then review the diff and commit it)")
        return 1
    print(f"ok     {target.name}")
    return 0


# -------------------------------------------------------------------- driver


def do_list() -> int:
    print("Golden targets. Goldens are snapshots, not a freeze — regenerating one and")
    print("reviewing its diff is always an acceptable resolution (docs/conformance.md,")
    print('"Golden policy: snapshot, not freeze"). Regenerate in the SAME commit as the')
    print("emitter change that moved the bytes.\n")
    for target in TARGETS:
        print(f"  {target.name:<11} {target.what}")
        print(f"  {'':<11} regen: python3 tools/regen_goldens.py {target.name}")
        if target.gate:
            print(f"  {'':<11} gate:  {target.gate}")
        for rel in target.files:
            mark = "  (regenerated, not drift-checked)" if rel in target.unstable else ""
            print(f"  {'':<11}   {rel}{mark}")
        for note in target.notes:
            print(f"  {'':<11} note:  {note}")
        print()
    print("  python3 tools/regen_goldens.py --check      drift-check every target")
    print("  python3 tools/regen_goldens.py --all        regenerate every target")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate or drift-check every checked-in golden.")
    parser.add_argument("targets", nargs="*", help=f"one or more of: {', '.join(BY_NAME)}")
    parser.add_argument("--all", action="store_true", help="every target")
    parser.add_argument("--check", action="store_true",
                        help="report drift instead of writing; exit 1 if any target drifted")
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    parser.add_argument("--worker", metavar="TARGET",
                        help=argparse.SUPPRESS)  # internal: run one target in-process
    args = parser.parse_args(argv)

    if args.worker:
        return worker(BY_NAME[args.worker], args.check)

    if args.list or (not args.targets and not args.all and not args.check):
        return do_list()

    unknown = [t for t in args.targets if t not in BY_NAME]
    if unknown:
        parser.error(f"unknown target(s): {', '.join(unknown)}. "
                     f"Known: {', '.join(BY_NAME)}")
    chosen = [BY_NAME[t] for t in args.targets] if args.targets else list(TARGETS)

    verb = "checking" if args.check else "regenerating"
    print(f"regen-goldens: {verb} {len(chosen)} target(s)", flush=True)
    worst = 0
    skipped = 0
    for target in chosen:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", target.name]
        if args.check:
            cmd.append("--check")
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        if rc == SKIPPED:
            skipped += 1
            continue
        worst = max(worst, rc)

    tail = f" ({skipped} skipped for a missing tool)" if skipped else ""
    if args.check:
        if worst == 0:
            print(f"regen-goldens: every golden that could be checked matches a fresh "
                  f"generation{tail}.")
        else:
            print("regen-goldens: DRIFT. Run the regen command each target printed, review")
            print("               the diff, and commit it. Do NOT bend the emitter back to")
            print("               the old bytes — see docs/conformance.md, golden policy.")
    return worst


if __name__ == "__main__":
    sys.exit(main())
