"""The `revl-gate` crate's differential corpus gate (roadmap item 332, Stage 3).

The roadmap's own exit test for this stage, admit half: *a standalone rust
binary depending only on the crate returns the same verdict as `revl compile`
across the corpus*. That is what this file drives.

Shape
-----
1. Assemble a STANDALONE consumer crate in a temp dir whose only dependency is
   `crates/revl-gate` (a path dep — no other crate, no revl source, no PYTHONPATH
   reaching the repo). `cargo build` it, run it, and feed it the corpus on stdin.
2. Compare its verdicts against the REFERENCE compiler's, program by program,
   over the same `ACCEPTED_PROGRAMS` / `REJECTED_PROGRAMS` the self-host lowering
   oracle uses — imported, not copied, so "same corpus" stays literal.
3. Assert the two directions ASYMMETRICALLY, because they are not symmetric:
   * the crate issuing an ADMISSION for what the reference refuses is the
     release blocker, the defect class the whole admission-gate arc exists to
     prevent. The crate closes it structurally — it has no `Admitted` arm at all
     and `to_json` reports `"admitted": false` on every arm, because the
     self-host gate decides the composition/guarantee layer and NOT the
     reference type layer — and this file holds that structurally-closed
     property over the whole corpus;
   * every REFUSAL the crate does issue must be a real reference refusal with
     the same code and the same message, verbatim. That is the sound direction
     and the one a consumer acts on;
   * the crate declining to decide (a `FRONTIER` verdict) on the covered corpus
     is a real finding and fails, in its own vocabulary.
4. Prove the FAIL-CLOSED path from the consumer side: a construct in the crate's
   generated frontier table, and an oversized source, both come back
   `{"verdict": "outside_frontier", "admitted": false, "code": "FRONTIER"}`.
5. Pin the MEASURED type-layer gap: programs the reference refuses that the
   self-host gate raises no objection to. That measurement is why the crate has
   no `Admitted` arm, so it is held here as evidence rather than left implicit.

Toolchain honesty
-----------------
This needs cargo AND a resolvable cordis-rs (the emitted self-host module speaks
`cordis::Value`). Where either is absent the whole module SKIPS WITH THE REASON
the driver reports — the `tools/bench_selfhost_rust.py` / `tests/test_run_rust.py`
discipline. A skipped tier is never green, and a green here always means a real
crate was built and really ran. The Python-free half of the gate (regenerate and
byte-compare) lives in `tests/test_gate_crate_drift.py` and needs no toolchain.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "crates" / "revl-gate"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

# The corpus and the reference classifier, IMPORTED from the self-host lowering
# oracle so the crate is measured against the same programs and the same
# guarantee vocabulary the oracle uses. A copy here would be free to drift.
import test_selfhost_lower as oracle  # noqa: E402

_RUST_REASON = rust_runtime_reason()
pytestmark = pytest.mark.skipif(
    _RUST_REASON is not None,
    reason=f"needs a resolvable cordis-rs toolchain to build the gate crate: "
           f"{_RUST_REASON}")


# ------------------------------------------------------------ the consumer
#
# Depends on `revl-gate` and NOTHING else: no serde, no revl, no Python. The
# corpus arrives NUL-separated on stdin (a revl source can never contain a NUL)
# and one JSON verdict per program goes out on stdout, in order.

CONSUMER_MAIN = r'''use std::io::Read;

fn main() {
    let mut argv = std::env::args().skip(1);
    if argv.next().as_deref() == Some("--version") {
        let v = revl_gate::gate_version();
        println!(
            "{{\"api\":\"{}\",\"language\":\"{}\",\"frontier\":\"{}\",\"layer\":\"{}\"}}",
            v.api, v.language, v.frontier, v.layer
        );
        return;
    }
    let mut blob = String::new();
    std::io::stdin().read_to_string(&mut blob).expect("read stdin");
    for source in blob.split('\0') {
        println!("{}", revl_gate::admit(source).to_json());
    }
}
'''


def _consumer_cargo_toml(crate_path: Path) -> str:
    return (
        "[package]\n"
        'name = "revl_gate_consumer"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        "\n"
        # An explicit empty workspace table so a stray Cargo.toml above the temp
        # dir cannot adopt this crate and change what gets built.
        "[workspace]\n"
        "\n"
        "[dependencies]\n"
        f'revl-gate = {{ path = "{crate_path.as_posix()}" }}\n'
    )


# --------------------------------------------------------------- cargo policy
#
# Offline first; a networked resolve only when the offline attempt failed to
# RESOLVE a crate, never to launder a build failure into a retry. Same policy as
# backends/rust/test_emit_rust.py and tools/bench_selfhost_rust.py.

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


def _cargo(subcommand: str, cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    # No PYTHONPATH, no VIRTUAL_ENV: the crate must build with nothing from this
    # repo's Python on the path. That is the "no Python installed" claim, held as
    # tightly as a test on a machine that does have Python can hold it.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=cwd, text=True,
        capture_output=True, timeout=1800, env=env, check=False)
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    if not _crates_io_reachable():
        return offline
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=cwd, text=True, capture_output=True,
        timeout=1800, env=env, check=False)


@pytest.fixture(scope="module")
def consumer(tmp_path_factory) -> Path:
    """A built standalone binary whose only dependency is `crates/revl-gate`."""
    work = tmp_path_factory.mktemp("revl_gate_consumer")
    (work / "src").mkdir()
    (work / "src" / "main.rs").write_text(CONSUMER_MAIN, encoding="utf-8")
    (work / "Cargo.toml").write_text(_consumer_cargo_toml(CRATE), encoding="utf-8")
    built = _cargo("build", work)
    assert built.returncode == 0, (
        "the standalone consumer crate failed to build against crates/revl-gate:\n"
        + (built.stderr or built.stdout or "")[-4000:])
    binary = work / "target" / "debug" / "revl_gate_consumer"
    assert binary.exists(), f"consumer binary not found at {binary}"
    return binary


def _crate_verdicts(binary: Path, sources: list[str]) -> list[dict]:
    """Run every source through the crate in ONE process and return the parsed
    verdicts, in order."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    run = subprocess.run([str(binary)], input="\0".join(sources), text=True,
                         capture_output=True, timeout=900, env=env, check=False)
    assert run.returncode == 0, (
        "the consumer binary exited nonzero:\n"
        + (run.stderr or run.stdout or "")[-4000:])
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert len(lines) == len(sources), (
        f"expected {len(sources)} verdicts, got {len(lines)}")
    return [json.loads(line) for line in lines]


def _reference(source: str) -> tuple[str, str]:
    """(tag, message) — ("", "") when the reference admits. The reference's own
    guarantee vocabulary, via the oracle's classifier."""
    try:
        compile_source(source, "gate-crate.rvl")
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


CORPUS: list[tuple[str, str]] = (
    [(f"accepted: {name}", src) for name, src in oracle.ACCEPTED_PROGRAMS]
    + [(f"rejected: {name}", src) for name, src, _ in oracle.REJECTED_PROGRAMS]
)


@pytest.fixture(scope="module")
def agreement(consumer) -> list[tuple[str, str, tuple[str, str], dict]]:
    """(name, source, reference verdict, crate verdict) for the whole corpus,
    computed once — the crate binary is invoked a single time."""
    verdicts = _crate_verdicts(consumer, [src for _, src in CORPUS])
    return [(name, src, _reference(src), verdict)
            for (name, src), verdict in zip(CORPUS, verdicts)]


# --------------------------------------------- the release-blocking direction


def test_the_crate_issues_no_admission_for_anything_in_the_corpus(agreement):
    """THE security clause.

    A native gate that refuses a program the reference admits is an
    inconvenience. A native gate that ADMITS a program the reference refuses is
    the defect class this arc exists to prevent — so the crate ships no
    admission at all, and this holds that over every corpus program: the wire
    `admitted` flag is false everywhere, and the only arms are `refused`,
    `no_objection` and `outside_frontier`.
    """
    offenders = [(name, verdict) for name, _src, _ref, verdict in agreement
                 if verdict["admitted"] is not False
                 or verdict["verdict"] not in
                 ("refused", "no_objection", "outside_frontier")]
    assert not offenders, (
        "the crate produced something a consumer could read as an admission:\n  "
        + "\n  ".join(f"{name}: {verdict}" for name, verdict in offenders))


def test_every_refusal_the_crate_issues_is_a_real_reference_refusal(agreement):
    """The sound direction, held on its own over the whole corpus at once: the
    crate must never REFUSE a program the reference admits either — that would
    be a false alarm a consumer acts on, and on the covered corpus there is no
    excuse for one."""
    false_alarms = [
        (name, verdict["code"], verdict["message"])
        for name, _src, (ref_tag, _ref_msg), verdict in agreement
        if verdict["verdict"] == "refused" and ref_tag == ""
    ]
    assert not false_alarms, (
        "the crate REFUSED programs the reference ADMITS, on the covered "
        "corpus:\n  "
        + "\n  ".join(f"{name}: {code} ({msg!r})"
                      for name, code, msg in false_alarms))


def test_the_wire_shape_carries_a_code_on_every_non_refusing_arm(agreement):
    """The wire shape fails closed: a refusal and a frontier gap both carry a
    code, a no-objection carries none, and `admitted` is false throughout — so a
    consumer branching on the boolean alone reads this gate as "never admits"
    rather than walking into a gap."""
    for name, _src, _ref, verdict in agreement:
        assert verdict["admitted"] is False, name
        if verdict["verdict"] == "no_objection":
            assert verdict["code"] is None, name
        else:
            assert verdict["code"], f"{name}: a non-no-objection must carry a code"


# ------------------------------------------------------ full corpus agreement


@pytest.mark.parametrize("index", range(len(CORPUS)),
                         ids=[name for name, _ in CORPUS])
def test_crate_and_reference_agree_on_the_covered_corpus(agreement, index):
    """Verdict shape agreement, per program: where the reference refuses, the
    crate refuses with the same code AND the same message, verbatim; where the
    reference admits, the crate raises no objection.

    The corpus is the COVERED corpus, so a `FRONTIER` verdict here is a real
    finding (either the frontier guard got more conservative or the self-host
    lost a surface), reported in its own vocabulary rather than as a plain
    disagreement.
    """
    name, _src, (ref_tag, ref_msg), verdict = agreement[index]
    if verdict["verdict"] == "outside_frontier":
        pytest.fail(
            f"{name}: the crate declined to decide a program on the COVERED "
            f"corpus — {verdict['message']}")
    if ref_tag == "":
        assert verdict["verdict"] == "no_objection", (
            f"{name}: the reference admits, the crate refused "
            f"{verdict['code']} ({verdict['message']!r})")
        return
    assert verdict["verdict"] == "refused", (
        f"{name}: the reference refuses {ref_tag}, the crate said "
        f"{verdict['verdict']}")
    assert verdict["code"] == ref_tag, (
        f"{name}: code — crate {verdict['code']!r} != reference {ref_tag!r} "
        f"({verdict['message']!r})")
    assert verdict["message"] == ref_msg, (
        f"{name}: message — crate {verdict['message']!r} != reference {ref_msg!r}")


# ------------------------------------------- the measured type-layer gap

# Programs the REFERENCE refuses and the self-host gate raises no objection to.
# This is not a wish list: it is the measurement that decided the crate's
# surface. `selfhost/lower.rvl`'s `admit_src` decides the composition/guarantee
# layer (G1..G4, A1, PRELUDE, BAD) and runs no type layer at all, so a
# non-refusal from it can never mean "the reference would admit this" — which is
# why `Verdict` has no `Admitted` arm and `to_json` reports `admitted: false`
# everywhere. Kept as a live probe so the day the self-host grows the type layer,
# this test says so and the crate docs can be updated with it.
TYPE_LAYER_GAP = [
    ("return type mismatch", 'fn f() -> Int { return "s" }'),
    ("undeclared name in a body", "fn f() -> Int { return undefined_name }"),
    ("return arrow with no type", "fn f() -> { }"),
    ("declared return, non-returning body", "fn f() -> Int { }"),
    ("unknown service in provides", "component C provides s: S { }"),
]


@pytest.mark.parametrize("name,source", TYPE_LAYER_GAP,
                         ids=[n for n, _ in TYPE_LAYER_GAP])
def test_the_type_layer_gap_never_reads_as_an_admission(consumer, name, source):
    """The gap is real; what must never be real is a consumer reading past it."""
    assert _reference(source)[0] != "", (
        f"probe bug: the reference ADMITS {name}; this list is for programs it "
        f"refuses")
    verdict = _crate_verdicts(consumer, [source])[0]
    assert verdict["admitted"] is False
    assert verdict["verdict"] in ("no_objection", "outside_frontier"), (
        f"{name}: unexpected arm {verdict['verdict']}")
    if verdict["verdict"] == "no_objection":
        assert verdict["code"] is None


# --------------------------------------------------------------- fail closed


FRONTIER_PROBES = [
    # `.is_digit()` / `.str()` are reference stdlib builtins the self-host
    # lowering does not treat as builtins, so they are in the crate's GENERATED
    # frontier table. The crate must decline to decide, not guess.
    ("excluded builtin is_digit",
     "fn f(s: Str) -> Bool { return s.charAt(0).is_digit() }"),
    ("excluded builtin str",
     "fn f(x: Int) -> Str { return x.str() }"),
]


@pytest.mark.parametrize("name,source", FRONTIER_PROBES,
                         ids=[n for n, _ in FRONTIER_PROBES])
def test_a_construct_outside_the_frontier_is_declined_not_admitted(
        consumer, name, source):
    """Fail closed at the frontier, proven from the consumer side."""
    verdict = _crate_verdicts(consumer, [source])[0]
    assert verdict["admitted"] is False, f"{name} must never read as admitted"
    assert verdict["verdict"] == "outside_frontier", (
        f"{name}: expected a frontier gap, got {verdict['verdict']} "
        f"({verdict['message']!r})")
    assert verdict["code"] == "FRONTIER"


def test_an_oversized_source_is_declined_not_decided(consumer):
    """A stack exhaustion in the deeply-recursive native front end ABORTS, and
    an abort cannot be turned back into a refusal — so a source above the bound
    is declined before it is ever handed to the gate."""
    meta = json.loads((CRATE / "GENERATED.json").read_text(encoding="utf-8"))
    big = "fn id(x: Int) -> Int { return x } " * 20_000
    assert len(big) > meta["max_source_bytes"]
    verdict = _crate_verdicts(consumer, [big])[0]
    assert verdict["admitted"] is False
    assert verdict["verdict"] == "outside_frontier"
    assert verdict["code"] == "FRONTIER"


def test_ill_formed_sources_are_refused_not_waved_through(consumer):
    """The native front end refuses what it cannot parse; nothing in the shim
    may soften that into a wave-through. Checked against the reference rather
    than against an assumption — the empty program, for instance, is a valid
    empty composition and BOTH gates admit it, which is the agreement this test
    is for."""
    unparseable = ["@@@ not revl @@@", "fn (((", "component {",
                   "service S { fn op("]
    for source, verdict in zip(unparseable, _crate_verdicts(consumer, unparseable)):
        assert _reference(source)[0] != "", f"probe bug: reference admits {source!r}"
        assert verdict["verdict"] == "refused", (
            f"{source!r}: the native front end must refuse what it cannot parse, "
            f"got {verdict['verdict']}")
        assert verdict["code"] == "BAD", source
    # The empty program is a valid EMPTY composition: the reference admits it and
    # the crate has nothing to refuse. Both agree, and neither calls it more than
    # that.
    assert _reference("")[0] == ""
    assert _crate_verdicts(consumer, [""])[0]["verdict"] == "no_objection"


# ------------------------------------------------------------ version surface


def test_the_consumer_reads_the_frontier_the_crate_was_generated_with(consumer):
    """`gate_version().frontier` is what lets an embedder — and item 337's seam
    re-admission — detect that two gates cover different surfaces before
    trusting their agreement. It must be the id the generator stamped."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    run = subprocess.run([str(consumer), "--version"], text=True,
                         capture_output=True, timeout=120, env=env, check=False)
    assert run.returncode == 0, run.stderr
    reported = json.loads(run.stdout.strip())
    meta = json.loads((CRATE / "GENERATED.json").read_text(encoding="utf-8"))
    assert reported["frontier"] == meta["frontier"]
    assert reported["api"] == meta["gate_api_version"]
    assert reported["language"] == meta["language_version"]
    assert reported["layer"] == meta["covered_layer"]
    assert "NOT the reference type layer" in reported["layer"], (
        "the version surface must say out loud which layer this gate does not "
        "decide, or a consumer will read a no-objection as an admission")


def test_the_crate_ships_its_own_cargo_tests(consumer):
    """`cargo test` inside the crate is the no-Python half of the evidence: the
    self-host's own in-file `test` blocks run natively there, alongside the
    shim's fail-closed assertions. Driven here so a red in the crate's suite is
    a red in this repo's suite."""
    tested = _cargo("test", CRATE)
    assert tested.returncode == 0, (
        "cargo test failed inside crates/revl-gate:\n"
        + (tested.stderr or tested.stdout or "")[-4000:])


# ------------------------------------------------- the census's fast engine


def test_the_census_fast_engine_answers_what_the_crate_answers(consumer):
    """`tools/gate_reference_census.py` runs on every PR through the frontend
    job, where there is no cargo. It gets its verdicts from the self-host
    emitted to PYTHON behind a python mirror of the crate's frontier guard —
    cheap, and worth nothing if it can disagree with the crate it stands in for.

    So the two engines are driven over the census corpus here, in the one job
    that has a rust toolchain, and every verdict must match: same arm, same
    code, same message. A divergence means the cheap gate on every PR is
    measuring something other than the artifact that ships.
    """
    spec = importlib.util.spec_from_file_location(
        "gate_reference_census", ROOT / "tools" / "gate_reference_census.py")
    census = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = census
    spec.loader.exec_module(census)

    cases = census.load_corpus(oracle)
    sources = [src for _, src in cases]
    fast = list(census.SelfhostEngine().verdicts(sources))
    crate = _crate_verdicts(consumer, sources)

    mismatches = []
    for (case_id, _src), got, want in zip(cases, fast, crate):
        kind, payload = got
        if kind == "refused":
            mine = ("refused", payload[0], payload[1])
        elif kind == "no_objection":
            mine = ("no_objection", "", "")
        elif kind == "frontier":
            mine = ("outside_frontier", None, None)
        else:
            # a python-only outcome (`RecursionError`, a wrapped index): the
            # emitted rust does not share python's recursion limit, so these are
            # not comparable and the census does not baseline them either
            continue
        theirs = (want["verdict"], want.get("code", ""), want.get("message", ""))
        if mine[0] != theirs[0]:
            mismatches.append(f"{case_id}: census {mine[0]}, crate {theirs[0]}")
        elif mine[0] == "refused" and mine[1:] != theirs[1:]:
            mismatches.append(
                f"{case_id}: census {mine[1:]!r}, crate {theirs[1:]!r}")
    assert not mismatches, (
        f"the census's fast engine and the crate disagree on "
        f"{len(mismatches)} of {len(cases)} programs:\n  "
        + "\n  ".join(mismatches[:20]))
