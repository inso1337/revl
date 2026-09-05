#!/usr/bin/env python
"""The live-systems demo, as a scripted exit test (v3.0 release gate E3).

One command runs revl's cross-tier live migration end to end, from a clean
checkout, and ASSERTS the observable outcome of each stage:

  1. `revl swap`  (roadmap 23, docs/swap.md) — split the composition across two
     processes over a Unix-socket seam, then live-migrate the provider into a
     fresh process on the target tier. Assert the successor booted, every
     consumer RE-POINTED onto the new socket, the old provider drained with a
     no-residue proof, and nothing was left behind.

  2. `revl why`   (roadmap 27, docs/why-runtime.md) — record a run's causal
     trace, then ask why a component was withdrawn. Assert the cause chain
     NAMES the migration (Api withdrew because its provider MemCache did), and
     the prediction-vs-actuality oracle reports CONFORMS — the runtime tore the
     composition down in exactly the set and LIFO order the compiler computed.

  3. `revl plan` / `revl apply` (roadmap 36, docs/apply.md) — turn a plan into
     an executable artifact and apply it. Assert a clean apply lands the change
     with no residue, and that a mid-plan failure ROLLS THE APPLIED PREFIX BACK
     by derived LIFO inverses (undo as a theorem), again with no residue.

Every stage shells out to the real `revl` CLI a human would type. The demo
composition (app.rvl, split.toml, candidate.rvl) lives beside this file. All
run artifacts are written under a throwaway temp dir, so the demo depends on no
developer-machine state and is repeatable: run it twice and it passes twice.

The live stages need the cordis-py runtime (`sh backends/python/setup.sh`).
Without it the demo SKIPS loudly and exits 0 — unless REVL_DEMO_REQUIRE=1, which
turns the skip into a failure (CI sets it, so a silently-missing runtime cannot
pass the gate green).

    # from a clean checkout, once:
    sh backends/python/setup.sh
    backends/python/.venv/bin/python demo/live_systems/run_demo.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP = HERE / "app.rvl"
SPLIT = HERE / "split.toml"
CANDIDATE = HERE / "candidate.rvl"


class DemoError(AssertionError):
    """A stage did not produce the outcome the demo asserts."""


def _env() -> dict:
    """Subprocess env: this checkout's `src` on PYTHONPATH so `revl` always
    resolves into the tree under test (a worktree, a fresh clone), ahead of any
    editable install the interpreter happens to carry."""
    env = dict(os.environ)
    src = str(ROOT / "src")
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not prior else src + os.pathsep + prior
    env["NO_COLOR"] = "1"
    return env


def _revl(args: list[str], stdin: str = "", timeout: int = 180) -> subprocess.CompletedProcess:
    """Run `revl <args>` under this interpreter (which must carry the
    cordis-py runtime for the live stages), capturing combined output. The
    absolute-interpreter fallback `python -P -m revl <args>` is the form
    actually used here — the `-P` is the PYTHONSAFEPATH safety bit, issue
    #317 — because the demo driver is the harness's own interpreter, where
    a console script would not be on PATH without going through the venv's
    `bin/` directory explicitly. The subprocess call therefore always
    passes `-P` alongside `-m`, the same shape `python -P -m revl …` in
    docs spells."""
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "revl", *args],
        input=stdin,
        cwd=str(ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return proc


def _require(condition: bool, message: str, out: str) -> None:
    if not condition:
        raise DemoError(message + "\n--- captured output ---\n" + out)


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


# --------------------------------------------------------------------------
# stage 1 — revl swap: live cross-process migration of the provider
# --------------------------------------------------------------------------

def stage_swap() -> None:
    _banner("stage 1/3  revl swap — live migration across a process seam")
    # scripted, non-interactive: the swap script arrives on stdin, one command
    # per line, and EOF tears the placement down (docs/swap.md "Scripted swaps").
    script = "swap MemCache --to py\n:keys\n:q\n"
    proc = _revl(["run", str(APP), "--placement", str(SPLIT)], stdin=script)
    out = proc.stdout

    _require(proc.returncode == 0, f"swap run exited {proc.returncode}", out)
    _require("swap refused" not in out, "the swap was REFUSED (see diagnostic)", out)
    # the seam answered before the cutover
    _require("cache.get('ada')| => '42'" in out,
             "the pre-cutover probe did not answer '42' across the seam", out)
    # a successor booted on the target tier
    _require("booting MemCache on the py tier (MemCache__t1)" in out,
             "no successor provider was booted on the target tier", out)
    # every consumer re-pointed onto the successor's socket
    _require("REPOINTED cache -> " in out and "MemCache__t1.sock" in out,
             "the consumer did not re-point onto the successor socket", out)
    # the old provider drained and proved no residue as it left
    _require("[provider] residue no residue" in out,
             "the old provider did not print a no-residue proof on drain", out)
    _require("MemCache now on py (MemCache__t1)" in out,
             "the swap did not report the component adopted on the new tier", out)
    # nothing anywhere left residue at final teardown
    _require("RESIDUE LEFT" not in out, "teardown reported residue left behind", out)
    print("  swap landed: successor booted, consumer re-pointed, old provider "
          "drained with no residue.", flush=True)


# --------------------------------------------------------------------------
# stage 2 — revl why: causal trace + prediction-vs-actuality oracle
# --------------------------------------------------------------------------

def stage_why(work: Path) -> None:
    _banner("stage 2/3  revl why — causal trace and the runtime oracle")
    trace = work / "run.jsonl"
    # withdraw the provider so the trace carries a real reactive cascade
    rec = _revl(["run", str(APP), "--trace", str(trace), "--withdraw", "MemCache"])
    _require(rec.returncode == 0, f"traced run exited {rec.returncode}", rec.stdout)
    _require(trace.is_file(), f"no trace written to {trace}", rec.stdout)

    why = _revl(["why", "MemCache", "--trace", str(trace), "--check", str(APP)])
    out = why.stdout
    _require(why.returncode == 0, f"revl why exited {why.returncode}", out)
    # the chain names the migration
    _require("why MemCache was withdrawn" in out,
             "the why chain did not explain MemCache's withdrawal", out)
    _require("withdrawn by operator" in out,
             "the root cause of the withdrawal was not named", out)
    # the prediction-vs-actuality oracle fires and conforms
    _require("CONFORMS" in out,
             "the runtime oracle did not report CONFORMS", out)
    _require("predicted teardown (LIFO): Api -> MemCache" in out
             and "actual   teardown (LIFO): Api -> MemCache" in out,
             "the oracle did not show predicted == actual LIFO teardown", out)

    # and the dependent's own chain: Api withdrew *because* its provider did
    dep = _revl(["why", "Api", "--trace", str(trace)])
    depout = dep.stdout
    _require("injects `cache`, provided by MemCache, which withdrew" in depout,
             "the dependent's cause chain did not name the provider that left", depout)
    print("  why explained it: cause chain names the migration, oracle CONFORMS "
          "(set and LIFO order).", flush=True)


# --------------------------------------------------------------------------
# stage 3 — revl plan / apply: executable plan with a derived rollback
# --------------------------------------------------------------------------

def stage_apply(work: Path) -> None:
    _banner("stage 3/3  revl plan / apply — a plan you can apply, rollback derived")
    running = work / "running.json"
    plan = work / "change.plan"

    comp = _revl(["compile", str(APP), "-o", str(running)])
    _require(comp.returncode == 0 and running.is_file(),
             "compiling the running composition failed", comp.stdout)

    pl = _revl(["plan", str(CANDIDATE), "--manifest", str(running), "-o", str(plan)])
    _require(pl.returncode == 0 and plan.is_file(),
             "planning the candidate failed", pl.stdout)
    _require("applyable plan" in pl.stdout, "plan did not write an applyable artifact", pl.stdout)

    # (a) a clean apply lands the change with no residue
    ap = _revl(["apply", str(plan)])
    out = ap.stdout
    _require("applied:" in out, "the clean apply did not report success", out)
    _require("Audit" in out, "the applied composition did not include the new component", out)
    _require("no residue: True" in out, "the clean apply left residue", out)
    print("  clean apply landed the change (Audit loaded), no residue.", flush=True)

    # (b) a mid-plan failure rolls the applied prefix back by DERIVED inverses.
    # Tamper one step's prediction so verification fails after a prefix applied;
    # the engine must undo that prefix LIFO and prove no residue (docs/apply.md).
    import json  # noqa: PLC0415
    artifact = json.loads(plan.read_text(encoding="utf-8"))
    tampered_ok = False
    for op in artifact["operations"]:
        if op["op"] == "load" and op["name"] == "Api":
            op["predict"]["state"] = "PENDING"   # it will actually be ACTIVE
            tampered_ok = True
    _require(tampered_ok, "could not find the 'load Api' step to tamper", pl.stdout)
    tampered = work / "tampered.plan"
    tampered.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    ta = _revl(["apply", str(tampered)])
    tout = ta.stdout
    _require("FAILED at `Api`" in tout,
             "the tampered apply did not fail at the tampered step", tout)
    _require("LIFO, derived inverses" in tout,
             "the rollback was not reported as derived LIFO inverses", tout)
    _require("no residue: True" in tout,
             "the rollback left residue behind", tout)
    print("  forced failure rolled the prefix back by derived LIFO inverses, no "
          "residue.", flush=True)


# --------------------------------------------------------------------------

def main() -> int:
    require = os.environ.get("REVL_DEMO_REQUIRE") == "1"
    # inserting src lets us import the frontend; the live stages additionally
    # need the cordis-py runtime on this interpreter.
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    try:
        import cordis  # noqa: F401  (presence probe)
    except ModuleNotFoundError:
        msg = ("SKIP: the cordis-py runtime is not installed, so the live "
               "migration demo cannot run.\n"
               "      set it up:  sh backends/python/setup.sh\n"
               "      then run as:  revl ...                                  # the documented happy path\n"
               "                      backends/python/.venv/bin/python -P -m revl ...  # absolute-interpreter fallback\n"
               "      OR re-run this demo under the venv:\n"
               "                      backends/python/.venv/bin/python demo/live_systems/run_demo.py")
        if require:
            print("error: REVL_DEMO_REQUIRE=1 but " + msg, file=sys.stderr)
            return 1
        print(msg, flush=True)
        return 0

    for path in (APP, SPLIT, CANDIDATE):
        if not path.is_file():
            print(f"error: demo fixture missing: {path}", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory(prefix="revl-e3-demo-") as tmp:
        work = Path(tmp)
        try:
            stage_swap()
            stage_why(work)
            stage_apply(work)
        except DemoError as err:
            print(f"\nDEMO FAILED: {err}", file=sys.stderr)
            return 1
        except subprocess.TimeoutExpired as err:
            print(f"\nDEMO FAILED: a stage timed out: {err}", file=sys.stderr)
            return 1

    print("\nlive-systems demo OK — swap migrated, why explained, apply rolled "
          "back. All three from a clean checkout.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
