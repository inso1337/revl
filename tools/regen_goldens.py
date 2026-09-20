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

If a producer returns the SAME text for two paths — one emission committed at
two places — say so in `twins`. That is an invariant of its own: one of the two
files moving alone is a defect whichever version is right, and until issue #1288
nothing said it out loud, so it surfaced as a generic one-file drift on an
unrelated PR's gate instead of at the producer. The worker refuses an undeclared
twin pair, so a new target cannot acquire the same silence by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Worker exit codes, and they are three different answers rather than degrees of
# the same one:
#
#   1 DRIFT      the golden and a fresh generation disagree. Regenerate.
#   2 BROKEN     the producer could not be RUN here, so nothing was compared.
#                This is not a stale golden and must never be reported as one.
#   3 SKIPPED    a tool the producer declares is absent. Also nothing compared,
#                but expected on this machine rather than a fault.
#
# Conflating 2 with 1 is how this tool first reported `DRIFT rust/java/wasm` in
# a job where the real fact was "the three crashproof producers never executed":
# `sh` is dash on ubuntu, the scripts are bash (`set -o pipefail`,
# `${BASH_SOURCE[0]}`), and a driver that ran them under the wrong interpreter
# reported the failure as staleness. "I could not check this" and "this is
# stale" resolve differently and must not print the same word.
DRIFT = 1
BROKEN = 2
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
    user_cache = emit.emit(_reference_ir())
    return {
        "backends/rust/golden/user_cache.rs": user_cache,
        "backends/rust/golden/jsonwire.rs": emit.emit(jsonwire),
        # The placement runner's components module (issue #1089). `revl run
        # --placement --backend rust` REGENERATES this file from the running
        # IR and restores it afterwards (src/revl/placement.py::_build_rust,
        # tests/test_run_rust.py), so the committed copy is the reference
        # composition's emission and nothing else — the same bytes as the
        # golden above, from the same producer and the same IR. It was a
        # committed emitted artifact that no target declared and no test
        # compared, and it had drifted 41 lines behind the emitter (its own
        # header still said cordis-rs 0.3.x).
        "backends/rust/placement_runner/src/components.rs": user_cache,
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
    twins: tuple[tuple[str, ...], ...] = ()      # path groups that are ONE emission
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
        what="reference-IR, jsonwire and placement-runner goldens plus the crashproof scenario",
        files=("backends/rust/golden/user_cache.rs",
               "backends/rust/golden/jsonwire.rs",
               "backends/rust/placement_runner/src/components.rs",
               "backends/rust/scenarios/crashproof/src/lib.rs"),
        produce=produce_rust,
        commands=(("bash", "backends/rust/scenarios/crashproof/regen.sh"),),
        requires=("bash",),
        gate="pytest tests/test_goldens.py backends/rust/test_emit_rust.py",
        # One emission, committed twice (issue #1288). `produce_rust` binds the
        # same string to both paths, so the ONLY way they can disagree is that
        # one of them was written by something other than this producer — and
        # that has happened: an `ir_version 3` emission of examples/outcome.rvl,
        # left behind by the placement runner's per-composition codegen, was
        # swept into components.rs alone by an unrelated docs commit. It was
        # caught days later as a one-file drift on someone else's PR.
        twins=(("backends/rust/golden/user_cache.rs",
                "backends/rust/placement_runner/src/components.rs"),),
    ),
    Target(
        name="java",
        what="reference-IR golden plus the crashproof scenario",
        files=("backends/java/golden/user_cache.java",
               "backends/java/scenarios/crashproof/revl/Components.java"),
        produce=produce_java,
        commands=(("bash", "backends/java/scenarios/crashproof/regen.sh"),),
        requires=("bash",),
        gate="pytest tests/test_goldens.py backends/java/test_emit_java.py",
        notes=("Components.java used to be regenerated but NOT drift-checked: the java "
               "emitter named a witnessed step's temporary from the AST node's `id()`, "
               "so two runs of the same input differed (`_revl_wit4419035648` vs "
               "`_revl_wit4345361728`). That gensym is emission-order indexed now "
               "(`_V3Ctx.next_gensym`), so the file is byte-reproducible and drift-"
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
        commands=(("bash", "backends/wasm/scenarios/crashproof/regen.sh"),),
        requires=("bash",),
        gate=("pytest tests/test_goldens.py tests/test_wasm_backend.py "
              "backends/wasm/test_v3_emit.py backends/wasm/test_canonical_abi.py"),
    ),
    Target(
        name="go",
        what="the emitted go under scenarios/emitted and v3 (needs gofmt)",
        files=_glob("backends/go/scenarios/emitted/*/gen*.go",
                    "backends/go/v3/*/gen*.go",
                    "backends/go/scenarios/crashproof/gen_crash_recovery_test.go"),
        commands=(("bash", "backends/go/regen.sh"),
                  ("bash", "backends/go/scenarios/crashproof/regen.sh")),
        gate="pytest backends/go/test_emit_go.py",
        requires=("bash", "gofmt"),
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
    Target(
        name="gate-wasm",
        what="crates/revl-gate-wasm, the gate as a wasm component (item 335)",
        files=("crates/revl-gate-wasm/",),
        commands=(("python3", "tools/build_gate_wasm.py"),),
        check_command=("python3", "tools/build_gate_wasm.py", "--check"),
        gate="pytest tests/test_gate_wasm_drift.py",
        notes=("The component INHERITS its frontier id, language version and covered "
               "layer from crates/revl-gate's provenance instead of restating them, so "
               "regenerating the rust crate rewrites these bytes too: the two targets "
               "move together and a PR that refreshes one without the other is red on "
               "drift alone. No toolchain is needed — the .wasm itself is not committed, "
               "only the crate source — so this is a pure regenerate-and-compare and it "
               "runs on every machine.",),
    ),
)

BY_NAME = {t.name: t for t in TARGETS}


# ---------------------------------------------------------------- twin rules
#
# A producer may own one emission committed at more than one path. That is a
# real invariant and it is not the same one `--check` already enforces: a
# per-file comparison against a fresh generation says "components.rs differs",
# which reads as ordinary staleness, and the reader regenerates whichever tree
# they happen to be standing in. "These two files are one emission and one of
# them moved alone" says what to do about it and, crucially, says it at the
# producer rather than on the next PR to run the gate.
#
# The three functions below are pure and take their bytes from a callable, so
# tests/test_emitted_artifacts_are_drift_gated.py can drive them off a
# synthetic tree and show they catch the thing they exist for.


def twin_mismatches(target: Target, read) -> list[str]:
    """One report line per declared twin group that is NOT byte-identical.

    `read(rel)` returns the file's bytes, or None when it is absent — a missing
    member is a mismatch, not a pass."""
    lines: list[str] = []
    for group in target.twins:
        by_digest: dict[str, list[str]] = {}
        for rel in group:
            blob = read(rel)
            digest = "<missing>" if blob is None else hashlib.sha256(blob).hexdigest()[:12]
            by_digest.setdefault(digest, []).append(rel)
        if len(by_digest) < 2:
            continue
        shown = "  ".join(f"[{digest}] {', '.join(paths)}"
                          for digest, paths in sorted(by_digest.items()))
        lines.append(
            f"{target.name}: these paths are ONE emission from one producer and one "
            f"input, and they are not byte-identical. One of them moved alone, which "
            f"is a defect whichever version is the right one.\n"
            f"         {shown}")
    return lines


def undeclared_twins(target: Target, produced: dict[str, str]) -> list[tuple[str, ...]]:
    """Path groups the producer returned identical text for that no `twins`
    group covers. Declaring them is what lets the cheap on-disk gate see them
    without running an emitter."""
    declared = [frozenset(group) for group in target.twins]
    by_text: dict[str, list[str]] = {}
    for rel, text in produced.items():
        by_text.setdefault(text, []).append(rel)
    return [tuple(sorted(paths)) for paths in by_text.values()
            if len(paths) > 1 and not any(frozenset(paths) <= group for group in declared)]


def stale_twin_declarations(target: Target, produced: dict[str, str]) -> list[tuple[str, ...]]:
    """Declared twin groups the producer does NOT in fact emit identically. The
    declaration is then a claim nothing backs, which is worse than none."""
    stale = []
    for group in target.twins:
        present = [rel for rel in group if rel in produced]
        if len(present) > 1 and len({produced[rel] for rel in present}) > 1:
            stale.append(tuple(group))
    return stale


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


def script_interpreter(path: Path) -> str | None:
    """The interpreter a script's own shebang names, or None if it has none.

    The registry has to invoke a script the way the script says it must be
    invoked. Every `regen.sh` in this repo is `#!/usr/bin/env bash` and uses
    bash-only syntax — `set -o pipefail`, `${BASH_SOURCE[0]}` — and the driver
    used to run all of them as `sh <script>`. On macOS `/bin/sh` IS bash in
    POSIX mode, so that worked; on ubuntu `/bin/sh` is dash, `set -o pipefail`
    is an error, and all three of the crashproof targets died on line one.
    `tests/test_emitted_artifacts_are_drift_gated.py` pins the pairing so a new
    script cannot be added under the wrong interpreter."""
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    words = first[2:].split()
    if not words:
        return None
    # `#!/usr/bin/env bash` -> bash; `#!/bin/sh` -> sh
    name = Path(words[0]).name
    return Path(words[1]).name if name == "env" and len(words) > 1 else name


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
        print(f"SKIP   {target.name}: needs {', '.join(missing)} on PATH — "
              f"NOTHING was compared for this target")
        return SKIPPED

    if target.produce is not None:
        produced: dict[str, str] = target.produce()
        undeclared = sorted(set(produced) - set(target.files))
        if undeclared:
            print(f"regen-goldens: {target.name} produces undeclared files: "
                  f"{', '.join(undeclared)}", file=sys.stderr)
            return BROKEN
        stale = stale_twin_declarations(target, produced)
        if stale:
            for group in stale:
                print(f"regen-goldens: {target.name} declares {', '.join(group)} as "
                      f"twins, but its producer returns different bytes for them. "
                      f"Either the producer changed or the declaration is wrong; do "
                      f"not leave a claim nothing backs.", file=sys.stderr)
            return BROKEN
        extra = undeclared_twins(target, produced)
        if extra:
            for group in extra:
                print(f"regen-goldens: {target.name} emits identical bytes to "
                      f"{', '.join(group)} without declaring them as twins. Add "
                      f"them to that target's `twins=` so one of them moving alone "
                      f"is reported as what it is, instead of as a one-file drift "
                      f"on whichever PR next runs the gate (issue #1288).",
                      file=sys.stderr)
            return BROKEN
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
            print(f"BROKEN {target.name}: its producer could not be RUN here, so "
                  f"NOTHING was compared for this target. This is not a drift "
                  f"report and regenerating will not fix it.\n"
                  f"       {exc}", file=sys.stderr)
            return BROKEN
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

    # The twin gate, read off DISK and therefore off the committed bytes on a
    # check run (both producer arms above leave the tree as they found it).
    # After a regen it reads the fresh bytes instead, where a mismatch can only
    # mean the producer itself is inconsistent — a different fault, reported as
    # one.
    def _on_disk(rel: str) -> bytes | None:
        path = ROOT / rel
        return path.read_bytes() if path.exists() else None

    mismatched = twin_mismatches(target, _on_disk)
    if mismatched and not check:
        for line in mismatched:
            print(f"BROKEN {line}", file=sys.stderr)
        print(f"       ...and this is AFTER regenerating {target.name}, so its "
              f"producer is not writing one emission to both paths. Nothing to "
              f"regenerate: fix the producer.", file=sys.stderr)
        return BROKEN
    if mismatched:
        for line in mismatched:
            print(f"TWIN   {line}")
        print(f"       fix: python3 tools/regen_goldens.py {target.name}   "
              f"(then review the diff and commit it)")

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
    if drifted or mismatched:
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
        for group in target.twins:
            print(f"  {'':<11} twins: one emission, committed at {len(group)} paths — "
                  f"they must stay byte-identical:")
            for rel in group:
                print(f"  {'':<11}   = {rel}")
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
    parser.add_argument("--strict", action="store_true",
                        help="a target that SKIPS for a missing tool is an ERROR. Use this "
                             "wherever the run is a gate: a skip and a pass are the same "
                             "colour on a dashboard, and a gate that skipped compared "
                             "nothing")
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
    drifted: list[str] = []
    broken: list[str] = []
    skipped: list[str] = []
    for target in chosen:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", target.name]
        if args.check:
            cmd.append("--check")
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        if rc == SKIPPED:
            skipped.append(target.name)
        elif rc == BROKEN:
            broken.append(target.name)
        elif rc == DRIFT:
            drifted.append(target.name)

    # Three answers, three sentences. A target that could not be checked has to
    # read differently from one that is stale: the first is resolved by fixing
    # the machine or the producer, the second by regenerating, and printing
    # "DRIFT" for both sends the next reader to regenerate something that was
    # never compared.
    if broken:
        which = "its producer" if len(broken) == 1 else "their producers"
        print(f"regen-goldens: COULD NOT CHECK {', '.join(broken)}: nothing was compared.")
        print(f"               Either {which} failed to RUN here, or the target's own")
        print("               declarations were refused (an undeclared output, an "
              "undeclared")
        print("               twin pair, a twin group the producer does not back). This is")
        print("               NOT a drift report and regenerating will not fix it: read the")
        print("               refusal line(s) above.")
    if skipped:
        note = "ERROR" if args.strict else "not checked"
        print(f"regen-goldens: {', '.join(skipped)} SKIPPED for a missing tool ({note}).")
        if args.strict:
            print("               --strict: this run is a gate, and a gate that skipped a")
            print("               target compared nothing for it. Run it in a job that has")
            print("               the tool, or stop claiming this job checks it.")
    if args.check and drifted:
        print("regen-goldens: DRIFT. Run the regen command each target printed, review")
        print("               the diff, and commit it. Do NOT bend the emitter back to")
        print("               the old bytes — see docs/conformance.md, golden policy.")
    if args.check and not (drifted or broken or skipped):
        print("regen-goldens: every golden checked matches a fresh generation.")
    if broken:
        return BROKEN
    if drifted:
        return DRIFT
    if skipped and args.strict:
        return SKIPPED
    if args.check and skipped:
        print(f"regen-goldens: every golden that could be checked matches a fresh "
              f"generation ({len(skipped)} skipped for a missing tool).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
