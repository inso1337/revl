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
    """The frontend gate: a program the checker refuses cannot be compiled.

    Pure and disk-pure (`compile_source` reads nothing from and writes nothing
    to disk). Returns an admitted `Verdict` when the reference compiler accepts
    `source`, else a refusal `Verdict` whose `code`/`message` are the reference
    compiler's, verbatim. The security clause: this delegates to the reference
    `compile_source`, so it can never admit what `revl` refuses."""
    from .compiler import compile_source  # noqa: PLC0415 — lazy, keeps layer 1 light
    from .errors import RevlError  # noqa: PLC0415
    try:
        compile_source(source)
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
        return self._gate._invoke_with_approval(
            self._gate._session.call, key, method, args)


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


# The process-global single live gate (v1). A second live `Gate` in one process
# is refused loudly at construction, the honest spelling of today's
# process-global admit-bridge/session-owner binds (`admit_bridge._SESSION`,
# `runtime._SESSION_OWNER`). Lifting this is owner-scoping work the arc does not
# need here (337's mesh runs one gate per tier process).
_ACTIVE_GATE: "Gate | None" = None


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
        if _ACTIVE_GATE is not None:
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
        _ACTIVE_GATE = self

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
        untouched); an admission hands back a `Handle`, never the artifact."""
        try:
            verdict = self._session.admit(
                source, list(granted),
                modules=dict(modules) if modules else None)
        except _session_error() as error:
            raise GateError(str(error)) from error
        if not verdict.admitted:
            return AdmitResult(False, code=verdict.code, message=verdict.message)
        return AdmitResult(True, keys=verdict.keys,
                           handle=Handle(self, verdict.handle))

    def call(self, key: str, method: str, args: list | None = None) -> dict:
        """Invoke a provided operation on the running composition, gated by the
        item-246 policy and the approver seam."""
        if not self._loaded:
            raise GateError("nothing is loaded; call load first")
        return self._invoke_with_approval(self._session.call, key, method, args)

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
        if _ACTIVE_GATE is self:
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
