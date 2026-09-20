"""The rust in-process admission gate harness, held against the py one
(roadmap item 333, Slice 2).

`bench/inprocess_gate_rust` is the rust twin of `bench/inprocess_gate_harness.py`:
an agent tool-generation loop that links `revl-gate` as a LIBRARY and screens
every component it proposes IN ITS OWN PROCESS - no `revl mcp serve`, no IPC, no
wire, and no Python anywhere. This test builds it, runs it, and holds it against
the py harness on the same programs.

What is being proved, and what deliberately is NOT
--------------------------------------------------
The py harness proves an IDENTITY: `revl.gate.admit` IS the reference admission
path (`compile_source` + `refuse_admission`), so its in-process verdict IS the
reference verdict. The rust crate cannot claim that and does not: it is the
SELF-HOST front end compiled to rust, it decides the composition/guarantee layer
(`G1`..`G4`, `A1`, `PRELUDE`, `BAD`) and runs no type layer, so it has no
admission arm at all (`Refused` / `NoObjection` / `OutsideFrontier`, with
`"admitted": false` on the wire for every one).

It does now answer TWO questions (issue #346): `admit(source)` asks the
standalone one, and `admit_into(source, manifest)` folds the G2/G3 legs over the
UNION of a running composition's rows and the candidate. The second is what makes
the realistic agent loop - screen a candidate AGAINST the composition already
running - expressible without Python. It buys the AMBIENT half of that question;
it still does not resolve a `requires` against a live provider, which is the
self-host type layer's work and its own roadmap lane. Both halves are held to the
py gate's own answers below, one test each, and neither is overstated.

So the claim held here is a DIFFERENTIAL, and an asymmetric one - the rule
`docs/design/333-inprocess-gate.md` states for the rust tier and 332's release
gate fixes:

* **the release-blocking direction**: the rust gate must never let a host read
  anything as an admission. Structurally closed (no arm exists), held here over
  the whole batch anyway - on BOTH questions - because "structurally closed" is a
  claim about the code and this is the measurement.
* **the sound direction**: every refusal the rust gate DOES issue must be a real
  py refusal, with the same code and the same why-trace. A false alarm is a
  candidate an agent throws away for no reason.
* **the tolerated direction**: a rust no-objection says nothing about py. py may
  admit it (agreement) or refuse it on a layer rust does not run (the hole draft,
  the type layer, a `requires` that resolves against the running composition).
  That gap is measured and reported here, never assumed away.

The two harnesses screen the SAME BYTES: the rust harness emits each candidate's
source in its `--json` report, this test re-derives the py verdict from those
exact bytes, and `test_the_shared_candidates_are_the_py_harness_bytes` pins the
shared ones against `bench/inprocess_gate_harness.py`'s own constants. A py-side
edit therefore reds this test instead of silently letting the two harnesses
screen different programs.

Toolchain honesty
-----------------
This needs cargo AND a resolvable cordis-rs (the emitted self-host module speaks
`cordis::Value`), so the module SKIPS WITH THE REASON the driver reports when
either is absent - the `tests/test_gate_crate_admit.py` discipline. A skipped
tier is never green, and a green here always means a real harness was really
built and really ran. The `backend-rust` CI job runs this file, so the skip is
not where it lives in CI.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "bench" / "inprocess_gate_rust"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import gate as py_gate  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.holes import refuse_admission  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

# The reference refusal rendered in the GATE's guarantee vocabulary. Imported
# from the self-host lowering oracle, not re-derived here, so the two gates are
# compared in one vocabulary that cannot drift into two: `RevlError.code` alone
# is not it (a G2 provision conflict carries no code, only a message marker).
import test_selfhost_lower as oracle  # noqa: E402

_RUST_REASON = rust_runtime_reason()
pytestmark = pytest.mark.skipif(
    _RUST_REASON is not None,
    reason=f"needs a resolvable cordis-rs toolchain to build the rust in-process "
           f"gate harness: {_RUST_REASON}")

# Timed iterations for the in-test run, deliberately tiny. A single screen of
# the large cell costs SECONDS (bench/results/inprocess-gate-rust.md - that cost
# is the finding, not an accident of this test), so a py-sized iteration count
# would turn one CI job into an hour. The call is deterministic and CPU-bound,
# so a handful of samples is enough to produce a well-shaped distribution and
# to hold the two scaling claims the report makes; the committed numbers come
# from a real `--iters 25` run.
ITERS = 3
WARMUP = 1

# A gross-regression ceiling, not a figure. The representative screen is ~10 ms
# locally; a slower CI runner still sits under this, and only a catastrophic
# regression (or disk I/O sneaking into the timed path) trips it. Never a tight
# wall-clock assert - machines vary, and this one is already slow enough that a
# tight assert would just be noise.
CEILING_MS = 2000.0


# --------------------------------------------------------------- cargo policy
#
# Offline first; a networked resolve only when the offline attempt failed to
# RESOLVE a crate, never to launder a build failure into a retry. Same policy as
# tests/test_gate_crate_admit.py, backends/rust/test_emit_rust.py and
# tools/bench_selfhost_rust.py.

_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode", "without the offline flag",
    "--offline was specified", "registry index was not found",
    "no matching package", "failed to select a version",
)
_REAL_FAILURE_MARKERS = (
    "error[e", "could not compile", "panicked at", "test result: failed",
)


def _crates_io_reachable() -> bool:
    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _is_offline_resolve_failure(proc: subprocess.CompletedProcess) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(marker in blob for marker in _REAL_FAILURE_MARKERS):
        return False
    return any(marker in blob for marker in _OFFLINE_RESOLVE_MARKERS)


def _clean_env(target_dir: Path) -> dict:
    """No PYTHONPATH, no VIRTUAL_ENV: the harness must build and run with
    nothing from this repo's Python on the path - that is the "no Python
    anywhere" half of the claim, held as tightly as a test on a machine that
    does have Python can hold it. CARGO_TARGET_DIR keeps the build out of the
    checkout so a test run never leaves an untracked artifact behind."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env["CARGO_TARGET_DIR"] = str(target_dir)
    return env


def _cargo(subcommand: str, target_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    env = _clean_env(target_dir)
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=HARNESS, text=True,
        capture_output=True, timeout=3600, env=env, check=False)
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    if not _crates_io_reachable():
        return offline
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=HARNESS, text=True,
        capture_output=True, timeout=3600, env=env, check=False)


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    """Build the harness in release (a debug build's timings would be a
    fiction), run it once, and return its whole JSON report - candidates, their
    exact source bytes, verdicts, order-independence, fail-closed probes and the
    cost distribution."""
    target = tmp_path_factory.mktemp("inprocess_gate_rust_target")
    built = _cargo("build", target, "--release")
    assert built.returncode == 0, (
        "the rust in-process gate harness failed to build against "
        "crates/revl-gate:\n" + (built.stderr or built.stdout or "")[-4000:])
    binary = target / "release" / "inprocess_gate_rust"
    assert binary.exists(), f"harness binary not found at {binary}"

    run = subprocess.run(
        [str(binary), "--json", "--iters", str(ITERS), "--warmup", str(WARMUP)],
        text=True,
        capture_output=True, timeout=1800, env=_clean_env(target), check=False)
    assert run.returncode == 0, (
        "the harness reported a FAILED invariant (it exits nonzero when an arm "
        "reads as an admission, the wire shape breaks, verdicts are "
        "order-dependent, or a fail-closed path does not hold):\n"
        + (run.stderr or "")[-4000:] + (run.stdout or "")[-4000:])
    return json.loads(run.stdout)


def _py_verdict(source: str) -> tuple[bool, str | None]:
    """The py in-process ADMISSION verdict for the identical bytes - the same
    `revl.gate.admit` the py harness embeds, which is `compile_source` followed
    by `refuse_admission`. Not `revl compile`: that skips `refuse_admission` and
    accepts a draft-with-holes (design A1)."""
    verdict = py_gate.admit(source)
    return (verdict.admitted, verdict.code)


def _py_reference(source: str) -> tuple[str, str]:
    """`(tag, message)` - `("", "")` when the py admission gate ADMITS.

    The SAME two calls `revl.gate.admit` makes (`compile_source` then
    `refuse_admission`), with the refusal rendered in the gate's guarantee
    vocabulary by the self-host oracle's classifier. `Verdict.code` is not
    enough on its own: a G2 provision conflict sets no `code` at all, so
    comparing against it would silently pass a rust `G2` against a py `None`."""
    try:
        refuse_admission(compile_source(source))
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


def _py_manifest_reference(source: str, base: dict) -> tuple[str, str]:
    """`(tag, message)` for the py MANIFEST arm - `("", "")` when py ADMITS.

    The twin of `_py_reference`, and the same call `revl.gate.admit_into` makes
    (`compile_source(source, manifest=dict(manifest))`). It stops short of
    `admit_into` itself only to keep the RAISED error: `admit_into` renders a
    refusal through `_verdict_from_error`, whose message is `str(error)`, while
    the vocabulary this test compares the two gates in is `_classify(error)` +
    `error.message` - and a G2 conflict carries no `code` to compare at all.
    `test_the_manifest_gap_is_priced_not_hidden` holds the public `admit_into`
    verb itself to this mirror, so the two cannot drift."""
    try:
        compile_source(source, manifest=dict(base))
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


# ------------------------------------------- the release-blocking direction


def test_no_candidate_reads_as_an_admission(report):
    """THE security clause. A native gate that refuses what the reference admits
    is an inconvenience; one that ADMITS what the reference refuses is the defect
    class the whole admission-gate arc exists to prevent. The crate closes it
    structurally - there is no `Admitted` arm and `to_json` reports
    `"admitted": false` on every arm - and this holds that over the batch a real
    embedder screens, on BOTH questions the crate now answers: standalone, and
    against the held manifest (issue #346's `admit_into`). The manifest arm is
    held to the same clause because it is the newer surface: it must read as a
    refusal or a no-objection, never as an admission into the running
    composition."""
    offenders = [
        (c["name"], c["wire"]) for c in report["candidates"]
        if '"admitted":false' not in c["wire"]
        or c["verdict"] not in ("refused", "no_objection", "outside_frontier")
    ]
    for candidate in report["candidates"]:
        arm = candidate["into"]
        if arm is None:
            continue
        if '"admitted":false' not in arm["wire"] or arm["verdict"] not in (
                "refused", "no_objection", "outside_frontier"):
            offenders.append((f"{candidate['name']} (into {candidate['manifest']})",
                              arm["wire"]))
    assert not offenders, (
        "the rust harness produced something a host could read as an "
        "admission:\n  " + "\n  ".join(f"{n}: {w}" for n, w in offenders))


# ------------------------------------------------------ the sound direction


def test_every_rust_refusal_is_a_real_py_refusal_with_the_same_code(report):
    """The direction a host acts on: where the rust gate REFUSES, the py
    admission gate must refuse the identical bytes, with the same guarantee tag
    AND the same why-trace, verbatim.

    A rust refusal of a program py ADMITS is a false alarm an agent throws a good
    candidate away over. A rust refusal carrying a DIFFERENT tag points the
    agent's repair at the wrong obligation. And a refusal whose message is not
    the reference's is not the reference diagnostic the crate claims to deliver
    - the whole reason a local refusal is worth having is that it is the one the
    reference would have given.
    """
    false_alarms = []
    tag_drift = []
    message_drift = []
    for candidate in report["candidates"]:
        if candidate["verdict"] != "refused":
            continue
        tag, message = _py_reference(candidate["source"])
        if tag == "":
            false_alarms.append((candidate["name"], candidate["code"],
                                 candidate["message"]))
            continue
        if tag != candidate["code"]:
            tag_drift.append((candidate["name"], candidate["code"], tag))
        if message != candidate["message"]:
            message_drift.append((candidate["name"], candidate["message"],
                                  message))

    assert not false_alarms, (
        "the rust gate REFUSED candidates the py admission gate ADMITS:\n  "
        + "\n  ".join(f"{n}: {c} ({m!r})" for n, c, m in false_alarms))
    assert not tag_drift, (
        "the rust gate refused with a different guarantee tag than py:\n  "
        + "\n  ".join(f"{n}: rust {r!r} != py {p!r}" for n, r, p in tag_drift))
    assert not message_drift, (
        "the rust gate's why-trace is not the reference's, verbatim:\n  "
        + "\n  ".join(f"{n}:\n    rust {r!r}\n    py   {p!r}"
                      for n, r, p in message_drift))


def test_every_rust_manifest_refusal_is_a_real_py_manifest_refusal(report):
    """The sound direction ON THE MANIFEST ARM, and the negative exit test of
    issue #346: a candidate the py gate refuses for a G2/G3 reason - a collision
    with the RUNNING composition - must be refused by the rust gate with the same
    code AND the same why-trace, byte for byte.

    Held against `revl.gate.admit_into` rather than the standalone gate, because
    the two ask different questions: the same bytes can be admitted standalone
    and refused against a live composition. A rust manifest refusal of a
    candidate py admits INTO the composition would be a false alarm the embedder
    acts on; a refusal under a different tag points the repair at the wrong
    obligation; and a message that is not the reference's is not the local
    diagnostic a rust embedder links the crate for.
    """
    import inprocess_gate_harness as py_harness  # noqa: PLC0415

    base = py_harness.base_manifest()
    false_alarms = []
    tag_drift = []
    message_drift = []
    arms = [(c["name"], c["into"]) for c in report["candidates"]
            if c["into"] is not None]
    assert [n for n, a in arms if a["verdict"] == "refused"], (
        "the manifest arm refused nothing, so the sound direction is not "
        "exercised at all and a stub that ignored its manifest argument would "
        "pass by refusing nothing: " + repr([n for n, _ in arms]))
    for candidate in report["candidates"]:
        arm = candidate["into"]
        if arm is None or arm["verdict"] != "refused":
            continue
        tag, message = _py_manifest_reference(candidate["source"], base)
        if tag == "":
            false_alarms.append((candidate["name"], arm["code"], arm["message"]))
            continue
        if tag != arm["code"]:
            tag_drift.append((candidate["name"], arm["code"], tag))
        if message != arm["message"]:
            message_drift.append((candidate["name"], arm["message"], message))

    assert not false_alarms, (
        "the rust gate REFUSED into the running manifest candidates the py "
        "manifest gate ADMITS:\n  "
        + "\n  ".join(f"{n}: {c} ({m!r})" for n, c, m in false_alarms))
    assert not tag_drift, (
        "the rust gate refused against the running manifest with a different "
        "guarantee tag than py:\n  "
        + "\n  ".join(f"{n}: rust {r!r} != py {p!r}" for n, r, p in tag_drift))
    assert not message_drift, (
        "the rust gate's manifest why-trace is not the reference's, verbatim:\n  "
        + "\n  ".join(f"{n}:\n    rust {r!r}\n    py   {p!r}"
                      for n, r, p in message_drift))


def test_the_batch_exercises_both_directions(report):
    """A batch that only ever refuses (or never does) would pass the checks
    above vacuously."""
    kinds = {c["verdict"] for c in report["candidates"]}
    assert "refused" in kinds, "no candidate was refused"
    assert "no_objection" in kinds, "no candidate got a no-objection"
    assert "outside_frontier" in kinds, (
        "no candidate exercised the fail-closed frontier path")


# ------------------------------- the tolerated direction, measured not assumed


def test_the_measured_layer_gap_is_real_and_never_reads_as_an_admission(report):
    """The gap that is the whole reason `NoObjection` is not `Admitted`.

    At least one candidate in the batch gets a rust no-objection while the py
    admission gate REFUSES the identical bytes (the hole draft: py refuses `T3`,
    the rust gate has no hole check; and the type-layer probe). This test fails
    if that stops being true - either because the self-host gained the layer
    (good news, but the crate docs, `Verdict`'s arms and this batch's notes then
    all need updating) or because the batch stopped probing it.
    """
    gap = [
        (c["name"], _py_verdict(c["source"]))
        for c in report["candidates"] if c["verdict"] == "no_objection"
    ]
    refused_by_py = [(name, code) for name, (admitted, code) in gap
                     if not admitted]
    assert refused_by_py, (
        "no candidate exercised the measured layer gap (a rust no-objection the "
        "py admission gate refuses). If the self-host gained the type/hole "
        "layer, update the crate docs, bench/inprocess_gate_rust and "
        "bench/results/inprocess-gate-rust.md - do NOT weaken this test.")
    # and the load-bearing half: none of it read as an admission.
    for candidate in report["candidates"]:
        if candidate["verdict"] == "no_objection":
            assert candidate["code"] is None
            assert '"admitted":false' in candidate["wire"]


def test_the_hole_draft_is_the_named_gap_not_a_silent_one(report):
    """The py harness's A1 probe, translated honestly. py REFUSES the hole draft
    with `T3` (an open typed hole may never run). The rust gate does not have
    that check - so what matters is that the harness NAMES this as a gap rather
    than letting a host read the non-refusal as an admission."""
    hole = next((c for c in report["candidates"] if c["name"] == "hole_draft"),
                None)
    assert hole is not None, "the batch must carry the py harness's hole draft"

    admitted, code = _py_verdict(hole["source"])
    assert admitted is False and code == "T3", (
        "the py admission gate must still refuse the hole draft with T3; if it "
        "stopped, the A1 property the py harness proves has regressed")
    assert hole["verdict"] != "refused" or hole["code"] == "T3", (
        "if the rust gate started refusing the hole draft it must do so with "
        "the reference's own code")
    assert '"admitted":false' in hole["wire"], (
        "whatever the rust gate says about a draft with an open hole, it may "
        "never read as an admission")


def test_the_manifest_gap_is_priced_not_hidden(report):
    """Which half of the manifest gap CLOSED (issue #346), and which is still
    open. Both are held to the py gate's own answers here; neither may be
    overstated.

    `crates/revl-gate` now carries a real `admit_into(source, manifest)`: it
    folds the G2/G3 legs over the UNION of the running composition's rows and
    the candidate, so the realistic agent shape - admit a candidate AGAINST the
    running composition - is askable on rust. What that buys, and what it does
    not:

    * **CLOSED - the ambient half.** `ambient_provision_conflict` collides with
      a key the RUNNING composition already provides. py's
      `revl.gate.admit_into` refuses it `G2`; the rust gate refuses it `G2` with
      the IDENTICAL why-trace; and py's STANDALONE `admit` ADMITS the same bytes.
      That last clause is what makes the agreement mean something: a rust gate
      that ignored its manifest argument would agree with the standalone gate,
      not with `admit_into`.
    * **CLOSED - requirement resolution.** The running composition's service
      block now carries each service's OPERATIONS, so the fold resolves
      `requires store: Store` against the running declaration:
      `calls_missing_method` calls an operation the running `Store` does not
      declare and is REFUSED `A6` with the reference's own sentence, while its
      twin `cache_layer` - the same shape calling an operation `Store` does
      declare - is not. Telling those two apart is the resolution; a gate
      reading only the wire's service NAMES answers the same thing about both.
    * **CLOSED - issuing the admission** (docs/design/457 T6). py's
      `admit_into` ADMITS `cache_layer`, and the rust gate now issues an
      admission for the identical bytes against the identical manifest:
      `revl_gate::issue_admission_into` returns `Admitted` and writes
      `"admitted": true` on a wire byte-identical to the py tier's. The
      `Verdict` surface is unchanged and still admission-free - the admission
      is a SECOND question, asked through a separate type and a separate entry
      point - so this test holds both: the verdict arm still says
      `"admitted": false`, and the admission arm says true.

    No half may be made to lie, and the last one was an open gap until T6
    landed. This test was TIGHTENED rather than replaced when it closed: the
    clause that used to read "rust cannot issue this" now reads "rust issues
    exactly this", so the gate that measured the distance is the gate that
    pins its absence. Do not relax it into a tautology, and do not delete it -
    a deleted test is not a closed gap.
    """
    import inprocess_gate_harness as py_harness  # noqa: PLC0415
    from revl.manifest import manifest_wire  # noqa: PLC0415

    base = py_harness.base_manifest()
    assert report["held_manifest"] == manifest_wire(base), (
        "the rust harness admits into a manifest the py harness does not hold:"
        f"\nrust: {report['held_manifest']!r}\npy:   {manifest_wire(base)!r}")

    into_arms = {c["name"]: c for c in report["candidates"]
                 if c["into"] is not None}
    assert set(into_arms) == {"cache_layer", "calls_missing_method",
                              "ambient_provision_conflict"}, (
        "the manifest arm's batch changed - one candidate must close the ambient "
        "half, one must close the requires-resolution half, and one must price "
        f"the open admission half: {sorted(into_arms)}")

    # ---------------------------------------------------------- the closed half
    ambient = into_arms["ambient_provision_conflict"]
    standalone_admitted, _ = _py_verdict(ambient["source"])
    assert standalone_admitted is True, (
        "py must ADMIT this candidate standalone - the contrast with the "
        "manifest refusal is the whole evidence that the manifest is read")
    assert ambient["verdict"] == "no_objection", (
        "the ambient-collision candidate must not be refused standalone "
        f"(got {ambient['verdict']})")

    py_tag, py_message = _py_manifest_reference(ambient["source"], base)
    assert py_tag == "G2", (
        f"the py manifest gate must refuse this with G2, not {py_tag!r} - the "
        "G2 leg is the half issue #346 closes")
    public = py_gate.admit_into(ambient["source"], base)
    assert public.admitted is False, (
        "the public `revl.gate.admit_into` verb must refuse it too - comparing "
        "the rust crate against a re-derivation of the call is not the same "
        "claim as comparing it against the call")
    assert ambient["into"]["verdict"] == "refused", (
        "the rust manifest arm must REFUSE the ambient collision, not decline "
        f"to object (got {ambient['into']['verdict']})")
    assert ambient["into"]["code"] == py_tag, (
        f"tag drift on the manifest arm: rust {ambient['into']['code']!r} != py "
        f"{py_tag!r}")
    assert ambient["into"]["message"] == py_message, (
        "the rust manifest why-trace is not the reference's, verbatim:\n"
        f"  rust: {ambient['into']['message']!r}\n  py:   {py_message!r}")

    # ------------------------------------------------- the half that has closed
    # The STANDALONE question about `cache_layer` is now answered, and answered
    # in the reference's own words: `selfhost/lower.rvl` resolves a component's
    # `requires`/`provides` annotations against its service table
    # (docs/design/457 §2.4), so a candidate naming a service its own text does
    # not declare is REFUSED rather than waved through. This used to be a
    # conditional clause - "if it became a refusal, it must carry the reference's
    # tag" - and is now a positive claim, which is the tightening the issue asks
    # for.
    cache_layer = into_arms["cache_layer"]
    standalone_admitted, _ = _py_verdict(cache_layer["source"])
    assert standalone_admitted is False, (
        "py must refuse this candidate standalone - if that changed, the "
        "contrast this half draws no longer exists")
    ref_tag, ref_message = _py_reference(cache_layer["source"])
    assert cache_layer["verdict"] == "refused", (
        "the rust gate must refuse this candidate standalone: it requires a "
        "service its own text never declares, which is the reference's own "
        f"first refusal (got {cache_layer['verdict']})")
    assert cache_layer["code"] == ref_tag, (
        f"tag drift: rust {cache_layer['code']!r} != py {ref_tag!r}")
    assert cache_layer["message"] == ref_message, (
        "the rust standalone why-trace is not the reference's, verbatim:\n"
        f"  rust: {cache_layer['message']!r}\n  py:   {ref_message!r}")

    # ------------------------------ the OTHER half that has closed: resolution
    # The running service block now carries each service's operations, so the
    # fold resolves `requires store: Store` against the running DECLARATION and
    # not only against its name. `calls_missing_method` is the evidence: the same
    # shape as `cache_layer`, calling an operation the running `Store` does not
    # declare, refused `A6` in the reference's own words. A gate reading only the
    # wire's service names could not tell the two candidates apart.
    missing = into_arms["calls_missing_method"]
    miss_tag, miss_message = _py_manifest_reference(missing["source"], base)
    assert miss_tag == "A6", (
        f"the py manifest gate must refuse this A6, not {miss_tag!r} - it is the "
        "resolution leg this half closes")
    assert py_gate.admit_into(missing["source"], base).admitted is False, (
        "the public `revl.gate.admit_into` verb must refuse it too")
    assert missing["into"]["verdict"] == "refused", (
        "the rust manifest arm must REFUSE a call to an operation the running "
        f"service does not declare (got {missing['into']['verdict']})")
    assert missing["into"]["code"] == miss_tag, (
        f"tag drift: rust {missing['into']['code']!r} != py {miss_tag!r}")
    assert missing["into"]["message"] == miss_message, (
        "the rust resolution why-trace is not the reference's, verbatim:\n"
        f"  rust: {missing['into']['message']!r}\n  py:   {miss_message!r}")
    assert '"admitted":false' in missing["into"]["wire"]

    # ------------------------------------------ the half that has NOW closed
    # The last step: py's `admit_into` ISSUES an admission for `cache_layer`,
    # and so does rust. docs/design/457 section 6 clause 2, held on the bytes.
    public = py_gate.admit_into(cache_layer["source"], base)
    assert public.admitted is True, (
        "the py gate must ADMIT this candidate INTO the running composition by "
        "resolving its `requires` against the live provider; the rust gate is "
        "held against that answer below")
    assert cache_layer["into"]["verdict"] == "no_objection", (
        "the VERDICT surface still issues no admission, so the only honest "
        "answer on its manifest arm is a no-objection - if this became a "
        "refusal, that is a false alarm and a defect "
        f"({cache_layer['into']['verdict']})")
    # ... and the contrast with `calls_missing_method` above is what makes the
    # no-objection a RESOLVED one rather than an unasked question: the same
    # shape, the same manifest, two different verdicts.
    assert cache_layer["into"]["code"] is None, (
        "a no-objection must carry no code")
    assert '"admitted":false' in cache_layer["into"]["wire"], (
        "the verdict surface may never read as an admission")
    assert '"admitted":false' in cache_layer["wire"]

    # The ADMISSION surface, which is where the yes lives. A separate type and
    # a separate entry point (`issue_admission_into`), so switching a consumer
    # from `admit_into` to it is an opt-in.
    admission = cache_layer["admission_into"]
    assert admission is not None, (
        "the rust harness must ask the ADMISSION question about every "
        "manifest-arm candidate, not only the refusal one")
    assert admission["arm"] == "admitted" and admission["admitted"] is True, (
        "the rust gate must ISSUE an admission for `cache_layer` against the "
        "held manifest - this is docs/design/457 section 6 clause 2, and it is "
        f"the half that was open until T6: {admission}")
    assert admission["wire"] == public.to_json(), (
        "the two tiers' admission wires must be byte-identical, so a seam "
        "comparing them compares equal bytes rather than two spellings of the "
        f"same yes:\n  rust: {admission['wire']!r}\n  py:   {public.to_json()!r}")
    assert '"admitted":true' in admission["wire"]
    assert admission["basis"] is not None and "bodies=1" in admission["basis"], (
        "an issued admission carries a certificate naming what it accounted "
        f"for, and the provide-method body is the new half: {admission['basis']}")

    # The two candidates the same manifest arm REFUSES are withheld here, which
    # is what stops the admission arm from being a rubber stamp: it is gated on
    # the refusal surface first.
    for name in ("calls_missing_method", "ambient_provision_conflict"):
        withheld = into_arms[name]["admission_into"]
        assert withheld["arm"] == "refused" and withheld["admitted"] is False, (
            f"{name} is refused into the manifest, so no admission may be "
            f"issued for it: {withheld}")


def test_every_rust_admission_is_a_py_admission(report):
    """The release-blocking direction of the admission arm, stated positively
    and over the WHOLE batch rather than the one candidate the exit test names.

    For every candidate the rust gate ISSUES an admission for - standalone
    (`issue_admission`) or into the held manifest (`issue_admission_into`) -
    `revl.gate` must admit the identical bytes against the identical question.
    Zero tolerance: one entry here is a host reading a rust green and running
    code the reference refuses, which is the defect class the whole
    admission-gate arc exists to prevent.

    The converse is NOT asserted and must not be: py admits plenty that rust
    withholds (`standalone_twin` among them - its inlined `Store` declares an
    `emission`, which is outside the admission surface). Under-admitting is the
    direction this gate is allowed to err in.
    """
    import inprocess_gate_harness as py_harness  # noqa: PLC0415

    base = py_harness.base_manifest()
    issued = []
    for candidate in report["candidates"]:
        if candidate["admission"]["admitted"]:
            issued.append((candidate["name"], "standalone"))
            assert py_gate.admit(candidate["source"]).admitted is True, (
                f"{candidate['name']}: the rust gate ISSUED a standalone "
                "admission for bytes the py gate does not admit - a false "
                f"admission: {candidate['admission']['basis']}")
        into = candidate["admission_into"]
        if into is not None and into["admitted"]:
            issued.append((candidate["name"], "into"))
            assert py_gate.admit_into(candidate["source"], base).admitted is True, (
                f"{candidate['name']}: the rust gate ISSUED an admission INTO "
                "the running composition for bytes the py gate does not admit "
                f"- a false admission: {into['basis']}")
    assert issued, (
        "no candidate in the batch drew an issued admission, so this guard "
        "measured nothing - the admission arm must stay reachable from the "
        "harness or it stops being able to fire")


# ------------------------------------------------- the two harnesses' bytes


def test_the_shared_candidates_are_the_py_harness_bytes(report):
    """The two harnesses screen the SAME programs, checked rather than assumed.
    Every candidate the rust harness marks `shared_with_py` must be byte-identical
    to the py harness's own constant, so editing one side without the other is a
    red here instead of a silent divergence."""
    import inprocess_gate_harness as py_harness  # noqa: PLC0415

    expected = {
        "standalone_twin": py_harness._STANDALONE_TWIN,
        "cache_layer": py_harness.al.CANDIDATE,
        "calls_missing_method": py_harness._CALLS_MISSING_METHOD,
        "incomplete_provide": py_harness._INCOMPLETE_PROVIDE,
        "syntax_error": py_harness._SYNTAX_ERROR,
        "hole_draft": py_harness._HOLE_DRAFT,
    }
    shared = {c["name"]: c["source"] for c in report["candidates"]
              if c["shared_with_py"]}
    assert set(shared) == set(expected), (
        f"the shared set drifted: rust marks {sorted(shared)}, the py harness "
        f"supplies {sorted(expected)}")
    for name, source in expected.items():
        assert shared[name] == source, (
            f"{name}: the rust harness screens different bytes than the py "
            f"harness.\nrust: {shared[name]!r}\npy:   {source!r}")


def test_the_gate_surfaces_are_kept_in_lockstep(report):
    """`gate_version().api` is the same semver on both tiers (the generator
    refuses to run otherwise), and the rust `frontier` id says which gate it is -
    the value an embedder must compare before trusting two gates' agreement."""
    version = report["gate_version"]
    assert version["api"] == py_gate.gate_version()["api"]
    assert version["frontier"].startswith("selfhost-admit:"), version["frontier"]
    assert py_gate.gate_version()["frontier"].startswith("reference-full:")
    assert "NOT the reference type layer" in version["layer"]


# ---------------------------------------------- statelessness + fail closed


def test_verdicts_are_order_independent(report):
    """A2's rust twin: screening the batch in a fixed order and in a shuffled
    order in the SAME process yields identical per-candidate verdicts - the
    property that proves the gate is stateless. Any drift would expose a
    per-process cache making screen N depend on screen N-1."""
    assert report["order_drift"] == [], (
        f"rust batch verdicts depend on ordering (statefulness bug): "
        f"{report['order_drift']}")


def test_the_fail_closed_paths_hold_from_the_consumer_side(report):
    """An oversized source is DECLINED rather than risked (the emitted front end
    is deeply recursive; a stack exhaustion aborts and cannot be turned back
    into a refusal), and `compile_to` refuses on both tiers (Stage 4)."""
    closed = report["fail_closed"]
    assert closed["oversized_declined"] is True
    assert closed["oversized_code"] == "FRONTIER"
    assert closed["compile_to_refuses_both_tiers"] is True
    assert report["max_source_bytes"] > 0


# ------------------------------------------------------------------- cost


def test_cost_is_a_well_shaped_distribution_under_a_generous_ceiling(report):
    """The harness reports a DISTRIBUTION per size cell, not one reasserted
    headline, and the representative screen stays under a generous
    gross-regression ceiling (no tight wall-clock assert)."""
    cells = report["cost"]["cells"]
    assert {c["label"] for c in cells} == {"small", "medium", "large"}
    for cell in cells:
        stats = cell["stats"]
        assert stats["n"] == ITERS
        assert stats["min"] <= stats["median"] <= stats["p90"] <= stats["p99"]
        assert stats["median"] > 0.0

    rep = report["cost"]["representative"]
    assert rep["median"] < CEILING_MS, (
        f"representative in-process screen median {rep['median']:.4f} ms "
        f"exceeded the {CEILING_MS} ms regression ceiling - investigate "
        f"bench/inprocess_gate_rust (expected tens of microseconds; this "
        f"ceiling only catches a catastrophic, order-of-magnitude regression)")


def test_cost_scales_with_candidate_size(report):
    """A5's rust twin: the cost is a function of candidate size, so a large
    candidate must cost strictly more than a small one. This is what stops the
    number collapsing into a single universal constant nobody can check."""
    by_label = {c["label"]: c["stats"]["median"] for c in report["cost"]["cells"]}
    assert by_label["large"] > by_label["small"], (
        f"large-candidate median {by_label['large']:.4f} ms should exceed "
        f"small-candidate median {by_label['small']:.4f} ms - the screen scales "
        f"with candidate source size")


def test_the_shape_probe_attributes_the_cost_to_tokens_not_bytes(report):
    """The cost finding, attributed instead of guessed at.

    Three candidates of roughly equal BYTE length and very different token and
    declaration counts. The comment-padded one (many bytes, few tokens) is
    dramatically cheaper than the other two, and the statement-heavy one (many
    tokens, ONE declaration) costs the same order as the declaration-heavy one.
    So the screen's cost tracks TOKENS - it lives in the emitted lexer/parser,
    not in the composition gate walking declarations, and not in raw source
    bytes. That is what makes the size curve a front-end problem
    (items 391/336), and it is the thing to re-measure after any change there.
    """
    shapes = {s["label"]: s for s in report["cost"]["shapes"]}
    assert set(shapes) == {"declaration-heavy", "statement-heavy",
                           "comment-padded"}

    sizes = [s["bytes"] for s in shapes.values()]
    assert max(sizes) - min(sizes) < 0.1 * min(sizes), (
        f"the shape probe only says something if the three sources are the same "
        f"size: {sizes}")

    comment = shapes["comment-padded"]["stats"]["median"]
    declaration = shapes["declaration-heavy"]["stats"]["median"]
    statement = shapes["statement-heavy"]["stats"]["median"]
    assert comment < declaration and comment < statement, (
        f"the comment-padded shape should be the cheapest of the three at equal "
        f"byte length (comment {comment:.3f} ms, declaration {declaration:.3f} "
        f"ms, statement {statement:.3f} ms) - if that stopped holding, the cost "
        f"attribution in bench/results/inprocess-gate-rust.md is stale")
