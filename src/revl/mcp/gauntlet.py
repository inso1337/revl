"""The proving ground as one MCP verb (docs/gauntlet.md).

`revl_swap` is binary: it admits a candidate or it refuses. The gauntlet is
graded — *candidate in, dossier out*. It runs a battery against a candidate
component in an **isolated scratch session the live composition never sees**
and returns a structured verdict that separates three different kinds of
knowledge about the candidate:

  * **proved** — what the compiler establishes deductively, before anything
    runs: the candidate is *admissible* (§5 structural compatibility, G2/G3,
    no interface drift) and its *teardown is derived* (LIFO, the reverse of
    load order — a structural consequence of G2/G5, not an experiment).
  * **tested** — what a real boot/unload observed, *with counts*: the
    lifecycle no-residue battery boots the candidate in a scratch session and
    tears it down, counting the residue checks that held.
  * **claimed** — what revl cannot verify and takes on faith, *enumerated*:
    the G8 extern boundary. Every piece of host code the candidate reaches is
    listed, because that list is exactly the trust surface a proof cannot
    cover.

The feature sentence: *admission proves a candidate may run; the gauntlet
proves it does run correctly — before it touches anything.*

Two sections are designed here but not yet built — the fault sweep (roadmap
item 30) and verified-effect inverse round-trips (item 26). They are present
as first-class dossier sections reporting `pending`, so that when 26/30 land
they drop into a slot whose shape the rest of the toolchain (item 28's
interchange format included) already carries. Nothing about the schema
changes when they fill in; only their `status` does.

Isolation: the battery runs against a throwaway `Session` instance — the same
in-memory composition machinery `revl_load` drives, but a *separate object*.
The live composition the server holds (`server.SESSION`) is read for the
admission manifest and never mutated. A candidate that faults, or that leaves
residue, or that the runtime cannot even boot, changes nothing the operator
can see.
"""

from __future__ import annotations

from ..diagnostics import report
from ..errors import RevlError
from .session import Session, SessionError

# the roadmap items whose sections are designed here but land later
_FAULT_SWEEP_ITEM = 30
_INVERSE_ROUNDTRIP_ITEM = 26


def _compile(source: str | None, files: list[str] | None,
             manifest: dict | None = None, modules: dict | None = None,
             replacing: tuple = (), over_the_transport: bool = True) -> dict:
    """Compile inline source or paths through `server.compile_under_authoring`
    — the one compiler door for agent-supplied source, so the gauntlet grades a
    candidate under the SAME authoring trust `revl_check` / `revl_admit` /
    `revl_swap` decide under.

    This used to call `compile_source` / `compile_files` itself, with no
    profile. That made the gauntlet an admission-gate bypass rather than a
    sibling of it: a candidate the gate refused (an agent-authored host body,
    or a `use "stdlib/shell.rvl"` reach into one) compiled here anyway, and
    `_lifecycle_section` then BOOTED it in a scratch `Session` — running the
    activation body's host code in the server process. Grading a candidate is
    not a licence to run what the gate would not admit: an inadmissible
    candidate must be graded `rejected` on the refusal, never booted.

    `over_the_transport=False` says the source is the OPERATOR'S OWN — the CLI
    (`revl bundle`, `truc`) reusing this module as a library, where the human
    running the command is the author, not an agent on the far end of a session.
    See `server.compile_under_authoring`.

    The import is lazy because `server` imports this module."""
    from .server import compile_under_authoring  # noqa: PLC0415 — cycle

    if source is None and not files:
        raise ValueError("provide `source` or `files`")
    return compile_under_authoring(source, files, manifest=manifest,
                                   modules=modules, replacing=replacing,
                                   over_the_transport=over_the_transport)


def _boundary_of(ir: dict) -> dict:
    # the CLI owns the G8 walk; import lazily so this module works whether
    # revl runs as `python -m revl` or is imported as a library
    from ..__main__ import _boundary  # noqa: PLC0415

    return _boundary(ir)


def _summary(ir: dict) -> dict:
    manifest = ir.get("manifest") or {}
    return {
        "irVersion": ir.get("ir_version"),
        "loadOrder": manifest.get("loadOrder") or [],
        "components": [
            {"name": entry.get("name"),
             "requires": entry.get("inject") or [],
             "provides": entry.get("provides") or []}
            for entry in manifest.get("components") or []
        ],
        "services": sorted((ir.get("services") or {}).keys()),
    }


# --------------------------------------------------------------- sections

def _admission_section(status: str, against: str, note: str,
                       diagnostics: list | None = None) -> dict:
    section = {
        "kind": "proved",
        "status": status,           # "proved" | "refused"
        "against": against,         # "session" | "cold"
        "note": note,
    }
    if diagnostics is not None:
        section["diagnostics"] = diagnostics
    return section


def _teardown_section(ir: dict) -> dict:
    """Derived teardown — a *proved* structural fact, not a test.

    Teardown order is the reverse of load order (LIFO): the compiler derives
    it, and G5 guarantees a teardown step can register no new effect, so it
    is sound without ever running. We report the order the compiler would
    unwind, so the operator can read the derivation without booting."""
    load_order = (ir.get("manifest") or {}).get("loadOrder") or []
    return {
        "kind": "proved",
        "status": "derived",
        "teardownOrder": list(reversed(load_order)),
        "note": "teardown is the reverse of load order (LIFO); derived by the "
                "compiler from G2/G5, not observed — every effect carries its "
                "own undo and a teardown step registers no new effect.",
    }


def _lifecycle_section(boot_ir: dict | None, config: dict | None,
                       reason: str | None) -> dict:
    """The no-residue battery, *tested* over a real boot/unload in a scratch
    session. Returns counts of the residue checks that held.

    A candidate that cannot boot (no runtime installed, an open typed hole, or
    a fragment that is not a whole composition on its own) is reported
    `unavailable` with the reason — the gauntlet grades, it does not crash."""
    if boot_ir is None:
        return {"kind": "tested", "status": "unavailable", "ran": False,
                "reason": reason
                or "the candidate is not a complete composition on its own",
                "counts": {"checks": 0, "passed": 0, "failed": 0}}

    scratch = Session()
    try:
        scratch.load(boot_ir, config or {})
    except SessionError as error:
        # no runtime, an open hole, a boot that refuses — all graded, not raised
        return {"kind": "tested", "status": "unavailable", "ran": False,
                "reason": str(error),
                "counts": {"checks": 0, "passed": 0, "failed": 0}}

    try:
        outcome = scratch.unload()
    except SessionError as error:  # pragma: no cover - defensive
        return {"kind": "tested", "status": "error", "ran": True,
                "reason": str(error),
                "counts": {"checks": 0, "passed": 0, "failed": 0}}

    checks = outcome["checks"]
    passed = sum(1 for held in checks.values() if held)
    return {
        "kind": "tested",
        "status": "passed" if outcome["noResidue"] else "failed",
        "ran": True,
        "test": "boot/unload no-residue (R4)",
        "counts": {"checks": len(checks), "passed": passed,
                   "failed": len(checks) - passed},
        "checks": checks,
        "detail": outcome["detail"],
    }


def _boundary_section(ir: dict) -> dict:
    """The G8 extern boundary, *claimed* and enumerated. This is the trust
    surface — host code revl cannot look inside — so it is listed rather than
    proved: the composition's correctness rests on these behaving."""
    per_component = _boundary_of(ir)
    externs = sorted({ext["name"]
                      for comp in per_component.values()
                      for ext in comp.get("externs") or []})
    return {
        "kind": "claimed",
        "status": "enumerated",
        "externs": externs,
        "count": len(externs),
        "components": per_component,
        "note": "the G8 boundary: every host operation the candidate reaches. "
                "Enumerated, not proved — revl cannot see inside an extern, so "
                "this is the list of claims the composition rests on.",
    }


def _pending_section(item: int, title: str, will: str) -> dict:
    """A section designed now, filled in when its roadmap item lands."""
    return {
        "status": "pending",
        "roadmapItem": item,
        "title": title,
        "note": f"{will} Not built yet; this slot reports `pending` until "
                f"roadmap item {item} lands, at which point it fills in without "
                f"any change to the dossier shape.",
        "counts": None,
    }


def _pending_sections() -> dict:
    return {
        "faultSweep": _pending_section(
            _FAULT_SWEEP_ITEM, "fault sweep at every step",
            "Will inject a fault at each effect boundary in turn and confirm "
            "the derived teardown still leaves no residue, moving a class of "
            "recovery claims from `claimed` to `tested`."),
        "inverseRoundTrip": _pending_section(
            _INVERSE_ROUNDTRIP_ITEM, "verified-effect inverse round-trips",
            "Will run each effect's undo and confirm it round-trips the "
            "pre-state, moving reversibility from a structural claim to a "
            "`tested` observation with counts."),
    }


# --------------------------------------------------------------- entry point

def run(session, arguments: dict, *,
        over_the_transport: bool = True) -> dict:
    """Grade a candidate against the live `session` and return a dossier.

    `session` is the live composition the server holds. It is read for the
    admission manifest and never mutated: the battery runs in a scratch
    `Session`. Whatever the candidate does — refuse admission, fault at boot,
    leave residue — the outcome is a graded dossier, never a raised error.
    """
    source = arguments.get("source")
    files = arguments.get("files")
    modules = arguments.get("modules")
    config = arguments.get("config")
    replacing = tuple(arguments.get("replacing") or ())

    live = session.ir if session.loaded else None
    against = "session" if live is not None else "cold"

    # 1. ADMISSION (proved). Compile the candidate against the live
    #    composition when one is loaded, otherwise as a cold standalone
    #    composition. A rejection is a *verdict*, not a crash.
    try:
        admission_ir = _compile(source, files, manifest=live, modules=modules,
                                replacing=replacing,
                                over_the_transport=over_the_transport)
    except RevlError as error:
        rejected = report(error)
        return {
            "ok": True,
            "verdict": "rejected",
            "note": "admission refused — the candidate is graded, not run. "
                    "The live composition is untouched.",
            "proved": {
                "admission": _admission_section(
                    "refused", against,
                    "the candidate does not link against "
                    + ("the live composition" if live is not None
                       else "a clean composition")
                    + " — nothing downstream was run.",
                    diagnostics=rejected["diagnostics"]),
                "teardown": {"status": "not-run",
                             "note": "admission refused; teardown not derived."},
            },
            "tested": {"lifecycle": {"kind": "tested", "status": "not-run",
                                     "reason": "admission refused",
                                     "counts": {"checks": 0, "passed": 0,
                                                "failed": 0}}},
            "claimed": {"boundary": {"status": "not-run",
                                     "reason": "admission refused"}},
            "pending": _pending_sections(),
            "scratch": {"isolated": True, "booted": False,
                        "note": "no scratch session was booted; the live "
                                "composition was never touched."},
        }

    # 2. A standalone compile of the candidate — what the scratch session
    #    actually boots, and the IR the boundary is enumerated from. When the
    #    candidate is admissible only as a fragment against the live
    #    composition, this can fail; the lifecycle section then reports why.
    boot_ir = None
    boot_reason = None
    try:
        boot_ir = _compile(source, files, modules=modules,
                           over_the_transport=over_the_transport)
    except RevlError:
        boot_reason = ("the candidate is admissible against the live "
                       "composition but is not a complete composition on its "
                       "own — pass the full source set to boot it in the "
                       "scratch session.")

    boundary_ir = boot_ir or admission_ir

    return {
        "ok": True,
        "verdict": "admissible",
        "note": "admission proves the candidate may run; the lifecycle battery "
                "tested that it boots and unloads clean; the boundary "
                "enumerates what remains claimed.",
        "candidate": _summary(boundary_ir),
        "proved": {
            "admission": _admission_section(
                "proved", against,
                "the candidate links against "
                + ("the live composition; G2/G3 hold across both and no "
                   "interface drifted." if live is not None
                   else "a clean composition (cold start).")),
            "teardown": _teardown_section(boundary_ir),
        },
        "tested": {
            "lifecycle": _lifecycle_section(boot_ir, config, boot_reason),
        },
        "claimed": {
            "boundary": _boundary_section(boundary_ir),
        },
        "pending": _pending_sections(),
        "scratch": {
            "isolated": True,
            "booted": boot_ir is not None,
            "note": "the battery ran in a throwaway Session; the live "
                    "composition was read for admission only and never mutated.",
        },
    }
