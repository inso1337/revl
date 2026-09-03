"""The quarantine tier — a candidate proves itself in the sandbox first
(roadmap item 45, docs/quarantine-tier.md).

Item 24's threat model is honest that "the gate does not sandbox host code" is a
*non-goal*: admission proves a candidate is structurally compatible and its
teardown is derived, but the moment a candidate's own body runs, revl is
trusting host code it cannot look inside. The quarantine tier is the
architectural answer, and it rests on the wasm tier's defining property: **the
paradigm is enforced by the sandbox — confinement is physical.** A candidate
compiled to a standard WASI-P2 component and run under wasmtime's component
model cannot reach past its linear memory; an escape attempt is a **wasm trap**,
caught by the runtime, not an incident the operator has to clean up after.

So this module stages **item 31's gauntlet through the wasm substrate**:

  * an untrusted candidate is first graded by the gauntlet (:mod:`revl.mcp.
    gauntlet`) exactly as before — admission is *proved*, teardown *derived*,
    the boundary *enumerated*;
  * then, before it is allowed anywhere near a hosted tier, it is compiled to a
    **standard component** over the landed 41-slice-3 canonical ABI
    (``backends/wasm/canonical.py``, imported read-only) and its **lifecycle +
    fault battery run ON THE SUBSTRATE** — booted and invoked under wasmtime's
    component model. A candidate whose fault battery would escape *traps in the
    sandbox*; a clean candidate returns for every probe and is eligible for
    admission to a hosted tier.

The feature sentence: *the gauntlet proves a candidate runs correctly; the
quarantine tier proves it cannot escape while doing so — physically, in the
sandbox, before it touches a hosted tier.*

Honest scope (item 45). The canonical ABI now lowers the pure functions whose
whole signature (params + result) is canonically representable: scalars
(``Int`` / ``Bool`` / ``Str``), records, lists, and variants / ``Opt`` /
``Result``. So the quarantine tier presents any such candidate over the
boundary. A candidate whose every boundary signature carries a type the
canonical boundary still cannot lower — **Float, Map, resources, or
function-values** — has no boundary function to present; such a candidate is
*deferred honestly* here (verdict ``deferred``), never faked through a boundary
that cannot yet carry it.

Verdicts (in ``report["quarantine"]["verdict"]``):

  * ``passed``      — admissible, and every substrate probe returned cleanly.
                      Eligible for admission to a hosted tier.
  * ``trapped``     — admissible, but a substrate probe **trapped in the
                      sandbox**. Contained: the host was never touched, so this
                      is a caught trap, not an incident. Not eligible.
  * ``rejected``    — admission refused; the candidate never reached the
                      substrate (the gauntlet graded it, nothing ran).
  * ``deferred``    — no canonical-ABI-emittable boundary function to present
                      over the canonical ABI: every boundary signature carries a
                      type still un-lowerable at the canonical boundary
                      (Float / Map / resource / function-value). Honestly deferred.
  * ``unavailable`` — the standard toolchain (``wasm-tools`` / ``wasmtime``) is
                      absent, so the substrate battery could not run. The flow
                      logic (gauntlet grade, canonical lowering) still ran; only
                      the physical run is skipped, with a reason.

Isolation. Nothing here mutates the live composition. The gauntlet already runs
its host battery in a throwaway :class:`~revl.mcp.session.Session`; the substrate
battery builds and runs a component in a temporary directory. Whatever the
candidate does — refuse admission, trap in the sandbox — the outcome is a graded
report, never a raised error and never a touched running system.
"""

from __future__ import annotations

# TODO(item 329/330 — the deferred wasm-import path): the untrusted-author
# admission profile (`revl.admit_profile`) delivers the cheapest-first cut —
# `no_extern` + a granted allowlist, refusing a per-turn source that declares
# host code or reaches outside its grant. The fuller answer is to ALLOW a
# per-turn host body but compile the turn to THIS substrate with the granted
# tools wired in as host imports, so confinement is physical (an escape is a
# trap) instead of a compile refusal — reusing the canonical ABI below, but as a
# per-turn admit-into-a-live-composition path rather than a standalone grading
# battery, and threading the granted-tool calls back onto the enclosing 245
# frame. See docs/design/329-untrusted-author-profile.md for the concrete TODO.

import importlib.util
import pathlib
import tempfile

from ..errors import RevlError
from . import gauntlet as _gauntlet

# ---------------------------------------------------------------------------
# The verb an operator profile (item 55) grants to *bypass* a required
# quarantine — the "who may admit an un-quarantined candidate" authority. Reused
# through `operator.Operator.allows` without touching operator.py: quarantine is
# a property of the admission the operator is driving, not of the verb dispatch.
# ---------------------------------------------------------------------------
QUARANTINE_BYPASS_VERB = "quarantine-bypass"

# The substrate battery. Each probe is invoked on every Str-surface boundary
# function under wasmtime's component model. The set exercises the canonical
# round trip (empty string is a real canonical case: a valid ptr, len 0), the
# allocator (a longer string), and length-derived arithmetic (so a candidate
# that overflows/faults on the size of its input traps here, not in production).
# Probes are kept ASCII and quote/backslash-free so they survive the WAVE
# literal `wasmtime run --invoke f("...")` parses (canonical.run_component_str).
_BATTERY: tuple[str, ...] = ("", "a", "revl", "x" * 64)


# --------------------------------------------------------------- canonical tier

def _load_by_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_canonical():
    """Import ``backends/wasm/canonical.py`` READ-ONLY, by path.

    The wasm backend lives outside the ``revl`` package (a sibling ``backends/``
    tree), and a parallel session owns that file — so it is loaded by location,
    never imported as a package and never modified. Returns the module, or
    ``None`` when the backend tree is not present (an installed-package
    environment with no in-tree ``backends/``), which the caller reports as an
    honest ``unavailable`` rather than a crash.

    ``canonical.py`` binds ``from emit import emit`` at load, resolving the bare
    name ``emit`` against ``sys.modules``. By the time quarantine runs, the
    gauntlet's scratch boot has already cached a *different* backend's
    ``emit.py`` there (the python driver's), so a naive load would bind the wrong
    emitter. We therefore pin ``sys.modules['emit']`` to the wasm backend's own
    ``emit.py`` for the duration of the canonical exec, then restore whatever was
    there — an import-state swap fully contained to this call, canonical.py
    itself unmodified.
    """
    import sys  # noqa: PLC0415

    from .._paths import backends_root  # noqa: PLC0415

    backend = backends_root() / "wasm"
    canonical_path = backend / "canonical.py"
    emit_path = backend / "emit.py"
    if not canonical_path.is_file() or not emit_path.is_file():
        return None

    saved = sys.modules.get("emit")
    try:
        wasm_emit = _load_by_path("emit", emit_path)
        sys.modules["emit"] = wasm_emit  # canonical's `from emit import` binds here
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        return _load_by_path("revl_wasm_canonical_quarantine", canonical_path)
    except Exception:  # pragma: no cover - defensive: a broken backend tree
        return None
    finally:
        # restore whatever `emit` the rest of the process expects (the python
        # driver's emitter the gauntlet cached), so nothing downstream is
        # perturbed by our temporary pin.
        if saved is not None:
            sys.modules["emit"] = saved
        else:
            sys.modules.pop("emit", None)


def _service_name(ir: dict, arguments: dict) -> str:
    """The WIT interface the candidate's boundary functions are grouped under.

    An explicit ``service`` argument wins; otherwise the sole declared service,
    else a stable default. The name only labels the exported interface — the
    substrate battery invokes by function name regardless."""
    explicit = arguments.get("service")
    if explicit:
        return str(explicit)
    services = sorted((ir.get("services") or {}).keys())
    if len(services) == 1:
        return services[0]
    return "Candidate"


# --------------------------------------------------------------- substrate battery

def _unavailable(reason: str, *, functions=None) -> dict:
    return {
        "kind": "confined",
        "status": "unavailable",
        "ran": False,
        "reason": reason,
        "functions": list(functions or []),
        "counts": {"probes": 0, "returned": 0, "trapped": 0},
    }


def _run_substrate(canonical, ir: dict, service: str) -> dict:
    """Compile the candidate to a standard component and run the lifecycle +
    fault battery on the substrate — the physical confinement proof.

    Every Str-surface boundary function is booted under wasmtime's component
    model and invoked across the probe battery. A probe that **returns** is a
    clean round trip; a probe that **traps** is caught here as a contained wasm
    trap (the candidate could not escape its linear memory) and recorded — never
    re-raised. The section's ``status`` is ``passed`` when every probe returned,
    ``trapped`` when any probe trapped, ``deferred`` when the candidate has no
    canonical-ABI-emittable boundary function to present (every boundary
    signature carries a still-un-lowerable type — Float/Map/resource/
    function-value), and ``unavailable`` when the toolchain is absent."""
    EmitError = canonical.EmitError

    # 1. Lower to the canonical ABI. No Str-surface function => the aggregate
    #    follow-on; deferred honestly, not forced through a boundary that cannot
    #    carry records/lists yet.
    try:
        emitted = canonical.emit_component(ir, service=service)
    except EmitError as error:
        if "no canonical-ABI-emittable" in str(error):
            return {
                "kind": "confined", "status": "deferred", "ran": False,
                "reason": str(error),
                "note": "no canonical-ABI-emittable boundary function — every "
                        "boundary signature carries a type that is still "
                        "un-lowerable at the canonical boundary (Float, Map, "
                        "resources, or function-values), so there is nothing to "
                        "present over the ABI. Deferred, not faked.",
                "functions": [],
                "counts": {"probes": 0, "returned": 0, "trapped": 0},
            }
        return _unavailable(f"canonical lowering failed: {error}")

    functions = emitted["functions"]

    # 2. The toolchain gate. Absent => skip the physical run with a reason; the
    #    flow (gauntlet grade + canonical lowering) still ran.
    if canonical.wasm_tools_binary() is None or canonical.wasmtime_binary() is None:
        return _unavailable(
            "wasm-tools and/or wasmtime not installed — the candidate lowered "
            "to a standard component, but it cannot be built and run in the "
            "sandbox here. Install both to exercise the substrate battery.",
            functions=functions)

    # 3. Build + validate the real component, then run the battery.
    with tempfile.TemporaryDirectory(prefix="revl-quarantine-") as tmp:
        out_dir = pathlib.Path(tmp)
        try:
            component = canonical.build_component(
                emitted["core_wat"], emitted["wit"], out_dir,
                emitted["world"], name="candidate")
            canonical.validate_component(component)
        except EmitError as error:
            # a build/validate failure is not a *trap* — the candidate never
            # ran. Report it as unavailable (the substrate could not be formed),
            # keeping "trapped" reserved for a real, contained runtime escape.
            return _unavailable(f"the candidate did not form a valid component: "
                                f"{error}", functions=functions)

        probes: list[dict] = []
        returned = trapped = 0
        for func in functions:
            for arg in _BATTERY:
                probe = {"function": func, "input": arg,
                         "inputLen": len(arg.encode("utf-8"))}
                try:
                    result = canonical.run_component_str(component, func, arg)
                    probe["outcome"] = "returned"
                    probe["result"] = result
                    returned += 1
                except EmitError as error:
                    # the physical confinement: wasmtime caught a trap and exited
                    # non-zero. The host was never touched — this is a contained
                    # trap, recorded, never re-raised.
                    probe["outcome"] = "trapped"
                    probe["trap"] = _trap_detail(str(error))
                    trapped += 1
                probes.append(probe)

    status = "trapped" if trapped else "passed"
    return {
        "kind": "confined",
        "status": status,
        "ran": True,
        "runtime": "wasmtime component-model (WASI Preview 2)",
        "interface": f"{emitted['package']}/{emitted['interface']}",
        "functions": functions,
        "counts": {"probes": len(probes), "returned": returned,
                   "trapped": trapped},
        "probes": probes,
        "note": ("every Str-surface function was booted and invoked under "
                 "wasmtime's component model; " + (
                     "a probe trapped in the sandbox — contained, the host was "
                     "never touched" if trapped else
                     "every probe returned cleanly")) + ".",
    }


def _trap_detail(message: str) -> str:
    """Condense wasmtime's failure text to the salient line(s). The full
    backtrace is noise in a dossier; the fact of the trap and its cause line
    are the signal that the sandbox physically caught the escape."""
    prefix = "component invocation failed: "
    text = message[len(prefix):] if message.startswith(prefix) else message
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # keep the first couple of meaningful lines (the error + first cause)
    return " / ".join(lines[:3]) if lines else "wasm trap (no detail)"


# --------------------------------------------------------------- admission gate

def _candidate_labels(report: dict) -> frozenset[str]:
    """The subject labels a bypass grant is matched against: every candidate
    component name plus the unnameable whole-composition ``*`` (an operator with
    ``may quarantine-bypass on *`` may bypass anything)."""
    from .operator import WHOLE  # noqa: PLC0415 — reuse item 55's token

    labels = {WHOLE}
    for comp in (report.get("candidate") or {}).get("components") or []:
        name = comp.get("name")
        if name:
            labels.add(name)
    return frozenset(labels)


def admission_decision(session, report: dict) -> dict:
    """Decide whether policy permits admission of this candidate to a hosted
    tier, given its quarantine ``report``.

    "Admit only after quarantine passes — *where policy says so*." The item-33
    boundary policy bound to the session (``session.sandbox``) decides whether
    quarantine is *required*; item 55's operator authority decides *who may
    bypass* a required quarantine that did not pass. With no policy, or a policy
    that does not require quarantine, the gate is advisory: the decision reports
    the verdict but never refuses (today's behaviour, unchanged)."""
    from .operator import WHOLE  # noqa: PLC0415

    policy = getattr(session, "sandbox", None)
    required = bool(policy is not None
                    and getattr(policy, "quarantine_required", False))
    verdict = report.get("verdict")
    passed = verdict == "passed"

    if not required:
        return {
            "gated": False, "required": False, "admit": True, "bypass": False,
            "verdict": verdict,
            "note": "no policy requires quarantine — this grade is advisory; "
                    "admission is not gated on it here.",
        }

    if passed:
        return {
            "gated": True, "required": True, "admit": True, "bypass": False,
            "verdict": verdict,
            "note": "policy requires quarantine and the candidate passed the "
                    "substrate battery — eligible for admission to a hosted tier.",
        }

    # required, and the candidate did not pass. Refuse — unless the bound
    # operator holds the bypass authority for this candidate (item 55).
    operator = getattr(session, "operator", None)
    labels = _candidate_labels(report)
    if operator is not None:
        allowed, _grant = operator.allows(QUARANTINE_BYPASS_VERB, labels)
        if allowed:
            return {
                "gated": True, "required": True, "admit": True, "bypass": True,
                "verdict": verdict, "operator": operator.token,
                "note": f"quarantine did not pass (verdict `{verdict}`), but "
                        f"operator `{operator.token}` holds "
                        f"`{QUARANTINE_BYPASS_VERB}` authority for this "
                        f"candidate — admitted under operator bypass (item 55).",
            }

    named = sorted(l for l in labels if l != WHOLE) or ["the candidate"]
    return {
        "gated": True, "required": True, "admit": False, "bypass": False,
        "verdict": verdict,
        "operator": getattr(operator, "token", None),
        "message": (f"admission refused: policy requires the candidate to pass "
                    f"quarantine, but its verdict is `{verdict}` — it did not "
                    f"prove itself in the sandbox. No operator authority to "
                    f"`{QUARANTINE_BYPASS_VERB}` "
                    f"[{', '.join(named)}] is bound to this session."),
        "note": "the running composition is untouched — a required quarantine "
                "the candidate did not pass, and no bypass authority.",
    }


# --------------------------------------------------------------- entry point

def run(session, arguments: dict, *,
        over_the_transport: bool = True) -> dict:
    """Quarantine a candidate: grade it with the gauntlet, then prove it in the
    sandbox, and return a report whose verdict is one of ``passed`` / ``trapped``
    / ``rejected`` / ``deferred`` / ``unavailable`` (see the module docstring).

    ``session`` is read for the admission manifest (the gauntlet grades against
    the live composition when one is loaded) and never mutated. The substrate
    battery builds and runs a component in a temporary directory. A rejected,
    trapping, or deferred candidate is *reported*, never raised, and the running
    system is untouched throughout.
    """
    # 1. The gauntlet grade (item 31), reused verbatim: admission proved,
    #    teardown derived, boundary enumerated, host lifecycle battery tested in
    #    an isolated scratch session. A rejection is a verdict, not a crash.
    dossier = _gauntlet.run(session, arguments,
                            over_the_transport=over_the_transport)

    if dossier.get("verdict") != "admissible":
        # admission refused: the candidate never reaches the substrate.
        return {
            "ok": True,
            "verdict": "rejected",
            "note": "admission refused — the candidate was graded by the "
                    "gauntlet and never reached the sandbox. The live "
                    "composition is untouched.",
            "gauntlet": dossier,
            "substrate": {
                "kind": "confined", "status": "not-run",
                "reason": "admission refused; nothing was lowered or run.",
                "counts": {"probes": 0, "returned": 0, "trapped": 0},
            },
            "admission": admission_decision(session, {"verdict": "rejected"}),
        }

    # 2. The substrate battery (item 45): lower to a standard component over the
    #    41-s3 canonical ABI and run the lifecycle + fault battery in the
    #    wasmtime sandbox, where an escape is a trap.
    canonical = _load_canonical()
    if canonical is None:
        substrate = _unavailable(
            "the wasm backend (backends/wasm/canonical.py) is not present in "
            "this environment — the substrate battery needs the in-tree "
            "canonical emitter. The gauntlet grade above still ran.")
    else:
        # compile the candidate standalone for lowering — the same IR the
        # gauntlet booted in its scratch session (candidate summary carries it).
        try:
            ir = _standalone_ir(arguments, over_the_transport)
            service = _service_name(ir, arguments)
            substrate = _run_substrate(canonical, ir, service)
        except RevlError as error:
            # admissible against the live composition but not a whole
            # composition on its own — the same honest split the gauntlet makes.
            substrate = _unavailable(
                f"the candidate is admissible but is not a complete composition "
                f"on its own, so it could not be lowered standalone: {error}")

    verdict = {
        "passed": "passed",
        "trapped": "trapped",
        "deferred": "deferred",
        "unavailable": "unavailable",
    }.get(substrate.get("status"), "unavailable")

    report = {
        "ok": True,
        "verdict": verdict,
        "note": _verdict_note(verdict),
        "candidate": dossier.get("candidate"),
        "gauntlet": dossier,
        "substrate": substrate,
    }
    report["admission"] = admission_decision(session, report)
    return report


def _standalone_ir(arguments: dict, over_the_transport: bool = True) -> dict:
    """Compile the candidate as a standalone composition — what the substrate
    lowers and boots (independent of the live composition it was admitted
    against). Mirrors the gauntlet's ``boot_ir`` compile.

    Through `server.compile_under_authoring`, the one compiler door for
    agent-supplied source: a candidate the admission gate refuses is not
    lowered here either. `run` reaches this only after `_gauntlet.run` has
    already graded the same source under the same trust, so in practice the
    refusal fires there first; this keeps the door closed if the order ever
    changes. Lazy import: `server` imports this module."""
    from .server import compile_under_authoring  # noqa: PLC0415 — cycle

    source = arguments.get("source")
    files = arguments.get("files")
    if source is None and not files:
        raise ValueError("provide `source` or `files`")
    return compile_under_authoring(source, files,
                                   modules=arguments.get("modules"),
                                   over_the_transport=over_the_transport)


def _verdict_note(verdict: str) -> str:
    return {
        "passed": "the candidate proved itself in the sandbox: every substrate "
                  "probe returned cleanly. Eligible for admission to a hosted "
                  "tier.",
        "trapped": "the candidate TRAPPED in the sandbox — a fault probe would "
                   "have escaped on a hosted tier, but here it was caught by "
                   "wasmtime, contained, the host never touched. Not eligible.",
        "deferred": "the candidate has no canonical-ABI-emittable boundary "
                    "function to present over the canonical ABI — every boundary "
                    "signature carries a type still un-lowerable at the canonical "
                    "boundary (Float/Map/resource/function-value). Deferred "
                    "honestly.",
        "unavailable": "the candidate was graded and lowered, but the substrate "
                       "battery could not run here (see substrate.reason). The "
                       "flow logic ran; only the physical sandbox run is skipped.",
    }.get(verdict, "quarantine reported a verdict.")


# --------------------------------------------------------------- swap gate hook

def gate_swap(session, arguments: dict) -> dict | None:
    """Enforce a required quarantine at hot-swap time, localized for
    ``revl.mcp.server._tool_swap``.

    Returns ``None`` — the common, zero-overhead case — unless the session's
    bound boundary policy (``session.sandbox``) *requires* quarantine. When it
    does, the candidate is quarantined (graded + run in the sandbox) and, if it
    did not pass and no operator bypass authority is bound, a refusal dict is
    returned (the running composition untouched); otherwise ``None`` lets the
    swap proceed, and the passing/bypassing quarantine report rides along on the
    caller's result when it wants it."""
    policy = getattr(session, "sandbox", None)
    if policy is None or not getattr(policy, "quarantine_required", False):
        return None  # not required — the default path pays nothing

    report = run(session, arguments)
    decision = report["admission"]
    if decision.get("admit"):
        return None  # passed, or admitted under operator bypass
    return {
        "ok": False,
        "admitted": False,
        "swapped": False,
        "note": decision.get("note", "the running composition is untouched"),
        "quarantine": {"verdict": report["verdict"],
                       "substrate": report["substrate"].get("counts"),
                       "reason": report["substrate"].get("reason")},
        "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "quarantine",
            "message": decision.get(
                "message", "admission refused: the candidate did not pass a "
                           "required quarantine"),
        }],
    }
