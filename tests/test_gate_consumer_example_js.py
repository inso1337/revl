"""The JavaScript external-consumer example — roadmap item 335 slice 4.

`examples/ecosystem-consumer-js/` is the third sibling of
`examples/ecosystem-consumer/` (py) and `examples/ecosystem-consumer-rs/`
(rust): a standalone-shaped project whose only revl import is the
`revl:gate@1.0.0` component of `crates/revl-gate-wasm`, transpiled to JS by
`jco` (`tools/build_gate_js.py`). It is what a browser page, an edge worker or
a serverless function looks like once it pre-filters agent-authored revl in
process, with no Python, no native toolchain and no server round trip.

Slices 0-2 landed the artifact and `tests/test_gate_wasm_vector.py` holds its
verdicts against the reference under wasmtime. This file holds the JS LANE: the
packaging step, the consuming pattern, and the one property that lane could
plausibly lose.

The property that could be lost in translation
----------------------------------------------
This gate has no admitting arm. `admitted` is `false` on every arm, its
non-refusing arm means "found nothing I am able to refuse" and never
"admitted", and a consumer has exactly two safe readings: REJECT on `refused`,
ESCALATE on `no-objection` and `outside-frontier`.

WIT has no singleton type, so the world can only say `admitted: bool` and jco
faithfully emits `admitted: boolean` — a WIDENING on the one field that must not
widen, which would let a TypeScript consumer write `if (v.admitted) run(x)` with
the type checker's blessing. `tools/build_gate_js.py` narrows it back to the
literal `false` during packaging, and that narrowing is checked here, on the
emitted declarations, not on the WIT.

Two layers of checking, deliberately split
------------------------------------------
* the SOURCE-LEVEL contract checks run everywhere, toolchain or not: they hold
  the properties a reviewer would otherwise have to re-read the example for;
* the BUILD-AND-RUN checks need the wasm toolchain, node and jco, and SKIP WITH
  THE REASON the tools report (the `tests/test_gate_crate_admit.py` discipline).
  A skipped tier is never green: a green here always means a component was
  really built, really transpiled, and really ran under node.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ecosystem-consumer-js"
CANDIDATES = EXAMPLE / "candidates"
ARTIFACTS = EXAMPLE / "artifacts"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _packager():
    path = ROOT / "tools" / "build_gate_js.py"
    spec = importlib.util.spec_from_file_location("revl_build_gate_js_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["revl_build_gate_js_test"] = module
    spec.loader.exec_module(module)
    return module


PKG = _packager()


# --------------------------------------------------------------------------- #
# Source-level contract checks. No toolchain needed; these always run.
# --------------------------------------------------------------------------- #

def test_the_example_never_invents_an_acceptance():
    """This tier has no admission arm, so a consumer of it has no local
    "accept" decision to make. The example's decisions are REJECT (on a
    refusal) and ESCALATE (on everything else), and a third decision word in
    the code would be exactly the overclaim the arc exists to prevent."""
    source = (EXAMPLE / "gate.mjs").read_text(encoding="utf-8")
    decisions = set(re.findall(r'^export const (\w+) = "', source, re.M))
    assert decisions == {"REJECT", "ESCALATE"}, (
        f"the example declared decisions {sorted(decisions)}; a consumer of a "
        f"gate with no admitting arm may only reject or escalate")

    # Comments are stripped first: the files' own docs discuss the words this
    # check forbids (that is the point of them), so what is scanned is the CODE,
    # which is what can actually be printed or decided.
    for name in ("gate.mjs", "prefilter.mjs", "worker.mjs"):
        code = "\n".join(
            line for line in (EXAMPLE / name).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("//"))
        for forbidden in ("REGISTER", "ADMITTED", "isAdmitted"):
            assert forbidden not in code, (
                f"{forbidden!r} appears in {name}'s code: this tier issues no "
                f"admissions, so no output of it may read as one")


def test_the_loader_cannot_instantiate_on_this_gates_word_alone():
    """The whole point of shipping a JS harness rather than a call snippet.

    `loadOrRefuse` is the copyable pattern, and it must have NO path that
    instantiates an artifact on a non-refusal: a browser consumer that read
    "the wasm gate did not refuse it" as "run it" is the single worst outcome
    this item could have. The escalation is structural, not documented.
    """
    source = (EXAMPLE / "gate.mjs").read_text(encoding="utf-8")
    body = source.split("export async function loadOrRefuse", 1)[1]
    assert "throw new Refused(verdict)" in body
    assert "throw new EscalationRequired(verdict)" in body
    # Exactly one instantiation site, and the two throws above it.
    assert body.count("WebAssembly.instantiate") == 1
    guard = body.index("EscalationRequired")
    assert guard < body.index("WebAssembly.instantiate"), (
        "the escalation guard must precede the only instantiation site")
    assert "options.reference.admitted !== true" in body, (
        "instantiation must require an explicit REFERENCE verdict, never this "
        "gate's non-refusal")


def test_the_loader_fails_closed_on_an_unrecognised_verdict():
    """A verdict shape this contract does not know is not a non-refusal: a
    stale, swapped or tampered `dist/` must raise rather than arrive as an
    escalation a caller then waves through."""
    source = (EXAMPLE / "gate.mjs").read_text(encoding="utf-8")
    assert "class UntrustedVerdict" in source
    assert "verdict?.admitted !== false" in source
    assert "admit: (source) => checked(mod.admit(source))" in source, (
        "the raw transpiled `admit` must not be re-exported unchecked")


def test_the_example_records_frontier_layer_and_tier():
    """`frontier` is a first-class contract field, `layer` is what says which
    layer was actually decided, and on this tier `tier` distinguishes the wasm
    packaging from the rust crate it wraps. All three must be surfaced, and
    `frontier` stored on every record."""
    prefilter = (EXAMPLE / "prefilter.mjs").read_text(encoding="utf-8")
    assert "version.layer" in prefilter
    assert "frontier: gate.version.frontier" in prefilter
    assert "version.frontier" in prefilter and "version.tier" in prefilter
    # The cache key is the full identity, never `language` alone.
    key = prefilter.split("function cacheKey", 1)[1].split("}", 1)[0]
    for field in ("api", "language", "frontier", "tier"):
        assert f"version.{field}" in key, f"cache key is missing {field}"


def test_the_browser_demo_shows_both_enforcement_layers():
    """The design asks slice 4 for a page that shows the double enforcement,
    "not just the call": the gate decides, and the substrate enforces, and the
    page must demonstrate each independently."""
    page = (EXAMPLE / "browser" / "index.html").read_text(encoding="utf-8")
    assert "loadOrRefuse" in page, "the page must use the guarded loader"
    assert "EscalationRequired" in page, "the escalation gap must be visible"
    # Layer 2: the policy IS the import object, and the page must let a reader
    # withhold a capability and watch the engine refuse.
    assert 'revl:host/db' in page and 'revl:host/log' in page
    assert "cap-db" in page and "cap-log" in page
    assert "issues no admissions" in page.lower() or \
           "no admissions" in page.lower()


def test_the_example_pins_its_transpiler():
    """The transpiled bytes are a function of the jco version, so a consumer
    following this project's instructions must get the version it was measured
    with rather than whatever npm resolves today."""
    manifest = json.loads((EXAMPLE / "package.json").read_text(encoding="utf-8"))
    jco = manifest["devDependencies"]["@bytecodealliance/jco"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", jco), (
        f"jco must be pinned exactly, found {jco!r}")
    assert manifest.get("private") is True, (
        "this is an in-tree worked example, not a package to publish")


def test_the_layer_two_artifact_is_readable_source_not_just_bytes():
    """The committed `.wasm` for the substrate demo must have committed source
    beside it. A binary a reader has to take on trust has no place in an
    example whose whole subject is not taking things on trust."""
    assert (ARTIFACTS / "reaching_tool.wat").is_file()
    assert (ARTIFACTS / "reaching_tool.wasm").is_file()
    wat = (ARTIFACTS / "reaching_tool.wat").read_text(encoding="utf-8")
    assert '(import "revl:host/db"' in wat
    assert '(import "revl:host/log"' in wat


@pytest.mark.skipif(shutil.which("wasm-tools") is None,
                    reason="needs wasm-tools to re-assemble the .wat")
def test_the_committed_artifact_bytes_are_the_committed_source():
    """Re-assemble the `.wat` and require the committed `.wasm` to match, so
    the binary is a checkable function of the source next to it."""
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "reaching_tool.wasm"
        done = subprocess.run(
            ["wasm-tools", "parse", str(ARTIFACTS / "reaching_tool.wat"),
             "-o", str(out)],
            capture_output=True, text=True, timeout=300, check=False)
        assert done.returncode == 0, done.stderr
        assert out.read_bytes() == (ARTIFACTS / "reaching_tool.wasm").read_bytes(), (
            "artifacts/reaching_tool.wasm is not what artifacts/reaching_tool.wat "
            "assembles to; re-run `wasm-tools parse reaching_tool.wat -o "
            "reaching_tool.wasm`")


# --------------------------------------------------------------------------- #
# Build-and-run: really transpiled, really ran under node.
# --------------------------------------------------------------------------- #

_REASON = PKG.js_toolchain_reason()
needs_js = pytest.mark.skipif(
    _REASON is not None,
    reason=f"needs the wasm toolchain plus node and jco to package the gate "
           f"for JavaScript: {_REASON}")


def _env() -> dict:
    """No PYTHONPATH, no VIRTUAL_ENV: what runs the transpiled gate is node,
    and it must need nothing from this repo's Python."""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}


@pytest.fixture(scope="module")
def packaged(tmp_path_factory) -> dict:
    """The example, copied out of the checkout, with a freshly packaged `dist/`.

    Copied rather than built in place so what runs is the committed source built
    from somewhere that is not this repo, the same "genuinely external" shape
    the py and rust siblings' tests arrange.
    """
    work = tmp_path_factory.mktemp("agent-prefilter-js")
    project = work / "agent-prefilter-js"
    shutil.copytree(EXAMPLE, project,
                    ignore=shutil.ignore_patterns("node_modules", "dist"))
    try:
        info = PKG.build_js(project / "dist")
    except RuntimeError as error:
        pytest.fail(f"the gate failed to package for JavaScript:\n{error}")
    return {"project": project, "info": info}


def _node(packaged: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["node", *args], cwd=packaged["project"], text=True,
                          capture_output=True, timeout=900, env=_env(),
                          check=False)


@pytest.fixture(scope="module")
def records(packaged) -> dict:
    proc = _node(packaged, "prefilter.mjs", "candidates/", "--json")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _reference(source: str) -> tuple[str, str]:
    """(tag, message) — ("", "") when the reference admits. The reference's own
    guarantee vocabulary, via the self-host oracle's classifier, so this tier
    and the reference are compared in ONE vocabulary."""
    import test_selfhost_lower as oracle  # noqa: PLC0415

    try:
        compile_source(source, "candidate.rvl")
        return ("", "")
    except RevlError as error:
        return (oracle._classify(error), error.message)


@needs_js
def test_the_packaged_gate_still_imports_nothing(packaged):
    """The soundness mechanism the whole design rests on, re-read off the
    artifact this lane actually ships. Transpilation cannot add an import the
    component does not have, but the check costs nothing and this is the lane
    where a host would notice last."""
    assert packaged["info"]["imports"] == [], packaged["info"]["imports"]


@needs_js
def test_the_packaged_types_cannot_express_an_admission(packaged):
    """The item's own risk, closed in the type a consumer programs against.

    jco emits `admitted: boolean`, which is a widening: it tells TypeScript that
    `if (v.admitted) run(candidate)` is a reachable branch. It is not, and the
    packaging step narrows the field to the literal `false` so that reading is a
    compile error rather than a comment nobody reads.
    """
    dist = packaged["project"] / "dist"
    assert packaged["info"]["narrowed_admitted"] is True, (
        "jco emitted no widened `admitted` to narrow; if a newer jco emits the "
        "literal itself, drop the narrowing rather than leaving it unchecked")
    assert PKG.check_surface(dist) == []
    types = (dist / "revl_gate.d.ts").read_text(encoding="utf-8")
    assert "admitted: false," in types
    assert "admitted: boolean" not in types


@needs_js
def test_the_packaged_gate_is_cheaper_than_the_runtime_it_replaces(packaged):
    """The cost baseline this item exists to beat is the playground's Pyodide
    lane: a 1.4 MB wheel plus an interpreter. The bound is deliberately loose,
    an order-of-magnitude tripwire rather than a byte budget; what it forbids is
    the JS lane quietly growing back into the thing it replaces."""
    info = packaged["info"]
    assert info["core_wasm_bytes"] + info["js_bytes"] < 4 * 1024 * 1024, info


@needs_js
def test_the_example_reports_the_wasm_gates_version_surface(records):
    """A wasm-gate consumer logs five fields: `layer` is what tells it the
    reference type layer was NOT decided here, `frontier` is what stops this
    verdict being confused with a py one, and `tier` names the packaging."""
    version = records["gate_version"]
    assert set(version) == {"api", "language", "frontier", "layer", "tier"}
    assert version["tier"] == "wasm"
    assert version["frontier"].startswith("selfhost-admit:")
    assert "NOT the reference type layer" in version["layer"]

    from revl.gate import gate_version as py_gate_version  # noqa: PLC0415

    py_version = py_gate_version()
    # Frontier skew, machine-checked: the two tiers agree on `language` and
    # disagree on `frontier`, which is why a verdict is only a fact together
    # with the gate that produced it.
    assert version["language"] == py_version["language"]
    assert version["frontier"] != py_version["frontier"]


@needs_js
def test_the_example_never_emits_an_admission(records):
    """THE security clause, from the JS consumer's side: nothing this project
    outputs can be read as an admission."""
    for record in records["results"]:
        assert record["admitted"] is False, record["name"]
        assert record["decision"] in ("REJECT", "ESCALATE"), record["name"]
        assert record["kind"] in (
            "refused", "no-objection", "outside-frontier"), record["name"]
        assert record["frontier"] == records["gate_version"]["frontier"]


@needs_js
def test_every_rejection_is_a_real_reference_refusal(records):
    """Clause 1 where it is load-bearing: a REJECT this project acts on must be
    a refusal the reference compiler also makes, same guarantee tag, same
    message verbatim. A false alarm here is a browser throwing away code the
    reference admits."""
    rejected = [r for r in records["results"] if r["decision"] == "REJECT"]
    assert rejected, "the candidate batch must exercise the REJECT path"
    for record in rejected:
        source = (CANDIDATES / record["name"]).read_text(encoding="utf-8")
        ref_tag, ref_message = _reference(source)
        assert ref_tag != "", (
            f"{record['name']}: rejected a candidate the reference ADMITS "
            f"({record['code']}: {record['message']!r})")
        assert record["code"] == ref_tag, record["name"]
        assert record["message"] == ref_message, record["name"]


@needs_js
def test_escalations_cover_both_non_refusing_arms(records):
    """A consumer that only ever saw `no-objection` would not learn that
    `outside-frontier` exists and is equally not an acceptance."""
    arms = {r["kind"] for r in records["results"] if r["decision"] == "ESCALATE"}
    assert {"no-objection", "outside-frontier"} <= arms, sorted(arms)


@needs_js
def test_a_py_admitted_candidate_is_still_only_escalated(records):
    """The asymmetry made concrete: `double_tool.rvl` is ADMITTED by the py
    reference-full gate and merely NOT REFUSED here."""
    from revl.gate import admit as py_admit  # noqa: PLC0415

    source = (CANDIDATES / "double_tool.rvl").read_text(encoding="utf-8")
    assert py_admit(source).admitted is True

    record = next(r for r in records["results"] if r["name"] == "double_tool.rvl")
    assert record["kind"] == "no-objection"
    assert record["decision"] == "ESCALATE"


@needs_js
def test_the_loader_refuses_and_escalates_and_the_substrate_enforces(packaged):
    """The double enforcement, exercised rather than described.

    Four states, in the order the browser demo walks a reader through them:
    a refused candidate is never instantiated; a non-refused one is not either,
    because a non-refusal is not an admission; with a reference verdict but a
    policy that withholds a declared reach the WASM ENGINE refuses to link it;
    and only with both layers satisfied does anything instantiate.
    """
    probe = packaged["project"] / "probe.mjs"
    probe.write_text(
        'import { readFile } from "node:fs/promises";\n'
        'import { EscalationRequired, Refused, loadGate, loadOrRefuse } '
        'from "./gate.mjs";\n'
        'const gate = await loadGate();\n'
        'const artifact = await readFile("artifacts/reaching_tool.wasm");\n'
        'const full = { "revl:host/log": { write: () => {} },\n'
        '               "revl:host/db": { query: (k) => k + 1 } };\n'
        'const partial = { "revl:host/log": { write: () => {} } };\n'
        'async function attempt(file, options) {\n'
        '  const source = await readFile("candidates/" + file, "utf8");\n'
        '  try {\n'
        '    const instance = await loadOrRefuse(gate, source,\n'
        '      { artifact, ...options });\n'
        '    return { outcome: "instantiated", ran: instance.exports.run(41) };\n'
        '  } catch (error) {\n'
        '    return { outcome: error.name };\n'
        '  }\n'
        '}\n'
        'const reference = { admitted: true };\n'
        'console.log(JSON.stringify({\n'
        '  refused: await attempt("undeclared_tool.rvl", '
        '{ policy: full, reference }),\n'
        '  no_reference: await attempt("double_tool.rvl", { policy: full }),\n'
        '  withheld: await attempt("double_tool.rvl", '
        '{ policy: partial, reference }),\n'
        '  granted: await attempt("double_tool.rvl", '
        '{ policy: full, reference }),\n'
        '}));\n',
        encoding="utf-8")
    proc = _node(packaged, "probe.mjs")
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)

    # Layer 1: a refusal is authoritative, and it wins even when the caller
    # brings a reference verdict and a fully granting policy.
    assert seen["refused"]["outcome"] == "Refused", seen

    # The escalation gap: no refusal is not an admission, and there is no flag
    # that skips this.
    assert seen["no_reference"]["outcome"] == "EscalationRequired", seen

    # Layer 2: the engine, not this code, refuses an artifact whose declared
    # reach the policy does not grant. Node reports the missing namespace as a
    # TypeError and a missing name as a LinkError; either is the substrate
    # refusing, and neither is an instantiation.
    assert seen["withheld"]["outcome"] in ("TypeError", "LinkError"), seen

    # Both layers satisfied is the only path to an instance.
    assert seen["granted"] == {"outcome": "instantiated", "ran": 42}, seen


@needs_js
def test_the_serverless_handler_has_no_success_arm(packaged):
    """The edge shape, held to the same contract: the worker answers 403 on a
    refusal and 202 (accepted for processing, forwarded to the reference) on
    everything else. There is no 200, because there is no arm that would
    justify one."""
    probe = packaged["project"] / "worker_probe.mjs"
    probe.write_text(
        'import { readFile } from "node:fs/promises";\n'
        'import { handler } from "./worker.mjs";\n'
        'const out = {};\n'
        'for (const name of ["undeclared_tool.rvl", "double_tool.rvl",\n'
        '                    "digit_tool.rvl"]) {\n'
        '  const body = await readFile("candidates/" + name, "utf8");\n'
        '  const res = await handler(new Request("http://edge/admit",\n'
        '    { method: "POST", body }));\n'
        '  out[name] = { status: res.status, body: await res.json() };\n'
        '}\n'
        'console.log(JSON.stringify(out));\n',
        encoding="utf-8")
    proc = _node(packaged, "worker_probe.mjs")
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)

    assert seen["undeclared_tool.rvl"]["status"] == 403
    assert seen["undeclared_tool.rvl"]["body"]["decision"] == "REJECT"
    for name in ("double_tool.rvl", "digit_tool.rvl"):
        assert seen[name]["status"] == 202, seen[name]
        assert seen[name]["body"]["decision"] == "ESCALATE", seen[name]
    for name, response in seen.items():
        assert response["status"] != 200, (
            f"{name}: the edge gate answered 200, which a caller reads as an "
            f"admission this tier cannot give")
        assert response["body"]["admitted"] is False, name
        assert response["body"]["gate"]["frontier"].startswith("selfhost-admit:")
