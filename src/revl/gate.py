"""The embeddable admission gate as a library surface (roadmap item 332).

A host program embeds revl and gets the SAME verdict, and the same downstream
guarantees, that `revl compile` and `revl run` give today, without spawning a
subprocess or standing up the item-144 bridge service. This module is the
DECLARED, versioned public surface over machinery that already ships in the
wheel (`revl.compiler.compile_source` and `revl.mcp.session.Session`): it adds a
doorway with a name, not a new engine.

Two layers, split on purity (docs/design/332-embeddable-gate-api.md):

* LAYER 1, the verdict surface (`admit`, `admit_into`, `compile_to`,
  `gate_version`). Pure functions of their arguments: no disk, no clock, no live
  state. Strings in, structured strings out, no host object graph on the
  boundary. This is the tier-portable layer; on py it is backed by the full
  reference compiler.
* LAYER 2, the session surface (`Gate`). Stateful and address-space-bound: a
  live composition an embedder loads, admits per-turn sources into, calls,
  commits, and aborts, with the item-245/246/330 guarantees intact because the
  facade delegates every operation to the `Session` member that implements it.
  v1 is synchronous and single-gate-per-process by design; the async
  multi-tenant story is deferred to items 333/334.

The security clause (load-bearing): the gate must NEVER admit what the reference
`revl` refuses. Layer-1 `admit` on py IS `compile_source`, so its accept/refuse
verdict and its refusal message are the reference compiler's, verbatim. A native
(rust) layer-1 gate is a separate, larger deliverable (the `revl-gate` crate)
and is NOT part of this module; see the module's `gate_version().frontier`.

The fail-closed contract: a class-(c) crossing (an irreversible emission with no
checked inverse) needs a human yes. Embedded, there is no operator channel
unless the host supplies one, so `Gate(approver=...)` takes a callback. With a
policy on and NO approver, a class-(c) crossing REFUSES (`GateRefused`), never
silent auto-approval. A host that exits mid-session leaves the WAL as the
recovery story; `revl.gate.recover` (re-exported from `revl.recovery`) replays
the inverses at next boot. The gate cannot and does not claim to confine its
host; its guarantees govern the ADMITTED code.
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Mapping

# Re-export the WAL recovery entry point so an embedder whose host crashed
# mid-session can replay inverses to a clean tree at next boot WITHOUT reaching
# into `revl.recovery` (the embedding-contract "mechanized fallback").
from .recovery import recover  # noqa: F401 — re-exported on the public surface

__all__ = [
    "Verdict",
    "Emit",
    "admit",
    "admit_into",
    "compile_to",
    "gate_version",
    "Gate",
    "GateError",
    "GateRefused",
    "AdmitResult",
    "ProposeResult",
    "Handle",
    "recover",
]

# The semver of the GATE SURFACE itself (`gate_version().api`). Bumped by
# surface changes only, independent of the language/package version. First
# public release of the item-332 surface.
GATE_API_VERSION = "1.0.0"


def _language_version() -> str:
    """The revl language/package version the gate admits (`gate_version()`
    .language). Read from the installed distribution metadata, falling back to
    the in-repo package version when running from a checkout with no install."""
    try:
        from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415
        try:
            return version("revl")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 — metadata is a convenience, never fatal
        pass
    return "2.0.0"


# ---------------------------------------------------------------------------
# Layer 1: the verdict surface (pure, tier-portable, disk-pure).
# ---------------------------------------------------------------------------


class Verdict:
    """A structured admission verdict. The one shape designed tier-agnostically
    (design "The Verdict is structured; the message is verbatim"): `admitted`
    and `code` are API (codes are append-only; an existing code never changes
    meaning), while `message` text is the reference compiler's why-trace
    VERBATIM at this version and is not promised stable across versions.

    Strings only on the boundary: no `Any`, no host object graph, so the same
    shape crosses a crate ABI (335/336) or a wasm-component boundary unchanged.
    """

    __slots__ = ("admitted", "code", "message")

    def __init__(self, admitted: bool, *, code: str | None = None,
                 message: str | None = None) -> None:
        self.admitted = bool(admitted)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"admitted": self.admitted, "code": self.code,
                "message": self.message}

    @classmethod
    def from_native(cls, wire: str) -> "Verdict":
        """Parse the native gate's internal `"<TAG>|<message>"` protocol
        (`selfhost/compile.rvl`) into the structured shape, message verbatim.
        An empty string admits. Split at the FIRST `|` only, so a message
        carrying `|` is preserved intact. This is the shared parser the design
        fixes tier-agnostically so a native (rust) package produces the SAME
        `{admitted, code, message}` through the SAME shape; the py package does
        not use it (it is backed by structured `RevlError`), but it is part of
        the fixed contract and lives here so every tier splits identically."""
        if wire == "":
            return cls(True)
        tag, _, message = wire.partition("|")
        return cls(False, code=tag or None, message=message or None)

    def __repr__(self) -> str:
        if self.admitted:
            return "Verdict(admitted=True)"
        return f"Verdict(admitted=False, code={self.code!r})"


class Emit:
    """A `compile_to` outcome: the `verdict` plus, on admission, the emitted
    target `output` source. Kept as two fields so a refusal and an emission
    cannot be confused by string sniffing (the selfhost header-prefix
    disambiguation stays internal)."""

    __slots__ = ("verdict", "output")

    def __init__(self, verdict: "Verdict", output: str | None = None) -> None:
        self.verdict = verdict
        self.output = output

    def as_dict(self) -> dict:
        return {"verdict": self.verdict.as_dict(), "output": self.output}


def _verdict_from_error(error: Exception) -> "Verdict":
    """A refusal `Verdict` carrying the reference compiler's diagnostic
    verbatim. `str(error)` is exactly what `revl compile` prints after its
    `error: ` prefix, so the gate's message is the reference message, byte for
    byte."""
    return Verdict(False, code=getattr(error, "code", None),
                   message=str(error))


def admit(source: str) -> "Verdict":
    """The frontend gate: a program the checker refuses cannot be compiled, and
    a draft with an open obligation may compile but may never run.

    Pure and disk-pure (`compile_source` reads nothing from and writes nothing
    to disk). Returns an admitted `Verdict` when the reference compiler accepts
    `source` AND the admission gate lets it run, else a refusal `Verdict` whose
    `code`/`message` are the reference compiler's, verbatim. The security clause
    (load-bearing): this delegates to the reference `compile_source` AND applies
    `refuse_admission` — the SAME admission gate `admit_into`
    (`compile_source(..., manifest=...)`), `Gate.load` (`Session.load`), and
    `revl run` apply — so it can never admit what `revl` refuses. Without that
    second call `admit` would accept a draft the reference refuses to run (a
    typed hole checks so the rest of the draft can be checked, but it has no
    implementation), which is exactly the false-admit the admission gate exists
    to close (docs/holes.md)."""
    from .compiler import compile_source  # noqa: PLC0415 — lazy, keeps layer 1 light
    from .errors import RevlError  # noqa: PLC0415
    from .holes import refuse_admission  # noqa: PLC0415
    try:
        document = compile_source(source)
        refuse_admission(document)
    except RevlError as error:
        return _verdict_from_error(error)
    return Verdict(True)


def admit_into(source: str, manifest: Mapping[str, Any]) -> "Verdict":
    """Runtime admission: admit `source` INTO a running composition described by
    `manifest` (a previously compiled IR document, or its `manifest`+`services`).
    G2/G3 span the running manifest, so a candidate that would collide with the
    live composition is refused with the collision's why-trace. This is the same
    `compile_source(..., manifest=...)` call the item-144 service and truc's
    gatekeeper make."""
    from .compiler import compile_source  # noqa: PLC0415
    from .errors import RevlError  # noqa: PLC0415
    try:
        compile_source(source, manifest=dict(manifest))
    except RevlError as error:
        return _verdict_from_error(error)
    return Verdict(True)


def compile_to(source: str, tier: str) -> "Emit":
    """Verdict plus emitted target source for `tier`. On admission `output` is
    the emitted source; on refusal `output` is None and the verdict carries the
    reference refusal. An unknown tier is a control verdict (`UNKNOWN_TIER`),
    fail closed, so a caller can never mistake a bad tier for an emission.

    Backed by the reference emitters, so the covered tiers are the reference
    backends (`py`, `ts`, `rust`, `java`, `wasm`, `go`). The native
    self-host `compile_to` (frontier-scoped, py/rust) is the crate's story,
    deferred here."""
    from .compiler import compile_source  # noqa: PLC0415
    from .errors import RevlError  # noqa: PLC0415

    if _tier_dir(tier) is None:
        return Emit(Verdict(False, code="UNKNOWN_TIER",
                            message=(f"unknown tier {tier!r} "
                                     f"(known: {', '.join(_KNOWN_TIERS)})")))
    try:
        ir = compile_source(source)
    except RevlError as error:
        return Emit(_verdict_from_error(error))
    output = _emit_for_tier(ir, tier)
    return Emit(Verdict(True), output=output)


# The tiers `compile_to` can emit on py (reference-backed): the CLI short name
# (`revl run --backend`) mapped to its `backends/<dir>` directory. Both the short
# name and the directory name are accepted.
_TIER_DIRS = {
    "py": "python", "python": "python",
    "ts": "typescript", "typescript": "typescript",
    "rust": "rust", "java": "java", "wasm": "wasm", "go": "go",
}
_KNOWN_TIERS = ("py", "ts", "rust", "java", "wasm", "go")


def _tier_dir(tier: str) -> str | None:
    """The `backends/<dir>` directory for a tier name, or None if unknown."""
    return _TIER_DIRS.get(tier)


def _emit_for_tier(ir: dict, tier: str) -> str:
    """Load the reference emitter for `tier` and emit `ir` to target source.
    The emitter modules live under `backends/<dir>/emit.py` and expose a
    module-level `emit(ir) -> str`; loaded the same way `tools/conformance.py`
    loads them, so what the gate emits is real reference emitter output."""
    import importlib.util  # noqa: PLC0415

    from ._paths import backends_root  # noqa: PLC0415
    tier_dir = _tier_dir(tier)
    path = backends_root() / tier_dir / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_gate_{tier_dir}_emit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def gate_version() -> dict:
    """Three values a host can branch on (design "Versioning"):

    * `api`: semver of the gate surface itself (bumped by surface changes only);
    * `language`: the revl language/package version the gate admits;
    * `frontier`: an identifier of the gate's COVERED surface, so an embedder
      (and later item 337's seam re-admission) can detect that two gates cover
      different surfaces before trusting their agreement.

    On py, layer 1 is backed by the FULL reference compiler, so the covered
    surface is the whole reference language at `language`; the frontier is
    pinned as `reference-full:<language>`. A native (rust) gate would instead
    pin the self-host corpus frontier (`selfhost/compile.rvl`'s covered rows);
    that gate is a separate deliverable and not this module."""
    language = _language_version()
    return {
        "api": GATE_API_VERSION,
        "language": language,
        "frontier": f"reference-full:{language}",
    }


# ---------------------------------------------------------------------------
# Layer 2: the session surface (stateful, address-space-bound).
# ---------------------------------------------------------------------------


class GateError(RuntimeError):
    """The one boundary exception type of the layer-2 facade. The facade raises
    `revl.gate` types only, never internal `SessionError`/`RevlError` classes,
    so a host's compatibility surface is the facade, not the internals."""


class GateRefused(GateError):
    """A fail-closed refusal at the embedding boundary: a class-(c) crossing
    that no approver answered yes to (no approver configured, or the approver
    declined). Carries the `ticket` the two-step would have relayed to a human,
    so the host can log or re-present it. Nothing fired."""

    def __init__(self, message: str, ticket: dict | None = None) -> None:
        super().__init__(message)
        self.ticket = ticket


class Handle:
    """A callable handle onto an admitted per-turn composition. Its `call`
    routes the turn's provided operations through the SAME choke point a
    top-level `Gate.call` takes (classification, the item-246 approval decision,
    the approver seam), so an admitted turn cannot escape the session's
    commit/abort. The facade hands back a Handle, never the emitted artifact:
    the module, driver, and fibers are not exported."""

    def __init__(self, gate: "Gate", inner: Any) -> None:
        self._gate = gate
        self._inner = inner
        self.keys = tuple(getattr(inner, "keys", ()) or ())

    def call(self, key: str, method: str, args: list | None = None) -> dict:
        if key not in self.keys:
            raise GateError(
                f"key {key!r} is not one the admitted turn provides "
                f"(provides: {', '.join(self.keys) or 'none'})")
        try:
            return self._gate._invoke_with_approval(
                self._gate._session.call, key, method, args)
        except _session_error() as error:
            # item 416e: the facade's boundary type is `GateError`. Without this
            # an embedder catching only `GateError` sees a raw internal
            # `SessionError` cross the library edge, from the two `call` methods
            # that lacked the wrapper every sibling carries.
            raise GateError(str(error)) from error


class AdmitResult:
    """The outcome of `Gate.admit` for a per-turn source. A refusal
    (`admitted` False) carries the repair signal as `message`/`code` and never
    touches the running composition; an admission carries a `handle` whose
    crossings register into THIS session's 245 frame. Never the artifact."""

    __slots__ = ("admitted", "code", "message", "keys", "handle")

    def __init__(self, admitted: bool, *, code: str | None = None,
                 message: str | None = None, keys: tuple[str, ...] = (),
                 handle: "Handle | None" = None) -> None:
        self.admitted = bool(admitted)
        self.code = code
        self.message = message
        self.keys = tuple(keys)
        self.handle = handle

    def as_dict(self) -> dict:
        return {"admitted": self.admitted, "code": self.code,
                "message": self.message, "keys": list(self.keys)}


class ProposeResult:
    """The outcome of `Gate.propose` (roadmap item 334): admit an AGENT-authored
    component under the untrusted-author profile AND hot-swap a running
    composition to it, in one process. Three terminal shapes, all data (a refusal
    is the repair signal, never a raised error the loop cannot catch):

    * `admitted` False, `code` `HALTED` — the session was E-STOPPED (item 443).
      Nothing was compiled and nothing was swapped. A halt DOMINATES: it is not
      reported as a refusal of the candidate (the candidate was never judged) and
      never as a revert (there is no gen N still serving to revert to — the
      instance is dead and every registered entry is stranded). The way back is
      `revl recover`, never another `propose`.
    * `admitted` False — the candidate was REFUSED before it ever became live:
      the forbidden-grant rule fired (`code` `FORBIDDEN_GRANT`), or the
      self-extension compile refused it (`code`/`message` the reference
      compiler's why-trace, verbatim — including the `G9` realm-placement
      refusal, item 334 slice 2). The running composition is UNTOUCHED.
    * `admitted` True, `swapped` False, `reverted` True — the candidate admitted
      but FAILED TO ACTIVATE (its activation raised/left a fiber FAILED, or a
      requirement was unmet -> PENDING, or a declared provide never resolved).
      The post-activation health gate rejected it and the swap REVERTED to gen N;
      the process keeps serving gen N (`code` `SWAP_REVERTED`).
    * `admitted` True, `swapped` True — the swap took: gen N+1 is live, `keys`
      are what it ACTUALLY provides (the live `providedKeys`, not the candidate's
      declarations — a declared key with no live provider is never reported), and
      `state` is the resulting session state."""

    __slots__ = ("admitted", "swapped", "reverted", "code", "message", "keys",
                 "state")

    def __init__(self, admitted: bool, *, swapped: bool = False,
                 reverted: bool = False, code: str | None = None,
                 message: str | None = None, keys: tuple[str, ...] = (),
                 state: dict | None = None) -> None:
        self.admitted = bool(admitted)
        self.swapped = bool(swapped)
        self.reverted = bool(reverted)
        self.code = code
        self.message = message
        self.keys = tuple(keys)
        self.state = state

    def as_dict(self) -> dict:
        return {"admitted": self.admitted, "swapped": self.swapped,
                "reverted": self.reverted, "code": self.code,
                "message": self.message, "keys": list(self.keys)}


# The service names that reach the decider — the admit/swap/owner-state control
# surface (item 334 FORBIDDEN-GRANT rule). Granting any of these to an untrusted
# candidate would hand it the loop's own re-entrant-admit plumbing: the stdlib
# `Admission` service (`stdlib/admit.rvl`, provided by component `AdmitGate`)
# emits `host_admit`, whose `@py` body calls `revl.mcp.admit_bridge.admit`, so a
# candidate granted it reaches the decider through a granted host body with NO
# `extern` of its own — the non-extern path the untrusted profile alone does not
# close. `propose` REJECTS a granted set naming any of these, before compiling,
# which is how "re-entrant propose is deferred" is ENFORCED rather than merely
# documented.
#
# The rule inspects the GRANTED SET only, so it is not on its own a bound on
# reach: a candidate that reaches the decider WITHOUT naming it here is R2's
# problem. That is why R2 resolves its internal-provision exemption by binding
# KEY and not by service name — keying it on the name let a candidate declare a
# decoy provider of `Admission` under an unused key, which exempted its real
# `requires admission: Admission` from the allowlist entirely and bound it to
# the live `admission` key of the running `AdmitGate`, with the granted set
# never naming a decider service at all (`admit_profile.check_allowlist`).
_DECIDER_SERVICES = frozenset({"Admission", "AdmitGate"})


# The process-global single live gate (v1). A second live `Gate` in one process
# is refused loudly at construction, the honest spelling of today's
# process-global admit-bridge/session-owner binds (`admit_bridge._SESSION`,
# `runtime._SESSION_OWNER`). Lifting this is owner-scoping work the arc does not
# need here (337's mesh runs one gate per tier process).
#
# The slot is a WEAK reference, not a strong one: a Gate dropped without
# `close()` (`g = Gate(); del g`) must not soft-brick the process by pinning the
# slot for a referent that is already gone. A weak slot lets a collected gate
# free the slot on its own, so the single-gate invariant tracks the gate that is
# actually LIVE, not merely the last one ever constructed.
_ACTIVE_GATE: "weakref.ref[Gate] | None" = None


def _live_gate() -> "Gate | None":
    """The gate currently holding the single-gate slot, or None if the slot is
    free — either never taken, released by `close()`, or vacated because the
    holder was dropped without `close()` and has since been collected."""
    if _ACTIVE_GATE is None:
        return None
    return _ACTIVE_GATE()


class Gate:
    """A live composition an embedder drives: `load`, `admit`, `call`, `commit`,
    `abort`, `unload`. Every operation delegates to the `Session` member that
    already implements it, so behavioral identity with `revl run` and
    `revl mcp serve` is definitional.

    Constructor knobs are the embedding seams:

    * `wal_path`: where the typed-approval / session WAL is written, so a host
      that exits mid-session has a WAL to `recover` from.
    * `approval_policy`: `"auto"` enables the item-246 gate (class (a) silent,
      (b) enumerate at commit, (c) prompt); `None` (default) leaves every call
      ungated, byte-identical to today.
    * `approver`: a callback `ticket -> bool` the facade calls for every
      class-(c) crossing. With a policy on and NO approver, class-(c) REFUSES
      (fail closed).
    * `record`: force WAL recording. Defaults to on whenever a policy is set
      (the policy requires a durable approval spend).

    v1 is synchronous (it owns its event loop) and single-gate-per-process. The
    async host-loop facade and multi-gate owner scoping are deferred to
    items 333/334, named here, not solved.
    """

    def __init__(self, *, wal_path: str | None = None,
                 approval_policy: str | None = None,
                 approver: Callable[[dict], bool] | None = None,
                 record: bool | None = None) -> None:
        global _ACTIVE_GATE
        if _live_gate() is not None:
            raise GateError(
                "a Gate is already live in this process. v1 is single-gate-per-"
                "process (item 332): the admit crossing and the session owner "
                "are process-global binds, so a second live Gate would misbind "
                "silently. Close the first gate (`gate.close()`) before "
                "constructing another. Multi-gate owner scoping is deferred to "
                "items 333/334.")
        from .mcp.session import Session  # noqa: PLC0415 — lazy: no cordis until load

        self._session = Session()
        self._session.approval_policy = approval_policy
        if wal_path is not None:
            self._session._wal_path = wal_path
        self._approver = approver
        self._wal_path = wal_path
        # a policy needs a durable approval spend (session.load refuses a policy
        # load without recording), so default recording on under a policy.
        self._record = record if record is not None else (
            approval_policy is not None)
        self._loaded = False
        _ACTIVE_GATE = weakref.ref(self)

    # -- lifecycle ---------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self, sources: str | Mapping[str, str],
             config: Mapping[str, Any] | None = None) -> dict:
        """Boot the base composition (admission IS load). `sources` is either a
        single source string or a mapping of `{path: source}` compiled as
        co-root files (components are never `use`-imported). Returns the session
        state. A class-(c) ACTIVATION crossing under a policy is resolved through
        the approver, or refuses fail-closed with no approver."""
        if self._loaded:
            raise GateError("a composition is already loaded; unload it first")
        ir = self._compile_base(dict(sources) if not isinstance(sources, str)
                                else sources)
        try:
            state = self._invoke_with_approval(
                self._session.load, ir, dict(config) if config else None,
                self._record)
        except _session_error() as error:
            raise GateError(str(error)) from error
        self._loaded = True
        return state

    def _compile_base(self, sources: str | dict) -> dict:
        """Compile the base composition. `sources` is a source string, or a
        mapping of `{path: text}` co-root files. A mapping VALUE of `None` marks
        a root read from disk (a real stdlib file composed alongside in-memory
        drafts), exactly as `compile_files(paths, sources=...)` allows."""
        from .compiler import compile_files, compile_source  # noqa: PLC0415
        from .errors import RevlError  # noqa: PLC0415
        import os  # noqa: PLC0415
        try:
            if isinstance(sources, str):
                return compile_source(sources)
            paths = [os.path.abspath(p) for p in sources]
            virtual = {os.path.abspath(p): text for p, text in sources.items()
                       if text is not None}
            return compile_files(paths, sources=virtual or None)
        except RevlError as error:
            raise GateError(str(error)) from error

    def admit(self, source: str, granted: list[str] | tuple[str, ...] = (),
              *, modules: Mapping[str, str] | None = None) -> "AdmitResult":
        """Admit a model-authored per-turn `source` reaching only the `granted`
        service names, into the running composition (the item-330 crossing). A
        refusal is the repair signal handed back as data (running system
        untouched); an admission hands back a `Handle`, never the artifact.

        Routed through the approver seam, exactly as `load`/`swap`/`call` are: a
        turn whose ACTIVATION body carries a class-(c) crossing raises its ticket
        at wire time, before the body runs, and the embedder answers it (or the
        gate refuses fail-closed and nothing is wired). Without the routing that
        ticket would escape `Gate.admit` unhandled."""
        from functools import partial  # noqa: PLC0415
        try:
            verdict = self._invoke_with_approval(partial(
                self._session.admit, source, list(granted),
                modules=dict(modules) if modules else None))
        except _session_error() as error:
            raise GateError(str(error)) from error
        if not verdict.admitted:
            return AdmitResult(False, code=verdict.code, message=verdict.message)
        return AdmitResult(True, keys=verdict.keys,
                           handle=Handle(self, verdict.handle))

    def propose(self, source: str,
                granted: list[str] | tuple[str, ...] = (),
                *, providers: Mapping[str, str] | None = None) -> "ProposeResult":
        """Admit an AGENT-authored component under the untrusted-author profile
        AND hot-swap the running composition to it — the item-334 self-extending
        crossing. The one verb neither `admit` (additive, refuses replacement)
        nor `swap` (replaces, but runs the full compiler) occupies: replace a
        running component with agent code that carries NO opaque host body.

        Three parts, wired in order, each load-bearing:

        0. FORBIDDEN-GRANT (enforced, before any compile): a `granted` set naming
           a gate/session/admit-control service (`Admission`/`AdmitGate`, the
           `host_admit`-reaching decider) is REJECTED. This ENFORCES "re-entrant
           propose is deferred": a granted decider service is the non-extern path
           to the decider that the untrusted profile alone does not block.

        1. STANDALONE decision compile: `source` (the agent-authored candidate) is
           compiled under `untrusted_author(granted)` on its OWN, NOT against the
           running manifest and NOT via `Session.admit` (whose verdict has no
           `ir` and which refuses replacement with G2). The profile forbids a new
           `extern`/host-block and bounds reach to `granted`, so the admitted
           candidate holds no host code and can name no Python object. A refusal
           is the repair signal, returned as data, the live composition untouched.

        2. HEALTH-GATED swap: the candidate must be a SELF-CONTAINED composition
           (partial single-component replacement is not what swap does — it
           replaces the whole composition). It brings its own granted providers:
           `source` declares the service INTERFACES it needs and its own
           consumer/tool component (pure revl), and the trusted `providers`
           (a `{path: source}` map, operator-supplied host code that provides
           those services) are composed as co-roots. That full composition is
           handed to `Session.swap`, whose item-334 post-activation health gate
           reverts to gen N if the successor fails to activate. `providers` may be
           omitted for a fully self-contained pure-revl candidate.

        The security boundary (G8, stated narrowly): revl guarantees the AGENT
        introduced nothing opaque (the decision compile refused any `extern`,
        any transitive host-extern reach, and any ungranted reach) and that the
        swap is a revertible envelope. It does NOT sandbox the trusted
        `providers`' host bodies; those are exactly as trusted as when the
        operator granted them. New agent-introduced host code is out of scope for
        autonomous self-extension (it needs the operator-gated trusted swap or
        item-411 confinement)."""
        if not self._loaded:
            raise GateError("nothing is loaded; load a base composition first")
        granted = list(granted)

        # -1. HALT DOMINANCE (item 443 + 334 slice 2) — checked FIRST, ahead of
        #     the forbidden-grant rule and any compile, because a halt is not one
        #     more verdict to be weighed against the candidate's: it dominates.
        #     `Session.swap` already refuses under a halt, so without this the
        #     halt reached the loop as the `SWAP_REVERTED` arm — which asserts
        #     three things that are all FALSE after an E-Stop: that the candidate
        #     was judged (it never was), that gen N is intact (its entries are
        #     STRANDED, not discharged), and that the process is still serving
        #     (the instance is dead). That mislabel is not cosmetic: SWAP_REVERTED
        #     is the RETRY-shaped verdict, the one a self-extension loop answers
        #     by generating a better candidate, so a halted gate would be proposed
        #     at forever instead of reconciled. Refuse as a halt, and say where
        #     the way back is.
        if self._session.halted:
            return ProposeResult(
                False, code="HALTED",
                message=(
                    "propose refused: this session was E-STOPPED (item 443) and "
                    "the instance is dead. Nothing was compiled and nothing was "
                    "swapped; the halt dominates, so this is not a verdict on "
                    "the candidate and not a revert to a running generation — "
                    "every registered entry is stranded, owed and not "
                    "discharged. There is no resume and no better candidate: "
                    "reconcile with `revl recover --wal <file>`, or `unload` "
                    "(which strands rather than unwinds) and start a fresh "
                    "session."))

        # 0. FORBIDDEN-GRANT — before any compile, independent of the operator.
        forbidden = sorted(set(granted) & _DECIDER_SERVICES)
        if forbidden:
            return ProposeResult(
                False, code="FORBIDDEN_GRANT",
                message=(
                    f"propose refused: the granted set names a gate/session/"
                    f"admit-control service ({', '.join(forbidden)}). Granting a "
                    f"decider service would let the untrusted candidate reach "
                    f"`host_admit`/swap/owner state through a granted host body "
                    f"with no `extern` of its own — the non-extern path the "
                    f"untrusted profile does not close. Re-entrant propose is "
                    f"deferred and this rule enforces it (item 334); drop the "
                    f"decider service from `granted`."))

        from .admit_profile import AdmissionProfile  # noqa: PLC0415
        from .compiler import compile_source  # noqa: PLC0415
        from .errors import RevlError  # noqa: PLC0415

        # 1. STANDALONE decision compile under the untrusted-author profile. This
        #    is the DECISION: it refuses a new extern (G8, `check_no_extern`), a
        #    transitive host-extern reach through the composed provider modules
        #    (G8, `check_no_host_extern_reach`), an ungranted reach (R2,
        #    `check_allowlist`), or a self-minted declassifier (G9), all as a
        #    compile refusal returned as data. The trusted `providers` are handed
        #    in as `modules=` so the candidate can compose the granted SERVICES
        #    they declare, while any attempt to reach their host EXTERNS directly
        #    (the import-and-call bypass) is refused across the whole transitive
        #    closure. It is compiled STANDALONE (no `manifest=`): the candidate
        #    reaches only what it is explicitly given, never the running base's
        #    other ambient services.
        #    Under `self_extension`, not `untrusted_author`: the delta is the
        #    item-334 slice-2 realm refusal. `untrusted_author` bounds WHAT the
        #    candidate reaches; the swap makes it the running composition, so the
        #    proposal also has to be bounded in WHERE it sits — a realm is the
        #    address the item-246/251 approval policy scopes its standing
        #    approvals and auto-approve rules by, and a candidate that picks its
        #    own realm picks which of them cover its class-(c) crossings. The
        #    component-glob half of that scope is no bound on an author who names
        #    its own components, so the realm was the only half left.
        profile = AdmissionProfile.self_extension(granted)
        try:
            compile_source(source, "<candidate>.rvl",
                           modules=dict(providers) if providers else None,
                           profile=profile)
        except RevlError as error:
            return ProposeResult(False, code=getattr(error, "code", None),
                                 message=str(error))

        # 2. Build the SELF-CONTAINED composition for the TRANSITION (the agent
        #    source validated above, plus the trusted granted providers), then
        #    hand it to the health-gated swap.
        try:
            ir = self._compile_candidate_composition(source, providers)
        except RevlError as error:
            return ProposeResult(False, code=getattr(error, "code", None),
                                 message=str(error))

        try:
            state = self._invoke_with_approval(self._session.swap, ir)
        except _session_error() as error:
            # A halt can engage BETWEEN the check above and here, in one thread,
            # with no concurrency: the swap runs the item-246 activation gate,
            # which calls the embedder's `approver` callback, which is host code
            # that may well answer a class-(c) prompt by hitting the E-Stop. That
            # is the honest ordering of the race, and the halt wins it: re-read
            # the latch and report HALTED rather than dressing a dead session up
            # as a healthy gen N that reverted.
            if self._session.halted:
                return ProposeResult(
                    False, code="HALTED",
                    message=(
                        f"propose refused: the session was E-STOPPED during the "
                        f"proposal (item 443) — {error}. The halt dominates the "
                        f"swap verdict: nothing was reverted, because an E-Stop "
                        f"runs nothing and unwinds nothing. Every entry "
                        f"registered up to the halt is STRANDED; read the "
                        f"inventory with `estop_report()` and reconcile with "
                        f"`revl recover --wal <file>`."))
            # the post-activation health gate (or a migration reject) rolled the
            # swap back: gen N is intact and still serving. Report it as data.
            return ProposeResult(True, swapped=False, reverted=True,
                                 code="SWAP_REVERTED", message=str(error))

        # `keys` is what gen N+1 ACTUALLY provides, read off the swap's own
        # state report (`providedKeys` -> `driver.resolved_keys()`, run.py:878),
        # never re-derived from the candidate IR's `provides` declarations. The
        # IR-derived form lied: a declaration whose provider is a `spawn` target
        # never reaches ROOT, so the loop was handed a key it could not call —
        # a refusal-shaped condition dressed as an admission. Reading the single
        # live source means the report and the health gate cannot disagree.
        keys = tuple(state.get("providedKeys") or ())
        return ProposeResult(True, swapped=True, keys=keys, state=state)

    def _compile_candidate_composition(self, source: str,
                                       providers: Mapping[str, str] | None
                                       ) -> dict:
        """Compile the candidate as a COMPLETE self-contained composition for the
        swap transition: the agent `source` as the root, plus any trusted
        `providers` as co-roots. Compiled WITHOUT the untrusted profile — the
        agent source was already admitted under the profile in the decision
        compile (step 1), and the providers are trusted operator-supplied host
        code the candidate is allowed to compose. `swap` replaces the WHOLE
        composition, so the returned document's `components` must span every
        provider the candidate requires or the successor faults and reverts."""
        from .compiler import compile_files, compile_source  # noqa: PLC0415
        import os  # noqa: PLC0415
        if not providers:
            # a fully self-contained pure-revl candidate: the source IS the whole
            # composition (it composes only providers it declares itself).
            return compile_source(source, "candidate.rvl")
        cand_abs = os.path.abspath("candidate.rvl")
        virtual = {cand_abs: source}
        paths = [cand_abs]
        for path, text in providers.items():
            abs_path = os.path.abspath(path)
            virtual[abs_path] = text
            paths.append(abs_path)
        return compile_files(paths, sources=virtual)

    def call(self, key: str, method: str, args: list | None = None) -> dict:
        """Invoke a provided operation on the running composition, gated by the
        item-246 policy and the approver seam."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        try:
            return self._invoke_with_approval(
                self._session.call, key, method, args)
        except _session_error() as error:
            # item 416e: see `Handle.call`. `GateRefused` is a `GateError`
            # subclass and `ApprovalRequired` is resolved inside the loop, so
            # only a genuine session refusal (an unknown key, a method that is
            # not callable, a faulted crossing) reaches here, and it fails
            # closed either way — this is a boundary-type fix, not a behavior
            # change.
            raise GateError(str(error)) from error

    def commit(self) -> dict:
        """Commit the session: the audited two-step (enumerate, then confirm the
        enumerated manifest by hash), driven as one facade call. What fires is
        exactly what was enumerated."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        try:
            manifest = self._session.commit()
            result = self._session.commit_confirm(manifest["hash"])
        except _session_error() as error:
            raise GateError(str(error)) from error
        self._loaded = False
        return result

    def abort(self) -> dict:
        """Abort the session: drop the deferral queue, replay the witnessed
        inverses, revert residue-free (item 245, Decision 5)."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        try:
            result = self._session.abort()
        except _session_error() as error:
            raise GateError(str(error)) from error
        self._loaded = False
        return result

    def estop(self, reason: str = "operator halt",
              operator: str | None = None) -> dict:
        """E-STOP this gate: the operator's emergency button (item 443).

        Exposed on the facade because item 334's guarantee is only worth the
        embedder having it. `propose` promises that a halt dominates a
        self-extension, and a promise about a button nobody can reach is not a
        guarantee — the embedded host is the only operator a `Gate` has.

        This is NOT `abort`. `abort` is a verdict on the work: it drops the
        deferral queue, replays every witnessed inverse and proves a clean world.
        `estop` stops dispatching NEW crossings immediately, runs NOTHING, and
        reports what was in flight; every registered entry is left STRANDED and
        every acquired handle stays held. The session is dead afterwards —
        `propose`, `call`, `commit` and `abort` all refuse, `unload` strands
        rather than unwinds, and the way back is `recover`, never a resume."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        try:
            return self._session.estop(reason, operator=operator)
        except _session_error() as error:
            raise GateError(str(error)) from error

    def estop_report(self) -> dict:
        """The halt inventory (item 443): what the halt could NAME when it
        engaged, and what the unwind has stranded since. Never `clean` — an
        E-Stop violates R4 by design and says so. Reads nothing into the world,
        so it is safe on a dead session and is the thing to read BEFORE deciding
        how to reconcile."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        try:
            return self._session.estop_report()
        except _session_error() as error:
            raise GateError(str(error)) from error

    def unload(self) -> dict:
        """Tear down and report the residue checks. Under a session owner a
        plain unload is the implicit terminal commit (the pre-245 default)."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        try:
            result = self._session.unload()
        except _session_error() as error:
            raise GateError(str(error)) from error
        self._loaded = False
        return result

    def close(self) -> None:
        """Release the process-global single-gate slot so another Gate can be
        constructed. Unloads a still-live composition first (the quiet default
        unload). Idempotent."""
        global _ACTIVE_GATE
        if self._loaded:
            try:
                self._session.unload()
            except Exception:  # noqa: BLE001 — best-effort teardown on close
                pass
            self._loaded = False
        if _live_gate() is self:
            _ACTIVE_GATE = None

    def __enter__(self) -> "Gate":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the approver seam (fail-closed class-(c)) -------------------------

    def _invoke_with_approval(self, fn: Callable, *args: Any) -> Any:
        """Run `fn(*args)`; on a class-(c) `ApprovalRequired`, resolve the
        ticket through the approver and retry the IDENTICAL call (which then
        consumes the standing approval and fires once). With no approver, or a
        declined ticket, `_resolve_ticket` raises `GateRefused` and the crossing
        never fires. The loop terminates: a resolved ticket mints a single-use
        standing approval the retry consumes, so at most one retry per ticket."""
        from .mcp.approval import ApprovalRequired  # noqa: PLC0415
        while True:
            try:
                return fn(*args)
            except ApprovalRequired as exc:
                self._resolve_ticket(exc.ticket)

    def _resolve_ticket(self, ticket: dict) -> None:
        if self._approver is None:
            raise GateRefused(
                "class-(c) crossing refused: an irreversible emission with no "
                "checked inverse needs a human yes, and no approver callback is "
                "configured, so the gate fails closed (item 332 embedding "
                "contract). Construct the Gate with `approver=` to answer "
                "class-(c) tickets.", ticket=ticket)
        decision = self._approver(dict(ticket))
        if not decision:
            raise GateRefused(
                "class-(c) crossing denied by the approver callback; nothing "
                "fired.", ticket=ticket)
        # a yes mints the single-use standing approval the retry consumes.
        self._session.approve_ticket(ticket["hash"])


def _session_error():
    """The internal `SessionError` type, lazily, so `except` clauses can catch
    it without importing cordis at module load."""
    from .mcp.session import SessionError  # noqa: PLC0415
    return SessionError
