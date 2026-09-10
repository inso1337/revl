"""The `revl-gate` crate's differential corpus gate (roadmap item 332, Stage 3).

The roadmap's own exit test for this stage: *a standalone rust binary depending
only on the crate returns the same verdict as `revl compile` across the corpus,
and `compile_to` is byte-identical to the reference on the covered corpus*. Both
halves are driven here from the SAME standalone consumer — the admit half in
full (it agrees with the reference byte-for-byte on the covered corpus), and the
compile half as a fail-closed guard plus a strict-xfail byte-identity tripwire,
because a native emitter is Stage 4 and not landed (see "the compile_to exit
clause" below).

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
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "crates" / "revl-gate"

# --------------------------------------------- the cross-check exclusion list
#
# WHAT IS GIVEN UP, stated rather than left to be inferred: the census's cheap
# self-host engine is NO LONGER VERIFIED AGAINST THE CRATE on these eight
# inputs. It is not a loss of census COVERAGE -- `tools/gate_reference_census.py`
# still runs every one of them through the fast engine and baselines the
# verdict, so a change in what the census says about them still reds. What is
# lost is CROSS-ENGINE AGREEMENT on the eight largest programs in the tree, and
# those are the compiler's own sources, so it is not a comfortable loss.
#
# WHY: `crates/revl-gate` is superlinear in input size (issue #333). Measured
# through the standalone consumer this file builds: 50 assorted corpus programs
# cost 4.0 s in total, `stdlib/str.rvl` (16 KB) costs 11.6 s, and
# `selfhost/parser.rvl` (31 KB, the SMALLEST of the eight) costs 90.1 s in a
# RELEASE build and over 150 s in the debug build this fixture uses. A release
# build buys roughly 2x and does not rescue it; the largest of the eight is
# 4.8x bigger than parser.rvl again. The 900 s cap on `_crate_verdicts` is not
# the problem and raising it would need hours, not minutes.
#
# BY NAME, never by size or by pattern: a size cap would silently swallow the
# next large file someone adds, which is how an exclusion becomes permanent
# without anyone choosing it. Adding a name here without also recording it in
# `tools/gate_crate_cross_check_exclusions.json` is a RED
# (`test_the_cross_check_exclusion_list_only_shrinks`). Removing one is free.
_CROSS_CHECK_EXCLUSIONS = {
    "selfhost/parser.rvl":
        "31 KB. 90.1 s release, >150 s debug, both measured (#333).",
    "selfhost/emit_go.rvl":
        "66 KB. >20 s debug measured; release not timed, and it is larger "
        "than parser.rvl which costs 90 s release (#333).",
    "selfhost/checker.rvl":
        "72 KB. >20 s debug measured; larger than parser.rvl (#333).",
    "selfhost/emit_wasm.rvl":
        "99 KB. >20 s debug measured; larger than parser.rvl (#333).",
    "selfhost/emit_rust.rvl":
        "101 KB. >20 s debug measured; larger than parser.rvl (#333).",
    "selfhost/emit_py.rvl":
        "105 KB. >20 s debug measured; larger than parser.rvl (#333).",
    "selfhost/emit_ts.rvl":
        "133 KB. >20 s debug measured; larger than parser.rvl (#333).",
    "selfhost/emit_java.rvl":
        "149 KB, the largest. >20 s debug measured, and a release run was "
        "still going at 570 s without completing (#333).",
}

_CROSS_CHECK_EXCLUSIONS_BASELINE = (
    ROOT / "tools" / "gate_crate_cross_check_exclusions.json")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.manifest import manifest_wire  # noqa: E402
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

// A minimal JSON string encoder, so this consumer takes no dependency beyond
// `revl-gate` itself (the "only one crate on the dependency line" claim).
fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn main() {
    let mut argv = std::env::args().skip(1);
    let mode = argv.next();
    if mode.as_deref() == Some("--version") {
        let v = revl_gate::gate_version();
        println!(
            "{{\"api\":\"{}\",\"language\":\"{}\",\"frontier\":\"{}\",\"layer\":\"{}\"}}",
            v.api, v.language, v.frontier, v.layer
        );
        return;
    }
    let mut blob = String::new();
    std::io::stdin().read_to_string(&mut blob).expect("read stdin");
    // `--compile <tier>` is the second half of item 332's exit test, driven
    // from the SAME standalone binary: for each source, `compile_to` on the
    // named tier. `ok` is whether an emission was produced; `output` is the
    // emitted target source verbatim (null when none); `error` is the crate's
    // fail-closed verdict wire shape when it did not emit.
    if mode.as_deref() == Some("--compile") {
        let tier = match argv.next().as_deref() {
            Some("py") => revl_gate::Tier::Py,
            Some("rust") => revl_gate::Tier::Rust,
            other => {
                eprintln!("unknown tier {:?}", other);
                std::process::exit(2);
            }
        };
        for source in blob.split('\0') {
            match revl_gate::compile_to(source, tier) {
                Ok(output) => println!(
                    "{{\"ok\":true,\"output\":{},\"error\":null}}",
                    json_string(&output)
                ),
                Err(verdict) => println!(
                    "{{\"ok\":false,\"output\":null,\"error\":{}}}",
                    verdict.to_json()
                ),
            }
        }
        return;
    }
    // `--into <manifest>` is the manifest arm of issue #346, driven from the
    // SAME standalone binary: `admit_into(source, manifest)` for each source,
    // against a running composition handed over as the flat manifest wire. The
    // wire comes from argv, not from a constant here, so the corpus can hold the
    // crate against the wire the PY side computes from its own running
    // composition - a crate that ignored the parameter and re-derived a
    // standalone verdict would red.
    if mode.as_deref() == Some("--into") {
        let manifest = match argv.next() {
            Some(value) => value,
            None => {
                eprintln!("--into needs a manifest wire");
                std::process::exit(2);
            }
        };
        for source in blob.split('\0') {
            println!("{}", revl_gate::admit_into(source, &manifest).to_json());
        }
        return;
    }
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


# ------------------------------------ the held-composition corpus (issue #346)
#
# `admit(source)` asks the STANDALONE question. Issue #346 added
# `admit_into(source, manifest)`, which folds the same G2/G3 legs over the UNION
# of a RUNNING composition's rows and the candidate — the shape an agent loop
# actually asks, and the one `bench/inprocess_gate_harness.py` already asks on
# py. It is driven here through the SAME standalone consumer, with `--into
# <manifest wire>`: the wire is computed below from the py side's own flattening
# of its own running composition, so a crate that ignored the parameter and
# re-derived a standalone verdict would DISAGREE rather than quietly agree.
#
# What the arm does, and what it deliberately does not, is measured here:
#
# * it REFUSES a candidate that collides with a key the running composition
#   already provides — `ambient_collision_*` — with the reference's own code and
#   message, verbatim; and
# * it does NOT resolve a `requires` against the running composition (that is the
#   reference type layer, a separate lane), so `requires_the_running_provider`
#   comes back a no-objection where the reference ADMITS. That entry is in the
#   corpus DELIBERATELY: it is the half that is still open, held here so the
#   distance stays visible instead of being papered over by a stub that answers
#   everything the same way.
#
# `ambient_collision_a` is the load-bearing entry: the reference ADMITS it
# standalone and REFUSES it into the running composition, so it is the case that
# can only pass if the manifest parameter is genuinely read.

HELD_RUNNING = """
service Store {
  fn get(key: Str) -> Str
}
service AppSvc {
  fn snapshot() -> Str
}
component Kv provides store: Store {
  provide store { fn get(key) = key }
}
component App requires store: Store provides app: AppSvc {
  provide app { fn snapshot() = store.get("x") }
}
"""

HELD_MANIFEST = manifest_wire(compile_source(HELD_RUNNING, "held.rvl"))

MANIFEST_CORPUS: list[tuple[str, str]] = [
    # The ambient collision the py gate refuses G2 and this gate must match: it
    # provides `store` in the shared realm, which `Kv` already provides. Its
    # `Store` carries the running provider's own surface, so no interface drift
    # is in play and the collision is the only verdict.
    ("ambient_collision_a", """
service Store {
  fn get(key: Str) -> Str
}
component Rogue provides store: Store {
  provide store { fn get(key) = key }
}
"""),
    # The same collision under a differently NAMED service: the conflict is on
    # the provision KEY, not on the interface's spelling.
    ("ambient_collision_b", """
service Narrow { fn get(key: Str) -> Str }
component Rogue provides store: Narrow {
  provide store { fn get(key) = key }
}
"""),
    # No collision: a fresh key. The reference admits it into the composition and
    # this gate raises no objection.
    ("clean_extension", """
service Extra { fn ping() -> Str }
component Add provides extra: Extra {
  provide extra { fn ping() = "p" }
}
"""),
    # THE OPEN HALF: the reference resolves `store` against the running `Kv`
    # provider and ADMITS this into the composition (it REFUSES it standalone).
    # This gate resolves nothing, so it can only decline to object.
    ("requires_the_running_provider", """
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"""),
    # G3 legs the manifest does not change: a component that requires a key it
    # provides itself, and a dependency cycle. Both must come back refused with
    # the reference's own tag and message, into the manifest as well as
    # standalone.
    ("self_requires", """
service Store { fn get(key: Str) -> Str }
component Odd provides odd: Store requires odd: Store {
  provide odd { fn get(key) = key }
}
"""),
    ("dependency_cycle", """
service A { fn a() -> Str }
service B { fn b() -> Str }
component CycleA provides a: A requires b: B {
  provide a { fn a() = "a" }
}
component CycleB provides b: B requires a: A {
  provide b { fn b() = "b" }
}
"""),
]


def _manifest_reference(source: str, base: dict) -> tuple[str, str]:
    """(tag, message) — ("", "") when the reference ADMITS this program INTO the
    composition. The same call `revl.gate.admit_into` makes, with the refusal
    rendered in the gate's guarantee vocabulary; a G2 conflict carries no
    `RevlError.code` for `_classify` to be replaced by."""
    try:
        compile_source(source, "gate-crate.rvl", manifest=dict(base))
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


def _py_admit_into(source: str, base: dict) -> bool:
    """Does the PUBLIC `revl.gate.admit_into` verb admit this program INTO the
    composition? Asked of the verb itself, not of a re-derivation of its two
    calls, so the corpus is held against what an embedder would actually call."""
    from revl import gate as py_gate

    return py_gate.admit_into(source, dict(base)).admitted


def _crate_into_verdicts(binary: Path, sources: list[str],
                         manifest: str) -> list[dict]:
    """One process, one manifest, one verdict per source — the crate's manifest
    arm, driven from the standalone binary exactly as an embedder would."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    run = subprocess.run([str(binary), "--into", manifest],
                         input="\0".join(sources), text=True,
                         capture_output=True, timeout=900, env=env, check=False)
    assert run.returncode == 0, (
        "the consumer binary exited nonzero on the manifest arm:\n"
        + (run.stderr or run.stdout or "")[-4000:])
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert len(lines) == len(sources), (
        f"expected {len(sources)} manifest-arm verdicts, got {len(lines)}")
    return [json.loads(line) for line in lines]


@pytest.fixture(scope="module")
def manifest_agreement(consumer) -> list[tuple[str, str, dict]]:
    """(name, source, crate manifest-arm verdict) for the whole held-composition
    corpus, computed once — the consumer binary is invoked a single time."""
    verdicts = _crate_into_verdicts(consumer, [src for _, src in MANIFEST_CORPUS],
                                    HELD_MANIFEST)
    return [(name, src, verdict)
            for (name, src), verdict in zip(MANIFEST_CORPUS, verdicts)]


def test_the_manifest_arm_issues_no_admission_either(manifest_agreement):
    """THE security clause, on the newer surface. `admit_into` answers a bigger
    question than `admit` — it is handed the world the candidate would join — so
    it is held to the same rule: no arm may read as an admission, on any corpus
    program. A refusal here is an inconvenience; an admission is the hole."""
    offenders = [(name, verdict) for name, _src, verdict in manifest_agreement
                 if verdict["admitted"] is not False
                 or verdict["verdict"] not in
                 ("refused", "no_objection", "outside_frontier")]
    assert not offenders, (
        "the crate's manifest arm produced something a consumer could read as an "
        "admission:\n  "
        + "\n  ".join(f"{name}: {verdict}" for name, verdict in offenders))


def test_every_manifest_refusal_is_a_real_reference_manifest_refusal(
        manifest_agreement):
    """The sound direction on the manifest arm: every refusal must be a real
    `revl.gate.admit_into` refusal with the same code and the same message,
    verbatim. A false alarm against a RUNNING composition is the expensive kind —
    it throws away a candidate the live system would have accepted."""
    base = compile_source(HELD_RUNNING, "held.rvl")
    false_alarms, tag_drift, message_drift = [], [], []
    refused = [name for name, _src, v in manifest_agreement
               if v["verdict"] == "refused"]
    assert refused, (
        "the manifest arm refused NOTHING in this corpus, so the sound direction "
        "is not exercised at all: the corpus must carry candidates the running "
        "composition genuinely conflicts with, or a manifest-ignoring stub would "
        "pass this test by refusing nothing")
    for name, src, verdict in manifest_agreement:
        if verdict["verdict"] != "refused":
            continue
        ref_tag, ref_msg = _manifest_reference(src, base)
        if ref_tag == "":
            false_alarms.append((name, verdict["code"], verdict["message"]))
            continue
        if verdict["code"] != ref_tag:
            tag_drift.append((name, verdict["code"], ref_tag))
        if verdict["message"] != ref_msg:
            message_drift.append((name, verdict["message"], ref_msg))
    assert not false_alarms, (
        "the crate's manifest arm REFUSED programs `revl.gate.admit_into` "
        "ADMITS:\n  "
        + "\n  ".join(f"{name}: {code} ({msg!r})"
                      for name, code, msg in false_alarms))
    assert not tag_drift, (
        "code drift on the manifest arm:\n  "
        + "\n  ".join(f"{name}: crate {c!r} != reference {r!r}"
                      for name, c, r in tag_drift))
    assert not message_drift, (
        "the manifest arm's message is not the reference's, verbatim:\n  "
        + "\n  ".join(f"{name}:\n    crate {c!r}\n    ref   {r!r}"
                      for name, c, r in message_drift))


def test_the_manifest_parameter_is_read_rather_than_ignored(manifest_agreement):
    """The positive claim, and the one a stub could not pass.

    At least one corpus program must be ADMITTED standalone by the reference and
    REFUSED by the reference when admitted against the running composition — and
    the crate's manifest arm must land on the refusal. A crate that ignored its
    manifest argument and returned the standalone verdict would be
    indistinguishable from this one everywhere else in the corpus, so this entry
    is what makes `admit_into` more than a renamed `admit`. The count of such
    entries is asserted, not just its presence, so deleting the case is a red
    rather than a silently weaker test."""
    base = compile_source(HELD_RUNNING, "held.rvl")
    contrast = []
    for name, src, verdict in manifest_agreement:
        standalone_tag, _ = _reference(src)
        into_tag, into_msg = _manifest_reference(src, base)
        if standalone_tag != "" or into_tag == "":
            continue
        contrast.append((name, into_tag, into_msg, verdict))
    assert len(contrast) >= 1, (
        "no corpus program distinguishes the standalone question from the "
        "manifest question, so this test would pass for a crate that ignored its "
        "manifest parameter: " + repr([n for n, _, _ in manifest_agreement]))
    for name, into_tag, into_msg, verdict in contrast:
        assert verdict["verdict"] == "refused", (
            f"{name}: the reference ADMITS it standalone and REFUSES it "
            f"{into_tag} into the running composition, but the crate's manifest "
            f"arm said {verdict['verdict']} — the manifest parameter is not "
            f"being read")
        assert verdict["code"] == into_tag, (
            f"{name}: crate {verdict['code']!r} != reference {into_tag!r}")
        assert verdict["message"] == into_msg, (
            f"{name}: crate {verdict['message']!r} != reference {into_msg!r}")
    # And the other direction is priced, never hidden: the entry the crate can
    # only decline to object to. If the self-host grows the type layer this
    # becomes an agreement and the assertion should be tightened, not dropped.
    open_half = [
        (name, verdict) for name, src, verdict in manifest_agreement
        if _reference(src)[0] != "" and _manifest_reference(src, base)[0] == ""
        and _py_admit_into(src, base)
    ]
    assert open_half, (
        "the corpus lost the requires-resolution case: without it this corpus "
        "would be claiming the whole manifest question is closed")
    for name, verdict in open_half:
        assert verdict["verdict"] == "no_objection" and verdict["code"] is None, (
            f"{name}: the crate cannot resolve a requires against the running "
            f"composition, so the only honest arm is a no-objection "
            f"(got {verdict['verdict']} {verdict['code']!r})")
        assert verdict["admitted"] is False, name


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


def _generated_frontier() -> tuple[list[str], list[str], int]:
    """The two lexical tables and the size bound the crate was GENERATED with,
    read out of `src/frontier.rs`.

    Read rather than hand-listed for a reason this test learned the hard way:
    the probes below used to name `.is_digit()` and `.str()`, item 391 ported
    both into `selfhost/lower.rvl`, the builtin row emptied, and the probes went
    on asserting a gap that had closed. A probe derived from the table cannot
    outlive the gap it probes."""
    src = (CRATE / "src" / "frontier.rs").read_text(encoding="utf-8")

    def table(name: str) -> list[str]:
        match = re.search(rf"{name}: &\[&str\] = &\[(.*?)\];", src, re.S)
        return re.findall(r'"([^"]+)"', match.group(1)) if match else []

    bound = re.search(r"MAX_SOURCE_BYTES: usize = (\d+);", src)
    return table("EXCLUDED_KEYWORDS"), table("EXCLUDED_BUILTINS"), int(bound.group(1))


def _frontier_probes() -> list[tuple[str, str]]:
    """One probe per live frontier trigger. The size bound is always live; the
    two lexical tables contribute a probe only while they have an entry (both
    are empty at this generation — the self-host lexes every reference keyword
    and lowers every reference stdlib builtin)."""
    keywords, builtins, bound = _generated_frontier()
    probes = [("oversized source",
               "fn id(x: Int) -> Int { return x } " * (bound // 30 + 1))]
    if builtins:
        probes.append((f"excluded builtin {builtins[0]}",
                       f"fn f(x: Str) -> Bool {{ return x.{builtins[0]}() }}"))
    if keywords:
        probes.append((f"excluded keyword {keywords[0]}",
                       f"fn f() -> Int {{ {keywords[0]} }}"))
    return probes


FRONTIER_PROBES = _frontier_probes()


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


# --------------------------------------------------------------- nesting bound
#
# The size bound above is not a nesting bound, and nesting is what the emitted
# front end spends stack on. Both probes here are far below `MAX_SOURCE_BYTES`
# and both used to take the process down: a rust stack overflow ABORTS, and
# `catch_unwind` cannot turn an abort back into a verdict, so an embedder got
# no answer, no log and no process. `selfhost/parser.rvl` now bounds its own
# descent and `admit_src` measures the bound ahead of the descent, which is
# what turns these from an abort into a refusal that names the limit.

_NESTING_PROBES = [
    # the descent's own shape: one level per bracket, a full pass down the
    # precedence ladder each time
    ("nested groups", "fn f() -> Int { return " + "(" * 500 + "1" + ")" * 500 + " }"),
    # read with a LOOP, so it costs the parser no stack — but it builds a tree
    # one level taller per operator, and every later walk over that tree
    # descends all of it
    ("operator spine", "fn f() -> Int { return " + "1 + " * 700 + "1 }"),
]


@pytest.mark.parametrize("name,source", _NESTING_PROBES,
                         ids=[n for n, _ in _NESTING_PROBES])
def test_a_deeply_nested_source_is_refused_rather_than_aborting(
        consumer, name, source):
    meta = json.loads((CRATE / "GENERATED.json").read_text(encoding="utf-8"))
    assert len(source) < meta["max_source_bytes"] // 10, (
        "the point of this probe is that it is far below the size bound")
    verdict = _crate_verdicts(consumer, [source])[0]
    assert verdict["admitted"] is False
    assert verdict["verdict"] == "refused", (
        f"{name}: expected a refusal, got {verdict['verdict']}")
    assert verdict["code"] == "BAD"
    # the refusal states the bound rather than describing a resource failure
    assert "nesting" in verdict["message"] and "200" in verdict["message"], (
        verdict["message"])


# ------------------------------------------------------------ manifest row bound
#
# The manifest is not a string the gate can scan and be done with: it is a
# `;`-separated wire that the fold consumes ONE STACK FRAME PER ROW, so the size
# bound above is not a bound on the fold. Measured against the crate as it was
# before this bound existed: `"A/b/;" * 20000` is 100 KB, well under
# `MAX_SOURCE_BYTES`, and took the process down with `fatal runtime error: stack
# overflow` (exit 134, `Abort trap: 6`) on a stock 8 MiB main thread; `";" * 2700`
# is 2.7 KB and took a 1 MiB thread down, which is the wasm module's default
# because the component build sets no `stack-size`; and the DEBUG profile the
# crate's own `cargo test` runs under went down at 20 000 rows too. A rust stack
# overflow ABORTS, and `catch_unwind` cannot turn an abort back into a verdict,
# so an embedder got no answer, no log and no process.
# `MANIFEST_ROW_LIMIT` counts the rows ahead of the parser and declines the wire
# with the same `outside_frontier` / `FRONTIER` shape the size bound uses.
#
# The wires below are deliberately INERT (`A/b/` names nothing a running
# composition could hold), so a wire under the bound stays `no_objection` and
# no probe here can pass by way of some unrelated refusal.

ROW_BOUND_PROBE_SOURCE = "fn id(x: Int) -> Int { return x }"


def _generated_manifest_row_limit() -> int:
    """The row bound the crate was GENERATED with, read out of `src/frontier.rs`
    for the same reason `_generated_frontier` reads the lexical tables: a probe
    derived from the bound cannot outlive the bound it probes."""
    src = (CRATE / "src" / "frontier.rs").read_text(encoding="utf-8")
    match = re.search(r"MANIFEST_ROW_LIMIT: usize = (\d+);", src)
    assert match, "the crate ships no manifest row bound in `src/frontier.rs`"
    return int(match.group(1))


def test_a_manifest_over_the_row_bound_is_declined_not_decided(consumer):
    """The refusal that keeps the fold from taking the host down, proven from
    the consumer side. The wire is a few KB, so the size bound cannot be what
    declines it."""
    limit = _generated_manifest_row_limit()
    meta = json.loads((CRATE / "GENERATED.json").read_text(encoding="utf-8"))
    manifest = ";".join(["A/b/"] * (limit + 1))
    assert len(manifest) < meta["max_source_bytes"] // 10, (
        "the point of this probe is that it is far below the size bound")
    verdict = _crate_into_verdicts(consumer, [ROW_BOUND_PROBE_SOURCE],
                                   manifest)[0]
    assert verdict["admitted"] is False
    assert verdict["verdict"] == "outside_frontier", (
        f"expected the fold to be declined by the row bound, got "
        f"{verdict['verdict']} ({verdict['message']!r})")
    assert verdict["code"] == "FRONTIER"
    # The refusal names the bound and the count, rather than describing a
    # resource failure an embedder could not act on.
    assert str(limit) in verdict["message"], verdict["message"]
    assert str(limit + 1) in verdict["message"], verdict["message"]
    assert "row" in verdict["message"], verdict["message"]


def test_a_manifest_that_used_to_take_the_host_down_is_declined_not_risked(
        consumer):
    """The unreduced wire: the row count the pre-fix crate aborted on. It has to
    come back as a verdict, because there is no recovering from the alternative
    - a refused manifest costs a denial, an aborted one costs the process."""
    limit = _generated_manifest_row_limit()
    rows = 20_500
    assert rows > limit
    manifest = ";".join(["A/b/"] * rows)
    assert len(manifest) < json.loads(
        (CRATE / "GENERATED.json").read_text(encoding="utf-8"))["max_source_bytes"]
    verdict = _crate_into_verdicts(consumer, [ROW_BOUND_PROBE_SOURCE],
                                   manifest)[0]
    assert verdict["admitted"] is False
    assert verdict["verdict"] == "outside_frontier", verdict
    assert verdict["code"] == "FRONTIER"
    assert str(rows) in verdict["message"], verdict["message"]


def test_a_manifest_at_or_under_the_row_bound_is_still_folded(consumer):
    """Non-vacuity in the other direction: a ceiling, not a wall. The wire at
    the bound is decided exactly as an empty manifest is, which is the standalone
    gate - so an embedder that hands over a large-but-bounded composition does
    not lose the arm."""
    limit = _generated_manifest_row_limit()
    for rows, label in ((limit, "at the bound"), (limit // 2, "under the bound")):
        manifest = ";".join(["A/b/"] * rows)
        verdict = _crate_into_verdicts(consumer, [ROW_BOUND_PROBE_SOURCE],
                                       manifest)[0]
        assert verdict["admitted"] is False, label
        assert verdict["verdict"] == "no_objection", (
            f"{rows} rows ({label}) must still be folded, got {verdict}")
        assert verdict["code"] is None, label


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


# ------------------------------------------------- the compile_to exit clause
#
# Item 332's exit test has TWO halves and the same standalone binary drives
# both: the admit half above (`admit` verdict == `revl compile`, byte-exact on
# the covered corpus), and the compile half here — "`compile_to(source, tier)`
# is byte-identical to the reference on the covered corpus".
#
# The compile half is STAGE 4 and not landed: the self-host emitters
# (`selfhost/emit_py.rvl`, `selfhost/emit_rust.rvl`) still carry `@py`-only
# helper externs and emit no native target, so the crate's `compile_to` has no
# native emitter to call and fails closed on every input. Two things are held
# here rather than left to prose:
#
#   * the SECURITY half, hard: over the covered corpus, on both tiers the crate
#     can name, `compile_to` never returns an emission — it fails closed with a
#     verdict that reads `admitted:false`, so a consumer can never receive
#     target source this crate did not actually produce from the reference;
#   * the byte-identity half, as a strict xfail: the day a native emitter lands,
#     the crate's output must equal the reference's, and the xfail flips to an
#     XPASS (a red) that forces this clause to be turned into a live assertion.


def _crate_compiles(binary: Path, tier: str, sources: list[str]) -> list[dict]:
    """Run every source through the crate's `compile_to` for `tier` in ONE
    process; return the parsed `{ok, output, error}` records, in order."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    run = subprocess.run([str(binary), "--compile", tier],
                         input="\0".join(sources), text=True,
                         capture_output=True, timeout=900, env=env, check=False)
    assert run.returncode == 0, (
        "the consumer binary exited nonzero on --compile:\n"
        + (run.stderr or run.stdout or "")[-4000:])
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert len(lines) == len(sources), (
        f"expected {len(sources)} compile records, got {len(lines)}")
    return [json.loads(line) for line in lines]


# The tiers the crate's `compile_to` can name (its `Tier` enum). The reference
# emits far more; these two are the crate's own surface, so they are what the
# exit clause is held over.
_CRATE_TIERS = ("py", "rust")

# The covered corpus for the compile half is the ACCEPTED programs: a program
# the reference REFUSES has no emission to be byte-identical to.
_ACCEPTED_CORPUS = [src for _name, src in oracle.ACCEPTED_PROGRAMS]


@pytest.mark.parametrize("tier", _CRATE_TIERS)
def test_compile_to_fails_closed_over_the_covered_corpus(consumer, tier):
    """The always-valid, security-relevant half of the compile clause: the
    crate emits NOTHING it did not produce from the reference. Today that means
    it emits nothing at all, and this holds that over the whole covered corpus
    from the consumer side — `ok` false, no `output`, and a verdict that reads
    `admitted:false` on the wire."""
    records = _crate_compiles(consumer, tier, _ACCEPTED_CORPUS)
    offenders = []
    for src, record in zip(_ACCEPTED_CORPUS, records):
        error = record.get("error")
        if (record["ok"] is not False
                or record["output"] is not None
                or error is None
                or error.get("admitted") is not False):
            offenders.append((src[:60], record))
    assert not offenders, (
        "the crate's compile_to did not fail closed on the covered corpus "
        f"(tier {tier}); a consumer could receive an emission the crate did not "
        "produce from the reference:\n  "
        + "\n  ".join(f"{src!r}: {record}" for src, record in offenders))


# One representative accepted program the reference emits on both crate tiers.
# Pinned small so, once a native emitter exists, the byte comparison is legible.
_BYTE_IDENTITY_PROBE = "fn id(x: Int) -> Int { return x }"


@pytest.mark.parametrize("tier", _CRATE_TIERS)
@pytest.mark.xfail(strict=True, reason=(
    "item 332 Stage 4: the self-host emitters carry @py-only helper externs and "
    "emit no native target, so the crate's compile_to fails closed and cannot be "
    "byte-identical to the reference yet (issue #98). When a native emitter "
    "lands this XPASSES, reddening the strict xfail so the clause is turned live."))
def test_compile_to_is_byte_identical_to_the_reference(consumer, tier):
    """The byte-identity half of item 332's exit test, held as a strict xfail
    tripwire. The reference is `revl.gate.compile_to` (the reference emitters);
    the crate must, once it emits at all, produce the same bytes."""
    from revl.gate import compile_to as reference_compile_to  # noqa: PLC0415

    reference = reference_compile_to(_BYTE_IDENTITY_PROBE, tier)
    record = _crate_compiles(consumer, tier, [_BYTE_IDENTITY_PROBE])[0]
    assert record["ok"] is True, (
        f"the crate did not emit on tier {tier}: {record.get('error')}")
    assert record["output"] == reference.output, (
        f"tier {tier}: crate emission diverges from the reference")


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

    cases = [(cid, src) for cid, src in census.load_corpus(oracle)
             if cid not in _CROSS_CHECK_EXCLUSIONS]
    skipped = sorted(_CROSS_CHECK_EXCLUSIONS)
    print(f"cross-check over {len(cases)} programs; "
          f"{len(skipped)} excluded for cost (#333): {', '.join(skipped)}")
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


def test_the_cross_check_exclusion_list_only_shrinks():
    """The exclusion list above may lose names freely and gain them only by a
    deliberate, reviewed edit to the recorded baseline.

    This is the same shape as the census baseline and the coverage ledgers: the
    check exists because narrowing a gate to make a branch go green is exactly
    the move that must never be quiet. Removing a name is the only free
    direction, and it is the direction issue #333 should eventually push.
    """
    recorded = json.loads(
        _CROSS_CHECK_EXCLUSIONS_BASELINE.read_text(encoding="utf-8"))
    baseline = set(recorded["excluded"])
    declared = set(_CROSS_CHECK_EXCLUSIONS)

    added = sorted(declared - baseline)
    assert not added, (
        "these inputs are excluded from the fast-engine/crate cross-check but "
        "are not recorded in tools/gate_crate_cross_check_exclusions.json:\n  "
        + "\n  ".join(added)
        + "\n\nAn exclusion is a gate being narrowed. Record it there, with "
          "the measurement and the issue, so it is reviewed on its own rather "
          "than riding along inside a test edit.")

    # Shrinking is free, and is the point. Report it so the baseline gets
    # tidied rather than quietly keeping a name nothing excludes any more.
    for name in sorted(baseline - declared):
        print(f"no longer excluded, safe to delete from the baseline: {name}")

    for name, reason in sorted(_CROSS_CHECK_EXCLUSIONS.items()):
        assert "#333" in reason, (
            f"{name}'s exclusion reason must cite the issue that owns the "
            f"underlying cost, so a reader can tell a known cost from a "
            f"known-broken input; got: {reason!r}")
