"""The rust external-consumer example — roadmap item 338, the polyglot slice.

`examples/ecosystem-consumer-rs/` is the rust sibling of
`examples/ecosystem-consumer/`: a small, standalone-shaped project (its own
`Cargo.toml` depending on `revl-gate` and nothing else, its own `src/main.rs`
importing only `revl_gate`) that demonstrates what an external tool does once
it pulls revl's NATIVE admission gate in as a library instead of shelling out
to the `revl` CLI. 338's design (docs/design/338-revl-as-dependency.md §1, §5)
named this half frontier-gated on 332 stage 3; the crate landed
(`crates/revl-gate`), so this is that half, minus the publish step.

The rust tier's contract is NOT the py tier's, and the whole point of the
example is that a consumer must not read it as if it were:

* a **refusal** is authoritative and fail-closed, byte-agreeing with the
  reference compiler on the covered corpus — the example REJECTs on it;
* a **no-objection** is NOT an admission (the native gate decides the
  composition/guarantee layer and runs no type layer at all), and
  **outside-frontier** is a declined decision — the example ESCALATEs on both,
  to the reference toolchain, and never accepts locally;
* there is no admission arm at all, so `gate_version().frontier` is
  `selfhost-admit:<hash>`, not py's `reference-full:<language>`, and a verdict
  cached from this tier must never be served to a reader of the other's
  (docs/design/338-revl-as-dependency.md §3, "Frontier skew").

Two layers of checking, deliberately split:

* the SOURCE-LEVEL contract checks below run everywhere, toolchain or not —
  they hold the properties a reviewer would otherwise have to re-read the
  example for (one dependency; no locally-invented acceptance);
* the BUILD-AND-RUN checks need cargo and a resolvable cordis-rs, and SKIP
  WITH THE REASON where either is missing (the `tests/test_gate_crate_admit.py`
  toolchain-honesty discipline). A skipped tier is never green: a green here
  always means the example really built against the crate and really ran.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "crates" / "revl-gate"
EXAMPLE_DIR = ROOT / "examples" / "ecosystem-consumer-rs"
CANDIDATES_DIR = EXAMPLE_DIR / "candidates"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402


# --------------------------------------------------------------------------- #
# Source-level contract checks. No toolchain needed; these always run.
# --------------------------------------------------------------------------- #

def test_the_example_depends_on_revl_gate_and_nothing_else():
    """"A third-party tool that `cargo add`s revl and admits code" is only an
    honest ecosystem claim if the tool really depends on the gate alone. The
    example hand-rolls its JSON for exactly this reason, so its dependency
    table must stay a single line."""
    manifest = (EXAMPLE_DIR / "Cargo.toml").read_text(encoding="utf-8")
    body = manifest.split("[dependencies]", 1)[1]
    deps = [line.split("=", 1)[0].strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
            and "=" in line and not line.strip().startswith("[")]
    assert deps == ["revl-gate"], (
        f"the example must depend on revl-gate alone; found {deps}")
    assert 'path = "../../crates/revl-gate"' in manifest


def test_the_example_never_invents_an_acceptance():
    """The rust gate has no admission arm, so a consumer of it has no local
    "accept" decision to make. The example's decisions are REJECT (on a
    refusal) and ESCALATE (on everything else) — a third decision word in the
    source would be exactly the overclaim 338's adversarial review exists to
    prevent (design §7, "admit-means-safe-to-run")."""
    source = (EXAMPLE_DIR / "src" / "main.rs").read_text(encoding="utf-8")
    decisions = set(re.findall(r'^const (\w+): &str = "', source, re.M))
    assert decisions == {"REJECT", "ESCALATE"}, (
        f"the example declared decisions {sorted(decisions)}; a native-gate "
        f"consumer may only reject or escalate")
    # Comments are stripped first: the file's own docs discuss the words this
    # check forbids (that is the point of them), so what is scanned is the
    # CODE — what the program can actually print or decide.
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("//"))
    for forbidden in ("REGISTER", "ADMITTED", "is_admitted"):
        assert forbidden not in code, (
            f"{forbidden!r} appears in the example's code: this tier issues "
            f"no admissions, so no output of it may read as one")


def test_the_example_records_frontier_and_layer():
    """`frontier` is a first-class contract field (design §2) and, on a native
    gate, `layer` is the field that says what was actually decided. The
    example must surface both, and store `frontier` on every record."""
    source = (EXAMPLE_DIR / "src" / "main.rs").read_text(encoding="utf-8")
    assert "version.layer" in source
    assert "frontier: &'static str" in source
    assert "version.frontier.to_string()" in source, (
        "the cache key must carry frontier, not just api/language")


# --------------------------------------------------------------------------- #
# Build-and-run: the example really built against the crate, and really ran.
# --------------------------------------------------------------------------- #

_RUST_REASON = rust_runtime_reason()
needs_rust = pytest.mark.skipif(
    _RUST_REASON is not None,
    reason=f"needs a resolvable cordis-rs toolchain to build the gate crate: "
           f"{_RUST_REASON}")

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
    # No PYTHONPATH, no VIRTUAL_ENV: this consumer must build with nothing from
    # the repo's Python on the path — the "no Python on the machine" claim, held
    # as tightly as a test on a machine that does have Python can hold it.
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
def prefilter(tmp_path_factory) -> Path:
    """Build the COMMITTED example, out of tree.

    The example is copied to a temp dir and its path dependency rewritten to an
    absolute path, so what runs is the committed source built from somewhere
    that is not this checkout — the same "genuinely external" shape
    `tests/test_gate_consumer_example.py` gets on py by assembling an isolated
    package copy. Building in place would also write `target/` into the repo.
    """
    work = tmp_path_factory.mktemp("agent-prefilter")
    project = work / "agent-prefilter"
    shutil.copytree(EXAMPLE_DIR, project,
                    ignore=shutil.ignore_patterns("target", "Cargo.lock"))
    manifest = (project / "Cargo.toml").read_text(encoding="utf-8")
    manifest = manifest.replace('path = "../../crates/revl-gate"',
                                f'path = "{CRATE.as_posix()}"')
    (project / "Cargo.toml").write_text(manifest, encoding="utf-8")

    built = _cargo("build", project)
    assert built.returncode == 0, (
        "the committed rust consumer example failed to build against "
        "crates/revl-gate:\n" + (built.stderr or built.stdout or "")[-4000:])
    binary = project / "target" / "debug" / "agent-prefilter"
    assert binary.exists(), f"consumer binary not found at {binary}"
    return binary


def _run(binary: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    return subprocess.run([str(binary), str(CANDIDATES_DIR), *args],
                          text=True, capture_output=True, timeout=900,
                          env=env, check=False)


@pytest.fixture(scope="module")
def records(prefilter) -> dict:
    proc = _run(prefilter, "--json")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _reference(source: str) -> tuple[str, str]:
    """(tag, message) — ("", "") when the reference admits. The reference's own
    guarantee vocabulary, via the self-host oracle's classifier, so the crate
    and the reference are compared in ONE vocabulary."""
    import test_selfhost_lower as oracle  # noqa: PLC0415 — heavy, and only
    # needed on the toolchain path
    try:
        compile_source(source, "candidate.rvl")
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


@needs_rust
def test_the_example_reports_the_native_gates_version_surface(records):
    """A native-gate consumer logs four fields, not py's three: `layer` is what
    tells it the reference type layer was NOT decided here, and `frontier` is
    what stops this verdict being confused with a py one."""
    version = records["gate_version"]
    assert set(version) == {"api", "language", "frontier", "layer"}
    assert version["frontier"].startswith("selfhost-admit:")
    assert "NOT the reference type layer" in version["layer"]

    from revl.gate import gate_version as py_gate_version  # noqa: PLC0415
    py_version = py_gate_version()
    # The frontier-skew demonstration, machine-checked: the two tiers agree on
    # `language` and DISAGREE on `frontier`, which is precisely why a consumer
    # must record the frontier with every verdict it keeps or transmits.
    assert version["language"] == py_version["language"]
    assert version["frontier"] != py_version["frontier"]


@needs_rust
def test_the_example_never_emits_an_admission(records):
    """The security clause, from the consumer side: no record this example
    produces can be read as an admission — `admitted` is false on every arm and
    the only decisions are REJECT and ESCALATE."""
    for record in records["results"]:
        assert record["admitted"] is False, record["name"]
        assert record["decision"] in ("REJECT", "ESCALATE"), record["name"]
        assert record["verdict"] in (
            "refused", "no_objection", "outside_frontier"), record["name"]
        assert record["frontier"] == records["gate_version"]["frontier"]


@needs_rust
def test_every_rejection_is_a_real_reference_refusal(records):
    """Clause 1 in the tier where it is load-bearing: a REJECT the example
    acts on must be a refusal the reference compiler also makes, with the same
    guarantee tag and the same message verbatim. A false alarm here would be a
    consumer throwing away code the reference admits."""
    rejected = [r for r in records["results"] if r["decision"] == "REJECT"]
    assert rejected, (
        "the example's candidate batch must exercise the REJECT path — that is "
        "the only decision this tier can make on its own")
    for record in rejected:
        source = (CANDIDATES_DIR / record["name"]).read_text(encoding="utf-8")
        ref_tag, ref_message = _reference(source)
        assert ref_tag != "", (
            f"{record['name']}: the example rejected a candidate the reference "
            f"ADMITS ({record['code']}: {record['message']!r})")
        assert record["code"] == ref_tag, record["name"]
        assert record["message"] == ref_message, record["name"]


@needs_rust
def test_escalations_cover_both_non_refusing_arms(records):
    """The example's batch must exercise BOTH ways this gate declines to
    decide, because a consumer that only ever saw `no_objection` would not
    learn that `outside_frontier` exists and is equally not an acceptance."""
    arms = {r["verdict"] for r in records["results"] if r["decision"] == "ESCALATE"}
    assert {"no_objection", "outside_frontier"} <= arms, (
        f"escalated arms seen: {sorted(arms)}")


@needs_rust
def test_a_py_admitted_candidate_is_still_only_escalated(records):
    """The asymmetry made concrete: `double_tool.rvl` is ADMITTED by the py
    reference-full gate and merely NOT REFUSED here. A consumer that read this
    tier's non-refusal as the py tier's admission would be trusting a verdict
    no gate gave it (design §7, "admit-is-one-fact-across-the-fleet")."""
    from revl.gate import admit as py_admit  # noqa: PLC0415
    source = (CANDIDATES_DIR / "double_tool.rvl").read_text(encoding="utf-8")
    assert py_admit(source).admitted is True

    record = next(r for r in records["results"]
                  if r["name"] == "double_tool.rvl")
    assert record["verdict"] == "no_objection"
    assert record["decision"] == "ESCALATE"
    assert record["admitted"] is False


@needs_rust
def test_the_human_log_says_what_a_non_refusal_means(prefilter):
    """The copyable reference must EMBODY the contract, not merely obey it: an
    operator reading the log has to be told that an escalation is not an
    acceptance (design §7's resolution — the example demonstrates the contract
    rather than the overclaim)."""
    proc = _run(prefilter)
    assert proc.returncode == 0, proc.stderr
    assert "gate_version: api=1.0.0" in proc.stdout
    assert "frontier=selfhost-admit:" in proc.stdout
    assert "this crate issues no admissions" in proc.stdout
    assert "REJECT" in proc.stdout
    assert "REGISTER" not in proc.stdout
