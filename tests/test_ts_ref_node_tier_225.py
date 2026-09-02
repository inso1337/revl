"""Item 225: a `= @ts ref` extern is emittable by the ts tier, so a component
reaching one belongs on the node tier — and the shipped host-module pin check
must actually refuse a tampered module there.

# The defect

`placement.ts_safe_ir` / `_py_only_externs` classified an extern by its
`bodies` alone. A `= @ts ref` extern carries EMPTY `bodies` and populated
`refs`, so it landed in the "no `@ts` spelling" bucket, `ts_safe_ir` deleted it,
and `tier_capability_gate` refused node placement to every component reaching
one — with the self-refuting advice to "give the extern a `@ts` body", which it
already effectively had.

The consequence is what makes this a security item rather than a portability
one. `spec.refs` is built from exactly those externs, so it was EMPTY for every
node placement process that could ever boot; and the item 396(B) / 410
deploy-contract hash check in `backends/typescript/placement_runner.ts` walks
`spec.refs`. A shipped, correct, reviewed check that has never once had a pin to
verify is indistinguishable, from the outside, from a check that passes.

# What this suite proves, in order

1. the predicate agrees with the ts emitter's own two-arm decision;
2. `ts_safe_ir` keeps a `@ts ref`-reaching component, and the tier gate ADMITS
   it on node (both were the opposite before);
3. the emitted node module really carries the lazy import thunk, so admitting
   it is not a paper admission;
4. a real conductor boot puts a non-empty `refs` pin list in the node process's
   spec;
5. **the proof that matters** — the shipped check block, extracted VERBATIM out
   of `placement_runner.ts` and run on node against that same real spec:
   untouched host module -> passes, and says how many pins it verified;
   tampered host module -> refuses with the pin mismatch, before any host code
   could run.

(5) is written the way PR #221 proved the swap case, and for the same reason: a
check whose input list is empty reports success. "It passed" is only evidence
when it also says what it looked at. Both directions are asserted here, and the
clean run's verified-pin count is asserted non-zero, so this suite cannot go
vacuously green the way the check itself did.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl import placement as _placement  # noqa: E402

from _backend_import import backend_emitter  # noqa: E402
from test_swap import _wire_conductor, _write  # noqa: E402

emit_ts = backend_emitter("typescript")

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="the shipped pin check is node code")

#: The extern shape the whole item is about: NO `@ts` body, a `@ts ref` instead.
#: This is `stdlib/fs.rvl`'s shape since item 410 stage 5, and the only reason
#: the corpus showed no component changing classification is that nothing had
#: yet combined the two — which is precisely why the check was never reached.
_REF_APP = """
extern pure fn shout(x: Str) -> Str
    = @ts ref shout from "host/shout.ts"

service App { async fn run() -> Str }

component Shouter provides app: App {
  provide app { async fn run() = shout("hi") }
}
"""

_REF_PLACEMENT = """
[processes.main]
backend = "node"
components = ["Shouter"]
"""

_HOST_TS = "export function shout(x: string): string { return x.toUpperCase() }\n"


def _ir(program):
    return program.to_ir() if hasattr(program, "to_ir") else program


@pytest.fixture
def ref_app(tmp_path):
    """The composition on disk, plus its compiled IR."""
    (tmp_path / "host").mkdir(parents=True, exist_ok=True)
    _write(tmp_path / "host", "shout.ts", _HOST_TS)
    app = _write(tmp_path, "app.rvl", _REF_APP)
    plc = _write(tmp_path, "app.toml", _REF_PLACEMENT)
    return app, plc, _ir(compile_files([str(app)]))


# ---------------------------------------------------------------------------
# 1-2. classification and the tier gate
# ---------------------------------------------------------------------------

def test_a_ts_ref_extern_is_not_ts_unemittable(ref_app):
    """The predicate must mirror the emitter's arms, not `bodies` alone.

    `backends/typescript/emit.py::_emit_ts_externs` refuses an extern only when
    it has neither a `@ts` body nor a `@ts` ref; a ref alone emits a thunk.
    """
    _, _, ir = ref_app
    extern = ir["externs"][0]
    assert extern["bodies"] == {}, "the fixture must be ref-only, or it proves nothing"
    assert "ts" in extern["refs"]
    assert _placement._ts_unemittable_externs(ir) == set()


def test_ts_safe_ir_keeps_a_component_that_reaches_a_ts_ref_extern(ref_app):
    """`ts_safe_ir` used to DELETE `Shouter` (and its extern) from the node
    slice, so the artifact silently lost the component the spec still listed."""
    _, _, ir = ref_app
    safe = _placement.ts_safe_ir(ir)
    assert safe is ir, "nothing is un-emittable here, so the slice is the document"
    assert [c["name"] for c in safe["components"]] == ["Shouter"]


def test_the_tier_gate_admits_a_ts_ref_component_on_node(ref_app):
    """The plan-time capability gate must not refuse the node tier here.

    Before the fix this returned the "reaches a `@py`-only extern (no `@ts`
    body)" refusal naming `Shouter` and `shout` — for an extern whose only body
    spelling IS `@ts`.
    """
    _, _, ir = ref_app
    refusal = _placement.tier_capability_gate(ir, {"Shouter": "main"}, {"main": "node"})
    assert refusal is None, refusal


def test_a_genuinely_unemittable_extern_is_still_refused_on_node(tmp_path):
    """The widening is exactly one arm wide: a `@py`-body-only extern is still
    refused, with its component and the extern both named."""
    src = ("extern pure fn only_py(x: Str) -> Str = @py { return x }\n"
           "service App { async fn run() -> Str }\n"
           "component P provides app: App {\n"
           "  provide app { async fn run() = only_py(\"hi\") }\n"
           "}\n")
    ir = _ir(compile_files([str(_write(tmp_path, "py_only.rvl", src))]))
    assert _placement._ts_unemittable_externs(ir) == {"only_py"}
    refusal = _placement.tier_capability_gate(ir, {"P": "main"}, {"main": "node"})
    assert refusal is not None
    assert "P" in refusal and "only_py" in refusal


# ---------------------------------------------------------------------------
# 3. the admission is not a paper one: the module really emits the thunk
# ---------------------------------------------------------------------------

def test_the_node_module_emits_the_lazy_ref_thunk(ref_app):
    """Admitting the component is only correct if the ts emitter can spell it.
    Emit the node slice and look for the item 396(B) thunk machinery."""
    _, _, ir = ref_app
    module = emit_ts.emit(_placement.ts_safe_ir(ir))
    assert "__REVL_REF_ROOT__" in module
    assert "shout" in module
    # a ref NEVER becomes a module-top static import (that would run host code
    # at module evaluation, which option B forbids)
    assert "import { shout }" not in module
    assert "from 'host/shout.ts'" not in module


# ---------------------------------------------------------------------------
# 4. a real boot puts pins in the node process spec
# ---------------------------------------------------------------------------

@pytest.fixture
def booted_node_spec(ref_app, tmp_path, monkeypatch):
    """Run the conductor over the composition with scripted children (the stub
    conductor: no ports, no real node process), and hand back the spec the node
    process was actually given."""
    app, plc, _ = ref_app
    procs = _wire_conductor(tmp_path, monkeypatch, [":q"])
    # the node preflight wants an installed cordis-ts runtime; the runtime is
    # not what is under test here and the child is scripted, so report it
    # present.
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)

    # the REAL ts emitter, called in process: `_emit_ts_module` shells out to
    # `emit.py`, and the stub conductor has replaced `subprocess.Popen` with the
    # scripted child. Same emitter, same `ts_safe_ir` narrowing, no subprocess —
    # so a component the tier could not actually emit still fails here.
    def emit_in_process(ir, tmp):
        module = tmp_path / "mod.ts"
        module.write_text(emit_ts.emit(_placement.ts_safe_ir(ir)), encoding="utf-8")
        return str(module)

    monkeypatch.setattr(_placement, "_emit_ts_module", emit_in_process)
    assert _placement.run_placement([app], plc, once=False) == 0
    assert "main" in procs, f"the node process never booted (procs: {list(procs)})"
    spec = procs["main"].spec
    # the boot is over, and the stub conductor's `subprocess.Popen` patch would
    # otherwise swallow the `node` invocation the tamper proof below makes.
    monkeypatch.undo()
    return spec


def test_the_node_spec_carries_the_host_module_pin(booted_node_spec):
    """The whole point: `spec.refs` is non-empty for a node process, so the
    runner's hash check has something to check.

    Before the fix the gate refused this placement outright, so no spec existed
    at all; the class of composition that could produce a pin was empty.
    """
    spec = booted_node_spec
    assert spec["backend"] == "node"
    assert [r["extern"] for r in spec["refs"]] == ["shout"], spec["refs"]
    assert spec["refs"][0]["path"] == "host/shout.ts"
    assert len(spec["refs"][0]["sha256"]) == 64
    # both roots are stated, so the runner's self-derivation fallback is not
    # what the tamper proof below exercises.
    assert spec["refRoot"] and spec["stdlibRefRoot"]


# ---------------------------------------------------------------------------
# 5. the tamper proof, over the SHIPPED check block
# ---------------------------------------------------------------------------

_RUNNER = ROOT / "backends" / "typescript" / "placement_runner.ts"
_CHECK_START = ";(globalThis as any).__REVL_REF_ROOT__"


def _shipped_check_block() -> str:
    """The pin check, lifted VERBATIM out of `placement_runner.ts`.

    Verbatim matters: a re-typed copy would prove that some check works, not
    that the shipped one does. The runner cannot simply be executed here — it
    imports cordis and boots a whole composition — so the block is sliced by
    its first line and the end of its `for (const ref ...)` loop, and the slice
    is asserted to contain the mismatch throw so a refactor that moves the code
    fails this test loudly instead of silently extracting nothing.
    """
    lines = _RUNNER.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(_CHECK_START))
    loop = next(i for i, line in enumerate(lines)
                if i > start and line.startswith("for (const ref of"))
    end = next(i for i, line in enumerate(lines) if i > loop and line == "}")
    block = "\n".join(lines[start:end + 1])
    assert "does not match the" in block and "createHash('sha256')" in block, block
    return block


_HARNESS_PREAMBLE = """\
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
"""

#: the count is the anti-vacuity assertion: PR #221's swap successor "PASSED
#: with 0 pin(s) verified", which is the failure this whole item is about.
_HARNESS_EPILOGUE = """
console.log(`PASSED with ${(spec.refs || []).length} pin(s) verified`)
"""


def _run_check(tmp_path, spec: dict):
    tmp_path.mkdir(parents=True, exist_ok=True)
    harness = tmp_path / "pin_check.ts"
    harness.write_text(_HARNESS_PREAMBLE + _shipped_check_block() + _HARNESS_EPILOGUE,
                       encoding="utf-8")
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.run(["node", str(harness), str(spec_file)],
                          capture_output=True, text=True, timeout=120)


@needs_node
def test_the_shipped_check_passes_on_the_untouched_host_module(booted_node_spec, tmp_path):
    """Baseline, and it must say what it verified: a run that reports zero pins
    is the vacuous pass this item exists to eliminate."""
    result = _run_check(tmp_path / "clean", booted_node_spec)
    assert result.returncode == 0, result.stderr
    assert "PASSED with 1 pin(s) verified" in result.stdout, result.stdout


@needs_node
def test_the_shipped_check_refuses_a_tampered_host_module(booted_node_spec, tmp_path):
    """THE proof. Change one byte of the pinned host module and the shipped
    check must refuse — before the runner imports the emitted module, so before
    any host code runs.

    If this passed, the classification fix would have made the check REACHABLE
    and simultaneously revealed it to be broken; that is the second defect the
    item warns about. It refuses, naming the extern, the expected digest and
    the file.
    """
    host = Path(booted_node_spec["refRoot"]) / booted_node_spec["refs"][0]["path"]
    original = host.read_text(encoding="utf-8")
    host.write_text(original.replace("toUpperCase", "toLowerCase"), encoding="utf-8")
    try:
        result = _run_check(tmp_path / "tampered", booted_node_spec)
    finally:
        host.write_text(original, encoding="utf-8")

    assert result.returncode != 0, (
        "the tampered host module was accepted — the pin check is reachable now "
        "but does not refuse:\n" + result.stdout)
    assert "PASSED" not in result.stdout
    assert "does not match the file pinned at compile" in result.stderr, result.stderr
    assert "shout" in result.stderr


@needs_node
def test_the_shipped_check_refuses_a_deleted_host_module(booted_node_spec, tmp_path):
    """The other half of the deploy contract: an unreadable pinned module is a
    refusal too, not a skipped pin."""
    host = Path(booted_node_spec["refRoot"]) / booted_node_spec["refs"][0]["path"]
    original = host.read_text(encoding="utf-8")
    host.unlink()
    try:
        result = _run_check(tmp_path / "missing", booted_node_spec)
    finally:
        host.write_text(original, encoding="utf-8")

    assert result.returncode != 0, result.stdout
    assert "cannot read" in result.stderr, result.stderr
