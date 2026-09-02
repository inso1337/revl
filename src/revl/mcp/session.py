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
import hashlib
import sys
from fnmatch import fnmatchcase

from .. import cap_order
from .._paths import backends_root
from ..holes import collect as collect_holes
from ..holes import summarize as summarize_holes
from ..taint import REDACTED_SECRET
from .approval import ApprovalRequired
from .approval import _args_digest as _cache_args_digest


class SessionError(RuntimeError):
    """The session cannot do what was asked (no runtime, nothing loaded…)."""


def _cap_covers(wide: str, narrow: str) -> bool:
    """`covers(wide, narrow)` over the two stored capability spellings: does the
    `wide` cone contain `narrow` (narrow at or below wide in the one order)?

    The single point where the gate speaks the item-294 partial order (the
    representation mandate: one relation, one implementation in `cap_order`).
    Both arguments are canonical stored spellings (bare dotted tokens, or the
    `token(name=value,...)` form the parser and `cap_order` emit). Fails CLOSED
    and ADDITIVE: a spelling `cap_order` cannot parse (never produced by the
    frontend, but a hand-passed verb argument might be malformed) falls back to
    byte-identical string equality, exactly the pre-Slice-2 predicate, so a
    parameter-free gate is bit-for-bit unchanged and no malformed string is ever
    silently widened into a match."""
    if wide == narrow:
        return True                     # the fast path: identical stored spelling
    try:
        return cap_order.covers(cap_order.parse_cap(wide),
                                cap_order.parse_cap(narrow))
    except cap_order.CapError:
        return False                    # unparseable -> only exact-string matches


def _collect_lease_requests(ir: dict) -> list:
    """Every `effect lease` acquisition in a composition's activation bodies
    (item 294 Slice 2). A lease step is a `let-effect` step carrying a `lease`
    marker (`{capability, ttlMs, uses}`); the lowering is the only producer.
    Returns `[{component, capability, ttlMs, uses}]` for the gate to raise a
    ticket per un-satisfied lease. A composition with no lease step yields the
    empty list, so the gate is inert (byte-identity)."""
    out: list = []

    def _walk(steps, component):
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            lease = step.get("lease")
            if lease is not None:
                out.append({"component": component,
                            "capability": lease.get("capability"),
                            "ttlMs": lease.get("ttlMs"),
                            "uses": lease.get("uses")})
            # leases only appear at activation-body top level (a `let l = effect
            # lease …` prelude), but walk nested `if` branches defensively.
            for key in ("then", "else"):
                if step.get(key):
                    _walk(step[key], component)

    for comp in ir.get("components", []) or []:
        _walk(comp.get("body"), comp.get("name"))
    return out


class AdmitVerdict:
    """The outcome of an in-language `admit(source, granted)` crossing (roadmap
    item 330). The verdict IS the permission decision: the type judgment the
    admit gate made, handed back to the composition as data rather than raised.

    A REFUSED verdict (`admitted` False) carries the compile refusal as
    `message` — the repair signal a code-mode agent reads to fix its turn and
    try again — and never touches the running system. An ADMITTED verdict
    carries a `handle` whose crossings register into the ENCLOSING session's 245
    frame, so 245's commit/abort and 246's class→approval policy govern them
    uniformly with every other crossing in the session.
    """

    __slots__ = ("admitted", "message", "handle", "keys", "code")

    def __init__(self, admitted: bool, *, message: str | None = None,
                 handle: "AdmitHandle | None" = None,
                 keys: tuple[str, ...] = (), code: str | None = None) -> None:
        self.admitted = admitted
        self.message = message
        self.handle = handle
        self.keys = tuple(keys)
        self.code = code

    def as_dict(self) -> dict:
        return {"admitted": self.admitted, "message": self.message,
                "keys": list(self.keys), "code": self.code}


class AdmitHandle:
    """A callable handle onto an admitted per-turn composition (roadmap item
    330). Its `call` runs the turn's provided operations through the SAME path a
    top-level `call` takes, so a granted-emission or witnessed-fs crossing the
    turn makes registers into the enclosing session's 245 frame — the whole
    point of the crossing: the per-turn actions are governed by the session's
    commit/abort, not a separate lifecycle the turn could escape into."""

    def __init__(self, session: "Session", keys: tuple[str, ...]) -> None:
        self._session = session
        self.keys = tuple(keys)

    def call(self, key: str, method: str, args: list | None = None) -> dict:
        if key not in self.keys:
            raise SessionError(
                f"key {key!r} is not one the admitted turn provides "
                f"(provides: {', '.join(self.keys) or 'none'})")
        return self._session.call(key, method, args)


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


def _activation_error():
    """The `run.ActivationError` type, imported lazily (importing `run` pulls
    cordis). Used by `load`/`swap` to catch a deferred activation that did not
    complete and convert it into a `SessionError` (roadmap item 372)."""
    from ..run import ActivationError  # noqa: PLC0415 — lazy: run imports cordis

    return ActivationError


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
        # item 290, §4: gauntlet dossiers this session produced (candidate name
        # -> the `mcp.gauntlet.run` dossier), so a `mcp requires evidence
        # [gauntlet admissible]` rule can threshold the operator-run session
        # gauntlet at admission. Operator-produced at evaluation time, so it
        # needs no attestation root. Populated by the gauntlet verb; empty until
        # a candidate has been graded.
        self._gauntlet_dossiers: dict = {}
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
        # roadmap 425 F3 / 427 F5: whether a CALLER-SUPPLIED resource valuation
        # may be written into the durable, cross-session approval WAL when a
        # crossing is approved. "bound" (the default, and what shipped) records
        # it — item 251's N1, what lets a distilled rule name a target. "withheld"
        # records it as UNRECORDED instead, closing the durable disclosure at the
        # cost of the distiller's fold over caller-argument targets. Author-written
        # literals are recorded under both. Set at serve time (`revl mcp serve
        # --approval-record-values`); see `_distillation_ledger_fields`.
        self.approval_record_values = "bound"
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
        # roadmap item 344 (fork b of the 246 open question 2 / 251 distillation
        # target, one item early): SESSION-SCOPED STANDING GRANTS an operator
        # mints once against a CAPABILITY (not one ticket hash) and per-call
        # class-(c) crossings consume against — decrementing `remainingUses`,
        # checked against `expiresAt` — instead of prompting single-use. Each
        # entry is keyed by (capability, reach-closure candidateHash, component,
        # session), so it takes the shell-escape shape (n repeat class-(c) calls
        # to the same capability, differing args) from n prompts to one mint.
        # Every Slice-1 invariant is preserved: hash-bound to the capability's
        # semantic identity, expiring (injectable clock, checked at the
        # crossing), single-session, and consumed durably via the same
        # consume-before-fire `approval-consumed` WAL. Persists across a swap
        # (the candidateHash check invalidates a stale grant, exactly as
        # `_ledger` does); reset per session (invariant 5).
        self._grants: list = []
        # roadmap item 251 Slice 2: distilled `AutoApproveRule`s loaded from the
        # bound policy (`self.sandbox.auto_approve_rules`) into a persistent
        # standing-grant analog, matched by a component glob + realm + resource
        # scope + taint-subset gate rather than a frozen candidate hash. Unlike a
        # 344 grant (session-scoped, minted at runtime), these are POLICY: an
        # operator wrote them (or applied a distilled offer), so they persist and
        # are re-materialized each generation. `_auto_reviewed` persists the
        # enumerated glob-member set each rule was reviewed against (the H1 bind,
        # §6 A1): a component ENTERING the glob that was not in that set suspends
        # the rule, fail-closed. Keyed by the rule's canonical DSL so it survives a
        # swap's re-materialize. Reset per session.
        self._auto_rules: list = []
        self._auto_reviewed: dict = {}
        # the in-memory mirror of the `approval-granted` / `approval-denied`
        # records this session produced, WITH the item-251 shape-key fields, so
        # `distillation_offers` folds the live ledger without re-reading the WAL.
        self._approval_records: list = []
        # how many per-call class-(c) crossings auto-approved against a distilled
        # rule, and the offers/revokes applied this session (attribution + metrics).
        self._auto_consumed: int = 0
        self._distillation_seq: int = 0
        # how many per-call class-(c) crossings auto-approved against a standing
        # grant this session — the grant-consumed half of the metrics the 344
        # measurement reads beside prompts-per-session (Decision 6). Off-policy
        # this is never surfaced (`approval_metrics` returns None), so the
        # manifest and `state()` stay byte-identical.
        self._grants_consumed: int = 0
        # item 310 (capability-aware caching): the seam-method cache. The index
        # maps a provided (key, method) to its `cache` IR descriptor, rebuilt per
        # generation next to the class map. `_cache_entries` is the authority-
        # scoped entry store (keyed on the recorded grant + generation + args
        # digest); an entry lives only while its recorded grant, its generation,
        # its ttl, and its `invalidated_by` epochs all stand, so a hit can never
        # launder authority (the check fires on every access; a live hit only
        # skips the host body and the use consumption a miss performs). No ledger
        # (no policy) means no entry store: every access is a miss and the
        # capability/external declaration is dynamically inert (design
        # §enforcement, no-policy decision). `_cache_inval_epoch` bumps when a
        # crossing of a subscribed `invalidated_by` token fires, WAL-ordered so a
        # session never reads its own stale write. `cache pure` is memoized in the
        # emitted body and never reaches this store. All reset per generation.
        self._cache_index: dict = {}
        self._cache_entries: dict = {}
        self._cache_inval_epoch: dict = {}
        self._cache_inval_tokens: set = set()
        self._cache_hits: int = 0
        # `cache pure` memo table: keyed on (key, method, args digest), no
        # authority scope (the pure class crosses nothing, so it has no ledger
        # interaction by construction — it memoizes even in a no-policy session).
        # Generation-scoped like the entry store.
        self._cache_pure: dict = {}
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
        # roadmap item 330: per-turn sources admitted THROUGH the in-language
        # crossing while a call is driving the loop, queued for wiring the moment
        # that call returns (a turn is never wired mid-call, exactly as the
        # self-evolution loop never swaps synchronously). Empty in every session
        # that does not admit, so nothing changes for one that does not.
        self._pending_admits: list[dict] = []
        # item 250 (session branching): once a session is FORKED, it is FROZEN —
        # retired at the fork step k, non-callable, so the shared rewound
        # workspace has exactly one live owner, the branch (Decision 4). `_frozen`
        # gates every op through `_require`; `_fork_at` names the step it was
        # frozen at, for the refusal message. A fresh session is never frozen.
        self._frozen = False
        self._fork_at: int | None = None
        # the pending fork enumeration (item 250, Decision 1): `fork` stores the
        # hash-bound rewound span here, and `fork_confirm` re-derives the hash and
        # refuses on any drift, exactly as the 245 commit binds its manifest hash.
        self._fork_pending: dict | None = None

    # -- plumbing ----------------------------------------------------------

    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    @property
    def loaded(self) -> bool:
        return self._driver is not None

    def _require(self):
        if self._frozen:
            # item 250, Decision 4: a forked parent is retired at k and
            # non-callable — the branch is the only live continuation over the
            # shared rewound workspace. This is what makes "a second concurrent
            # fork" and any parent op after the fork a refusal, by construction.
            raise SessionError(
                f"session forked at step {self._fork_at}, frozen — a forked "
                "parent is retired at k and non-callable; the branch is the only "
                "live continuation over the shared workspace (item 250)")
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
        self._enforce_evidence(ir)
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
        # item 251 Slice 2: materialize the bound policy's distilled auto-approve
        # rules against this generation, atomically with the class map so a call is
        # never decided against a stale rule set. Inert (empty) when the policy
        # names no `auto-approve` rule, so an off-distillation load is byte-identical.
        self._install_auto_approve_rules()
        # item 310: the seam-method cache index, rebuilt atomically with the class
        # map so a call is never decided against a stale cache contract. Surface H
        # runs first: the applicability fold over the PROVIDER closure, which only
        # a linked composition knows, refuses an uncacheable reach before boot.
        self._check_cache_applicability(ir, self._class_map)
        self._install_cache_index(ir)
        self._enforce_activation_gate(ir)
        # item 294 Slice 2: the capability-lease gate, alongside the activation
        # gate and BEFORE any runtime is touched. A `let l = effect lease …`
        # acquisition is class-(c)-gated and ticket-mediated: an ungated run
        # refuses it (no operator to consent), and a gated run raises ONE ticket
        # the operator mints the standing grant from — never a silent self-mint.
        self._enforce_lease_gate(ir)
        emit, runtime_mod, Context, FiberState = _backend()
        driver_class = _capturing_driver_class()
        self.config = config or {}
        self._driver = driver_class(ir, self.config, emit, runtime_mod, Context, FiberState)
        self.ir = ir
        self.origin = origin
        self.draft = None
        # roadmap item 330: bind this session as the live target of the classified
        # in-language `admit` crossing (stdlib/admit.rvl), so a composition that
        # `use`s it admits per-turn sources against THIS running session. Bound
        # here — before any activation body runs — so the crossing is live from
        # the first call. A no-op import cost for a composition that never admits.
        from . import admit_bridge as _admit_bridge  # noqa: PLC0415
        _admit_bridge.bind(self)
        # roadmap item 403: bind this session as the live target of the
        # un-privileged in-language `resolved_keys()` reflection query
        # (stdlib/reflect.rvl), so a composition that composes it reflects THIS
        # running session. A pure read — no frame, no approval — bound alongside
        # admit so both in-language seams see the same live session.
        from . import reflect_bridge as _reflect_bridge  # noqa: PLC0415
        _reflect_bridge.bind(self)
        self.recorder = replay_module().Recorder(ir) if record else None
        self._generation = 1
        self._history = []
        # item 246, Decision 2 (F6): make the refusal above TRUE. It refuses a
        # policy load without recording because "without a WAL there is no
        # durable approval spend and no audit join" — but recording alone never
        # opened one: the only opener was `_configure_owner_approvals`, gated on
        # `_typed_approval_active`, so a composition with no typed-approval edge
        # and no `requires approval` rule ran the WHOLE ticket two-step against a
        # dict. `_consume_approval` and `_consume_grant` document
        # consume-before-fire as crash-safe BECAUSE the spend is durable, so open
        # the WAL for every policy-enabled session, here, before any activation
        # body can cross. `_ensure_wal_open` is idempotent, so the typed-approval
        # path below finds it open and is unchanged.
        if self.recorder is not None and (
                self.approval_policy is not None or policy_requires_approval
                or _ir_has_approval_edges(ir)):
            self._ensure_wal_open()
        # item 245: register the session commit-state owner before the
        # composition loads, so every activation frame joins its live-frame
        # registry (the commit verb's gate target). Installed through the shared
        # helper `swap` and `_abort_swap` also use, so no generation's load can
        # drift out of owner scope (the successor frames would otherwise capture a
        # cleared ambient owner and take the pre-245 implicit-commit path).
        self._install_session_owner(ir)
        try:
            self._run(self._driver._load(ir, self._prepare_module(ir)))
        except _activation_error() as exc:
            # item 372: a component's deferred activation did not complete —
            # "loaded" would be a lie. Tear the half-loaded composition down and
            # report not-loaded, surfacing the loud, named diagnostic instead of
            # leaving a fiber listed ACTIVE with no provision in ROOT.
            runtime_mod.clear_session_owner()
            try:
                self._run(self._driver._dispose_all(ir))
            except Exception:  # noqa: BLE001 — best-effort teardown of a partial load
                pass
            self._reset()
            raise SessionError(str(exc)) from exc
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

    def _install_session_owner(self, ir: dict) -> None:
        """Create a FRESH session commit-state owner for the generation about to
        load, seed its typed-approval state, and make it the process-global owner
        (item 245) BEFORE any activation Frame is built.

        Every path that loads a generation — `load`, `swap`'s successor, and
        `_abort_swap`'s reloaded predecessor — installs the owner this way, so the
        generation's frames all capture a live `_SESSION_OWNER` at `Frame.__init__`
        and join its live-frame registry (the commit/abort gate target). Without
        it, a successor generation's frames capture a cleared ambient owner (None),
        never `register_frame`, and take the pre-245 implicit-commit path at
        teardown — so a later session abort restores nothing. One owner per
        generation matches `load` (each `load` builds a fresh owner) and the swap
        lifecycle (the previous generation's frames were disposed at `swap`'s
        `_dispose_all` before the successor loads). The caller guarantees
        `clear_session_owner()` in a finally that covers the load."""
        runtime_mod = self._driver.runtime
        prev = self._owner
        # through `_approval_wal`, so the owner sees an OPEN log or none — a
        # closed one must never be written into from inside a crossing.
        self._owner = runtime_mod.SessionOwner(wal_getter=self._approval_wal)
        # item 247 second-pass (F2, data loss): a hot-swap disposes the
        # PREDECESSOR generation (`swap`/`_abort_swap` run `_dispose_all` just
        # above) while the predecessor owner's verdict is still pending, so those
        # frames ESCROW their undischarged transactional/compensation entries
        # into `prev` (`Frame.drain`'s mid-session-withdrawal branch). Installing
        # a fresh owner for the successor generation must CARRY THAT ESCROW OVER,
        # or the pre-swap witnessed mutations are orphaned in the dead owner: a
        # later `Session.abort()` reverts nothing (`begin_abort`/`finalize_abort`
        # iterate the NEW owner's escrow) and a commit never discharges their WAL
        # descriptors (`_witnessed_seqs` reads the new owner), so `revl recover`
        # would roll back committed work. Transfer the escrow and the session-
        # cumulative residue/prompt bookkeeping onto the successor; the per-
        # generation approval state is re-seeded fresh below (the reach-closure
        # candidate hashes change per generation, so a stale ledger must NOT
        # carry over). `load` sees `prev is None` (a fresh session), so this is
        # inert there — only a swap/abort-swap has a predecessor to inherit from.
        if prev is not None:
            self._owner._escrow = prev._escrow
            self._owner.compensation_residue = prev.compensation_residue
            self._owner.prompts = prev.prompts
            self._owner.flush_residue = prev.flush_residue
            self._owner.approvals = prev.approvals
        # item 246, Slice 3: seed the SessionOwner with the typed-approval state
        # BEFORE the activation body runs, so a `with a` crossing in the activation
        # body checks and consumes its token against the live ledger (the runtime
        # frame check). No-op unless the program uses the surface, so a session
        # without typed approvals is byte-identical.
        self._configure_owner_approvals(ir)
        # item 294 Slice 2: the lease acquire/revoke bridge. Inert unless the
        # program acquires a lease; the acquisition resolves the grant the lease
        # gate already minted, and its disposer's own-requestId revoke rides here.
        self._owner.lease_acquire = self._runtime_lease_acquire
        self._owner.lease_revoke = self._runtime_lease_revoke
        runtime_mod.set_session_owner(self._owner)

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
        #
        # item 413 rides in `_ensure_wal_open`: the approval WAL defaults into a
        # durable, owner-only per-user state directory, not the reboot-wiped/
        # world-traversable tempdir. "The gate's authority is the WAL" — it must
        # outlive a reboot and stay unreadable to other local accounts. Idempotent,
        # so a policy session whose log `load` already opened is untouched.
        self._ensure_wal_open()

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

    def record_gauntlet(self, dossier: dict) -> None:
        """Store an admissible gauntlet dossier under each component name it
        graded (item 290, §4), so a later `mcp requires evidence [gauntlet
        admissible]` admission can read the operator-run session dossier."""
        if not isinstance(dossier, dict) or dossier.get("verdict") != "admissible":
            return
        for comp in (dossier.get("candidate") or {}).get("components") or []:
            name = comp.get("name")
            if name:
                self._gauntlet_dossiers[name] = dossier

    def _enforce_evidence(self, ir: dict) -> None:
        """item 290, §4: the confidence/evidence admission rules over agent
        output. When the sandbox policy carries `requires evidence` rules, every
        MCP-admitted component must clear its thresholds. The live session
        gauntlet dossier (operator-run, no attestation root) is plumbed in for
        components it graded; every other facet stays `unavailable` for a draft,
        so a rule thresholding published evidence refuses fail-closed.

        Additive: a no-op unless the bound sandbox names evidence rules, so an
        evidence-free sandbox admits exactly as before."""
        if self.sandbox is None \
                or not getattr(self.sandbox, "evidence_rules", ()):
            return
        from ..audit_diff import audit_report  # noqa: PLC0415 — lazy, no cordis
        from ..policy import evaluate, first_error  # noqa: PLC0415
        from .. import registry as reg  # noqa: PLC0415

        audit = audit_report(ir)
        everyone = frozenset(audit.get("boundary") or {})
        evidence = {
            name: reg.EvidenceBundle(gauntlet=self._gauntlet_dossiers[name])
            for name in everyone if name in self._gauntlet_dossiers}
        error = first_error(evaluate(self.sandbox, audit,
                                     mcp_components=everyone, evidence=evidence))
        if error is not None:
            raise SessionError(str(error).split("\n")[0])

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
        self._enforce_evidence(ir)
        # item 246: classify the candidate and gate its activation reach BEFORE
        # any teardown — a swap-in whose activation body reaches a class-(c)
        # emission answers for it with the ticket two-step before it boots, which
        # is the bypass Fix 1 exists to keep shut (an emission moved from a
        # provide-method into the activation body must not dodge the prompt). The
        # new map replaces the live one atomically only once the swap completes,
        # so a call mid-swap is impossible.
        new_map = self._build_class_map(ir)
        self._enforce_activation_gate(ir, class_map=new_map)
        # item 310, surface H: the successor's cache declarations are folded over
        # the SUCCESSOR's provider closure, here — before any teardown — so a swap
        # that moves a cached method onto an uncacheable reach refuses with the
        # running composition untouched.
        self._check_cache_applicability(ir, new_map)
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
        # item 334 (EDGE 1): the keys gen N ACTUALLY served, captured before
        # teardown while its providers are still live. The health gate below
        # holds the successor to these — see `_assert_successor_activated`.
        pre_resolved = set(driver.resolved_keys())
        saved_previous, saved_previous_origin = self.previous, self.previous_origin
        self.previous = self.ir
        self.previous_origin = self.origin
        self._run(driver._dispose_all(self.ir))
        driver.ir = self.ir = ir
        self.origin = origin
        self.draft = None  # a new generation makes any uncommitted edit stale
        self._generation += 1
        # item 245: install a fresh owner for the successor generation BEFORE its
        # load, exactly as `load` does, so every frame the successor builds joins
        # this generation's live-frame registry. Without this the successor's
        # frames captured the cleared ambient owner (None) and took the pre-245
        # implicit-commit path, so a post-swap witnessed mutation could never be
        # aborted (it was made permanent — the swap-owner-scoping data-loss bug).
        self._install_session_owner(ir)
        try:
            self._run(driver._load(ir, self._prepare_module(ir)))
            # item 334 (EDGE 1): the POST-ACTIVATION HEALTH GATE. `driver._load`
            # returns WITHOUT raising for the two most likely candidate faults —
            # item 372 makes a mid-body FAILED activation "honest and observable"
            # (non-raising, run.py:_settle) and leaves an unmet-requirement fiber
            # PENDING (also non-raising). Without a gate here, `swap` would have
            # already disposed gen N above and would now install a broken gen N+1
            # that provides nothing, with no gen N to fall back to — the exact
            # opposite of the revert guarantee. So assert the successor activated
            # CLEANLY and, if not, raise into the `_activation_error` branch below,
            # which routes to `_abort_swap` (revert to gen N, keep serving gen N).
            self._assert_successor_activated(ir, pre_resolved)
        except _activation_error() as exc:
            # item 372: the successor's activation did not complete — roll the
            # whole swap back to the predecessor (which activated cleanly) so the
            # running system keeps serving, and surface the loud diagnostic
            # rather than leaving a half-loaded generation reporting loaded.
            # `_abort_swap` reinstalls the owner around the predecessor reload.
            self._abort_swap(old_ir, pre, saved_previous, saved_previous_origin,
                             handoff_pre)
            self._record_generation()
            raise SessionError(
                f"swap rejected: {exc}. The running composition is untouched "
                f"(rolled back to the previous generation)."
            ) from None
        finally:
            # frames are built during the successor load; stop capturing so a
            # later, unrelated Frame does not join this generation's registry.
            driver.runtime.clear_session_owner()
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
        # item 251 Slice 2: re-materialize the distilled rules against the new
        # generation. The H1 review bind (`_auto_reviewed`) persists across the
        # swap, so a component the swap moves INTO a rule's glob that was not in the
        # reviewed set suspends the rule (fail-closed), never silently carried.
        self._install_auto_approve_rules()
        # item 310: the new generation's cache index goes live atomically too, and
        # every prior-generation entry dies (a generation change is a liveness
        # event — a swapped provider may answer differently, so a stale entry is a
        # laundered result).
        self._install_cache_index(ir)
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
        # item 245: the reloaded predecessor is a fresh generation — install its
        # owner so its frames join the live-frame registry (else a subsequent
        # abort of the rolled-back session would restore nothing, the same bug the
        # swap successor had). Cleared once the reload's frames are all built.
        self._install_session_owner(old_ir)
        try:
            self._run(driver._load(old_ir, self._prepare_module(old_ir)))
        finally:
            driver.runtime.clear_session_owner()
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

    def _assert_successor_activated(self, ir: dict,
                                    pre_resolved: set | None = None) -> None:
        """The item-334 post-activation health gate (EDGE 1).

        `driver._load` returns cleanly even when the successor did not truly come
        up: item 372 makes a mid-body FAILED activation non-raising and leaves an
        unmet-requirement fiber PENDING. A swap that trusted the clean return
        would dispose gen N and install a broken gen N+1 that provides nothing.
        So assert, after the load, that the successor is HEALTHY:

          * no successor fiber is left FAILED or PENDING, and
          * every key the successor is RESPONSIBLE for providing resolves to a
            live provider (`driver.resolved_keys()`, run.py:878).

        Part 2's responsibility set has two halves.

        ROOT provisions — every key declared by a NON-template component. A
        template component's key is excluded here (`manifest.templates`, roadmap
        item 10): a template's provisions come up PER-INSTANCE through the
        dynamic instance layer, reached through the spawn handle, never at ROOT,
        so requiring ROOT resolution for them would reject every legitimate
        instance composition (tests/test_instance_migration.py). A template
        component also gets no top-level fiber of its own, so Part 1 never sees
        it, and a genuinely broken instance migration is caught by the
        state-compat gate in `_reconcile_instances`, not here.

        INHERITED provisions — every key gen N ACTUALLY SERVED (`pre_resolved`,
        captured in `swap` before teardown) that the successor still DECLARES.
        This half exists because `manifest.templates` is derived from the
        candidate's own `spawn` targets (lower.py) and is therefore CANDIDATE-
        CONTROLLED: naming its own provider in a `spawn` lifts that provider out
        of the static composition AND, under a templates-only exclusion, out of
        the gate — so a successor could declare `greeter`, provide it nowhere a
        caller can reach, and still be installed over a live gen N that was
        serving `greeter`. The exclusion may say "this key comes up
        per-instance"; it may not say "this key may stop existing while the
        composition keeps claiming it". Holding the successor to the keys its
        predecessor genuinely served closes that without judging keys the
        predecessor never provided: a composition may introduce a brand-new
        template-provided key (legitimately absent from ROOT), and may drop a
        key outright (the successor then no longer declares it, and `state()`
        and `Gate.propose` both report the smaller set honestly). What it may
        not do is claim a key it inherited and deliver nothing.

        On any failure, raise `ActivationError` so the enclosing `swap` catches it
        in its `_activation_error()` branch and routes to `_abort_swap` — reverting
        to gen N exactly as a raised activation fault does. `_dispose_all` in the
        predecessor reload has already run against the successor, so a witnessed
        effect the successor's activation performed before it faulted is reverted
        as part of the rollback (the 245 owner was installed before this load)."""
        ActivationError = _activation_error()
        driver = self._driver
        # 1) no successor fiber may be FAILED or PENDING.
        for name, fiber in driver.fibers.items():
            state = driver.FiberState(fiber.state).name
            if state in ("FAILED", "PENDING"):
                comp = next((c for c in (ir.get("components") or [])
                             if c.get("name") == name), {})
                keys = list((comp.get("provides") or {}).keys())
                raise ActivationError(
                    name, keys,
                    f"the successor fiber is {state} after the swap load — a swap "
                    f"must not install a generation whose component failed to "
                    f"activate (FAILED) or has an unmet requirement (PENDING). "
                    f"item 372 makes both non-raising, so item 334's health gate "
                    f"rejects them here")
        # 2) every key the successor is responsible for must resolve to a live
        #    provider: the ROOT provisions (non-template declarations) plus the
        #    INHERITED ones (keys gen N actually served that the successor still
        #    declares — including through a component the candidate's own
        #    `manifest.templates` names, which is why the exclusion cannot be
        #    taken on the candidate's word alone).
        templates = set((ir.get("manifest") or {}).get("templates") or [])
        declared_root: set[str] = set()
        declared_all: set[str] = set()
        for comp in (ir.get("components") or []):
            keys = set((comp.get("provides") or {}).keys())
            declared_all |= keys
            if comp.get("name") not in templates:
                declared_root |= keys
        inherited = declared_all & set(pre_resolved or ())
        resolved = driver.resolved_keys()
        missing = sorted((declared_root | inherited) - resolved)
        if missing:
            demoted = sorted((inherited - declared_root) - resolved)
            detail = (
                f" Key(s) {demoted} were served by the previous generation and "
                f"are still declared by the successor, but its composition puts "
                f"them out of ROOT's reach — a swap may not retire a live "
                f"provision while still claiming it."
                if demoted else "")
            raise ActivationError(
                "<successor>", missing,
                "declared provided key(s) did not resolve to a live provider "
                "after the swap load — the successor composition would report "
                f"loaded while ROOT lacks the provision (item 334 health gate).{detail}")

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
        elif owner is not None:
            # item 247 second-pass (F3, data loss): an aborting unload must
            # REPLAY the escrow, exactly as `Session.abort` does. The
            # not-aborting branch discharges via `finalize_commit`; the aborting
            # branch previously did NEITHER, so an escrowed entry (a mid-session
            # -withdrawn component's witnessed mutation) was dropped un-reverted
            # and no `aborted` WAL completion record was written. `_dispose_all`
            # above already replayed the live frames' inverses (their frames are
            # `_aborting` and the verdict is `abort`). Mark the ESCROWED frames
            # aborting too, exactly as `begin_abort` does before an explicit
            # abort: their `_owner` is the dead predecessor generation's owner
            # whose verdict never settled, so without this `_hold_for_session`
            # would re-hold the entry (frame still `_holding`, dead owner still
            # pending) and `finalize_abort` would skip the replay. Then
            # `finalize_abort` replays the escrowed entries (LIFO, in its two
            # phases) and writes the `aborted` record naming every seq that ran.
            for entry in owner._escrow:
                entry.frame.abort()
            owner.finalize_abort()
        residue = self._surface_compensation_residue(owner)
        report = self._teardown_report(driver)
        self._reset()
        return {"unloaded": True, "compensationResidue": residue, **report}

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
        residue = self._surface_compensation_residue(owner)
        report = self._teardown_report(driver)
        prompts = dict(owner.prompts)
        self._reset()
        return {"committed": True, "flushed": flush["fired"],
                "flushResidue": flush["flushResidue"], "discharged": discharged,
                "prompts": prompts, "compensationResidue": residue, **report}

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
        self._run(driver._dispose_all(self.ir))    # replay inverses (+ Phase 2)
        result = owner.finalize_abort()            # aborted record (+ escrow Phase 2)
        self._close_wal()
        residue = self._surface_compensation_residue(owner)
        report = self._teardown_report(driver)
        prompts = dict(owner.prompts)
        self._reset()
        return {"aborted": True, "replayed": result["replayed"],
                "droppedDeferred": dropped, "prompts": prompts,
                "compensationResidue": residue, **report}

    # -- session branching (roadmap item 250) ------------------------------

    def _fork_timeline(self, component: str | None):
        """The timeline the fork rewinds, with `at` bound and enumeration ready.
        Raises `SessionError` (recording off / unknown component / bad step)."""
        return self._timeline(component)

    def _fork_target(self, timeline, at: int) -> dict:
        """The hash payload binding a fork (item 250, Decision 1): the rewound
        span identity, the crossed set, the would-cross set, and the live
        composition. Any drift since enumeration — a new effect, a new emission,
        a swap — changes this, so `fork_confirm` refuses a stale hash exactly as
        `SessionOwner.approve` refuses a stale commit hash."""
        part = timeline.partition_tail(at)
        return {
            "component": timeline.component,
            "at": part["at"],
            "inversesRan": [e["index"] for e in part["inversesRan"]],
            "provisionsWithdrawn": [e["index"] for e in part["provisionsWithdrawn"]],
            "emissionsCrossed": [e["index"] for e in part["emissionsCrossed"]],
            "emissionsCompensated": [e["index"] for e in part["emissionsCompensated"]],
            "wouldCrossOnRewind": [e["index"] for e in part["wouldCrossOnRewind"]],
            "unrestored": [e["index"] for e in part["unrestored"]],
            "composition": sorted(
                c.get("name") for c in (self.ir or {}).get("components") or []),
        }

    @staticmethod
    def _fork_hash(target: dict) -> str:
        import json  # noqa: PLC0415 — stdlib
        return hashlib.sha256(
            json.dumps(target, sort_keys=True).encode("utf-8")).hexdigest()

    def _fork_report(self, timeline, at: int) -> dict:
        """Build the honest fork report from the total tail partition (item 250,
        Decision 3), plus the `residue.clean` verdict and the binding hash."""
        part = timeline.partition_tail(at)
        queue = []
        if self._owner is not None:
            queue = [d.descriptor() for d in self._owner._queue]
        crossed = part["emissionsCrossed"]
        offset = part["emissionsCompensated"]
        would = part["wouldCrossOnRewind"]
        unrestored = part["unrestored"]
        clean = not (crossed or offset or would or unrestored)
        outstanding = sorted(
            e["index"] for e in (crossed + offset + would + unrestored))
        target = self._fork_target(timeline, at)
        report = {
            "forked": False,                       # enumeration only; nothing rewound
            "at": part["at"],
            "atLabel": part["atLabel"],
            "parent": self._session_id,
            "rewound": {
                "inversesRan": part["inversesRan"],
                "provisionsWithdrawn": part["provisionsWithdrawn"],
            },
            "droppedDeferred": queue,              # held, never crossed
            "emissionsCrossed": crossed,
            "emissionsCompensated": offset,
            "wouldCrossOnRewind": would,
            "unrestored": unrestored,
            "residue": {
                "clean": clean,
                "outstanding": outstanding,
                "proof": (
                    f"{len(crossed)} emission(s) crossed the boundary between step "
                    f"{part['at']} and head and cannot be undone; {len(would)} "
                    "inverse(s) whose scope crosses the boundary were enumerated "
                    f"but not fired; {len(unrestored)} step(s) could not be "
                    "restored. All are listed above. The host-confined fs state "
                    "and provisions above this line were restored to step "
                    f"{part['at']}; nothing else is claimed." if not clean else
                    "the rewound span is only host-confined witnessed effects, "
                    "provisions, and held sends: nothing crossed, nothing would "
                    "cross on rewind, nothing was left unrestored — a provably "
                    "exact rewind to step " + str(part["at"]) + "."),
            },
            "warning_emissions": (
                f"{len(crossed)} emission(s) are still out in the world."
                if crossed else "no emission crossed the boundary in the rewound "
                "span."),
            "guarantee": replay_module().GUARANTEE,
            "hash": self._fork_hash(target),
        }
        return report

    def fork(self, at: int, component: str | None = None) -> dict:
        """Step 1 of the two-step, hash-bound session fork (item 250, Decision 1).

        ENUMERATES: walks the whole tail (steps > `at`) into the total honest
        partition (Decision 3) and returns the fork report plus a `hash` binding
        the rewound span and the live composition. Nothing is rewound yet, no
        workspace state changes, no branch is minted — the crossed-emission and
        would-cross-on-rewind residue MUST be seen (and acknowledged through the
        hash) before the rewind happens.

        Refuses up front (a clear diagnostic, no side effect): a fork whose tail
        contains a `KIND_OPAQUE` step the recorder cannot restore (Decision 3); a
        fork whose rewound span holds a declared non-idempotent-total inverse
        (Decision 5); and a fork before an already-flushed commit boundary
        (Decision 6)."""
        self._require()
        timeline = self._fork_timeline(component)
        try:
            at = timeline._bound(at)
        except replay_module().ReplayError as error:
            raise SessionError(str(error)) from None
        self._fork_refuse(timeline, at)
        report = self._fork_report(timeline, at)
        self._fork_pending = {
            "hash": report["hash"], "at": at, "component": timeline.component,
        }
        return report

    def _fork_refuse(self, timeline, at: int) -> None:
        """The Slice-1 fork refusals (item 250, Decision 3/5/6). Each raises a
        `SessionError` with a clear diagnostic and touches nothing."""
        # Decision 6: a fork whose `at` lies before a crossing that has already
        # durably committed (a `flushed`/`commit-approved` record exists) is
        # refused — you cannot rewind past a send that already committed and
        # closed. Within one uncommitted activation there are no such records.
        if self._has_committed_boundary():
            raise SessionError(
                "fork refused: this session's WAL already carries a durably "
                "committed crossing (a `flushed`/`commit-approved` record). A fork "
                "cannot rewind to a point before a send that has already committed "
                "and closed — the rewindable window is [last commit boundary, head] "
                "(item 250, Decision 6)")
        hazards = timeline.fork_hazards(at)
        if hazards["opaque"]:
            labels = ", ".join(e["label"] for e in hazards["opaque"])
            raise SessionError(
                "fork refused: the tail contains a KIND_OPAQUE step the recorder "
                f"cannot restore ({labels}). The fork must not claim step-{at} "
                "state over a disposer it cannot run, so it is refused up front "
                "rather than shipping an unrestored item and a false 'actually at "
                "step k' (item 250, Decision 3)")
        if hazards["nonIdempotent"]:
            labels = ", ".join(e["label"] for e in hazards["nonIdempotent"])
            raise SessionError(
                "fork refused: the rewound span contains a declared "
                f"non-idempotent-total inverse ({labels}). A crash mid-fork could "
                "re-run it against a partially rewound workspace and double-apply "
                "it, so Slice 1 refuses the span rather than shipping an unsound "
                "recovery (item 250, Decision 5)")

    def _has_committed_boundary(self) -> bool:
        """Whether this session's WAL already carries a durably committed crossing
        (item 250, Decision 6). Reads the on-disk WAL through the tier-agnostic
        core; a session with no WAL open has none."""
        wal = self.recorder.wal if self.recorder is not None else None
        if wal is None:
            return False
        from ..wal import read_wal  # noqa: PLC0415 — tier-agnostic core, lazy
        try:
            doc = read_wal(wal.path)
        except OSError:
            return False
        return any(r.get("record") in ("flushed", "commit-approved")
                   for r in doc["records"])

    def _ensure_wal_open(self) -> None:
        """Open this session's durable WAL if none is open (item 250): the fork
        bracket (`fork-begin`/`fork-complete`/`fork-frozen`) must be durable, the
        approval spend must be durable (item 246, Decision 2), and the MCP
        session otherwise leaves the WAL closed.

        "None is open" includes a WAL object whose handle was closed — a closed
        log is not a durable sink. Only ever called on a LIVE session (load, and
        `fork_confirm`); the terminal closers (`_commit_wal`, `_close_wal`) are
        each followed by `_reset`, so this never re-opens past an
        `activation-complete` marker. `Recorder.open_wal` rebinds the live
        timelines, so a log opened here actually receives their step records."""
        if self.recorder is None:
            return
        wal = self.recorder.wal
        if wal is None or not getattr(wal, "is_open", True):
            from ..wal import default_wal_path  # noqa: PLC0415
            path = (getattr(wal, "path", None) if wal is not None else None) \
                or self._wal_path or default_wal_path(self._session_id)
            self.recorder.open_wal(path, self._generation)

    def fork_confirm(self, fork_hash: str) -> dict:
        """Step 2 of the session fork — PERFORMS (item 250, Decision 1 sequence).

        Re-derives the hash and refuses on any drift (a fresh report, a result not
        an error, exactly as `commit_confirm` refuses a stale manifest). On match:
        writes `fork-begin` to the parent WAL, runs the scope-gated rewind to k
        (Decision 2, host-confined inverses only), drops the parent deferral queue,
        FREEZES the parent (non-callable, Decision 4), snapshots the step-k state,
        mints the branch identity (fresh session_id + WAL + SessionOwner, no
        approval carry — item 246 invariant 5), restores the snapshot into the
        branch, and writes `fork-frozen`/`fork-complete`.

        Returns `{forked, at, parent, branch, rewound, residue, ..., branchSession}`
        where `branchSession` is the live branch `Session` (the only live
        continuation over the shared workspace)."""
        self._require()
        pending = self._fork_pending
        if pending is None:
            raise SessionError(
                "no fork is pending — call `fork(at)` first to enumerate and "
                "acknowledge the crossed and would-cross residue (item 250)")
        timeline = self._fork_timeline(pending["component"])
        at = pending["at"]
        current = self._fork_hash(self._fork_target(timeline, at))
        if fork_hash != current:
            self._fork_pending = None
            return {"forked": False, "refused": True,
                    "reason": ("stale fork hash — the timeline or the live "
                               "composition changed since enumeration, so the "
                               "confirm is refused. Re-enumerate: what is rewound "
                               "must be exactly what was acknowledged (item 250, "
                               "Decision 1)"),
                    **self._fork_report(timeline, at)}

        report = self._fork_report(timeline, at)
        crossed = report["emissionsCrossed"]
        would = report["wouldCrossOnRewind"]
        dropped = report["droppedDeferred"]

        # 1. the fork bracket opens durably, BEFORE the rewind touches the fs.
        self._ensure_wal_open()
        wal = self.recorder.wal if self.recorder is not None else None
        if wal is not None:
            wal.record_fork_begin(parent=self._session_id, at=at,
                                  crossed=crossed, would_cross=would)

        # 2. the scope-gated, non-emitting rewind to k (Decision 2).
        rewind = self._run(timeline.step_back(at, compensate=False))

        # 3. drop the parent deferral queue — the held sends never crossed.
        if self._owner is not None:
            self._owner._queue = []

        # 4/5. snapshot the step-k state, then FREEZE the parent.
        snap = self.snapshot()
        parent_id = self._session_id
        branch_id = None

        # 6. mint the branch identity over the (now rewound) shared workspace.
        branch = Session()
        branch.approval_policy = self.approval_policy
        # the durability posture rides with the policy: a fork must not become a
        # session where an approved caller value is recorded that the parent's
        # operator had withheld.
        branch.approval_record_values = self.approval_record_values
        branch.sandbox = self.sandbox
        branch.restore(snap)               # fresh session_id + owner; no approval carry
        if self._wal_path:
            # keep the branch's distinct WAL beside the parent's, rather than in
            # the shared per-user state dir, when the parent named an explicit one
            import os  # noqa: PLC0415 — stdlib
            branch._wal_path = os.path.join(
                os.path.dirname(os.path.abspath(self._wal_path)),
                f"revl-branch-{branch._session_id}.wal")
        branch._ensure_wal_open()          # a distinct branch WAL
        branch_id = branch._session_id

        # 7. close the bracket on the parent WAL and retire the parent.
        if wal is not None:
            wal.record_fork_frozen(parent=parent_id, at=at)
            wal.record_fork_complete(branch=branch_id)
            wal.close()
        self._frozen = True
        self._fork_at = at
        self._fork_pending = None

        return {
            **report,
            "forked": True,
            "parent": parent_id,
            "branch": branch_id,
            "rewound": {
                "inversesRan": rewind["inversesRan"],
                "provisionsWithdrawn": rewind["provisionsWithdrawn"],
            },
            "droppedDeferred": dropped,
            "branchSession": branch,
        }

    # -- teardown plumbing -------------------------------------------------

    def _surface_compensation_residue(self, owner) -> list:
        """Collect the session's unresolved compensation residue at the teardown
        boundary (item 247 gap 2, design Decision 2 / Slice 3) and count each as
        a residue prompt (the same `prompts["residue"]` channel a flush-residue
        uses — item 246, so prompts-per-session reflects an offset that did not
        land). Returns the residue records for the boundary report. Empty and
        prompt-neutral when nothing was owed (a clean or compensation-free
        session), so the report and metrics stay byte-identical there.

        Called BEFORE the `prompts` snapshot and `_reset`, once the frames'
        Phase-2 drain and the owner's `finalize_abort` have run."""
        if owner is None:
            return []
        residue = owner.collect_compensation_residue()
        owner.prompts["residue"] += len(residue)
        return residue

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
        # item 330: drop the admit-crossing binding so a torn-down session is
        # never reachable through `stdlib/admit.rvl` (only the live one is).
        from . import admit_bridge as _admit_bridge  # noqa: PLC0415
        if _admit_bridge.current() is self:
            _admit_bridge.bind(None)
        # item 403: likewise drop the reflection-query binding so a torn-down
        # session is never reachable through `stdlib/reflect.rvl` (only the live
        # one is).
        from . import reflect_bridge as _reflect_bridge  # noqa: PLC0415
        if _reflect_bridge.current() is self:
            _reflect_bridge.bind(None)
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
        self._grants = []
        self._grants_consumed = 0
        # item 251 Slice 2: distilled-rule materialization and its H1 review bind.
        self._auto_rules = []
        self._auto_reviewed = {}
        self._approval_records = []
        self._auto_consumed = 0
        self._distillation_seq = 0
        # item 310: the seam-method cache is session-scoped, exactly as the ledger
        # and grants are (a cached result cannot outlive the session that
        # authorized its miss).
        self._cache_index = {}
        self._cache_entries = {}
        self._cache_pure = {}
        self._cache_inval_epoch = {}
        self._cache_inval_tokens = set()
        self._cache_hits = 0
        # item 250: a torn-down session is not a frozen one — clear the fork state
        # so a reused Session object starts fresh and callable.
        self._frozen = False
        self._fork_at = None
        self._fork_pending = None

    # -- the per-turn admit+run crossing (roadmap item 330) ----------------

    def admit(self, source: str, granted=None, *,
              filename: str = "<turn>.rvl",
              modules: dict[str, str] | None = None) -> AdmitVerdict:
        """Admit and wire a per-turn source into the RUNNING composition — the
        first-class admit+run crossing (roadmap item 330).

        This is the host target of the classified in-language `admit` crossing
        (stdlib/admit.rvl + revl.mcp.admit_bridge, docs/mcp-bridge.md): a running
        composition hands a model-authored per-turn `source` and the set of
        service names it is `granted` to reach, and gets back an `AdmitVerdict`.
        The verdict is the type judgment that IS the permission decision — a
        REFUSAL is the repair signal handed back as data, never a raised error
        the turn cannot catch — plus, on admission, a handle whose crossings
        register into THIS session's 245 frame.

        The decision applies the item-329 untrusted-author profile: the turn may
        declare no new `extern`/host-block (no smuggled host code, G8) and may
        reach no service outside `granted` (the allowlist). And it is ADDITIVE
        only — a turn that would replace a running component is refused, because
        an untrusted turn composes granted providers, it never swaps them.
        """
        from ..admit_profile import AdmissionProfile  # noqa: PLC0415
        from ..compiler import compile_source  # noqa: PLC0415
        from ..errors import RevlError  # noqa: PLC0415

        self._require()
        if self._owner is None:
            raise SessionError(
                "admit needs a running session owner (item 245) — load a "
                "composition first, so the turn's crossings have a frame to "
                "register into")
        profile = AdmissionProfile.untrusted_author(granted or ())
        try:
            turn_doc = compile_source(source, filename, manifest=self.ir,
                                      modules=modules, profile=profile)
        except RevlError as error:
            # the refusal is the repair signal — returned as a verdict, the
            # running composition untouched, never raised into the turn.
            return AdmitVerdict(False, message=str(error),
                                code=getattr(error, "code", None))

        running = {c["name"] for c in (self.ir or {}).get("components") or []}
        clash = sorted(c["name"] for c in turn_doc["components"]
                       if c["name"] in running)
        if clash:
            return AdmitVerdict(False, code="G2", message=(
                f"admission refused: the per-turn source would replace running "
                f"component(s) {', '.join(clash)}. An admitted turn composes "
                f"granted providers into the running composition; it does not "
                f"swap them (item 330 is additive-only — hot-swap is `revl_swap`, "
                f"a separate, operator-gated verb)."))

        keys = tuple(k for c in turn_doc["components"]
                     for k in (c.get("provides") or {}))
        # WIRING vs DECISION. Admitting is the decision; wiring the turn into the
        # live driver needs the event loop, which may already be running when the
        # admit arrives THROUGH the in-language crossing (that call is itself
        # driving the loop). So, exactly as the self-evolution loop never swaps
        # synchronously (`demo/evolve_bridge.propose`), a turn admitted from
        # inside a live call is QUEUED and wired the moment that call returns; a
        # turn admitted directly (no loop in flight) wires now.
        if self._loop is not None and self._loop.is_running():
            self._pending_admits.append(turn_doc)
        else:
            self._wire_turn(turn_doc)
        return AdmitVerdict(True, handle=AdmitHandle(self, keys), keys=keys)

    def _wire_turn(self, turn_doc: dict) -> tuple[str, ...]:
        """Plug an admitted turn's components into the LIVE driver with the
        session owner active — so every frame the turn builds joins the same
        live-frame registry the enclosing session's commit/abort iterate — then
        adopt the turn into the live composition ir so `call`, commit and abort
        span it. Returns the turn's provided keys.

        Under the item-329 no-extern profile the turn holds no host code of its
        own; its granted-emission and witnessed-fs crossings execute in the
        granted PROVIDERS' frames (already in the owner registry). Plugging the
        turn is what makes those providers reachable from the turn and its own
        provided keys callable, all inside the one 245 frame.

        Wiring WIDENS the callable surface, so it is a generation change and it
        rebuilds every per-generation index the same way `load` and `swap` do —
        the class map first of all. Skipping that rebuild was a TOTAL class-(c)
        approval bypass: the turn's keys became callable immediately, but
        `ClassMap.classify_call(turnKey, m)` found no provider in the STALE map,
        the per-call decision read that as "not a boundary call", and a crossing
        that prompts when called directly fired through the turn with no ticket,
        no ledger entry and no posture counter. The turn needs no host code of
        its own for that — the item-329 untrusted-author profile is not a
        mitigation, because the turn only forwards to a GRANTED provider whose
        emission is class (c).

        The gates that decide BEFORE the runtime is touched therefore run before
        the plug, against the composition that WILL be live: the activation gate
        over the turn's own activation bodies (a class-(c) emission moved into
        the turn's activation body fires at wire time, before any call exists to
        gate), and the lease gate over the turn's acquisitions. A refusal there
        leaves the running composition untouched — nothing is plugged, nothing
        is adopted."""
        driver = self._driver
        runtime_mod = driver.runtime
        turn_components = list(turn_doc["components"])

        # the post-admission composition, built BEFORE anything is plugged so the
        # pre-boot gates decide against the surface the turn actually creates.
        merged = self._merged_turn_ir(turn_doc)
        turn_names = {c["name"] for c in turn_components}
        new_map = self._build_class_map(merged)
        # item 294: an admitted turn may not self-mint a lease either. Scoped to
        # the turn's own acquisitions — the base's were satisfied at load.
        self._enforce_lease_gate({"components": turn_components})
        # item 246, Fix 1: the turn's ACTIVATION body answers for its class-(c)
        # crossings before it runs, exactly as a loaded/swapped generation does.
        self._enforce_activation_gate(merged, new_map, components=turn_names)
        # item 310, surface H: a turn widens the composition, so a cached method
        # the turn newly resolves (or newly reaches) is folded against the MERGED
        # closure before anything is plugged.
        self._check_cache_applicability(merged, new_map)

        module = self._prepare_module(turn_doc)

        async def _plug() -> None:
            for comp in turn_components:
                name = comp["name"]
                fiber = runtime_mod.plug(driver.root, getattr(module, name),
                                         driver.config.get(name, {}))
                driver.fibers[name] = fiber
                await driver._flush()
                if fiber.state == driver.FiberState.LOADING:
                    # an async activation body in flight — settle it exactly as
                    # `_Driver._load` does, so a PENDING/LOADING race cannot leave
                    # the turn half-wired.
                    await asyncio.wait_for(asyncio.shield(fiber), 2)
            await driver._flush()

        # the owner must be the process-global session owner while the turn's
        # frames are BUILT, so each `Frame.__init__` joins its registry; cleared
        # after, exactly as `load` scopes it, so no later stray frame joins.
        runtime_mod.set_session_owner(self._owner)
        try:
            self._run(_plug())
        finally:
            runtime_mod.clear_session_owner()

        # adopt the turn into the live composition: `turn_doc["manifest"]`
        # already describes the WHOLE resulting composition (base + turn), so
        # `_dispose_all`/`_namespace`/load order span both and teardown is
        # residue-free over the union.
        self.ir["components"].extend(turn_components)
        for name, spec in turn_doc["services"].items():
            self.ir["services"].setdefault(name, spec)
        self.ir["manifest"] = turn_doc["manifest"]
        driver.ir = self.ir

        # the per-generation indexes go live with the widened surface, atomically
        # and in the same order `load` and `swap` install them, so no call is ever
        # decided against a map that predates the keys it is deciding about.
        self._class_map = self._build_class_map(self.ir)
        self._install_auto_approve_rules()
        self._install_cache_index(self.ir)
        # invariant 4: the runtime frame check compares a typed `Approval[C]`
        # against the LIVE reach-closure candidate hash. The turn joins the
        # closure of anything it reaches, so re-seed the owner's candidates or a
        # `with a` crossing checks against a hash the generation no longer has.
        if self._owner is not None and self._typed_approval_active(self.ir):
            self._owner.approval_enforced = True
            self._owner.approval_candidates = \
                self._approval_candidate_hashes(self.ir)

        keys: list[str] = []
        for comp in turn_components:
            keys.extend((comp.get("provides") or {}).keys())
        return tuple(keys)

    def _merged_turn_ir(self, turn_doc: dict) -> dict:
        """The composition that WILL be live once `turn_doc` is wired: the running
        ir widened by the turn's components, services and externs, over the turn's
        manifest (which already describes base + turn). A fresh dict over fresh
        containers, so building it cannot mutate the running composition — a gate
        that refuses leaves nothing behind."""
        base = self.ir or {}
        merged = dict(base)
        merged["components"] = list(base.get("components") or []) \
            + list(turn_doc.get("components") or [])
        services = dict(base.get("services") or {})
        for name, spec in (turn_doc.get("services") or {}).items():
            services.setdefault(name, spec)
        merged["services"] = services
        # the untrusted-author profile forbids the turn declaring an extern of
        # its own, so this is normally the base's set unchanged; folded anyway so
        # the class map never classifies against a missing extern declaration.
        externs = list(base.get("externs") or [])
        known = {e.get("name") for e in externs}
        externs += [e for e in (turn_doc.get("externs") or [])
                    if e.get("name") not in known]
        merged["externs"] = externs
        merged["manifest"] = turn_doc["manifest"]
        return merged

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

        # item 310: the seam-method cache gate. Active only for a `cache
        # capability`/`external` method under a policy with a ledger — a no-policy
        # session has no authority scope to key an entry on, so the declaration is
        # dynamically inert and every access is a miss (design §enforcement,
        # no-policy decision). `cache pure` is memoized in the emitted body and
        # never reaches here. Non-cache methods leave `cache_active` False and take
        # the byte-for-byte path below.
        cache_spec = self._cache_index.get((key, method))
        cache_class = cache_spec.get("class") if cache_spec else None
        pure_key = None
        if cache_class == "pure_fn":
            # `cache pure`: memoize on the args digest. No ledger interaction (the
            # reach crosses nothing), so it hits in any session, policy or not, and
            # a hit is observationally equivalent to the call (G6: equal args, equal
            # result). Generation-scoped: a swap drops the memo.
            pure_key = (key, method, _cache_args_digest(args))
            if pure_key in self._cache_pure:
                self._cache_hits += 1
                return {"result": _plain(self._cache_pure[pure_key]),
                        "trace": [], "cacheHit": True}
        cache_active = (
            cache_class in ("capability_result", "external_effect")
            and self.approval_policy is not None
            and self._class_map is not None)
        entry_key = None
        if cache_active:
            entry_key = (key, method, _cache_args_digest(args))
            entry = self._cache_entries.get(entry_key)
            if entry is not None and self._cache_entry_live(entry):
                # LIVE HIT: the seam checks liveness BEFORE it consumes, so a hit
                # skips the consumption a miss performs (design laundering point 2)
                # — no host body runs, no use is spent, no boundary is crossed, so
                # a hit can invalidate nothing. The entry's own liveness IS the
                # authority binding (its recorded grant must still be live, or its
                # covering approval's ttl unlapsed), so a hit cannot launder
                # authority: an access whose recorded authority has died takes the
                # miss path below and is refused exactly as an uncached call.
                self._cache_hits += 1
                self._record_cache_hit(key, method, entry)
                return {"result": _plain(entry["value"]), "trace": [],
                        "cacheHit": True}
            # MISS: fall through to today's consume-before-fire path, then store.

        # item 246: the auto-approve decision, at the single chokepoint every
        # internal re-invocation passes through — `replay_forward` re-invokes
        # `self.call(...)`, so a class-(c) replayed step is refused here exactly as
        # a fresh one (Fix 2, exit test 13). class none/(a)/(b) proceed and are
        # counted; class (c) consumes a standing approval or raises the ticket.
        # A no-policy session never enters this (returns immediately). item 310:
        # `decision` records which authority the miss consumed, so the stored entry
        # binds to THAT grant/approval (not any that could have covered).
        decision: dict | None = {} if cache_active else None
        self._approval_decide_call(key, method, args, record=decision)

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
        # item 245/246-F2: the session owner must be the process-global owner
        # WHILE the call runs, so any Frame a spawn-in-call builds captures a live
        # owner at `Frame.__init__` and joins its live-frame registry — the
        # commit/abort gate target and the runtime class-(c) crossing check.
        # Without it a frame created during the call captures a cleared ambient
        # owner (None) and takes the fail-open path (an unchecked class-(c)
        # crossing + a lost revert), the sibling of the swap-owner bug. Scoped
        # exactly as `load` and `_wire_turn` scope it: installed here, cleared in
        # the finally, so no later stray frame joins this session's registry.
        runtime_mod = driver.runtime
        runtime_mod.set_session_owner(self._owner)
        try:
            result = self._run(invoke())
        finally:
            runtime_mod.clear_session_owner()
            if self.recorder is not None:
                self.recorder.activation_origin()
        # item 330: a per-turn source admitted through the in-language crossing
        # DURING this call was queued (the loop was busy); wire it now the call
        # has returned and the loop is free — the turn's keys become callable and
        # its crossings are already governed by this session's 245 frame.
        self._drain_pending_admits()
        # item 310: store the miss result. A `cache pure` result is memoized
        # unconditionally (no authority); a capability/external result is stored
        # scoped to the authority the miss consumed.
        if pure_key is not None:
            self._cache_pure[pure_key] = result
        if cache_active and entry_key is not None:
            self._cache_store(entry_key, result, cache_spec, decision)
        # item 310: fire `invalidated_by` for any subscribed token this call
        # crossed, BEFORE the result is delivered — the write and the invalidation
        # ride the same order, so a session never reads its own stale write
        # (design §invalidated_by). Guarded on the subscribed-token set, so a
        # composition with no `invalidated_by` clause is byte-identical.
        if self._cache_inval_tokens:
            self._fire_cache_invalidations(key, method)
        return {"result": _plain(result), "trace": driver.drain_events()}

    # -- item 310: the seam-method cache entry store ------------------------

    def _grant_live_by_id(self, request_id: str, now: int) -> bool:
        """Whether the standing grant `request_id` recorded on a cache entry is
        still live: present, un-revoked, un-exhausted (`_consume_grant` sets
        `consumed` when uses hit zero), and unexpired. A one-use grant consumed by
        the miss is dead here, so it yields one miss and zero hits (design: no
        residue of authority)."""
        for g in self._grants:
            if g["requestId"] != request_id:
                continue
            if g.get("consumed") or g.get("revoked"):
                return False
            exp = g.get("expiresAt")
            if exp is not None and now > exp:
                return False
            return True
        return False

    def _cache_entry_live(self, entry: dict) -> bool:
        """Whether a cache entry may still answer — the check the seam runs on
        EVERY access, hit or miss. An entry dies at the FIRST of: a generation
        change, its ttl lapsing, its covering approval's ttl lapsing (for an
        approval-required token — the consumed flag is deliberately NOT consulted,
        design §single-use approvals), an `invalidated_by` token crossing since it
        was born, or its recorded grant dying (revocation / exhaustion / expiry).
        Past any line the access is a miss and re-crosses under full authority."""
        if entry["generation"] != self._generation:
            return False
        now = self._now_ms()
        exp = entry.get("expiresAt")
        if exp is not None and now > exp:
            return False
        aexp = entry.get("approvalExpiresAt")
        if aexp is not None and now > aexp:
            return False
        for token, epoch in (entry.get("invalEpochs") or {}).items():
            if self._cache_inval_epoch.get(token, 0) != epoch:
                return False
        for rid in entry.get("grantIds") or ():
            if not self._grant_live_by_id(rid, now):
                return False
        return True

    def _cache_store(self, entry_key: tuple, value, cache_spec: dict,
                     decision: dict | None) -> None:
        """Store a miss result, scoped to the authority the miss consumed. A
        capability/external entry with NO consumed grant or approval has no
        authority scope to be keyed on or to die with — an ambient store the
        laundering section forbids — so it is NOT stored (fail closed into
        correctness). The ttl `expiresAt` and the `invalidated_by` epoch snapshot
        are captured here so the liveness check reads a fixed floor."""
        scope = (decision or {}).get("scope")
        grant_ids: list = []
        approval_expires = None
        if scope is not None:
            kind, val = scope
            if kind == "grants":
                grant_ids = list(val)
            elif kind == "approval":
                approval_expires = (decision or {}).get("approvalExpiresAt")
        if not grant_ids and approval_expires is None:
            # no authority scope (an off-crossing class-none/(a)/(b) result, or a
            # scope the miss did not consume): an unscoped entry is the ambient
            # store the design forbids — leave it uncached, dynamically inert.
            return
        now = self._now_ms()
        ttl_ms = cache_spec.get("ttl_ms")
        expires_at = (now + ttl_ms) if ttl_ms is not None else None
        tokens = cache_spec.get("invalidated_by") or []
        inval_epochs = {tok: self._cache_inval_epoch.get(tok, 0) for tok in tokens}
        self._cache_entries[entry_key] = {
            "value": value,
            "generation": self._generation,
            "grantIds": grant_ids,
            "approvalExpiresAt": approval_expires,
            "expiresAt": expires_at,
            "invalEpochs": inval_epochs,
        }

    def _fire_cache_invalidations(self, key: str, method: str) -> None:
        """When a call crosses a subscribed `invalidated_by` token, bump that
        token's epoch so every entry declaring it is a miss on its next access
        (single-process, WAL-ordered — design §invalidated_by). A crossing of a
        wider token invalidates a narrower subscription (the item-294 order)."""
        if self._class_map is None:
            return
        reach = self._class_map.classify_call(key, method)
        if reach is None:
            return
        crossed = reach.get("capabilities") or set()
        if not crossed:
            return
        from .approval import _cap_covers  # noqa: PLC0415
        for token in self._cache_inval_tokens:
            if any(_cap_covers(cap, token) for cap in crossed):
                self._cache_inval_epoch[token] = \
                    self._cache_inval_epoch.get(token, 0) + 1

    def _record_cache_hit(self, key: str, method: str, entry: dict) -> None:
        """Write a WAL record naming the hit and the miss crossing it re-delivers
        (design laundering point 5: hits are on the record). Best-effort — a
        session with no WAL still counts the hit in `state()` (the `cacheHits`
        counter), it just has no durable audit line."""
        wal = self._approval_wal()
        if wal is None:
            return
        recorder = getattr(wal, "record_cache_hit", None)
        if callable(recorder):
            recorder({"key": key, "method": method,
                      "grantIds": entry.get("grantIds") or []})

    def _drain_pending_admits(self) -> None:
        """Wire every turn admitted (and queued) during the call that just
        returned. Kept off the hot path: a session that never admits has an empty
        queue and pays nothing.

        The queue is taken before wiring, so an `ApprovalRequired` from the
        turn's activation gate leaves NOTHING queued: nothing was wired and
        nothing fired, and the caller re-admits after answering the ticket (the
        retry then consumes the standing approval). Re-queuing instead would
        double-wire the turn on that retry."""
        if not self._pending_admits:
            return
        pending, self._pending_admits = self._pending_admits, []
        for turn_doc in pending:
            self._wire_turn(turn_doc)

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

    # -- item 310: the seam-method cache index + entry store ----------------

    def _build_cache_index(self, ir: dict) -> dict:
        """`(key, method) -> cache IR descriptor` for every provided seam method
        that declares `cache` (item 310). Walks the components' `provide` steps
        (key -> service) and reads the descriptor off the service-method interface
        (`ir["services"][svc]["methods"][m]["cache"]`), so every provider of a
        `cache`-declaring method inherits the same contract. Empty for any
        composition with no `cache` clause, so the seam is byte-identical."""
        services = ir.get("services") or {}
        index: dict = {}

        def walk(node) -> None:
            if isinstance(node, dict):
                if node.get("step") == "provide":
                    key = node.get("name")
                    svc = services.get(node.get("service")) or {}
                    methods = svc.get("methods") or {}
                    for mname, spec in methods.items():
                        cache = spec.get("cache")
                        if cache is not None:
                            index[(key, mname)] = cache
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(ir.get("components") or [])
        return index

    def _check_cache_applicability(self, ir: dict, class_map) -> None:
        """Surface H (item 310): refuse a `cache`-declaring seam method whose
        PROVIDER CLOSURE is not cacheable, BEFORE the generation is committed.

        The compile-time admission checks see only the declaring method's
        declared reach shape; the clause is an interface contract every provider
        inherits, so what the cached reach actually crosses is a fact about the
        linked composition. Called from `load` (pre-boot), `swap` (pre-teardown,
        next to the activation gate) and `_wire_turn` (pre-plug) — never after a
        generation is committed, so a refusal never leaves a half-installed
        index. Inert for a composition that declares no `cache`."""
        index = self._build_cache_index(ir)
        if not index:
            return
        from .approval import (ClassMap,  # noqa: PLC0415
                               cache_applicability_refusal)
        # the class map is None when no approval policy is configured, but the
        # fold is not a policy gate: `cache pure` memoizes in every session (the
        # no-policy inertness rule covers the ENTRY STORE, not applicability), so
        # an uncacheable reach must refuse off-policy too. Built here, only for a
        # composition that actually declares `cache`.
        problem = cache_applicability_refusal(
            class_map if class_map is not None else ClassMap(ir), index)
        if problem is not None:
            raise SessionError(problem)

    def _install_cache_index(self, ir: dict) -> None:
        """Rebuild the per-generation cache index and drop every entry from the
        prior generation (a generation change is itself a liveness event, design
        laundering point 3). Called next to the class-map rebuild at load/swap."""
        self._cache_index = self._build_cache_index(ir)
        self._cache_entries = {}
        self._cache_pure = {}
        self._cache_inval_epoch = {}
        self._cache_inval_tokens = set()
        for cache in self._cache_index.values():
            for token in cache.get("invalidated_by") or ():
                self._cache_inval_tokens.add(token)

    def _approval_wal(self):
        """The session's OPEN WAL, when recording (an enabled policy requires
        one, and `load` now opens it). None otherwise — the in-memory ledger
        still binds, but a policy load without recording is refused up front, so
        during a live policy session this is never None.

        A CLOSED log answers None too. `commit_confirm` stamps
        `activation-complete` and closes (`_commit_wal`) and `abort` closes
        (`_close_wal`); both are immediately followed by `_reset`, so no crossing
        can follow and nothing is left unrecorded — but `self.recorder` survives
        the reset, so without this check a stray later write would append past a
        terminal marker or raise `ReplayError` from inside a consume. Failing
        during a spend is the worst failure mode available: the gate has already
        decided and the effect is in flight. A closed log is not a durable sink,
        so it reads as absent."""
        wal = self.recorder.wal if self.recorder is not None else None
        if wal is None or not getattr(wal, "is_open", True):
            return None
        return wal

    def _operator_token(self) -> str:
        """The bound operator identity (item 55) attributed to a grant, so the
        distiller (item 251) can require every grant of a shape to share one
        author. Empty when no operator profile is bound."""
        op = getattr(self, "operator", None)
        return getattr(op, "token", "") if op is not None else ""

    def _wal_ledger_records(self) -> list:
        """Every record currently flushed to the approval WAL file (item 251 Slice
        3, the time axis). The WAL outlives a session and, when successive sessions
        share a path, accumulates their `approval-granted` / `distillation-applied`
        records side by side, so this is the CROSS-session ledger the
        prompts-per-session series and `distillationImpact` fold over - not the
        in-memory `_approval_records`, which resets each session. Empty (never an
        error) when no WAL is open or the file is unreadable: the metric degrades
        to a single-session view rather than failing `state()`."""
        wal = self._approval_wal()
        path = getattr(wal, "path", None)
        if path is None:
            return []
        import os  # noqa: PLC0415 — stdlib
        if not os.path.exists(path):
            return []
        try:
            return replay_module().WriteAheadLog.read(path)["records"]
        except Exception:  # noqa: BLE001 — a metric never crashes state()
            return []

    def _distillation_ledger_fields(self, ticket: dict) -> dict:
        """The item-251 shape-key fields a grant record carries so the distiller
        folds it (design §1.1, §2.1): the crossing's realm, its bound resource
        valuations (`resourceScopes`, the registered-resource projection the N1
        fix binds at ticket time), the resource-bound `classCCapabilities`, the
        recorded post-endorsement `taintOrigins`, and the attributed operator. Only
        present-on-the-ticket fields are copied, so a bare crossing records exactly
        as before plus the realm and operator.

        Roadmap 425 F3 / 427 F5 — the DURABILITY boundary, and the one knob over
        it. This record is the only sink in the approval path that OUTLIVES the
        decision: `record_approval_granted` appends it to a cross-session WAL file
        in the user's state directory, plaintext at rest, read back later by the
        distiller and by another session. The ticket is ephemeral by comparison,
        and it keeps the real value either way — an operator cannot answer a prompt
        that hides the target, and on a fresh call the value is the caller's own,
        just sent.

        `approval_record_values` is the operator's answer to "may an approved
        crossing's CALLER-SUPPLIED resource value be written down forever?":

          * `bound` (the default, and what shipped): yes. This is item 251's N1 —
            the ledger records the destination that actually crossed, which is what
            lets a distilled rule name a target instead of comparing an opaque
            `argsDigest`. Flipping this default would silently disable N1 for the
            common case, since most resource targets ARE caller arguments.
          * `withheld`: no. A valuation the ticket marked caller-supplied
            (`resourceScopesFromCallerArgs`) records as `None` — the
            "resource-bearing but UNRECORDED" shape `distill._project_resource`
            already understands and already FAILS CLOSED on — and its bound
            spelling drops back to the bare token in `classCCapabilities`. The cost
            is exact and worth stating: a series of approvals whose target came
            from a caller argument can no longer fold into a distilled standing
            rule. Author-written string LITERALS are never withheld — a literal is
            in the source already and discloses nothing about the caller — so a
            literal-targeted crossing distills identically under both modes.

        What is NOT on offer is recording a placeholder. That would be worse than
        the leak: every distinct target would fold to one shape and a rule minted
        over it would cover all of them, the over-authorization
        `mint_standing_grant` already refuses a placeholder-scoped grant for.
        """
        fields: dict = {"realm": ticket.get("realm", ""),
                        "operator": self._operator_token(),
                        # item 251 Slice 3: the grant's session (invariant 5) so
                        # the WAL, read back across sessions sharing a path, groups
                        # the per-session prompt series the time axis folds (§4).
                        "session": self._session_id}
        from_caller = (set(ticket.get("resourceScopesFromCallerArgs") or ())
                       if self.approval_record_values == "withheld" else set())
        scopes = ticket.get("resourceScopes") or {}
        # the spellings that carry a caller value, so the bound form is dropped
        # from BOTH channels: `_project_resource` reads an inline-parameterised
        # `classCCapabilities` entry in preference to the `resourceScopes` map,
        # so redacting only the map would leave the value in the other one.
        withheld = {scopes[t] for t in from_caller if t in scopes}
        if ticket.get("classCCapabilities"):
            fields["classCCapabilities"] = sorted(
                cap_order.parse_cap(cap).token if cap in withheld else cap
                for cap in ticket["classCCapabilities"])
        if scopes:
            fields["resourceScopes"] = {
                token: (None if token in from_caller else spelling)
                for token, spelling in scopes.items()}
        if ticket.get("taintOrigins"):
            fields["taintOrigins"] = ticket["taintOrigins"]
        return fields

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

    def _approval_decide_call(self, key: str, method: str, args,
                              record: dict | None = None) -> None:
        """The per-call decision (Decision 2). Off -> return immediately (byte-
        identical). class none/(a)/(b) -> proceed and count. class (c) -> consume
        a standing approval and proceed, else mint a ticket, count the prompt, and
        raise `ApprovalRequired` (the two-step's refusal).

        `record` (item 310, additive out-param) is filled in place with the
        authority the miss actually consumed, so the seam cache can bind an entry
        to THAT authority (the recorded grant, not any grant that could have
        covered): `{"scope": ("grants", [requestId, ...])}` or
        `{"scope": ("approval", requestId)}`, and `{"scope": None}` for a
        class-none/(a)/(b) call. Default `None` -> nothing recorded, so every
        existing caller is byte-identical."""
        if self.approval_policy is None or self._class_map is None:
            return
        reach = self._class_map.classify_call(key, method)
        if reach is None:
            # UNRESOLVED, not "not a boundary call". `Session.call` already
            # proved the key is provided and the method callable, so reaching
            # here means the live class map cannot answer for a call that is
            # about to fire: a stale map (a generation whose surface widened
            # without a rebuild), or a key whose provider is ambiguous across
            # realms. Either way the policy has no class for the crossing, and
            # "I could not classify it" is not "it is harmless" — proceeding was
            # the total class-(c) bypass. Refuse (fail closed); the caller
            # reloads or disambiguates.
            raise SessionError(
                f"cannot decide `{key}.{method}`: the approval policy is "
                f"enabled and the live class map resolves no crossing class for "
                f"this call, so its irreversibility is unknown. An unclassified "
                f"call is refused, never auto-approved — an unresolved "
                f"classification is not a proof of class none (item 246, "
                f"refuse-don't-degrade). This means the key resolves to no "
                f"single provider in the live generation (an ambiguous "
                f"multi-realm key), or the map is stale against the running "
                f"composition")
        klass = reach["class"]
        if klass in (None, "a", "b"):
            self._count_posture(klass)
            if record is not None:
                record["scope"] = None
            return
        # class (c): a standing approval, or a fresh ticket.
        from .approval import ApprovalRequired  # noqa: PLC0415
        ticket = self._class_map.build_ticket(reach, args)
        standing = self._find_standing_approval(ticket)
        if standing is not None:
            self._consume_approval(standing)   # durable spend before the fire
            if record is not None:
                record["scope"] = ("approval", standing["requestId"])
                record["approvalExpiresAt"] = standing.get("expiresAt")
            return
        # item 344: a session-scoped standing grant keyed by the capability's
        # semantic identity covers ANY class-(c) call reaching it (differing
        # args included), so the shell-escape shape auto-approves against one
        # mint instead of prompting per call.
        grants = self._find_standing_grant(ticket)
        if grants is not None:
            for g in grants:                   # every class-(c) cap is covered
                self._consume_grant(g)         # durable spend before the fire
            if record is not None:
                record["scope"] = ("grants", [g["requestId"] for g in grants])
            return
        # item 251 Slice 2: a distilled `AutoApproveRule` in the bound policy that
        # covers the crossing (component glob + realm + resource scope + taint
        # subset) auto-approves it, consuming a use durably before the fire. Same
        # runtime enforcement as a hand-written rule of the identical text - the
        # distiller only selected it.
        auto = self._find_auto_approve(ticket)
        if auto is not None:
            self._consume_auto_rule(auto)      # durable spend before the fire
            if record is not None:
                record["scope"] = ("auto", auto["requestId"])
            return
        self._tickets[ticket["hash"]] = ticket
        if self._owner is not None:
            self._owner.prompts["perCall"] += 1
        self._count_posture("c")
        raise ApprovalRequired(ticket)

    def _enforce_activation_gate(self, ir: dict, class_map=None,
                                 components: set | None = None) -> None:
        """The activation gate (Fix 1): under an enabled policy, a candidate whose
        ACTIVATION reach is class (c) turns the load/swap response itself into the
        ticket two-step before boot. class (a)/(b) activation reach follows the
        table (proceed / enqueue, both activation-safe). Raises `ApprovalRequired`
        naming the activation crossing, unless a standing approval covers it.

        `components` restricts the gate to the named components' activation
        bodies. `load`/`swap` boot a whole generation and pass None (every
        activation body is about to run). An admitted turn (item 330) is ADDITIVE
        — only its own components activate, the running ones already booted and
        were gated then — so `_wire_turn` names the turn's components and the
        base is not re-prompted for an activation it already ran."""
        cm = class_map if class_map is not None else self._class_map
        if self.approval_policy is None or cm is None:
            return
        from .approval import ApprovalRequired  # noqa: PLC0415
        for reach in cm.activation_reaches():
            if reach["class"] != "c":
                continue
            if components is not None and reach.get("component") not in components:
                continue
            ticket = cm.build_ticket(reach)
            standing = self._find_standing_approval(ticket)
            if standing is not None:
                self._consume_approval(standing)
                continue
            grants = self._find_standing_grant(ticket)   # item 344
            if grants is not None:
                for g in grants:            # every class-(c) cap is covered
                    self._consume_grant(g)
                continue
            auto = self._find_auto_approve(ticket)        # item 251 Slice 2
            if auto is not None:
                self._consume_auto_rule(auto)
                continue
            self._tickets[ticket["hash"]] = ticket
            if self._owner is not None:
                self._owner.prompts["perCall"] += 1
            self._count_posture("c")
            raise ApprovalRequired(ticket)

    # -- item 294 Slice 2: the capability-lease gate ------------------------

    def _enforce_lease_gate(self, ir: dict) -> None:
        """Gate every `effect lease` acquisition BEFORE boot (item 294 Slice 2).

        A lease is a class-(c)-gated, ticket-mediated acquisition, NEVER a silent
        self-mint: this is the consent bypass the design closes. Two outcomes per
        un-satisfied lease:

          * UNGATED run (no approval policy loaded): refuse the lease outright.
            There is no operator to raise the ticket to, and an unenforceable
            lease is not a lease (honest sentence 2). A program cannot self-convert
            prompt-per-call into prompt-never by declaring a lease off-policy.
          * GATED run: if a live standing grant already covers the lease cone
            (the post-approval retry, or a pre-minted grant), the lease is
            satisfied and boot proceeds. Otherwise raise ONE ticket naming the
            lease's capability cone and ttl/uses; the operator mints the grant
            FROM that ticket (`mint_standing_grant(ticket_hash=…)`) and re-loads.
            The one prompt moves from the first crossing to the acquisition — the
            item-248 economics (3 prompts to 1), never to 0.

        Inert for every composition with no lease step, so byte-identity holds."""
        leases = _collect_lease_requests(ir)
        if not leases:
            return
        if self.approval_policy is None:
            names = ", ".join(sorted({lz["capability"] for lz in leases}))
            raise SessionError(
                f"`effect lease` refused: no approval policy is loaded, so there "
                f"is no operator to consent to the lease(s) [{names}]. A lease "
                f"acquisition is class-(c)-gated and ticket-mediated — an ungated "
                f"run cannot silently mint the grant (item 294: a program may not "
                f"self-convert prompt-per-call into prompt-never). Load under "
                f"`--approval-policy` so the lease raises a ticket to approve")
        from .approval import ApprovalRequired  # noqa: PLC0415
        for lz in leases:
            cap = lz["capability"]
            component = lz["component"]
            if self._live_lease_grant(component, cap) is not None:
                continue  # already minted from an approved ticket (retry / pre-mint)
            ticket = self._build_lease_ticket(lz)
            self._tickets[ticket["hash"]] = ticket
            if self._owner is not None:
                self._owner.prompts["perCall"] += 1
            self._count_posture("c")
            raise ApprovalRequired(ticket)

    def _build_lease_ticket(self, lz: dict) -> dict:
        """A class-(c) lease ticket the operator mints the standing grant from.

        Resolves the lease capability to its live class-(c) crossing (cone-aware,
        so a narrow lease resolves against a wider declared crossing) to bind the
        grant's component and reach-closure candidate hash — the same identity a
        proactive mint binds, so the minted grant is live for the body's crossings
        under the same closure. Refuses a lease over a capability the composition
        does not cross as class-(c) (nothing to grant) or one reachable via more
        than one closure (ambiguous — mirrors the mint/F5b guard)."""
        import hashlib  # noqa: PLC0415
        import json  # noqa: PLC0415
        cap = lz["capability"]
        targets = self._class_map.crossings_for_capability(cap) \
            if self._class_map is not None else []
        if not targets:
            raise SessionError(
                f"`effect lease {cap}` names a capability that is not a live "
                f"class-(c) crossing in this composition — there is nothing to "
                f"lease (a lease grants a boundary the code actually crosses)")
        comps = sorted({t["component"] for t in targets})
        if len(comps) > 1:
            raise SessionError(
                f"`effect lease {cap}` is reachable via {len(comps)} distinct "
                f"closures (components {', '.join(comps)}) — an ambiguous lease "
                f"cone (item 294 / F5b). Narrow the lease capability so it binds "
                f"one crossing")
        component = targets[0]["component"]
        candidate_hash = targets[0]["candidateHash"]
        body = {
            "component": component,
            "candidateHash": candidate_hash,
            "capabilities": [cap],
            "classCCapabilities": [cap],
            "kind": "lease",
            "leaseTtlMs": lz["ttlMs"],
            "leaseUses": lz["uses"],
            "leaseComponent": lz["component"],
        }
        body["hash"] = hashlib.sha256(
            json.dumps(body, sort_keys=True).encode()).hexdigest()
        return body

    def _live_lease_grant(self, component: str, capability: str):
        """The live standing grant minted for the lease over `capability` bound to
        the crossing `component`, or None. Matched by the `lease` tag, the session
        (invariant 5), un-consumed/un-revoked, and cone coverage of the lease
        capability. Used both by the gate (has this lease already been minted?)
        and by the runtime acquire (which grant does the handle name?)."""
        for g in self._grants:
            if not g.get("lease"):
                continue
            if g.get("consumed") or g.get("revoked"):
                continue
            if g.get("session") != self._session_id:
                continue
            if g.get("component") != component:
                continue
            if _cap_covers(g["capability"], capability):
                return g
        return None

    def _runtime_lease_acquire(self, component: str, capability: str,
                               ttl_ms, uses) -> str | None:
        """The runtime `_revl_frame.acquire_lease` bridge: resolve the standing
        grant the lease gate minted and return its `requestId` (the handle names
        it). None if no live lease grant backs it — the frame then fails closed
        (a lease never self-mints). `ttl_ms`/`uses` are informational here; the
        bounds live on the already-minted grant (its `expiresAt`/`remainingUses`).

        The grant's own component is the CROSSING component the ticket bound
        (`crossings_for_capability`), which may differ from the acquiring
        activation body's component; match on the crossing component so the handle
        names the grant the body's crossings actually consume against."""
        g = self._live_lease_grant(component, capability)
        if g is None:
            # the acquiring body's component is not the crossing component (the
            # lease grant was bound to the provider). Fall back to a cone match
            # ignoring component — still this session, still lease-tagged.
            for cand in self._grants:
                if cand.get("lease") and not cand.get("consumed") \
                        and not cand.get("revoked") \
                        and cand.get("session") == self._session_id \
                        and _cap_covers(cand["capability"], capability):
                    g = cand
                    break
        return g["requestId"] if g is not None else None

    def _runtime_lease_revoke(self, request_id: str) -> dict:
        """The lease disposer's own-requestId revoke (item 294, the scoped
        exemption). Retires the grant the acquiring scope minted, by id, on the
        LIFO teardown — the always-safe direction (revoking your OWN authority
        only narrows). It bypasses the operator gate because it names only the
        grant it owns; a disposer naming any other grant never reaches here (the
        lowering refuses a lease `undo` that is not `l.revoke()`)."""
        return self.revoke_standing_grant(request_id=request_id)

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

    def _ledger_entry_for_ticket(self, ticket_hash: str) -> dict | None:
        """The ledger entry already minted for this ticket hash, if any (F5: one
        ticket mints AT MOST one approval). Returns it regardless of `consumed` —
        `approve_ticket`'s idempotent-resend path needs to recognize a ticket
        that already minted, whether or not that mint has fired yet, not only a
        still-spendable one (that is `_find_standing_approval`'s job, at the
        crossing)."""
        for entry in self._ledger:
            if entry["hash"] == ticket_hash:
                return entry
        return None

    def approve_ticket(self, ticket_hash: str) -> dict:
        """Mint a standing approval bound to an outstanding ticket (Decision 2/3).
        Refuses a hash the server never issued (the outstanding-ticket table) — an
        approval can only be minted for a question the server actually asked. The
        entry is single-use and bound to the ticket hash, the reach-closure
        candidate hash, the component, and the session; a `approval-granted` WAL
        record makes it durable.

        F5: one ticket mints AT MOST one approval. A duplicate `approve_ticket`
        call against a still-outstanding ticket — the identical action re-sent by
        a UI, a retried RPC — is an IDEMPOTENT NO-OP: it returns the SAME entry
        already minted rather than appending a second unconsumed ledger entry.
        Idempotent, not a refusal, because a legitimate re-send names a ticket
        hash it did not forge — the server itself issued it, and asking the same
        question twice should not read as an error to a caller that cannot tell
        its first request landed. It is still safe against the adversarial
        replay this closes: the second call mints nothing new (no new
        `expiresAt`, no second WAL record, no second fireable ledger row), so a
        human's one yes still authorizes exactly one crossing — the per-entry
        single-use guarantee (`_find_standing_approval`/`_consume_approval`)
        becomes a per-TICKET guarantee too. The ticket stays in `self._tickets`
        (never retired) precisely so this idempotent path keeps recognizing it;
        retiring it would turn a benign resend into a confusing "unknown ticket
        hash" refusal instead."""
        ticket = self._tickets.get(ticket_hash)
        if ticket is None:
            raise SessionError(
                f"unknown ticket hash {ticket_hash!r} — the server never issued "
                f"it (or the generation changed and the outstanding-ticket table "
                f"was replaced). Re-issue the call to get a fresh ticket, then "
                f"approve that (item 246, the outstanding-ticket table)")
        existing = self._ledger_entry_for_ticket(ticket_hash)
        if existing is not None:
            return {"approved": True, "hash": existing["hash"],
                    "component": existing["component"], "key": existing.get("key"),
                    "method": existing.get("method"), "kind": existing.get("kind"),
                    "candidateHash": existing["candidateHash"]}
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
        granted = {
            "requestId": entry["requestId"], "hash": entry["hash"],
            "candidateHash": entry["candidateHash"],
            "component": entry["component"], "kind": entry["kind"],
            **self._distillation_ledger_fields(ticket)}
        wal = self._approval_wal()
        if wal is not None:
            wal.record_approval_granted(granted)
        self._approval_records.append({"record": "approval-granted", **granted})
        return {"approved": True, "hash": ticket["hash"],
                "component": ticket["component"], "key": ticket.get("key"),
                "method": ticket.get("method"), "kind": ticket.get("kind"),
                "candidateHash": ticket["candidateHash"]}

    # -- item 344: session-scoped standing capability grants ----------------

    def _grant_covers(self, grant: dict, capability: str) -> bool:
        """The grant-coverage predicate for the AUTO-APPROVE path
        (`_find_standing_grant` via `_live_grant_for`). A standing grant covers a
        class-(c) crossing capability iff the crossing is AT OR BELOW the grant in
        the one partial order (`cap_order.covers(grant, crossing)`): same token,
        and the crossing narrows every parameter the grant binds.

        Item 294 Slice 2 widens this WITHIN a token's cone only. A grant minted
        for `fs.write(path="/tmp")` covers a `fs.write(path="/tmp/job-42")`
        crossing (containment) and does NOT cover bare `fs.write` (that would be
        widening). The F1 property is preserved by clause 1 (token identity): a
        grant for token A never covers token B, distinct tokens stay discrete. A
        bare-token grant `Cap(T, ())` tops its cone, so it covers every crossing
        on token T — bit-for-bit today's identity comparison for the parameter-
        free grants every existing test mints. Grant liveness (component /
        candidate hash / session / expiry / uses) is a separate axis, applied by
        `_live_grant_for` at the crossing."""
        return _cap_covers(grant["capability"], capability)

    def _grant_within(self, grant: dict, capability: str) -> bool:
        """The grant-coverage predicate for the REVOKE path
        (`revoke_standing_grant`). A revoke SPELLING retires a grant iff the grant
        is AT OR BELOW the revoke spelling (`cap_order.covers(spelling, grant)`):
        revoking `fs.write(path="/tmp")` retires every grant at or below it (its
        whole sub-cone: `fs.write(path="/tmp/job-42")`) and no other — not bare
        `fs.write` (wider) and not a sibling `fs.write(path="/etc")`. Revoking the
        bare token retires the whole cone (every narrow grant under it). This is
        the SAME `covers` relation as `_grant_covers`, evaluated with the roles
        swapped, so find and revoke agree on the parameter-free grants exactly as
        the identity predicate did before Slice 2."""
        return _cap_covers(capability, grant["capability"])

    def _live_grant_for(self, capability: str, ticket: dict, now: int):
        """The first LIVE standing grant covering `capability` under this ticket's
        live closure: `_grant_covers` on the minted key, the same component and
        reach-closure candidate hash (invariants 4/5), the same session, unexpired
        (clock checked HERE, not at mint — invariant 3), and uses remaining. None
        when nothing live covers it. A swap that changed the closure recomputes a
        different candidate hash, so a stale grant fails here with no revocation
        bookkeeping (the same trick as the Slice-1 token)."""
        for g in self._grants:
            if g["consumed"]:
                continue
            if not self._grant_covers(g, capability):
                continue                 # a grant for A does not cover B
            if g["component"] != ticket["component"]:
                continue                 # invariant 5: minted for another deputy
            if g["candidateHash"] != ticket["candidateHash"]:
                continue                 # invariant 4: the closure changed under it
            if g["session"] != self._session_id:
                continue                 # invariant 5: cross-session replay
            exp = g.get("expiresAt")
            if exp is not None and now > exp:
                continue                 # invariant 3: expired at the crossing
            remaining = g.get("remainingUses")
            if remaining is not None and remaining <= 0:
                continue                 # uses exhausted
            return g
        return None

    def _find_standing_grant(self, ticket: dict):
        """Every LIVE standing grant needed to JOINTLY cover this class-(c)
        ticket, or None when even one class-(c) capability the ticket reaches has
        no live covering grant (fail-closed: the crossing then prompts single-use
        exactly as before). Returns a LIST (possibly one element) so the caller
        can spend a use of each grant involved.

        The ticket's class-(c) capability set is derived from its CROSSINGS
        (`classCCapabilities`), NOT the worst-class-over-reach `capabilities` fold.
        So a grant minted for one capability can never silently authorize a
        DISTINCT un-granted class-(c) capability the same call also reaches — the
        245/246-F1 over-coverage hole. Coverage per capability is `_grant_covers`,
        the identical predicate `revoke_standing_grant` retires by. Class-(a)/(b)
        capabilities need no grant (they are absent from `classCCapabilities`).
        Several live grants MAY jointly cover a multi-capability call, but EVERY
        class-(c) capability must be covered by some live grant."""
        class_c = ticket.get("classCCapabilities") or []
        if not class_c:
            return None  # no class-(c) capability to cover (fail-closed)
        now = self._now_ms()
        grants: list[dict] = []
        seen: set[int] = set()
        for cap in class_c:
            g = self._live_grant_for(cap, ticket, now)
            if g is None:
                return None  # a class-(c) capability with no live grant -> prompt
            if id(g) not in seen:
                seen.add(id(g))
                grants.append(g)
        return grants

    def _consume_grant(self, grant: dict) -> None:
        """Spend one use of a standing grant durably BEFORE the crossing fires
        (Decision 3, consume-before-fire — the Slice-2 WAL ordering still
        applies). Decrements `remainingUses`; a grant whose uses hit zero is
        marked `consumed` so it never covers again. A crash between this
        `approval-consumed` record and the fire leaves consumed-but-unfired —
        fail-closed, exactly as for a single-use token."""
        remaining = grant.get("remainingUses")
        if remaining is not None:
            grant["remainingUses"] = remaining - 1
            if grant["remainingUses"] <= 0:
                grant["consumed"] = True
        self._grants_consumed += 1
        wal = self._approval_wal()
        if wal is not None:
            wal.record_approval_consumed(grant["requestId"])

    # -- item 251 Slice 2: distilled AutoApproveRule enforcement -------------

    def _glob_members(self, glob: str) -> frozenset[str]:
        """The component names currently selected by a rule's component glob
        (`fnmatchcase`, the same machinery every item-33 rule uses). The H1
        signature: a component ENTERING this set that was not in the reviewed
        blast set suspends the rule (§6 A1)."""
        # source the generation's components from the class map (set atomically
        # with the rules), so membership is correct even while `self.ir` is still
        # being assigned during load, and always names the live generation.
        ir = self._class_map.ir if self._class_map is not None else (self.ir or {})
        names = [c.get("name") for c in ir.get("components") or []]
        return frozenset(n for n in names if n and fnmatchcase(n, glob))

    def _install_auto_approve_rules(self) -> None:
        """Materialize the bound policy's `AutoApproveRule`s into the live standing
        analog for this generation (item 251 Slice 2). Each entry carries the
        parsed rule caps (for the resource-scope `covers` check), the `admitting`
        taint set, a `remainingUses`/`expiresAt` bound, and the enumerated glob
        members it is REVIEWED against (the H1 bind). The reviewed set is snapshot
        the first time a rule is seen and PERSISTS across a swap (`_auto_reviewed`),
        so a swap that moves a new component into the glob is detected as growth.

        Inert when the policy names no `auto-approve` rule (`self._auto_rules`
        stays empty), so a composition with no distilled rule is byte-identical."""
        self._auto_rules = []
        pol = self.sandbox
        rules = getattr(pol, "auto_approve_rules", ()) if pol is not None else ()
        now = self._now_ms()
        for i, rule in enumerate(rules):
            key = rule.to_dsl()
            members = self._glob_members(rule.component)
            reviewed = self._auto_reviewed.get(key)
            if reviewed is None:
                reviewed = members
                self._auto_reviewed[key] = reviewed
            try:
                caps = [cap_order.parse_cap(c) for c in rule.caps]
            except cap_order.CapError:
                continue  # a malformed rule cannot admit anything (fail-closed)
            self._auto_rules.append({
                "requestId": "auto:" + hashlib.sha256(
                    key.encode("utf-8")).hexdigest()[:16],
                "kind": "auto-approve-rule",
                "rule": rule,
                "glob": rule.component,
                "realm": rule.realm,
                "caps": caps,
                "admitting": rule.admitting,
                "reviewedComponents": reviewed,
                "remainingUses": rule.uses,
                "expiresAt": (now + rule.ttl_ms) if rule.ttl_ms is not None
                else None,
                "consumed": False,
                "suspended": False,
            })

    def _admission_taint_origins(self, component: str) -> frozenset[str] | None:
        """The crossing's taint origin set AT ADMISSION, or None when this tier has
        no honest source (design §2.2, §6 A2 H2 corollary). The static item-249
        over-approximation (`ClassMap.static_taint`, post-endorsement) is the floor
        on a tier with the audit; a session flagged as having no runtime/static
        value taint (`_runtime_taint_available = False`) returns None, and the
        caller substitutes ALL FIVE origins (fail-closed, over-prompt is safe),
        NEVER an empty set a `{} subset admitting` test would wave through."""
        if getattr(self, "_runtime_taint_available", True) is False:
            return None
        if self._class_map is None:
            return None
        return self._class_map.static_taint(component)

    def _auto_rule_suspended(self, entry: dict) -> bool:
        """Whether a distilled rule is suspended by glob-membership GROWTH (§6 A1,
        the H1 fix). Any component currently selected by the glob that was not in
        the reviewed blast set is a signature change: the rule is bound to the
        enumerated set it was distilled from, not to the open glob, so a new member
        suspends it and it must be re-offered - never silently auto-approved."""
        current = self._glob_members(entry["glob"])
        return bool(current - entry["reviewedComponents"])

    def _auto_rule_covers(self, entry: dict, ticket: dict, now: int) -> bool:
        """Whether one distilled rule auto-approves this class-(c) ticket, using the
        SAME predicates a hand-written rule is checked by (§2.2): the component
        glob, the realm scope, the resource-scope `covers` order over EVERY
        class-(c) capability, and the taint-subset gate with the H2 admission
        floor. Liveness (uses/expiry) and the H1 suspend are checked here too."""
        if entry["consumed"]:
            return False
        exp = entry.get("expiresAt")
        if exp is not None and now > exp:
            return False
        remaining = entry.get("remainingUses")
        if remaining is not None and remaining <= 0:
            return False
        component = ticket.get("component", "")
        if not fnmatchcase(component, entry["glob"]):
            return False
        # H1: a grown glob suspends the whole rule (fail-closed, re-offer).
        if self._auto_rule_suspended(entry):
            entry["suspended"] = True
            return False
        if entry["realm"] is not None \
                and entry["realm"] != ticket.get("realm", ""):
            return False
        # every class-(c) capability the crossing reaches must be COVERED by some
        # rule cap in the item-294 order - a host-scoped rule never admits a send
        # to another host (the N1 fix, enforced by `covers`).
        class_c = ticket.get("classCCapabilities") or []
        if not class_c:
            return False
        for cap_str in class_c:
            try:
                crossing = cap_order.parse_cap(cap_str)
            except cap_order.CapError:
                return False
            if not any(cap_order.covers(rc, crossing) for rc in entry["caps"]):
                return False
        # the taint-subset gate with the H2 floor (§2.2, §6 A2 H2 corollary): an
        # UNKNOWN admission taint (None - a tier with no honest source) is treated
        # as ALL FIVE origins (fail-closed, over-prompt is safe), NEVER an empty
        # set a `{} subset admitting` test would wave through. A KNOWN set (from the
        # static over-approximation, possibly empty for a clean crossing) is used
        # as-is, so a genuinely untainted send still auto-approves.
        from ..policy import TAINT_FOLD_ORIGINS  # noqa: PLC0415
        raw = self._admission_taint_origins(component)
        admission = TAINT_FOLD_ORIGINS if raw is None else raw
        return admission <= entry["admitting"]

    def _find_auto_approve(self, ticket: dict):
        """The first LIVE distilled rule that auto-approves this class-(c) ticket,
        or None (the crossing then prompts, exactly as before - fail-closed). A
        single rule must cover EVERY class-(c) capability the crossing reaches, so
        a distilled rule can never silently authorize a capability outside the text
        an operator reviewed."""
        if not self._auto_rules:
            return None
        now = self._now_ms()
        for entry in self._auto_rules:
            if self._auto_rule_covers(entry, ticket, now):
                return entry
        return None

    def _consume_auto_rule(self, entry: dict) -> None:
        """Spend one use of a distilled rule durably BEFORE the crossing fires
        (consume-before-fire, reusing the 344 WAL ordering). A `uses`-bounded rule
        decrements `remainingUses` and marks `consumed` at zero, so an applied rule
        cannot double-fire and a crash between this `approval-consumed` record and
        the fire re-prompts (fail-closed). An unbounded rule records the spend for
        the audit join without a counter."""
        remaining = entry.get("remainingUses")
        if remaining is not None:
            entry["remainingUses"] = remaining - 1
            if entry["remainingUses"] <= 0:
                entry["consumed"] = True
        self._auto_consumed += 1
        wal = self._approval_wal()
        if wal is not None:
            wal.record_approval_consumed(entry["requestId"])

    def mint_standing_grant(self, *, ticket_hash: str | None = None,
                            capability: str | None = None,
                            uses: int | None = None,
                            ttl_ms: int | None = None) -> dict:
        """Mint a SESSION-SCOPED STANDING GRANT (roadmap item 344, fork b).

        Unlike `approve_ticket` — which binds ONE ticket hash single-use, so it
        covers only the identical call — a standing grant is keyed by a
        CAPABILITY and its reach-closure candidate hash, so it covers every
        class-(c) call reaching that capability under the same closure (differing
        args included) until its uses are exhausted or its TTL lapses. This is
        the missing shape the harness measurement found: the shell-escape session
        (n repeat `term.shell` crossings) mints one grant and auto-approves the
        rest instead of prompting per call.

        Two ways to name what is granted, both resolving to the same key:

          * from an OUTSTANDING TICKET (`ticket_hash`): the ticket already
            carries the resolved capabilities, the component, and the candidate
            hash. When the ticket reaches exactly one capability it is used; when
            it reaches several, `capability` must name which to widen.
          * PROACTIVELY against a `capability` (no ticket): the live class map
            resolves it to its class-(c) crossing target. An unknown capability,
            or one reachable via more than one distinct closure, is refused
            rather than minting an under- or over-scoped grant.

        Bounded by construction: at least one of `uses`, `ttl_ms`, or a policy
        `requires approval ttl` rule must bound the grant — an unbounded standing
        grant is refused. Gated (in the mcp verb dispatch) by the `approve`
        operator verb, exactly as `approve_ticket`."""
        if uses is not None:
            if not isinstance(uses, int) or isinstance(uses, bool) or uses < 1:
                raise SessionError(
                    "`uses` must be a positive integer — the number of class-(c) "
                    "crossings this standing grant may auto-approve")
        if ttl_ms is not None and (not isinstance(ttl_ms, int)
                                   or isinstance(ttl_ms, bool) or ttl_ms < 1):
            raise SessionError(
                "`ttlMs` must be a positive integer (milliseconds) — the window "
                "over which this standing grant stays live")

        component: str | None = None
        candidate_hash: str | None = None
        policy_ttl: int | None = None
        is_lease = False

        if ticket_hash is not None:
            ticket = self._tickets.get(ticket_hash)
            if ticket is None:
                raise SessionError(
                    f"unknown ticket hash {ticket_hash!r} — the server never "
                    f"issued it (or the generation changed and the outstanding-"
                    f"ticket table was replaced). Re-issue the call for a fresh "
                    f"ticket, then mint the grant against that (item 246, the "
                    f"outstanding-ticket table)")
            # item 294 Slice 2: a lease ticket carries the lease's own bounds
            # (`leaseTtlMs`/`leaseUses`) so approving it mints a grant bounded
            # exactly as the source `effect lease … ttl … uses …` declared,
            # without the operator re-spelling them. The grant is tagged `lease`
            # so the acquire resolves it and the disposer revokes it.
            if ticket.get("kind") == "lease":
                is_lease = True
                if uses is None:
                    uses = ticket.get("leaseUses")
                if ttl_ms is None:
                    ttl_ms = ticket.get("leaseTtlMs")
            # item 427 F3: mint against the SPELLINGS THE OPERATOR WAS SHOWN.
            # `capabilities` is the worst-class-over-reach fold and holds BARE
            # tokens; `classCCapabilities` is the class-(c) subset carrying the
            # resource-bound spellings the ticket renders and `_find_standing
            # _grant` enforces. Reading the former meant `revl_approve(hash=…,
            # uses=N)` with no explicit capability minted bare `http_post` off a
            # ticket reading `http_post(host="api.stripe.com")`: the bare token
            # TOPS the cone, so the grant then auto-approved a send to any host.
            # That contradicted `_grant_covers`'s own docstring. Now the two
            # agree: the string minted is a string the operator read.
            caps = ticket.get("classCCapabilities") or []
            if capability is None:
                if len(caps) != 1:
                    raise SessionError(
                        f"this ticket reaches {len(caps)} class-(c) capabilities "
                        f"({', '.join(caps) or 'none'}) — name which one to "
                        f"widen into a standing grant via `capability`")
                capability = caps[0]
            elif not any(_cap_covers(c, capability) for c in caps):
                raise SessionError(
                    f"capability {capability!r} is not on this ticket's class-(c) "
                    f"reach ({', '.join(caps) or 'none'}): a grant may only "
                    f"narrow a capability the crossing actually reaches, never "
                    f"widen past it (item 294: the mint must be at or below a "
                    f"declared crossing cone; item 427 F3: at or below the "
                    f"resource-bound spelling the ticket SHOWED, so a grant can "
                    f"never top the cone the operator read). To grant a wider "
                    f"cone, mint proactively against the declared capability "
                    f"with `revl_approve(capability=…)` instead of this ticket")
            component = ticket["component"]
            candidate_hash = ticket["candidateHash"]
            policy_ttl = self._ticket_ttl_ms(ticket)
        elif capability is not None:
            if self._class_map is None:
                raise SessionError(
                    "no approval policy is active, so there is no class map to "
                    "resolve a capability grant against — load under "
                    "`--approval-policy`")
            targets = self._class_map.crossings_for_capability(capability)
            if not targets:
                raise SessionError(
                    f"capability {capability!r} is not a live class-(c) crossing "
                    f"in this generation — nothing to grant (a witnessed or "
                    f"deferred capability never prompts, so it needs no grant)")
            if len(targets) > 1:
                comps = sorted({t["component"] for t in targets})
                raise SessionError(
                    f"capability {capability!r} is reachable via {len(targets)} "
                    f"distinct closures (components {', '.join(comps)}) — mint "
                    f"from an outstanding ticket to bind the exact crossing "
                    f"rather than an ambiguous proactive grant")
            component = targets[0]["component"]
            candidate_hash = targets[0]["candidateHash"]
        else:
            raise SessionError(
                "provide a `capability` (+ `uses`/`ttlMs`) to mint a standing "
                "grant, or a ticket `hash` to mint one from an outstanding "
                "class-(c) ticket")

        # item 416c: a resource dimension declared `Secret[T]` binds to the
        # REDACTED placeholder (approval.bind_resource_scope), never the real
        # value — an operator can never have SEEN the value to scope a grant to
        # it. Every secret-valued crossing of that operation binds to the SAME
        # placeholder token, so a grant minted against it would cover every
        # future crossing regardless of the (different) real value each one
        # actually carries — the exact over-authorization a resource-scoped
        # grant exists to prevent. Refused rather than silently widened; the
        # crossing keeps prompting single-use.
        if capability is not None and REDACTED_SECRET in capability:
            raise SessionError(
                f"capability {capability!r} scopes a `Secret[T]` resource "
                f"parameter — its value is never shown, so it cannot be named "
                f"in a standing grant (item 416c: this would silently widen "
                f"one approval to cover every future secret value at this "
                f"position). Approve each crossing individually with "
                f"`revl_approve(hash=...)` instead.")

        # item 294 Slice 2: erase a ceiling parameter (`calls=N`) from the
        # grant's stored valuation and translate it into the shipped
        # `remainingUses` counter. A CROSSING binds no `calls` (one call is one
        # call), so a grant that kept `calls=N` in its cone would cover no
        # crossing ever; the ceiling governs the mint, not per-crossing matching.
        # The coverage checks above ran on the FULL spelling (ceiling included in
        # the mint-vs-declaration bound), so erasure happens only now, for
        # storage. `calls` sets `remainingUses` (the tighter of it and any
        # explicit `uses`); other ceiling kinds (`size`) are dropped from the
        # cone with no counter — revl does not meter bytes (scoped to a later
        # item), so an unmetered ceiling leaves boundedness to `uses`/`ttl`.
        try:
            minted_cap = cap_order.parse_cap(capability)
        except cap_order.CapError:
            minted_cap = None
        if minted_cap is not None and not minted_cap.is_bare():
            resource_cap, ceilings = cap_order.split_ceilings(minted_cap)
            calls = ceilings.get("calls")
            if calls is not None:
                uses = calls if uses is None else min(uses, calls)
            if ceilings:
                capability = resource_cap.to_str()

        effective_ttl = ttl_ms if ttl_ms is not None else policy_ttl
        if uses is None and effective_ttl is None:
            raise SessionError(
                "a standing grant must be bounded — give `uses`, `ttlMs`, or "
                "load a policy with a `requires approval ttl` rule for this "
                "capability. An unbounded standing grant is refused (item 344 "
                "keeps consent bounded)")

        now = self._now_ms()
        request_id = ("grant:" + str(len(self._grants) + 1) + ":" + capability)
        entry = {
            "requestId": request_id,
            "kind": "standing-grant",
            "capability": capability,
            "candidateHash": candidate_hash,
            "component": component,
            "session": self._session_id,   # invariant 5: session-bound
            "grantedAt": now,
            "expiresAt": (now + effective_ttl) if effective_ttl is not None
            else None,
            "remainingUses": uses,          # None = bounded only by TTL/session
            "consumed": False,
            "revoked": False,               # item 379: set by an early revoke
            "lease": is_lease,              # item 294: minted from an effect lease
        }
        self._grants.append(entry)
        tk = self._tickets.get(ticket_hash) if ticket_hash else None
        dist = (self._distillation_ledger_fields(tk) if tk is not None
                else {"operator": self._operator_token()})
        granted = {
            "requestId": entry["requestId"], "kind": entry["kind"],
            "capability": entry["capability"],
            "candidateHash": entry["candidateHash"],
            "component": entry["component"], "session": entry["session"],
            "expiresAt": entry["expiresAt"],
            "remainingUses": entry["remainingUses"], **dist}
        wal = self._approval_wal()
        if wal is not None:
            wal.record_approval_granted(granted)
        self._approval_records.append({"record": "approval-granted", **granted})
        return {"granted": True, "kind": "standing-grant",
                "requestId": entry["requestId"], "capability": capability,
                "component": component, "candidateHash": candidate_hash,
                "remainingUses": uses, "expiresAt": entry["expiresAt"]}

    def revoke_standing_grant(self, *, capability: str | None = None,
                              request_id: str | None = None) -> dict:
        """Retire a SESSION-SCOPED STANDING GRANT EARLY (roadmap item 379).

        The symmetric partner of `mint_standing_grant` (item 344). A grant lapses
        on its own at its TTL, when its uses run out, or at session end; this is
        the only way to retire one BEFORE any of those, effective immediately and
        mid-session. Once revoked, the next class-(c) crossing the grant WOULD
        have covered prompts again (fail-closed) — the grant no longer
        auto-approves anything.

        Targeted by the SAME key `revl_approve` mints against, so revoke composes
        with a token-keyed (item 343) grant exactly as mint does:

          * a CAPABILITY (the default, capability-keyed shape) revokes EVERY live
            standing grant for that capability in this session — the token
            `emission[gateway.send]` declares when scoped (item 343), the extern
            name otherwise. This mirrors `mint_standing_grant(capability=...)`,
            which is also capability-keyed;
          * a `request_id` (the id `mint_standing_grant` returns) revokes exactly
            the one grant it names — the precise shape for retiring one of several
            grants held for the same capability.

        Revoking a capability (or id) with no LIVE grant is a clean no-op: a typed
        `{"revoked": True, "count": 0, ...}`, never a crash — idempotent, so a
        double-revoke or a stale id is harmless. Each retired grant is marked
        `revoked` (and `consumed`, so `_find_standing_grant` skips it with no new
        branch on the hot path) and gets a durable `approval-revoked` WAL record,
        so the audit can tell an operator's early cut from a natural lapse.

        Gated (in the mcp verb dispatch) by the `approve` operator verb, exactly
        as `revl_approve` — deciding to withdraw consent is the same authority as
        granting it."""
        if capability is None and request_id is None:
            raise SessionError(
                "provide a `capability` to revoke every standing grant for it, "
                "or a `requestId` (the id the mint returned) to revoke one "
                "specific grant (item 379)")

        # item 379 / revocation-F5b: a capability-wide revoke must carry the SAME
        # ambiguity refusal a proactive `mint_standing_grant(capability=...)` has.
        # A capability reachable via more than one distinct closure (component)
        # is ungated at the operator layer — `_approve_targets` cannot scope it to
        # one crossing component, so it defers (None) and `decide` lets it through
        # on the assumption the handler refuses. That is true for mint but was
        # FALSE for revoke, so a subject-scoped operator (e.g. `may approve on
        # payments`) could revoke grants on components outside its scope. Refuse
        # the ambiguous capability-wide revoke here, exactly as mint does; retiring
        # one grant of an ambiguous capability is still available by `requestId`.
        if capability is not None and self._class_map is not None:
            targets = self._class_map.crossings_for_capability(capability)
            comps = sorted({t["component"] for t in targets})
            if len(comps) > 1:
                raise SessionError(
                    f"capability {capability!r} is reachable via {len(comps)} "
                    f"distinct closures (components {', '.join(comps)}) — a "
                    f"capability-wide revoke would cross operator scopes. Revoke "
                    f"one grant by `requestId` (the id the mint returned), exactly "
                    f"as an ambiguous proactive mint is refused (item 379)")

        revoked_ids: list[str] = []
        for g in self._grants:
            if g.get("revoked") or g["consumed"]:
                continue                     # already retired or spent to zero
            if g["session"] != self._session_id:
                continue                     # invariant 5: this session's grants
            if request_id is not None and g["requestId"] != request_id:
                continue
            if capability is not None and not self._grant_within(g, capability):
                continue                     # retire the revoke spelling's whole sub-cone
            g["revoked"] = True
            g["consumed"] = True             # so _find_standing_grant skips it
            revoked_ids.append(g["requestId"])
            wal = self._approval_wal()
            if wal is not None:
                wal.record_approval_revoked(g["requestId"])

        out = {"revoked": True, "count": len(revoked_ids),
               "requestIds": revoked_ids}
        if capability is not None:
            out["capability"] = capability
        if request_id is not None:
            out["requestId"] = request_id
        return out

    # -- item 251 Slice 2: the distillation operator surface ----------------

    def distillation_offers(self) -> dict:
        """Fold this session's approval ledger to candidate `AutoApproveRule`
        offers (roadmap item 251, §4). Read-only and PROPOSE-ONLY: it applies no
        policy. Scoped to the CALLER's own grants - an operator sees offers
        distilled only from yeses attributed to them (an empty operator token sees
        the unattributed grants, the ungated single-agent case). Each offer carries
        its rule text, blast radius, attributed operator, and source sessions."""
        from ..distill import distill  # noqa: PLC0415 - pure, lazy
        me = self._operator_token()
        records = [r for r in self._approval_records
                   if str(r.get("operator", "")) == me]
        result = distill(records)
        offers = []
        for off in result.offers:
            offers.append({
                "offerId": "offer:" + hashlib.sha256(
                    off.rule_text.encode("utf-8")).hexdigest()[:16],
                "rule": off.rule_text,
                "operator": off.operator,
                "sessions": list(off.sessions),
                "grantCount": off.grant_count,
                "blast": {
                    "covered": off.blast.covered,
                    "total": off.blast.total,
                    "resourceScope": off.blast.resource_scope,
                    "destinations": list(off.blast.destinations),
                    "negativeGuarantee": sorted(off.blast.negative_guarantee),
                }})
        refusals = [{"reason": r.reason.value, "token": r.token,
                     "realm": r.realm, "detail": r.detail} for r in result.refusals]
        return {"offers": offers, "refusals": refusals}

    def _offer_by_id(self, offer_id: str):
        """Resolve an offer id back to its `DistilledOffer` (re-folding the ledger,
        so the applied rule is exactly the reviewed text - never operator-supplied
        rule text that the review never saw)."""
        from ..distill import distill  # noqa: PLC0415
        me = self._operator_token()
        records = [r for r in self._approval_records
                   if str(r.get("operator", "")) == me]
        for off in distill(records).offers:
            oid = "offer:" + hashlib.sha256(
                off.rule_text.encode("utf-8")).hexdigest()[:16]
            if oid == offer_id:
                return off
        return None

    def apply_distillation(self, offer_id: str) -> dict:
        """Install a distilled offer as a live `AutoApproveRule` (roadmap item 251,
        §4). Writes the rule into the bound policy (`self.sandbox`), re-materializes
        it against the live generation (binding the H1 reviewed component set to
        the offer's blast set), and records a `distillation-applied` WAL fact with
        the attribution (`distilledBy`, `reviewedBy`, `appliedAt`, the ledger
        window). Gated in the mcp verb dispatch by the item-55 `approve` verb - the
        same authority as granting the underlying yeses.

        Carries the mint/revoke ambiguity refusal: a distilled cone whose rule text
        already stands in the policy is refused rather than silently duplicated."""
        import dataclasses  # noqa: PLC0415 - stdlib
        from ..policy import Policy  # noqa: PLC0415
        offer = self._offer_by_id(offer_id)
        if offer is None:
            raise SessionError(
                f"unknown distillation offer {offer_id!r} - re-read "
                f"`distillation_offers` (the ledger folds to the current offers; "
                f"an offer id is stable only while its shape is still settled)")
        rule = offer.rule
        pol = self.sandbox if self.sandbox is not None else Policy()
        existing = {r.to_dsl() for r in getattr(pol, "auto_approve_rules", ())}
        if rule.to_dsl() in existing:
            raise SessionError(
                f"a rule with the identical text is already applied "
                f"({rule.to_dsl()!r}) - an offer already covered by a live rule is "
                f"refused rather than duplicated (item 251, the apply ambiguity "
                f"refusal)")
        self.sandbox = dataclasses.replace(
            pol, auto_approve_rules=tuple(pol.auto_approve_rules) + (rule,))
        # bind the H1 reviewed set to the offer's blast components (the enumerated
        # set the operator reviewed), not merely the current glob membership.
        reviewed = frozenset(nc.component for nc in offer.blast.not_covered) \
            | self._glob_members(rule.component)
        self._auto_reviewed[rule.to_dsl()] = reviewed
        self._install_auto_approve_rules()
        now = self._now_ms()
        me = self._operator_token()
        fields = {
            "rule": rule.to_dsl(),
            "distilledBy": offer.operator,
            "reviewedBy": me,
            "distilledFrom": {"sessions": list(offer.sessions),
                              "grantCount": offer.grant_count},
            "blast": {"covered": offer.blast.covered, "total": offer.blast.total},
            "appliedAt": now,
        }
        wal = self._approval_wal()
        if wal is not None:
            wal.record_distillation_applied(fields)
        self._distillation_seq += 1
        return {"applied": True, "offerId": offer_id, "rule": rule.to_dsl(),
                "distilledBy": offer.operator, "reviewedBy": me}

    def revoke_distillation(self, rule: str) -> dict:
        """Retire an applied distilled rule from the live policy (roadmap item 251,
        §4), the symmetric partner of `apply_distillation`. Removes the rule
        (matched by its canonical DSL text), re-materializes, and records a
        `distillation-revoked` WAL fact - the NEXT matching crossing prompts again
        (fail-closed); consume-before-fire already covers any in-flight crossing.
        Revoking a rule with no live match is a clean no-op (`count: 0`). Gated by
        the item-55 `approve` verb, exactly as apply."""
        import dataclasses  # noqa: PLC0415
        pol = self.sandbox
        rules = tuple(getattr(pol, "auto_approve_rules", ())) if pol is not None \
            else ()
        # canonicalize the target through the parser so a caller can name the rule
        # by any equivalent spelling; fall back to the raw text on a parse miss.
        want_dsl = {rule}
        try:
            from ..policy import parse_policy  # noqa: PLC0415
            want_dsl |= {r.to_dsl() for r in parse_policy(rule).auto_approve_rules}
        except Exception:  # noqa: BLE001 - a bare id or non-DSL fragment
            pass
        kept, removed = [], []
        for r in rules:
            if r.to_dsl() in want_dsl:
                removed.append(r.to_dsl())
            else:
                kept.append(r)
        if removed and pol is not None:
            self.sandbox = dataclasses.replace(pol,
                                               auto_approve_rules=tuple(kept))
            self._install_auto_approve_rules()
            me = self._operator_token()
            wal = self._approval_wal()
            for dsl in removed:
                if wal is not None:
                    wal.record_distillation_revoked({"rule": dsl, "revokedBy": me})
            self._distillation_seq += 1
        return {"revoked": True, "count": len(removed), "rules": removed}

    # -- item 251 Slice 3: the time axis over the persisted ledger -----------

    # a "prompt" in the series is a human decision the ledger recorded: a yes
    # (`approval-granted`) or a no (`approval-denied`). A crossing a standing
    # grant or a distilled rule auto-approved writes neither, so it never counts
    # here - which is exactly why the series falls as distillation lands.
    _PROMPT_RECORDS = ("approval-granted", "approval-denied")

    def _prompts_per_session_series(self, records: list) -> list:
        """The persisted per-session prompt tally (§4, the time axis): one entry
        per session that raised at least one class-(c) approval prompt, in the
        order the sessions FIRST appear in the WAL. The WAL is append-ordered and
        fsync'd per record, so first-appearance order is a stable wall-clock
        ordering with no timestamp tie to break - deterministic across reads.

        Distillation is what bends the series down: a crossing a distilled rule
        now auto-approves stops writing an `approval-granted`, so that session's
        count trends toward the irreducible floor instead of resetting each
        session (the item-248 headline made measurable over time)."""
        order: list = []
        counts: dict = {}
        for rec in records:
            if rec.get("record") not in self._PROMPT_RECORDS:
                continue
            sid = str(rec.get("session", ""))
            if sid not in counts:
                counts[sid] = 0
                order.append(sid)
            counts[sid] += 1
        return [{"session": sid, "prompts": counts[sid]} for sid in order]

    def _distillation_impact(self, records: list) -> dict:
        """The before/after prompt reduction each applied rule achieved, plus the
        irreducible floor (§4). Both are computed from the SAME WAL the series
        reads and the SAME predicates the runtime consent path enforces:

          * `perRule`: one entry per `distillation-applied` record, in ledger
            order. `before` is the count of recorded prompts of the rule's shape
            that fell BEFORE the apply point, `after` the count that fell after -
            using `distill.blast_radius`, the runtime coverage predicate, so a
            prompt counts iff the rule would have covered it. A settled rule drives
            `after` to zero: the matching crossings now auto-approve and write no
            prompt, so `reduced = before - after` is the prompts the rule removed.
          * `floor`: the count of distinct shape keys the distiller CANNOT distill
            (seen in one session, mixed-operator, taint-varying, or spanning
            resource values with no common cone) - the irreducible count of
            genuinely novel decisions the series converges on, not a rhetorical
            target."""
        from ..distill import blast_radius, distill  # noqa: PLC0415 — pure, lazy
        from ..policy import parse_policy  # noqa: PLC0415
        # keep each prompt's WAL POSITION, not the dict, so two shape-identical
        # prompts never collide the way `list.index` (value equality) would.
        prompts = [(i, r) for i, r in enumerate(records)
                   if r.get("record") in self._PROMPT_RECORDS]
        per_rule: list = []
        for idx, rec in enumerate(records):
            if rec.get("record") != "distillation-applied":
                continue
            text = str(rec.get("rule", ""))
            try:
                rules = parse_policy(text).auto_approve_rules
            except Exception:  # noqa: BLE001 — a malformed record is not a metric
                continue
            if not rules:
                continue
            rule = rules[0]
            before_win = [r for pos, r in prompts if pos < idx]
            after_win = [r for pos, r in prompts if pos > idx]
            before = blast_radius(rule, before_win).covered
            after = blast_radius(rule, after_win).covered
            per_rule.append({
                "rule": rule.to_dsl(),
                "before": before,
                "after": after,
                "reduced": before - after,
                "appliedAt": rec.get("appliedAt"),
            })
        floor = len(distill(records).refusals)
        return {"floor": floor, "perRule": per_rule}

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
        # item 251 Slice 3: the persisted time axis, folded from the WAL (which
        # outlives the session) rather than the in-memory single-session ledger.
        ledger = self._wal_ledger_records()
        return {
            "policy": self.approval_policy,
            "approvals": approvals,
            "percentAutoApproved": percent,
            "promptsPerSession": sum(prompts.values()) if prompts else 0,
            "prompts": prompts,
            "outstandingTickets": sorted(self._tickets),
            # item 344: the grant-consumed half of the picture — per-call
            # class-(c) crossings that auto-approved against a standing grant
            # instead of prompting, and the grants currently live. This is what
            # takes prompts-per-session below the single-use floor for a
            # repeat-shaped session.
            "grantsConsumed": self._grants_consumed,
            # item 310: seam-method cache hits in their OWN counter, never folded
            # into `silent` (which would overstate the auto-approved-with-proof
            # number). A hit is a prompt avoided by proof of FRESHNESS, not of
            # revertibility — its own line (design laundering point 5).
            "cacheHits": self._cache_hits,
            "standingGrants": [
                {"capability": g["capability"], "component": g["component"],
                 "remainingUses": g.get("remainingUses"),
                 "expiresAt": g.get("expiresAt"), "consumed": g["consumed"],
                 # item 379: True when an operator retired the grant EARLY (as
                 # opposed to it lapsing on uses/TTL). Both leave it out of the
                 # live set; this bit distinguishes them for the audit.
                 "revoked": g.get("revoked", False)}
                for g in self._grants],
            # item 251 Slice 3: the TIME AXIS. `promptsPerSessionSeries` is the
            # per-session prompt tally read from the WAL (deterministic, ordered by
            # first appearance), and `distillationImpact` carries the before/after
            # prompt reduction each applied rule achieved plus the irreducible
            # floor (§4). Both fold the persisted ledger, so prompts-per-session
            # trends toward the floor ACROSS sessions instead of resetting.
            "promptsPerSessionSeries": self._prompts_per_session_series(ledger),
            "distillationImpact": self._distillation_impact(ledger),
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
            # item 372: the provided-key report derives from ROOT itself, so a
            # key is listed loaded IFF it actually has a live provider. A fiber
            # left ACTIVE by a cancelled deferred activation, with no provision
            # in ROOT, can never be reported as providing its key here.
            "providedKeys": sorted(driver.resolved_keys()),
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
