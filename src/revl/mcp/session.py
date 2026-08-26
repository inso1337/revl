"""A live composition an agent can drive in memory.

`revl run` boots a composition and holds it for a human at a REPL. This is
the same thing for a machine: no files, no stdout trace (which would corrupt
the JSON-RPC stream), and every step reported as data.

The point is the loop it enables. An agent can *generate → check → admit →
load → call → assert-no-residue → fix* without a candidate component ever
existing on disk: a rejected or misbehaving draft leaves nothing behind to
clean up, and only code that has already proven itself gets written out.

Implementation note: `run._Driver` already knows how to emit, load and tear
down a composition, so this subclasses it and redirects the trace instead of
duplicating the lifecycle. The event loop is owned here and advanced per
call — between tool calls the composition is simply idle.
"""

from __future__ import annotations

import asyncio
import sys

from .._paths import backends_root
from ..holes import collect as collect_holes
from ..holes import summarize as summarize_holes
from .approval import ApprovalRequired


class SessionError(RuntimeError):
    """The session cannot do what was asked (no runtime, nothing loaded…)."""


# How many generations the undo history retains (roadmap item 65,
# docs/generation-history.md). Every admitted change appends one snapshot; the
# oldest age out past this bound, so `undo --to` below the floor is refused
# honestly rather than silently reaching for a generation that is gone.
HISTORY_LIMIT = 64


def _backend():
    """Import the cordis-py runtime, with the same guidance `revl run` gives."""
    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    try:
        import emit  # noqa: PLC0415 — backend import after path setup
        import runtime as runtime_mod  # noqa: PLC0415
        from cordis import Context  # noqa: PLC0415
        from cordis.fiber import FiberState  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise SessionError(
            f"the cordis-py runtime is not installed ({exc.name!r} missing) — "
            f"set it up with `sh {backend_dir / 'setup.sh'}` and run the server "
            f"under {backend_dir / '.venv' / 'bin' / 'python'}"
        ) from exc
    return emit, runtime_mod, Context, FiberState


def replay_module():
    """The backwards-replay engine (`backends/python/replay.py`).

    Imported on its own path because it needs no cordis: the timeline, the
    step-back and the forward plan are pure python over the accumulator, so
    they can be exercised without a runtime installed.
    """
    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import replay  # noqa: PLC0415 — backend import after path setup

    return replay


def _capturing_driver_class():
    """`run._Driver`, with its trace captured instead of printed."""
    from ..run import _Driver  # noqa: PLC0415 — lazy: importing run pulls cordis

    class _CapturingDriver(_Driver):
        def __init__(self, *args, **kwargs):
            self.events: list[dict] = []
            super().__init__(*args, **kwargs)

        def _log(self, channel: str, subject: str, detail: str = "") -> None:
            self.events.append({"channel": channel, "subject": subject,
                                "detail": detail})

        def drain_events(self) -> list[dict]:
            events, self.events = self.events, []
            return events

    return _CapturingDriver


class Session:
    """One live composition, driven step by step."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._driver = None
        self.ir: dict | None = None
        self.previous: dict | None = None  # the generation `rollback` restores
        self.config: dict = {}
        self.recorder = None  # replay.Recorder, when loaded with record=True
        # the session commit-state owner (roadmap item 245): the deferral queue,
        # the discharge escrow, and the live-frame registry — the gate target the
        # commit verb derives. A `runtime.SessionOwner`, created at load. None
        # when nothing is loaded.
        self._owner = None
        # the admission inputs that produced the live composition — the
        # *sources*, kept so the composition can be snapshotted for
        # re-admission (docs/persistence.md). None when a composition was
        # loaded without them (e.g. a hand-built IR); such a session cannot be
        # snapshotted, because there is nothing to replay through the gate.
        self.origin: dict | None = None
        self.previous_origin: dict | None = None  # origin `rollback` restores
        # the retained generation history (roadmap item 65). `previous` above is
        # the depth-1 slot `rollback` uses and keeps its old semantics; this is
        # the deep version: every admitted change (load, swap, undo) appends a
        # generation snapshot here, so `undo` can return to N−1 and `undo --to`
        # to any still-retained generation — each undo itself a gated change.
        # Each entry is `{generation, snapshot, ir, origin, createdAt}` where
        # `snapshot` is a persist re-admittable bundle (None when the generation
        # was loaded without sources — an undo to it is then refused, never
        # rehydrated past the gate). See docs/generation-history.md.
        self._history: list[dict] = []
        # the server-side working source an agent edits with `revl_edit`
        # (deltas, not documents — docs/mcp-bridge.md, roadmap item 50). None
        # means "no uncommitted edits": the working source re-derives from
        # `origin`, i.e. from what is actually running. It is set only while an
        # edit has advanced the source but not yet swapped (open holes remain),
        # and cleared on every generation change below, since a swap/rollback
        # makes any older draft stale.
        self.draft: dict | None = None
        # which generation is live: a fresh boot is 1, every swap/rollback moves
        # it on. This is the "which world" a live query answers for — see
        # `live_state` and docs/queries.md §9.
        self._generation = 0
        # the agent-sandbox profile (roadmap item 33): a `revl.policy.Policy`
        # whose `mcp` allow-list bounds what agent-generated code admitted here
        # may reach — "agent output may reach [llm, kv*] and nothing else",
        # enforced at load/swap as a machine-checked invariant instead of a
        # review convention. None = no sandbox (the default).
        self.sandbox = None
        # the bound operator identity (roadmap item 55): a `revl.mcp.operator.
        # Operator` whose grants bound which management verbs this session may
        # call, over which components and realms. None = no profile: every verb
        # is ungated (today's root-over-transport), so nothing breaks. Set at
        # serve time from `--operator-profile`; the gate lives in the mcp verb
        # dispatch (`revl.mcp.server`), not here — this is only the binding.
        self.operator = None
        # component leases (roadmap item 61): operator-scoped, TTL-bound claims
        # on component *names* that govern who may *replace* a component (never
        # a lock on the running one, which keeps serving). The book is pure
        # bookkeeping over the clock (`revl.mcp.leases`); the claim/advisory/
        # enforced wiring lives in the mcp verb dispatch, surfaced below in
        # `state`. Empty by default, so a single-agent session is unchanged.
        from .leases import LeaseBook  # noqa: PLC0415 — no cordis, additive
        self.leases = LeaseBook()
        # the auto-approve policy (roadmap item 246, docs/design/246-auto-approve.md).
        # None = OFF: the decision in `call`/load/swap returns ungated and every
        # call proceeds — today's behaviour, byte for byte (the item-55 clause).
        # "auto" enables the second orthogonal gate: class (a) auto-approves
        # silently, (b) auto-approves and enumerates at commit, (c) prompts per
        # call via the ticket two-step. Set at serve time
        # (`revl mcp serve --approval-policy auto`).
        self.approval_policy = None
        # the per-generation class map (item 246, Decision 2): rebuilt atomically
        # at every load/swap so a call decided against a stale map is impossible.
        # None when nothing is loaded or the policy is off.
        self._class_map = None
        # the outstanding-ticket table (Fix 8): every class-(c) ticket the server
        # issues, keyed by its hash. `revl_approve` refuses a hash not in here —
        # an approval can only be minted for a question the server actually asked.
        # Replaced atomically with the class map at swap (a ticket from a previous
        # generation is gone, not stale).
        self._tickets: dict = {}
        # the approval ledger (item 246, Decision 3): granted class-(c) approvals,
        # each bound to its ticket hash and the reach-closure candidate hash,
        # single-use. Persists across swaps (the candidate-hash check invalidates a
        # stale entry, so no revocation bookkeeping exists to forget).
        self._ledger: list = []
        # item 246, Slice 2/3: granted typed `Approval[C]` entries (the language
        # surface, `await approval` / `with`). Distinct from `_ledger` (the
        # operator-layer ticket path): each carries a single `capability`, its
        # component, the reach-closure `candidateHash`, the `session`, an
        # `expiresAt` from the policy ttl, and the single-use `consumed` bit. The
        # runtime frame check (`Frame.approval_crossing`) reads these off the
        # SessionOwner, which the load below seeds from here.
        self._approval_grants: list = []
        # a stable identity for THIS session, bound onto every typed approval so a
        # token minted here is refused in a later session over the same workspace
        # and WAL (invariant 5, cross-session replay).
        import uuid  # noqa: PLC0415 — stdlib
        self._session_id = uuid.uuid4().hex
        # where the typed-approval WAL is written (item 246: recording makes the
        # consume-before-fire spend durable). The MCP session opens a WAL only for
        # a typed-approval composition; None picks a per-session temp path. Set by
        # the operator/test before load to control the location.
        self._wal_path: str | None = None
        self._clock_ms = None   # injectable ms clock (invariant 3); None = wall

    # -- plumbing ----------------------------------------------------------

    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    @property
    def loaded(self) -> bool:
        return self._driver is not None

    def _require(self):
        if self._driver is None:
            raise SessionError("nothing is loaded — call revl_load first")
        return self._driver

    # -- lifecycle ---------------------------------------------------------

    def load(self, ir: dict, config: dict | None = None,
             record: bool = False, origin: dict | None = None) -> dict:
        if self._driver is not None:
            raise SessionError("a composition is already loaded — swap or unload it")
        # booting is admission: a draft with open obligations is checkable but
        # not runnable, and the refusal belongs here rather than in the Python
        # emitter's lap (docs/holes.md)
        open_holes = collect_holes(ir)
        if open_holes:
            raise SessionError(
                f"cannot load: {len(open_holes)} open typed hole(s) — "
                f"{summarize_holes(open_holes)}. A hole has a type and no "
                f"implementation, so it may never enter a running composition; "
                f"`revl_check` lists them (docs/holes.md)")
        self._enforce_sandbox(ir)
        # item 246, Slice 2: the policy-owned `requires approval` gate. A component
        # reaching an approval-required capability with no `with` edge is refused
        # at admission, before any runtime is touched (the same place the sandbox
        # refuses). Operator-owned — an author cannot waive it by omission.
        self._enforce_approval_admission(ir)
        # item 246: an enabled approval policy REQUIRES recording — without a WAL
        # there is no durable ticket spend and no answer to "which human decision
        # authorized this crossing"; a policy whose approvals evaporate is worse
        # than none. Refuse, don't degrade (Decision 2). A policy FILE that names
        # an approval-required capability enables the gate the same way as the
        # operator `--approval-policy` flag (Decision 3), so it too requires a WAL.
        policy_requires_approval = (
            self.sandbox is not None
            and getattr(self.sandbox, "requires_approval", None) is not None
            and self.sandbox.requires_approval())
        if (self.approval_policy is not None or policy_requires_approval
                or _ir_has_approval_edges(ir)) and not record:
            raise SessionError(
                "the approval policy requires recording — load with `record: "
                "true`. Without a WAL there is no durable approval spend and no "
                "audit join for a class-(c) crossing, so no in-memory approval "
                "mode ships (item 246, Decision 2, refuse-don't-degrade)")
        # item 246: the per-generation class map, and the activation gate over it,
        # BEFORE any runtime is touched — a candidate whose activation body reaches
        # a class-(c) emission does not boot without approval (Fix 1). Off when no
        # policy is configured, so nothing below runs and the load is byte-identical.
        self._class_map = self._build_class_map(ir)
        self._enforce_activation_gate(ir)
        emit, runtime_mod, Context, FiberState = _backend()
        driver_class = _capturing_driver_class()
        self.config = config or {}
        self._driver = driver_class(ir, self.config, emit, runtime_mod, Context, FiberState)
        self.ir = ir
        self.origin = origin
        self.draft = None
        self.recorder = replay_module().Recorder(ir) if record else None
        self._generation = 1
        self._history = []
        # item 245: register the session commit-state owner before the
        # composition loads, so every activation frame joins its live-frame
        # registry (the commit verb's gate target). The WAL getter reads the
        # recorder's log lazily — it is opened during load, after this point.
        self._owner = runtime_mod.SessionOwner(
            wal_getter=lambda: self.recorder.wal if self.recorder else None)
        # item 246, Slice 3: seed the SessionOwner with the typed-approval state
        # BEFORE the activation body runs, so a `with a` crossing in the activation
        # body checks and consumes its token against the live ledger (the runtime
        # frame check). No-op unless the program uses the surface, so a session
        # without typed approvals is byte-identical.
        self._configure_owner_approvals(ir)
        runtime_mod.set_session_owner(self._owner)
        try:
            self._run(self._driver._load(ir, self._prepare_module(ir)))
        finally:
            # frames are built during load; stop capturing so a later, unrelated
            # Frame (a bare test) does not join this session's registry.
            runtime_mod.clear_session_owner()
        self._record_generation()
        return self.state(drain=True) | ({"recording": True} if record else {})

    def _typed_approval_active(self, ir: dict) -> bool:
        """Whether the typed-approval frame check enforces for this composition
        (item 246, Slice 3). True when the program uses the `with a` surface, when
        an extern declared `requires approval`, or when the bound boundary policy
        names an approval-required capability. False = the frame check is a
        passthrough and load is byte-identical."""
        if self.sandbox is not None and getattr(self.sandbox,
                                                "requires_approval", None) \
                and self.sandbox.requires_approval():
            return True
        for ext in ir.get("externs") or []:
            if ext.get("requires_approval"):
                return True
        return _ir_has_approval_edges(ir)

    def _approval_candidate_hashes(self, ir: dict) -> dict:
        """Per-component reach-closure candidate hash for the runtime frame check
        (invariant 4). Reuses the Slice-1 `ClassMap.candidate_hash` over each
        component's activation reach closure; a swap that changes any semantic
        entry in the closure changes the hash, invalidating standing tokens."""
        from .approval import ClassMap  # noqa: PLC0415 — lazy, no cordis
        cm = ClassMap(ir)
        out: dict = {}
        for comp in ir.get("components") or []:
            name = comp["name"]
            sid = f"{name}:activation"
            reach = cm._reach.get(sid)
            closure = (reach["closureComponents"] if reach is not None
                       else {name})
            out[name] = cm.candidate_hash(closure)
        return out

    def _configure_owner_approvals(self, ir: dict) -> None:
        """Push the session's typed-approval state onto the SessionOwner so the
        runtime frame check can enforce it (item 246, Slice 3)."""
        owner = self._owner
        if owner is None or not self._typed_approval_active(ir):
            return
        owner.approval_enforced = True
        owner.session_id = self._session_id
        owner.approval_candidates = self._approval_candidate_hashes(ir)
        owner.now_ms = self._now_ms   # the same injectable clock (invariant 3)
        for grant in self._approval_grants:
            owner.grant_approval(grant)
        # item 246, Decision 3: make the consume-before-fire spend durable. The
        # MCP session otherwise leaves the WAL closed (only `revl run --wal`
        # opens it); a typed-approval composition needs it open BEFORE the
        # activation body's crossing, so the `approval-consumed`/`approval-emission`
        # pair is written and the audit can join them on `requestId`.
        if self.recorder is not None and self.recorder.wal is None:
            import tempfile  # noqa: PLC0415
            path = self._wal_path or (
                tempfile.gettempdir() + f"/revl-approval-{self._session_id}.wal")
            self.recorder.open_wal(path, self._generation)

    def grant_language_approval(self, capability: str, component: str,
                                fields: dict | None = None,
                                ttl_ms: int | None = None,
                                candidate_hash: str | None = None) -> dict:
        """Mint a typed `Approval[C]` grant — the language-surface analogue of
        `approve_ticket` (item 246, Decision 3). Binds the capability, the
        component, the live reach-closure candidate hash, the session, and an
        `expiresAt` from the policy ttl (or the rule's default). Single-use; the
        runtime frame check consumes it at the crossing. A `approval-granted` WAL
        record makes it durable."""
        candidate = candidate_hash
        if candidate is None and self._class_map is not None:
            candidate = self._class_map.candidate_hash({component})
        elif candidate is None and self._owner is not None:
            candidate = self._owner.approval_candidates.get(component)
        rule = (self.sandbox.approval_rule_for(capability)
                if self.sandbox is not None
                and getattr(self.sandbox, "approval_rule_for", None) else None)
        if ttl_ms is None and rule is not None:
            ttl_ms = rule.ttl_ms
        now = self._now_ms()
        request_id = "appr:" + str(len(self._approval_grants) + 1) + ":" + capability
        entry = {
            "requestId": request_id,
            "capability": capability,
            "component": component,
            "candidateHash": candidate,
            "session": self._session_id,
            "fields": dict(fields or {}),
            "grantedAt": now,
            "expiresAt": (now + ttl_ms) if ttl_ms is not None else None,
            "consumed": False,
        }
        self._approval_grants.append(entry)
        if self._owner is not None:
            self._owner.grant_approval(entry)
            wal = self._approval_wal()
            if wal is not None:
                wal.record_approval_granted({
                    "requestId": request_id, "capability": capability,
                    "component": component, "candidateHash": candidate,
                    "session": self._session_id, "fields": entry["fields"]})
        return dict(entry)

    def _enforce_approval_admission(self, ir: dict) -> None:
        """item 246, Slice 2: refuse admission for a component reaching a
        policy-approval-required capability with no covering `with` edge. Set
        operations over the audit graph, so nothing boots on a breach (the same
        no-runtime admission the sandbox uses). No-op unless the bound policy
        names an approval-required capability."""
        if self.sandbox is None \
                or getattr(self.sandbox, "approval_rules", None) in (None, ()):
            return
        from ..policy import approval_admission, first_error  # noqa: PLC0415
        error = first_error(approval_admission(self.sandbox, ir))
        if error is not None:
            raise SessionError(str(error).split("\n")[0])

    def _enforce_sandbox(self, ir: dict) -> None:
        """The agent-sandbox invariant (roadmap item 33). When a `sandbox`
        policy is set, every component being admitted here is agent output:
        its G8 reach must stay within the policy's `mcp` allow-list. A breach
        refuses admission before any runtime is touched — the sandbox profile
        is a machine-checked gate, not a review convention.

        Deliberately additive and self-contained: no runtime is needed to
        decide it (it is set operations over the audit graph), so a draft that
        over-reaches is refused without the composition ever booting."""
        if self.sandbox is None or self.sandbox.mcp_allow is None:
            return
        from ..audit_diff import audit_report  # noqa: PLC0415 — lazy, no cordis
        from ..policy import evaluate  # noqa: PLC0415

        audit = audit_report(ir)
        everyone = frozenset(audit.get("boundary") or {})
        violations = evaluate(self.sandbox, audit, mcp_components=everyone)
        if violations:
            detail = "; ".join(v.message.split(" — ")[0] for v in violations)
            raise SessionError(
                f"agent-sandbox refuses admission: {detail}. The sandbox "
                f"permits [{', '.join(self.sandbox.mcp_allow)}] and nothing "
                f"else (boundary policy, docs/boundary-policy.md)")

    def _prepare_module(self, ir: dict):
        """Emit the module and, when recording, instrument it before load.

        Instrumentation has to happen between emit and `plugin`, because it
        replaces each component's `apply` — the fiber's context chain is fixed
        at plugin time and there is no way in afterwards.
        """
        driver = self._driver
        module = driver._emit_module(ir)
        if self.recorder is not None:
            filename, source = driver.emitted
            self.recorder.register_source(filename, source)
            self.recorder.activation_origin()
            self.recorder.timelines.clear()
            self.recorder.instrument(module, ir)
        return module

    def swap(self, ir: dict, origin: dict | None = None,
             migrate: str = "generational") -> dict:
        """Replace the running composition. The caller has already had the
        candidate admitted; this performs the transition.

        When a swapped *template* has live instances (spawned children of some
        component — docs/design-v2-instances.md), their **state migrates** onto
        the successor template (roadmap item 10, "hot-swap with live
        instances"). `migrate` selects the reconciliation policy:

        * ``"generational"`` (default) — capture each live instance's state
          before teardown, then re-seat it onto the successor's freshly-spawned
          instances, gated by a **state-compatibility** check. An incompatible
          successor (one that drops or retypes an instance's state, or is gone)
          is *rejected*: the whole swap rolls back with the original state
          intact, rather than silently dropping it (Q3/Q4).
        * ``"respawn"`` — today's teardown semantics: instances die with the
          old composition and the successor starts them cold. No migration.

        Composition-level (static) state is unaffected either way; this only
        reconciles the dynamic instance layer the static swap never saw."""
        driver = self._require()
        self._enforce_sandbox(ir)
        # item 246: classify the candidate and gate its activation reach BEFORE
        # any teardown — a swap-in whose activation body reaches a class-(c)
        # emission answers for it with the ticket two-step before it boots, which
        # is the bypass Fix 1 exists to keep shut (an emission moved from a
        # provide-method into the activation body must not dodge the prompt). The
        # new map replaces the live one atomically only once the swap completes,
        # so a call mid-swap is impossible.
        new_map = self._build_class_map(ir)
        self._enforce_activation_gate(ir, class_map=new_map)
        old_ir = self.ir
        # capture BEFORE teardown — while the old instances are still live and
        # their state still exists (Q2). Empty unless something spawned, so a
        # non-instance swap is byte-identical to before.
        pre = (self._capture_instances(old_ir, ir)
               if migrate == "generational" else {})
        # item 53: capture each stateful *provider's* live state (its
        # effect-created world) before teardown too, so a component that
        # declared a `handoff` starts its successor warm. Admission has already
        # proved the exported/accepted shapes are §5-compatible; this threads
        # the value. Empty unless a running provider declared a hand-off, so a
        # stateless swap is byte-identical to before.
        handoff_pre = self._capture_provider_state(old_ir)
        saved_previous, saved_previous_origin = self.previous, self.previous_origin
        self.previous = self.ir
        self.previous_origin = self.origin
        self._run(driver._dispose_all(self.ir))
        driver.ir = self.ir = ir
        self.origin = origin
        self.draft = None  # a new generation makes any uncommitted edit stale
        self._generation += 1
        self._run(driver._load(ir, self._prepare_module(ir)))
        migration = None
        handoff = None
        if pre:
            try:
                migration = self._reconcile_instances(pre)
            except driver.runtime.StateIncompatible as exc:
                # atomicity (Q4): unwind the whole swap and re-seat the original
                # instance state, so a rejected migration leaves nothing changed
                # and nothing half-migrated.
                self._abort_swap(old_ir, pre, saved_previous, saved_previous_origin,
                                 handoff_pre)
                raise SessionError(
                    f"swap rejected: a live instance's state cannot migrate onto "
                    f"the successor — {exc}. The running composition is untouched "
                    f"(rolled back to the previous generation, instance state "
                    f"intact). Dropping the state would be residue, so admission "
                    f"refuses it (docs/service-compat.md, state-compat gate)."
                ) from None
        if handoff_pre:
            try:
                handoff = self._restore_provider_state(ir, handoff_pre)
            except driver.runtime.StateIncompatible as exc:
                # defence in depth: admission gates the *declared* shapes, but a
                # provider whose resource vector diverges from its declaration
                # still rolls the whole swap back rather than dropping state.
                self._abort_swap(old_ir, pre, saved_previous, saved_previous_origin,
                                 handoff_pre)
                raise SessionError(
                    f"swap rejected: a provider's hand-off state cannot cross onto "
                    f"the successor — {exc}. The running composition is untouched "
                    f"(rolled back to the previous generation, state intact). "
                    f"Dropping it would be residue (docs/state-handoff.md)."
                ) from None
        self._record_generation()
        # item 246: the new generation's class map goes live atomically with the
        # composition, and the outstanding-ticket table is replaced (a ticket
        # issued against the previous generation is gone, not stale — its hash
        # gets the unknown-hash refusal and the caller re-issues).
        self._class_map = new_map
        self._tickets = {}
        state = self.state(drain=True)
        if migration is not None:
            state["migration"] = migration
        if handoff is not None:
            state["handoff"] = handoff
        return state

    # -- live-instance state migration (roadmap item 10) -------------------

    def _capture_instances(self, old_ir: dict, new_ir: dict) -> dict:
        """Snapshot the live instances of every template being swapped (Q1/Q2).

        Enumerated from the runtime's live-instance registry, in spawn order,
        so reconciliation correlates old↔new positionally. Records whether the
        template survives into the successor, so a *vanished* template's
        instances are caught by the gate rather than silently dropped."""
        runtime = self._driver.runtime
        old_templates = (old_ir.get("manifest") or {}).get("templates") or []
        new_templates = set((new_ir.get("manifest") or {}).get("templates") or [])
        pre: dict = {}
        for name in old_templates:
            handles = runtime.live_instances(name)
            if not handles:
                continue
            pre[name] = {
                "present": name in new_templates,
                "captured": [h.capture_state() for h in handles],
            }
        return pre

    def _reconcile_instances(self, pre: dict) -> dict:
        """Gate then migrate (Q2/Q3). Two passes so the whole cohort is checked
        before any of it is written — a single incompatible instance rejects the
        migration with nothing applied (Q4)."""
        runtime = self._driver.runtime
        plan: dict = {}
        for name, info in pre.items():
            captured = info["captured"]
            if not info["present"]:
                raise runtime.StateIncompatible(
                    f"template {name!r} is gone from the successor composition — "
                    f"its {len(captured)} live instance(s) have nowhere to land")
            new_handles = runtime.live_instances(name)
            if len(new_handles) != len(captured):
                raise runtime.StateIncompatible(
                    f"template {name!r} had {len(captured)} live instance(s); the "
                    f"successor spawned {len(new_handles)} — cannot correlate them")
            pairs = list(zip(new_handles, captured))
            for handle, cap in pairs:
                handle.check_state(cap)  # gate only, no mutation
            plan[name] = pairs
        report = {"policy": "generational", "templates": {}}
        for name, pairs in plan.items():
            for handle, cap in pairs:
                handle.restore_state(cap)
            # operator visibility (item 53 rule, success case): name the count
            # of resources actually carried, so the report is honest about what moved.
            report["templates"][name] = {
                "instances": len(pairs), "migrated": True,
                "resources": sum(len(cap) for _, cap in pairs)}
        return report

    def _abort_swap(self, old_ir: dict, pre: dict,
                    saved_previous: dict | None,
                    saved_previous_origin: dict | None,
                    handoff_pre: dict | None = None) -> None:
        """Undo a swap whose migration was rejected: tear down the rejected
        successor, reload the predecessor, and re-seat the captured state —
        both spawned-instance state and provider hand-off state — onto the
        re-loaded predecessor, so the composition is left exactly as if the
        swap had never been attempted."""
        driver = self._driver
        self._run(driver._dispose_all(self.ir))
        driver.ir = self.ir = old_ir
        self.origin = self.previous_origin
        self.previous, self.previous_origin = saved_previous, saved_previous_origin
        self._generation += 1
        self._run(driver._load(old_ir, self._prepare_module(old_ir)))
        for name, info in pre.items():
            for handle, cap in zip(driver.runtime.live_instances(name),
                                   info["captured"]):
                try:
                    handle.restore_state(cap)  # same template — always compatible
                except driver.runtime.StateIncompatible:  # pragma: no cover
                    pass
        # re-seat provider hand-off state onto the reloaded predecessor (same
        # component code — its resource vector matches, so this cannot reject).
        for key, info in (handoff_pre or {}).items():
            fiber = driver.fibers.get(info["component"])
            resources = self._frame_resources(fiber)
            for res, (_old_type, snap) in zip(resources, info["captured"]):
                if snap is not None:
                    try:
                        res.__revl_restore__(snap)
                    except Exception:  # pragma: no cover — defensive
                        pass

    # -- provider state hand-off (roadmap item 53) -------------------------

    def _frame_resources(self, fiber) -> list:
        """The ordered vector of stateful host resources a live component's
        activation acquired (its `Map`s, …) — the same `frame._resources` the
        instance-migration path reads, but for a *composition-level* provider
        fiber rather than a spawned instance. `[]` when the component never
        activated (its provisions never came up) or the runtime tracks no
        frame for it."""
        if fiber is None:
            return []
        frame = self._driver.runtime._frame_for_ctx(fiber.ctx)
        return list(frame._resources) if frame is not None else []

    def _capture_provider_state(self, old_ir: dict | None) -> dict:
        """Snapshot the live state of every running provider that declared a
        `handoff` (item 53), keyed by the provided key it hangs off.

        The value is the ordered `(resource_type, state)` vector of the
        provider's activation frame — captured while the old provider is still
        live and its world still exists, before teardown drops it. Keyed by
        provided key (not component name) so the successor's provider, which may
        be a differently-named component, is correlated by *what it provides*."""
        pre: dict = {}
        for comp in (old_ir or {}).get("components") or []:
            handoff = comp.get("handoff")
            if not isinstance(handoff, dict) or not handoff.get("key"):
                continue
            fiber = self._driver.fibers.get(comp["name"])
            resources = self._frame_resources(fiber)
            captured = [
                (type(res),
                 res.__revl_state__() if hasattr(res, "__revl_state__") else None)
                for res in resources
            ]
            pre[handoff["key"]] = {
                "component": comp["name"],
                "type": handoff.get("type"),
                "captured": captured,
            }
        return pre

    def _restore_provider_state(self, new_ir: dict, pre: dict) -> dict:
        """Thread each captured provider state onto the successor that
        re-provides its key (item 53). Two passes — check the whole cohort,
        then apply — so a single incompatible provider rejects with nothing
        half-written (the same atomicity the instance path guarantees).

        A captured key whose successor declares no `handoff` is *not* migrated:
        the successor opted out of inheriting the state (a deliberate, if lossy,
        author choice), so it starts cold rather than being force-fed a shape it
        never declared."""
        runtime = self._driver.runtime
        new_by_key: dict = {}
        for comp in new_ir.get("components") or []:
            handoff = comp.get("handoff")
            if isinstance(handoff, dict) and handoff.get("key"):
                new_by_key[handoff["key"]] = comp
        plan: list = []
        report: dict = {}
        for key, info in pre.items():
            comp = new_by_key.get(key)
            if comp is None:
                continue  # successor does not accept this key's state — cold
            captured = info["captured"]
            fiber = self._driver.fibers.get(comp["name"])
            resources = self._frame_resources(fiber)
            if len(resources) != len(captured):
                raise runtime.StateIncompatible(
                    f"provider of {key!r} held {len(captured)} stateful "
                    f"resource(s); the successor acquires {len(resources)} — "
                    f"state cannot migrate without dropping or inventing one")
            for pos, (res, (old_type, _snap)) in enumerate(zip(resources, captured)):
                if type(res) is not old_type:
                    raise runtime.StateIncompatible(
                        f"provider of {key!r}: resource #{pos} was "
                        f"{old_type.__name__}, the successor acquires "
                        f"{type(res).__name__} — retyped state cannot migrate")
            plan.append((key, comp["name"], resources, captured))
        for key, cname, resources, captured in plan:
            for res, (_old_type, snap) in zip(resources, captured):
                if snap is not None:
                    res.__revl_restore__(snap)
            report[key] = {
                "component": cname, "migrated": True,
                # honest count of what actually crossed (item 53 operator rule)
                "resources": sum(1 for _t, s in captured if s is not None),
            }
        return report or None

    def rollback(self) -> dict:
        if self.previous is None:
            raise SessionError("no previous generation to roll back to")
        restored, self.previous = self.previous, None
        restored_origin, self.previous_origin = self.previous_origin, None
        # rollback restores the *code* of the previous generation; it keeps
        # today's teardown semantics for instances (no cross-generation state
        # migration back), so it stays byte-identical to the pre-item-10 path.
        return self.swap(restored, origin=restored_origin,
                         migrate="respawn") | {"rolledBack": True}

    # -- generation history and undo (roadmap item 65) ---------------------

    def _record_generation(self, readmittable: bool = True) -> None:
        """Append the now-live generation to the retained history.

        The entry is a *re-admittable snapshot* (item 15's persist bundle) plus
        the compiled IR — the snapshot is what an undo replays through the gate,
        the IR is what the dossier reads its boundary surface off. A generation
        without recorded sources snapshots to None: it stays in the history for
        the count and the crossing report, but an undo *to* it is refused rather
        than rehydrated (docs/generation-history.md).

        `readmittable=False` forces the snapshot to None even when an origin is
        present — the apply path uses it, because a plan-artifact apply leaves
        `origin` reproducing the *pre-apply* sources, which must never stand in
        as the post-apply generation's re-admission bundle."""
        from .persist import build_snapshot  # noqa: PLC0415 — lazy

        # best-effort: a files-based origin materializes by reading the source
        # off disk, which can fail if the file has since moved (e.g. a session
        # restored from a snapshot whose original file is gone). History capture
        # must never break the change it is recording, so a snapshot that cannot
        # be built leaves the entry present but not re-admittable (snapshot=None).
        snap = None
        if readmittable:
            try:
                snap = build_snapshot(self.ir, self.origin, self.config,
                                      self.recorder is not None)
            except OSError:
                snap = None
        self._history.append({
            "generation": self._generation,
            "snapshot": snap,
            "ir": self.ir,
            "origin": self.origin,
        })
        # bounded retention: the oldest generations age out. `undo --to` below
        # the floor is refused (see `undo`) instead of reaching for one gone.
        if len(self._history) > HISTORY_LIMIT:
            del self._history[:-HISTORY_LIMIT]

    def history_document(self) -> dict:
        """The retained history as a portable document: the generation numbers
        and their re-admittable snapshots. A consumer (the `revl undo` CLI)
        replays it into a fresh session to reach the same live history, then
        undoes. Versioned in the additive-only spirit of the interchange
        format — gate on the MAJOR, ignore unknown members."""
        return {
            "kind": "revl.generation-history",
            "schemaVersion": "1.0",
            "current": self._generation,
            "generations": [
                {"generation": e["generation"], "snapshot": e["snapshot"]}
                for e in self._history
            ],
        }

    def _component_names(self, ir: dict | None) -> list[str]:
        return [c.get("name") for c in (ir or {}).get("components") or []]

    def _undo_dossier(self, current: dict, target: dict) -> dict:
        """The undo plan/dossier — computed like any other change (item 65).

        An undo is a swap to the target generation's shape, so its delta is
        `plan.plan(target sources vs the running IR)`: what unloads, what
        provisions/components drop (item 53's honesty, in reverse), the reactive
        cascade. On top of that it enumerates the boundary crossings the interim
        generations made that no undo can un-emit — compensation is not
        inversion (paper §6.1; docs/erase-report.md)."""
        from ..plan import plan as build_plan  # noqa: PLC0415 — read-only reuse
        from ..audit_diff import audit_report, crossings  # noqa: PLC0415

        sources = target["snapshot"]["sources"]
        # components the running composition has but the target lacks are
        # *withdrawn* by the undo — name them via `replacing` so the plan's
        # teardown/withdrawn buckets are accurate for a whole-composition revert.
        running_names = set(self._component_names(self.ir))
        target_names = set(self._component_names(target["ir"]))
        replacing = tuple(sorted(running_names - target_names))
        p = build_plan(source=sources.get("source"), files=sources.get("files"),
                       modules=sources.get("modules"), manifest=self.ir,
                       replacing=replacing)

        # what boundary crossings the interim generations made that no undo can
        # un-emit. Every generation strictly after the target, up to and
        # including the current one, was live and could reach the boundary; the
        # union of their G8 surfaces is exposure the code-undo cannot reverse.
        target_cross = crossings(audit_report(target["ir"]))
        interim: list[dict] = []
        union: set[str] = set()
        for entry in self._history:
            if entry["generation"] > target["generation"]:
                cr = crossings(audit_report(entry["ir"]))
                union |= cr
                interim.append({"generation": entry["generation"],
                                "crossings": sorted(cr)})
        unemittable = {
            "crossings": sorted(union),
            "givenUp": sorted(union - target_cross),
            "persisting": sorted(union & target_cross),
            "interim": interim,
            "note": (
                "these boundary crossings were reachable by the interim "
                "generations while they were live; undoing their code cannot "
                "un-emit what already left the system (compensation is not "
                "inversion — paper §6.1, docs/erase-report.md). `givenUp` are "
                "reaches the target generation no longer has: authority "
                "relinquished going forward, yet already exercised and possibly "
                "observed downstream."),
        }
        return {
            "fromGeneration": current["generation"],
            "toGeneration": target["generation"],
            "admissible": p["admissible"],
            "unloads": p["cascade"]["withdrawn"],
            "stateDropped": {
                "provisions": p["provisions"]["withdrawn"],
                "components": p["components"]["withdrawn"],
                "teardownOrder": p["teardownOrder"],
            },
            "cascade": p["cascade"],
            "provisions": p["provisions"],
            "unemittableCrossings": unemittable,
        }

    def undo(self, to: int | None = None) -> dict:
        """Return to an earlier generation — and be, itself, a gated change.

        `undo()` returns to generation N−1; `undo(to=g)` to any still-retained
        generation `g`. The target's sources are re-admitted through the *same*
        gate a live swap runs (compile + admission): a target the current
        checker rejects is refused, never bypassed — an undo that skipped the
        gate would be the one unverified path into a running system. The report
        computed alongside is the undo's dossier (`_undo_dossier`)."""
        self._require()
        if len(self._history) < 2:
            raise SessionError(
                "no earlier generation to undo to — only the current "
                "generation is in the history")
        current = self._history[-1]
        if to is None:
            target = self._history[-2]
        else:
            matches = [e for e in self._history if e["generation"] == to]
            if not matches:
                retained = [e["generation"] for e in self._history]
                raise SessionError(
                    f"generation {to} is not in the retained history "
                    f"(retained: {retained}) — it never ran, or it has aged out "
                    f"of the bounded history (the last {HISTORY_LIMIT} kept)")
            target = matches[-1]
            if target is current:
                raise SessionError(
                    f"generation {to} is already the running generation — "
                    f"nothing to undo")
        if target["snapshot"] is None:
            raise SessionError(
                f"generation {target['generation']} was loaded without recorded "
                f"sources, so it cannot be re-admitted — undo replays the "
                f"target's sources through the gate, and there are none to "
                f"replay (it is not rehydrated past the gate)")

        # the dossier is computed against the *pre-undo* live composition
        dossier = self._undo_dossier(current, target)

        # the gate: re-admit the target's sources exactly as a swap would. A
        # rejected recompile is a *result* (the running system is untouched),
        # not a crash — the undo never bypasses admission.
        from ..errors import RevlError  # noqa: PLC0415
        from ..diagnostics import classify  # noqa: PLC0415
        from .persist import _origin_from, _recompile  # noqa: PLC0415

        try:
            target_ir = _recompile(target["snapshot"]["sources"])
        except RevlError as error:
            diag = classify(error)
            return {
                "undone": False,
                "refused": True,
                "toGeneration": target["generation"],
                "reason": (
                    f"generation {target['generation']} no longer admits — "
                    f"{diag.get('message', str(error))}. The running composition "
                    f"is untouched: an undo is a gated change, and a target the "
                    f"current checker rejects is refused (never a bypass)."),
                "diagnostics": [diag],
                "dossier": dossier,
                "state": self.state(drain=True),
            }

        # execute: the undo IS an admitted swap through the same gate. `swap`
        # enforces the sandbox and appends the resulting generation to the
        # history, so an undo can itself be undone (git-revert of a git-revert).
        state = self.swap(
            target_ir, origin=_origin_from(target["snapshot"]["sources"]),
            migrate="respawn")
        return {
            "undone": True,
            "toGeneration": target["generation"],
            "generation": self._generation,
            "dossier": dossier,
            **state,
        }

    # -- apply a plan artifact (docs/apply.md) -----------------------------

    def _provided_keys(self) -> list[str]:
        """The keys currently *served* — a declared key whose provider is
        inactive reads as absent, which is what drift and step-verification
        both want to see."""
        driver = self._driver
        if driver is None:
            return []
        return sorted(k for k, v in driver._namespace().items() if v is not None)

    def _live_fingerprint(self) -> dict:
        """The live composition in the same shape `apply.fingerprint` derives
        from an IR — but provisions are the ones *actually served now*, so a
        component that has drifted to PENDING since planning shows up as a
        vanished provision, not a phantom one."""
        from ..run import _components, _load_order  # noqa: PLC0415 — lazy

        ir = self.ir or {}
        served = set(self._provided_keys())
        provisions = []
        for comp in _components(ir):
            for key in comp.get("provides") or {}:
                if key in served:
                    provisions.append({"key": key, "provider": comp["name"]})
        return {
            "components": sorted(c["name"] for c in _components(ir)),
            "loadOrder": list(_load_order(ir)),
            "provisions": sorted(provisions, key=lambda p: (p["key"], p["provider"])),
        }

    def live_state(self) -> dict:
        """What the running fibers know that the static graph does not — the
        input a live query folds into its envelope (query.as_live).

        `servedKeys` is the set actually served *now* (a provider that drifted
        to an inactive state drops out); `componentStates` is each fiber's live
        state; `generation` is which world (post-swap) this is."""
        driver = self._driver
        states = {}
        if driver is not None:
            states = {name: driver.FiberState(fiber.state).name
                      for name, fiber in driver.fibers.items()}
        return {
            "generation": self._generation,
            "servedKeys": self._provided_keys(),
            "componentStates": states,
        }

    async def _plug(self, name: str, module) -> None:
        """Bring one component up from `module`, awaiting an async body exactly
        as `run._Driver._load` does for the whole composition."""
        driver = self._driver
        body = getattr(module, name, None)
        if body is None:
            raise SessionError(
                f"the composition has no component `{name}` to load — the plan "
                "artifact is inconsistent with its resulting IR")
        fiber = driver.root.plugin(body, driver.config.get(name, {}))
        driver.fibers[name] = fiber
        await driver._flush()
        if fiber.state == driver.FiberState.LOADING:
            try:
                await asyncio.wait_for(asyncio.shield(fiber), 2)
            except asyncio.TimeoutError:
                pass
        await driver._flush()

    def _apply_one(self, op: dict, module) -> dict:
        """Perform one plan operation and read the live effect back."""
        driver = self._driver
        name = op["name"]
        if op["op"] == "dispose":
            fiber = driver.fibers.pop(name, None)
            if fiber is not None:
                self._run(fiber.dispose())
                self._run(driver._flush())
            return {"name": name, "absent": name not in driver.fibers,
                    "state": None, "providedKeys": self._provided_keys()}
        if op["op"] == "load":
            self._run(self._plug(name, module))
            fiber = driver.fibers.get(name)
            state = driver.FiberState(fiber.state).name if fiber is not None else None
            return {"name": name, "absent": fiber is None, "state": state,
                    "providedKeys": self._provided_keys()}
        raise SessionError(f"unknown apply operation {op['op']!r}")

    def _rollback_apply(self, applied: list[dict], running_module,
                        running_ir: dict) -> list[dict]:
        """Undo the applied prefix, last-in-first-out, by derived inverses: a
        load's inverse is a dispose, a teardown's inverse is a re-load of the
        withdrawn body from the pre-apply composition."""
        driver = self._driver
        rolled: list[dict] = []
        for op in reversed(applied):
            name = op["name"]
            if op["op"] == "load":
                fiber = driver.fibers.pop(name, None)
                if fiber is not None:
                    self._run(fiber.dispose())
                    self._run(driver._flush())
                rolled.append({"undo": "dispose", "name": name})
            else:  # dispose -> re-load the component that was torn down
                self._run(self._plug(name, running_module))
                rolled.append({"undo": "restore", "name": name})
        driver.ir = self.ir = running_ir
        self._run(driver._flush())
        return rolled

    def apply(self, artifact: dict) -> dict:
        """Execute a `revl plan -o` artifact against this live composition.

        Refuses on drift; verifies each step's effect against the plan's
        prediction; on any failure rolls the applied prefix back by derived
        LIFO inverses and proves no residue. See docs/apply.md.
        """
        from .. import apply as apply_mod  # noqa: PLC0415 — lazy, no cordis

        apply_mod.validate_artifact(artifact)
        driver = self._require()

        # (a) drift — the composition may have moved since the plan was computed
        diff = apply_mod.drift(artifact["basis"], self._live_fingerprint())
        if diff is not None:
            raise SessionError(apply_mod.render_drift(diff))

        running_ir = self.ir
        resulting_ir = artifact["resultingIR"]
        operations = artifact["operations"]

        registry_baseline = driver.root.registry.size
        result_module = driver._emit_module(resulting_ir)
        running_module = (driver._emit_module(running_ir)
                          if any(o["op"] == "dispose" for o in operations) else None)
        # the driver now reflects the resulting composition so its namespace
        # sees the keys under change; a rollback restores this to `running_ir`.
        driver.ir = self.ir = resulting_ir

        applied: list[dict] = []
        steps: list[dict] = []
        current: dict | None = None
        reason: str | None = None
        try:
            for op in operations:
                current = op
                observed = self._apply_one(op, result_module)
                applied.append(op)
                steps.append({"op": op["op"], "name": op["name"],
                              "state": observed.get("state"),
                              "providedKeys": observed.get("providedKeys")})
                mismatch = apply_mod.verify_step(op, observed)
                if mismatch is not None:
                    reason = mismatch
                    break
            else:
                current = None
                reason = apply_mod.verify_final(
                    artifact, self._live_fingerprint(), self._provided_keys())
        except Exception as error:  # noqa: BLE001 — any failure triggers rollback
            reason = f"{type(error).__name__}: {error}"

        if reason is not None:
            rolled = self._rollback_apply(applied, running_module, running_ir)
            residue_ok = (
                driver.root.registry.size == registry_baseline
                and apply_mod._canon(self._live_fingerprint())
                == apply_mod._canon(artifact["basis"]))
            return {
                "applied": False,
                "failedAt": (current or {}).get("name"),
                "reason": reason,
                "steps": steps,
                "rolledBack": rolled,
                "noResidue": residue_ok,
                "registry": {"baseline": registry_baseline,
                             "afterRollback": driver.root.registry.size},
                "state": self.state(drain=True),
            }

        self.previous = None  # a completed apply is not a swap to roll back to
        # an apply is an admitted change, so it is a generation in the history
        # (item 65). It was executed from a plan *artifact* (an IR, not sources),
        # so it has no source origin to re-admit its result: the entry snapshots
        # to None, which means an undo *to* this generation is refused honestly
        # rather than rehydrated. Undoing *past* it (to an earlier source-backed
        # generation) still works, and its boundary crossings are enumerated.
        self._generation += 1
        self._record_generation(readmittable=False)
        return {
            "applied": True,
            "steps": steps,
            "resulting": artifact["resulting"]["components"],
            "state": self.state(drain=True),
        }

    # -- persistence (docs/persistence.md) ---------------------------------

    def snapshot(self) -> dict:
        """`{sources, manifest, meta}` for the live composition — the inputs
        needed to *re-admit* it, not a dump of runtime objects."""
        from .persist import snapshot as _snapshot  # noqa: PLC0415 — lazy

        return _snapshot(self)

    def restore(self, snap: dict) -> dict:
        """Re-admit a snapshot into this (empty) session by replaying
        admission — compile through the gate, then boot. A component the
        current checker rejects fails loudly and loads nothing."""
        from .persist import restore as _restore  # noqa: PLC0415 — lazy

        return _restore(self, snap)

    def unload(self) -> dict:
        """Tear down and report the residue checks — R4 from inside the
        protocol, so an agent can prove its component leaves nothing.

        Under a session owner (item 245), a plain unload is the IMPLICIT
        terminal commit (the pre-245 "a clean unload IS the commit"): the
        witnessed mutations discharge and the discharge record is written, unless
        a frame was marked aborting (an in-process `Frame.abort()`), in which case
        the inverses replay and no discharge record is written. This preserves
        every existing witnessed-runtime test. The explicit `commit`/`abort`
        verbs are the audited, WAL-marked path; unload is the quiet default."""
        driver = self._require()
        owner = self._owner
        aborting = owner is not None and any(
            getattr(f, "_aborting", False) for f in owner._registry)
        if owner is not None:
            owner._verdict = "abort" if aborting else "commit"
            if aborting:
                owner._queue = []   # DROP: an unload that reverts drops the queue
        self._run(driver._dispose_all(self.ir))
        if owner is not None and not aborting:
            owner.finalize_commit()   # the consolidated commit proof
        report = self._teardown_report(driver)
        self._reset()
        return {"unloaded": True, **report}

    # -- the session commit protocol (roadmap item 245) --------------------

    def commit(self) -> dict:
        """Enumerate the commit manifest — step 1 of the two-step, hash-bound
        commit (docs/design/245-session-commit.md, Decision 4). Returns the
        manifest whose `summary` is the human's one-line prompt and whose `hash`
        binds the gate target (the deferral queue, the discharge escrow, the live
        registry). Nothing crosses yet; call `commit_confirm(hash)` to flush."""
        self._require()
        if self._owner is None:
            raise SessionError("no session owner is registered — nothing to commit")
        return self._owner.manifest()

    def commit_confirm(self, manifest_hash: str) -> dict:
        """Execute the approved commit — step 2 (Decision 3/4). The durable
        record order is exactly: `commit-approved`, then each `flushed`, then the
        one `discharge`, then `activation-complete`. If the queue or the live
        composition drifted since enumeration the recomputed hash mismatches and
        the confirm is REFUSED with a fresh manifest — what fires is exactly what
        was approved, never a superset."""
        driver = self._require()
        owner = self._owner
        if owner is None:
            raise SessionError("no session owner is registered — nothing to commit")
        try:
            flush = owner.approve(manifest_hash)   # commit-approved + flush FIFO
        except driver.runtime.SessionCommitError as exc:
            return {"committed": False, "refused": True, "reason": str(exc),
                    "manifest": owner.manifest()}
        # dispose the live frames: verdict is 'commit' and none is aborting, so
        # each discharges (mutation persists, witness GC'd), per-frame discharge
        # record suppressed — the owner writes the ONE consolidated one next.
        self._run(driver._dispose_all(self.ir))
        discharged = owner.finalize_commit()       # one discharge record
        self._commit_wal(driver)                   # activation-complete + close
        report = self._teardown_report(driver)
        prompts = dict(owner.prompts)
        self._reset()
        return {"committed": True, "flushed": flush["fired"],
                "flushResidue": flush["flushResidue"], "discharged": discharged,
                "prompts": prompts, **report}

    def abort(self) -> dict:
        """Abort the session (Decision 5): mark every live frame aborting BEFORE
        any teardown starts, drop the deferral queue (zero cost, zero crossings),
        replay the witnessed inverses, and write the `aborted` completion record.
        A session that only ever used classes (a) and (b) aborts to a provably
        clean world."""
        driver = self._require()
        owner = self._owner
        if owner is None:
            raise SessionError("no session owner is registered — nothing to abort")
        dropped = len(owner._queue)
        owner.begin_abort()                        # mark frames abort, drop queue
        self._run(driver._dispose_all(self.ir))    # replay inverses
        result = owner.finalize_abort()            # aborted record
        self._close_wal()
        report = self._teardown_report(driver)
        prompts = dict(owner.prompts)
        self._reset()
        return {"aborted": True, "replayed": result["replayed"],
                "droppedDeferred": dropped, "prompts": prompts, **report}

    # -- teardown plumbing -------------------------------------------------

    def _teardown_report(self, driver) -> dict:
        """The R4 residue checks after a teardown, and the drained trace."""
        checks = {
            "registry": driver.root.registry.size == 0,
            "provisions": driver.root.reflect.store == {},
            "effects": (driver.root.fiber._disposables.length
                        == driver._baseline_disposables),
            "listeners": driver._hooks() == driver._baseline_hooks,
        }
        detail = {
            "registrySize": driver.root.registry.size,
            "provisions": sorted(driver.root.reflect.store),
            "disposables": driver.root.fiber._disposables.length,
            "disposablesBaseline": driver._baseline_disposables,
        }
        driver.runtime.set_trace(None)
        return {"noResidue": all(checks.values()), "checks": checks,
                "detail": detail, "trace": driver.drain_events()}

    def _commit_wal(self, driver) -> None:
        """Stamp `activation-complete` and close the session's WAL (the recorder
        owns it in a Session; the driver's own `_commit_wal` is for `revl run`)."""
        if self.recorder is not None and self.recorder.wal is not None:
            names = [c.get("name") for c in (self.ir or {}).get("components") or []]
            self.recorder.commit_wal(names)

    def _close_wal(self) -> None:
        if self.recorder is not None and self.recorder.wal is not None:
            self.recorder.wal.close()

    def _reset(self) -> None:
        if self._driver is not None:
            self._driver.runtime.clear_session_owner()
        self._driver = None
        self._owner = None
        self.ir = None
        self.previous = None
        self.origin = None
        self.previous_origin = None
        self.draft = None
        self._generation = 0
        self._history = []
        # item 246: the class map, the outstanding-ticket table, and the approval
        # ledger are session-scoped — a new session starts with none, so a token
        # minted in one session can never be replayed in a later one over the same
        # workspace (invariant 5). `approval_policy` is a serve-time binding and is
        # deliberately NOT reset here.
        self._class_map = None
        self._tickets = {}
        self._ledger = []

    # -- interaction -------------------------------------------------------

    def call(self, key: str, method: str, args: list | None = None) -> dict:
        """Invoke a provided service operation on the running composition —
        how an agent actually *tests* what it just loaded."""
        driver = self._require()
        namespace = driver._namespace()
        if key not in namespace:
            raise SessionError(f"no provided key {key!r} "
                               f"(provided: {', '.join(sorted(namespace)) or 'none'})")
        service = namespace[key]
        if service is None:
            raise SessionError(f"key {key!r} is declared but not currently provided "
                               "— its provider is inactive")
        target = getattr(service, method, None)
        if target is None or not callable(target):
            raise SessionError(f"`{key}.{method}` is not callable on the provided value")

        # item 246: the auto-approve decision, at the single chokepoint every
        # internal re-invocation passes through — `replay_forward` re-invokes
        # `self.call(...)`, so a class-(c) replayed step is refused here exactly as
        # a fresh one (Fix 2, exit test 13). class none/(a)/(b) proceed and are
        # counted; class (c) consumes a standing approval or raises the ticket.
        # A no-policy session never enters this (returns immediately).
        self._approval_decide_call(key, method, args)

        async def invoke():
            result = target(*(args or []))
            if hasattr(result, "__await__"):
                result = await result
            await driver._flush()
            return result

        # tag whatever this call accumulates with the invocation that caused
        # it — that provenance is what makes forward replay expressible
        if self.recorder is not None:
            self.recorder.set_origin({"phase": "call", "key": key,
                                      "method": method, "args": list(args or [])})
        try:
            result = self._run(invoke())
        finally:
            if self.recorder is not None:
                self.recorder.activation_origin()
        return {"result": _plain(result), "trace": driver.drain_events()}

    # -- the auto-approve policy (roadmap item 246) ------------------------

    def _build_class_map(self, ir: dict):
        """The per-generation class map, or None when the policy is off. Derived
        from the CHECKED effect facts (`query.Composition` + the emission fixed
        point), activation scopes included — never the advisory schema walk
        (Decision 2, Fix 10)."""
        if self.approval_policy is None:
            return None
        from .approval import ClassMap  # noqa: PLC0415 — lazy, no cordis
        return ClassMap(ir)

    def _approval_wal(self):
        """The session WAL, when recording (an enabled policy requires it). None
        otherwise — the in-memory ledger still binds, but a policy load without
        recording is refused up front, so this is None only with the policy off."""
        return self.recorder.wal if self.recorder is not None else None

    def _find_standing_approval(self, ticket: dict):
        """The first unconsumed ledger entry that covers this ticket: same ticket
        hash, same reach-closure candidate hash against the LIVE generation, same
        component (invariants 2/4/5). A swap that changed the closure recomputes a
        different candidate hash, so a stale token fails here with no revocation
        bookkeeping."""
        for entry in self._ledger:
            if entry["consumed"]:
                continue
            if entry["hash"] != ticket["hash"]:
                continue
            if entry["candidateHash"] != ticket["candidateHash"]:
                continue  # candidate-invalidates: the closure changed under it
            if entry["component"] != ticket["component"]:
                continue  # non-replayable: minted for another component
            exp = entry.get("expiresAt")
            if exp is not None and self._now_ms() > exp:
                continue  # expiring: checked at the crossing, not at mint (inv. 3)
            return entry
        return None

    def _now_ms(self) -> int:
        """The session clock in ms (item 246, invariant 3). Injectable so expiry
        is testable without sleeping; defaults to the wall clock."""
        clock = getattr(self, "_clock_ms", None)
        if clock is not None:
            return clock()
        import time  # noqa: PLC0415
        return int(time.time() * 1000)

    def _consume_approval(self, entry: dict) -> None:
        """Spend the token durably BEFORE the crossing fires (Decision 3,
        consume-before-fire). A crash between the spend and the fire leaves
        consumed-but-unfired: fail-closed, a fresh approval is demanded."""
        entry["consumed"] = True
        wal = self._approval_wal()
        if wal is not None:
            wal.record_approval_consumed(entry["requestId"])

    def _count_posture(self, action_class: str | None) -> None:
        """Count a decided boundary call by posture (Decision 6). Class none is
        not a boundary call — it stays out of every bucket and the percent
        denominator."""
        owner = self._owner
        if owner is None or action_class is None:
            return
        bucket = {"a": "silent", "b": "atCommit", "c": "prompted"}.get(action_class)
        if bucket is not None:
            owner.approvals[bucket] += 1

    def _approval_decide_call(self, key: str, method: str, args) -> None:
        """The per-call decision (Decision 2). Off -> return immediately (byte-
        identical). class none/(a)/(b) -> proceed and count. class (c) -> consume
        a standing approval and proceed, else mint a ticket, count the prompt, and
        raise `ApprovalRequired` (the two-step's refusal)."""
        if self.approval_policy is None or self._class_map is None:
            return
        reach = self._class_map.classify_call(key, method)
        if reach is None:
            return  # no crossing (or unresolved) — not a boundary call
        klass = reach["class"]
        if klass in (None, "a", "b"):
            self._count_posture(klass)
            return
        # class (c): a standing approval, or a fresh ticket.
        from .approval import ApprovalRequired  # noqa: PLC0415
        ticket = self._class_map.build_ticket(reach, args)
        standing = self._find_standing_approval(ticket)
        if standing is not None:
            self._consume_approval(standing)   # durable spend before the fire
            return
        self._tickets[ticket["hash"]] = ticket
        if self._owner is not None:
            self._owner.prompts["perCall"] += 1
        self._count_posture("c")
        raise ApprovalRequired(ticket)

    def _enforce_activation_gate(self, ir: dict, class_map=None) -> None:
        """The activation gate (Fix 1): under an enabled policy, a candidate whose
        ACTIVATION reach is class (c) turns the load/swap response itself into the
        ticket two-step before boot. class (a)/(b) activation reach follows the
        table (proceed / enqueue, both activation-safe). Raises `ApprovalRequired`
        naming the activation crossing, unless a standing approval covers it."""
        cm = class_map if class_map is not None else self._class_map
        if self.approval_policy is None or cm is None:
            return
        from .approval import ApprovalRequired  # noqa: PLC0415
        for reach in cm.activation_reaches():
            if reach["class"] != "c":
                continue
            ticket = cm.build_ticket(reach)
            standing = self._find_standing_approval(ticket)
            if standing is not None:
                self._consume_approval(standing)
                continue
            self._tickets[ticket["hash"]] = ticket
            if self._owner is not None:
                self._owner.prompts["perCall"] += 1
            self._count_posture("c")
            raise ApprovalRequired(ticket)

    def _ticket_ttl_ms(self, ticket: dict) -> int | None:
        """The tightest ttl a bound policy imposes on this ticket's capabilities,
        or None (item 246, Slice 2). A ticket covering several capabilities is
        bounded by the shortest of their `requires approval ttl` rules."""
        if self.sandbox is None \
                or getattr(self.sandbox, "approval_rule_for", None) is None:
            return None
        ttls = []
        for cap in ticket.get("capabilities") or []:
            rule = self.sandbox.approval_rule_for(cap)
            if rule is not None and rule.ttl_ms is not None:
                ttls.append(rule.ttl_ms)
        return min(ttls) if ttls else None

    def approve_ticket(self, ticket_hash: str) -> dict:
        """Mint a standing approval bound to an outstanding ticket (Decision 2/3).
        Refuses a hash the server never issued (the outstanding-ticket table) — an
        approval can only be minted for a question the server actually asked. The
        entry is single-use and bound to the ticket hash, the reach-closure
        candidate hash, the component, and the session; a `approval-granted` WAL
        record makes it durable."""
        ticket = self._tickets.get(ticket_hash)
        if ticket is None:
            raise SessionError(
                f"unknown ticket hash {ticket_hash!r} — the server never issued "
                f"it (or the generation changed and the outstanding-ticket table "
                f"was replaced). Re-issue the call to get a fresh ticket, then "
                f"approve that (item 246, the outstanding-ticket table)")
        now = self._now_ms()
        # item 246, Slice 2: the ttl from the policy rule covering this ticket's
        # capabilities. The tightest (min) ttl over the covered required
        # capabilities bounds the token; None = session-end at the latest.
        ttl_ms = self._ticket_ttl_ms(ticket)
        entry = {
            "requestId": ticket["hash"],
            "hash": ticket["hash"],
            "candidateHash": ticket["candidateHash"],
            "component": ticket["component"],
            "key": ticket.get("key"),
            "method": ticket.get("method"),
            "argsDigest": ticket.get("argsDigest"),
            "kind": ticket.get("kind"),
            "session": self._session_id,   # invariant 5: session-bound
            "fields": {},           # the human's evidence (the language path fills)
            "grantedAt": now,
            "expiresAt": (now + ttl_ms) if ttl_ms is not None else None,
            "consumed": False,
        }
        self._ledger.append(entry)
        wal = self._approval_wal()
        if wal is not None:
            wal.record_approval_granted({
                "requestId": entry["requestId"], "hash": entry["hash"],
                "candidateHash": entry["candidateHash"],
                "component": entry["component"], "kind": entry["kind"]})
        return {"approved": True, "hash": ticket["hash"],
                "component": ticket["component"], "key": ticket.get("key"),
                "method": ticket.get("method"), "kind": ticket.get("kind"),
                "candidateHash": ticket["candidateHash"]}

    def approval_metrics(self) -> dict | None:
        """The auto-approve headline numbers for `session.state()` (Decision 6):
        the posture tally, percent auto-approved-with-proof, prompts-per-session,
        and the outstanding class-(c) tickets. None when no policy is configured,
        so `state()` stays byte-identical off-policy."""
        if self.approval_policy is None:
            return None
        owner = self._owner
        approvals = dict(owner.approvals) if owner is not None \
            else {"silent": 0, "atCommit": 0, "prompted": 0}
        prompts = dict(owner.prompts) if owner is not None else {}
        percent = owner.percent_auto_approved() if owner is not None else None
        return {
            "policy": self.approval_policy,
            "approvals": approvals,
            "percentAutoApproved": percent,
            "promptsPerSession": sum(prompts.values()) if prompts else 0,
            "prompts": prompts,
            "outstandingTickets": sorted(self._tickets),
        }

    # -- backwards replay (docs/replay.md) ---------------------------------

    def _timeline(self, component: str | None):
        if self.recorder is None:
            raise SessionError(
                "this composition was not loaded with recording on — call "
                "revl_load with `record: true` (recording has to be installed "
                "before activation, so it cannot be turned on retroactively)")
        replay = replay_module()
        try:
            return self.recorder.timeline(component)
        except replay.ReplayError as error:
            raise SessionError(str(error)) from None

    def timeline(self, component: str | None = None) -> dict:
        """The recorded accumulator: every effect step in order, its inverse,
        and every emission — marked as the one thing that has none."""
        if self.recorder is None:
            raise SessionError(
                "this composition was not loaded with recording on — call "
                "revl_load with `record: true`")
        if component is None:
            return self.recorder.as_dict()
        return self._timeline(component).as_dict()

    def inspect_step(self, component: str | None, at: int) -> dict:
        return self._timeline(component).inspect(at)

    def bisect(self, component: str | None, predicate: str) -> dict:
        """Binary-search the recorded timeline for the first step at which
        `predicate` flips — git-bisect for an execution. Read-only: the
        predicate is evaluated over the `inspect` reconstruction of each probed
        step, so the timeline is never mutated and the answer is reached in
        log2(N) evaluations. The report carries the found step's full record and
        the verified/unverified status of the effects on the path to it."""
        replay = replay_module()
        timeline = self._timeline(component)
        try:
            return timeline.bisect(predicate)
        except replay.ReplayError as error:
            raise SessionError(str(error)) from None

    def step_back(self, component: str | None, to: int,
                  force: bool = False) -> dict:
        """Unwind the accumulator to step `to`, leaving the component LIVE.

        This is not a teardown: no fiber is disposed, the provisions that
        survive stay callable, and the composition can be inspected and
        stepped forward again.
        """
        replay = replay_module()
        timeline = self._timeline(component)
        try:
            report = self._run(timeline.step_back(to, force=force))
        except replay.IrreversibleStep as error:
            raise SessionError(str(error)) from None
        except replay.ReplayError as error:
            raise SessionError(str(error)) from None
        if self._driver is not None:
            self._run(self._driver._flush())
            report["trace"] = self._driver.drain_events()
            report["providedKeys"] = sorted(
                k for k, v in self._driver._namespace().items() if v is not None)
        return report

    def replay_forward(self, component: str | None, frm: int) -> dict:
        """Re-run the tail after step `frm` by re-invoking the calls that
        produced it. Activation-body steps are reported as not replayable
        rather than faked — see docs/replay.md."""
        timeline = self._timeline(component)
        plan = timeline.forward_plan(frm)
        replayed = []
        for item in plan["replay"]:
            if item.get("kind") != "call":
                # a REPL-origin step (`revl run --record`); the session has no
                # REPL to re-evaluate it in, so it is reported, not guessed at
                replayed.append({**item, "error": "not replayable from a session "
                                                  "— this step came from a REPL line"})
                continue
            outcome = {"key": item["key"], "method": item["method"],
                       "args": item["args"]}
            try:
                outcome["result"] = self.call(item["key"], item["method"],
                                              item["args"])["result"]
            except ApprovalRequired as exc:
                # item 246, exit test 13: a replayed class-(c) crossing is refused
                # at the same chokepoint a fresh call hits (the decision lives in
                # `Session.call`, which `replay_forward` re-invokes), so the ticket
                # surfaces here too — the replay is not a bypass.
                outcome["approvalRequired"] = True
                outcome["ticket"] = exc.ticket
            except Exception as exc:  # noqa: BLE001 — a failed replay is a result
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            replayed.append(outcome)
        return {**plan, "replayed": replayed}

    def state(self, drain: bool = False) -> dict:
        if self._driver is None:
            # even with nothing loaded, the workspace's active leases (item 61)
            # are visible — an agent can survey who holds what before it loads.
            return {"loaded": False, "leases": self.leases.document()}
        driver = self._driver
        manifest = (self.ir or {}).get("manifest") or {}
        return {
            "loaded": True,
            "components": [
                {"name": name,
                 "state": driver.FiberState(fiber.state).name}
                for name, fiber in driver.fibers.items()
            ],
            "loadOrder": manifest.get("loadOrder") or [],
            "providedKeys": sorted(driver._namespace()),
            "canRollback": self.previous is not None,
            "recording": self.recorder is not None,
            "generation": self._generation,
            "history": [e["generation"] for e in self._history],
            "canUndo": len(self._history) >= 2,
            # active component leases (item 61): holder, component, expiry — the
            # multi-agent workspace, visible before anyone acts.
            "leases": self.leases.document(),
            # item 246: the auto-approve metrics, present only when a policy is
            # configured (off-policy `state()` is byte-identical).
            **({"approval": self.approval_metrics()}
               if self.approval_policy is not None else {}),
            **({"trace": driver.drain_events()} if drain else {}),
        }


def _ir_has_approval_edges(ir: dict) -> bool:
    """Whether any emit step in the IR carries a `with a` approval edge (item
    246). Cheap structural walk over the lowered body, used to switch the runtime
    frame check on only for programs that use the surface (byte-identity else)."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("step") == "emit" and node.get("approval") is not None:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False
    return walk(ir.get("components") or [])


def _plain(value):
    """Best-effort JSON-able rendering of a service call's result."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "__dict__") and vars(value):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return repr(value)
