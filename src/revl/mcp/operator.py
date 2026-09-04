"""Operator capabilities — G4 for the management plane (roadmap item 55).

Everything a *component* may reach is bounded by G4 and, at the composition
level, by the boundary policy (item 33). But the MCP session's own verbs —
``revl_swap``, ``revl_unload``, ``revl_restore``, ``revl_rollback``,
``revl_edit``, ``revl_load``, ``revl_snapshot`` — can rewrite a running system,
and nothing there authenticates or scopes the caller. Anyone who reaches the
transport is root over the composition. This module adds the missing floor: a
declared **operator profile** that bounds which management verbs a session may
call, over which components and realms.

The triad it joins:

    G4 (per component)        what may a component reach?
    policy item 33 (compose)  what may anything in the composition reach?
    operators item 55 (this)  what may the *operator* driving the session do?

An operator profile is the exact same shape as the boundary policy: allow/deny
rules over globs, evaluated as pure set operations, refusing with a why-trace
(:mod:`revl.why`) that names the offending chain. It reuses item 33's glob
decision (:mod:`revl.policy`) rather than reimplementing it — the realm of a
target component is resolved with ``policy.component_realms``, and subject
matching is the same ``fnmatch`` semantics ``policy`` uses for capabilities.

Binding. A session runs *as* one operator (its token). Today the stdio
transport carries a single session, so the identity is fixed at serve time
(``revl mcp serve --operator-profile FILE [--operator TOKEN]``). When the
transport later carries a per-caller token (item 39), the same registry maps
each token to its operator with no change here.

Back-compatible. With **no** profile configured, ``session.operator`` is None
and every verb is ungated — today's root-over-transport, unchanged. The profile
is opt-in, for networked / multi-operator use; it is the pre-networking
safeguard, not a new default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase

from ..errors import RevlError
from ..why import CHAIN, TraceStep, WhyTrace

# The management verbs an operator profile scopes, keyed by the MCP tool that
# performs each. A tool absent here is a read-only / diagnostic verb and is
# never gated (docs/operator-capabilities.md).
TOOL_VERB = {
    "revl_load": "load",
    "revl_swap": "swap",
    "revl_edit": "edit",
    "revl_unload": "unload",
    "revl_restore": "restore",
    "revl_snapshot": "snapshot",
    "revl_rollback": "undo",
    "revl_undo": "undo",  # item 65: deep history revert — same authority as rollback
    # item 245: the session commit/abort verbs. `commit` gates who may cross the
    # session boundary (flush the deferral queue, discharge the witnessed
    # escrow); enumeration (`revl_commit`) and abort share that authority — an
    # operator that cannot commit cannot decide the session's verdict.
    "revl_commit": "commit",
    "revl_commit_confirm": "commit",
    "revl_abort": "commit",
    # item 246: minting a class-(c) approval is its own scoped authority. `approve`
    # gates who may say yes to an irreversible crossing, in the same profile
    # grammar as `commit` — an operator without it cannot launder authority
    # through the prompt (docs/design/246-auto-approve.md, Decision 4). Its
    # `_targets` branch resolves the presented ticket hash — or an item-344
    # standing grant's capability — to the crossing's component, so a subject-
    # scoped `may approve on payments` grant is usable while other components are
    # live.
    "revl_approve": "approve",
    # item 379: revoking a standing grant early is the mirror of granting it —
    # withdrawing consent is the SAME authority as saying yes, so `revl_revoke`
    # gates under the same `approve` verb. Its `_approve_targets` branch resolves
    # the revoke's `capability` (or the grant's `requestId`) to the crossing
    # component, so a subject-scoped `may approve on payments` grant governs who
    # may take a grant BACK exactly as it governs who may mint one.
    "revl_revoke": "approve",
    # item 251: applying or revoking a distilled `AutoApproveRule` installs (or
    # withdraws) a STANDING auto-approve - the same authority as granting the
    # underlying yeses, so both gate under `approve`. `_approve_targets` resolves
    # the offer/rule to the components its glob selects, so a subject-scoped
    # `may approve on payments` grant governs distillation over payments exactly as
    # it governs a mint. `revl_distillation_offers` is read-only (propose-only) and
    # deliberately ungated (absent from this map).
    "revl_apply_distillation": "approve",
    "revl_revoke_distillation": "approve",
    # item 443: the operator E-Stop. Its own verb, never folded into `unload`
    # or `commit`: an E-Stop is not a teardown and not a verdict on the work,
    # it is the authority to STOP DISPATCHING, and an operator trusted to
    # unload cleanly is not automatically trusted to strand two hundred
    # brackets. It is also the one verb a composition or an agent must never
    # be able to invoke on itself, which is what holding it here buys
    # (docs/design/443-estop.md, docs/operator-capabilities.md).
    #
    # Its target set is the WHOLE running composition (`_targets`' fall-through
    # to `_live_targets`), because a halt that stopped one component would not
    # be a halt. So a subject-scoped `may estop on tenant_a*` authorizes only
    # while every live component is in `tenant_a*`; an operator who must be
    # able to hit the button always needs `may estop on *`.
    "revl_estop": "estop",
}

# ---------------------------------------------------------- composed verbs
#
# The gate is positional over the DISPATCH TABLE — `server.handle` runs
# `decide` before it calls a handler — but a tool that reaches a privileged
# operation through ANOTHER tool's machinery is dispatched under its own name
# and so was gated by nothing. Two do:
#
#   * `revl_ship` fuses check -> admit -> plan -> swap and, with `apply: true`,
#     calls the `revl_swap` HANDLER directly. `handle` gated `revl_ship`, which
#     carried no verb, and never saw the swap underneath it.
#   * `revl_repair`'s remediation step calls `Session.swap` itself, so it went
#     past both the operator gate and the item-61 lease check `_tool_swap`
#     performs.
#
# They are mapped to `swap` — the authority the operation they perform already
# has — rather than to new verbs, because that is what they DO: an operator who
# may not swap a component may not ship or repair it either, and one who may
# needs no second grant. Both are CONDITIONAL: each has a rehearsal mode that
# mutates nothing (`revl_ship` unless `apply`, `revl_repair` with
# `apply: false`), and a rehearsal is not a privileged action.
#
# The rule for anyone adding a verb: if a tool can reach `Session.swap` /
# `.load` / `.unload` / `.restore` / `.rollback` / `.undo` / `.estop` — its own
# handler or any handler it calls — it belongs in `TOOL_VERB` or here.
# `tests/test_mcp_authority_gate.py` enumerates every advertised tool and
# fails on one that is neither gated nor recorded as ungated with a reason.
COMPOSED_TOOL_VERB = {
    "revl_ship": "swap",
    "revl_repair": "swap",
}


def composed_applies(tool_name: str, arguments: dict) -> bool:
    """Does this composed verb actually perform its mutation with these
    arguments? `revl_ship` swaps only when `apply` is truthy (it defaults to
    the read-only rehearsal); `revl_repair` swaps unless `apply` is explicitly
    false (it defaults to applying)."""
    if tool_name == "revl_ship":
        return bool(arguments.get("apply"))
    if tool_name == "revl_repair":
        return arguments.get("apply", True) is not False
    return True


def swap_arguments(tool_name: str, arguments: dict) -> dict:
    """A composed verb's arguments in the shape `_targets`/`leases.check_swap`
    read a swap in, so both derive targets for the swap that will actually run
    rather than for the wrapper's own argument shape.

    `revl_ship` already carries `source`/`files`/`modules`/`replacing` at the
    top level. `revl_repair` carries them under `candidate`, and its swap
    replaces the component it was asked to repair — so `replacing` is that
    component, which is exactly what makes the derivation compile (a repair
    candidate redeclares the faulting component; without `replacing` it would
    collide with the running one on G2 and derive nothing)."""
    if tool_name == "revl_repair":
        candidate = arguments.get("candidate") or {}
        component = arguments.get("component")
        return {"source": candidate.get("source"),
                "files": candidate.get("files"),
                "modules": candidate.get("modules"),
                "replacing": [component] if component else []}
    return arguments

# friendly verb aliases the profile author may write (canonical on the right)
VERB_ALIASES = {"rollback": "undo"}

# the subject token that stands for "the whole composition, unscoped" — a
# target no named realm/component glob can bound, only a literal ``*``. Mirrors
# how policy treats the unnameable ``*`` reach.
WHOLE = "*"


# --------------------------------------------------------------------- model


@dataclass(frozen=True)
class Grant:
    """One allow-or-deny rule: a set of verb globs over a set of subject globs.

    ``allow`` is True for ``may`` (a capability the operator holds), False for
    ``may not`` (a prohibition that refuses regardless of any allow — deny
    wins, exactly as in the boundary policy). A subject glob matches a target's
    component *name* or any *realm* it is isolated into.
    """

    verbs: tuple[str, ...]
    subjects: tuple[str, ...]
    allow: bool

    def covers_verb(self, verb: str) -> bool:
        return any(fnmatchcase(verb, pat) for pat in self.verbs)

    def covers_subject(self, labels: frozenset[str]) -> bool:
        # a `*` subject grant covers anything, including the unnameable WHOLE;
        # a named glob covers WHOLE only if it is literally `*`.
        for pat in self.subjects:
            for label in labels:
                if label == WHOLE:
                    if pat == WHOLE:
                        return True
                    continue
                if fnmatchcase(label, pat):
                    return True
        return False


@dataclass(frozen=True)
class Operator:
    """One operator identity: a token and the grants bound to it."""

    token: str
    grants: tuple[Grant, ...] = ()

    def allows(self, verb: str, labels: frozenset[str]) -> tuple[bool, Grant | None]:
        """Decide one (verb, target) pair. Deny wins over allow; an allow must
        exist for the pair or it is refused (closed by default)."""
        for grant in self.grants:
            if not grant.allow and grant.covers_verb(verb) \
                    and grant.covers_subject(labels):
                return False, grant  # explicit prohibition
        for grant in self.grants:
            if grant.allow and grant.covers_verb(verb) \
                    and grant.covers_subject(labels):
                return True, grant
        return False, None  # no allow selects it — refused


@dataclass(frozen=True)
class OperatorRegistry:
    """Every operator a profile declares, keyed by token."""

    operators: dict[str, Operator]
    source: str | None = None

    def get(self, token: str) -> Operator | None:
        return self.operators.get(token)

    def sole(self) -> Operator | None:
        """The only operator, when the profile declares exactly one — so a
        single-operator serve need not repeat ``--operator``."""
        if len(self.operators) == 1:
            return next(iter(self.operators.values()))
        return None


# ------------------------------------------------------------------- parsing


class ProfileError(RevlError):
    """A malformed operator-profile file (a parse error, not a refusal)."""


def _canon_verbs(verbs) -> tuple[str, ...]:
    return tuple(VERB_ALIASES.get(v, v) for v in verbs)


def _split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _parse_dsl(text: str, source: str | None) -> OperatorRegistry:
    """The line DSL (blank lines and ``#`` comments ignored). Grammar:

        operator <token> may     <verb>[, ...] on <subject>[, ...]
        operator <token> may not <verb>[, ...] on <subject>[, ...]
        operator <token> may     <verb>[, ...]                       # on *

    A verb of ``*`` matches every management verb; a subject of ``*`` matches
    every component and realm. ``on`` may be omitted to mean ``on *``.
    """
    operators: dict[str, list[Grant]] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0].lower() != "operator":
            raise ProfileError(source, lineno,
                               f"expected `operator <token> may ...`: {raw.strip()!r}")
        token = parts[1]
        rest = parts[2]
        low = rest.lower()
        for verb, allow in (("may not ", False), ("may ", True)):
            if low.startswith(verb):
                clause = rest[len(verb):]
                break
        else:
            raise ProfileError(source, lineno,
                               f"expected `may` or `may not` after the token: "
                               f"{raw.strip()!r}")
        if " on " in clause:
            verbs_text, subjects_text = clause.split(" on ", 1)
            subjects = _split(subjects_text)
        else:
            verbs_text, subjects = clause, (WHOLE,)
        verbs = _canon_verbs(_split(verbs_text))
        if not verbs:
            raise ProfileError(source, lineno,
                               f"profile line names no verb: {raw.strip()!r}")
        if not subjects:
            raise ProfileError(source, lineno,
                               f"profile line names no subject: {raw.strip()!r}")
        operators.setdefault(token, []).append(Grant(verbs, subjects, allow))
    return OperatorRegistry(
        {t: Operator(t, tuple(g)) for t, g in operators.items()}, source)


def _parse_json(text: str, source: str | None) -> OperatorRegistry:
    """The JSON equivalent — same model, machine-authored.

    { "operators": [
        { "token": "alice",
          "grants": [ {"verbs": ["swap"], "on": ["tenant_a*"]},
                      {"verbs": ["unload"], "on": ["*"], "deny": true} ] } ] }
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileError(source, exc.lineno, f"invalid profile JSON: {exc.msg}")
    if not isinstance(doc, dict):
        raise ProfileError(source, 1, "a JSON operator profile must be an object")
    operators: dict[str, Operator] = {}
    for entry in doc.get("operators") or []:
        token = entry.get("token")
        if not token:
            raise ProfileError(source, 1, "an operator entry needs a `token`")
        grants: list[Grant] = []
        for g in entry.get("grants") or []:
            verbs = _canon_verbs(tuple(g.get("verbs") or ()))
            subjects = tuple(g.get("on") or g.get("subjects") or (WHOLE,))
            if not verbs:
                raise ProfileError(source, 1,
                                   f"a grant for `{token}` names no verb")
            grants.append(Grant(verbs, subjects, not g.get("deny")))
        operators[token] = Operator(token, tuple(grants))
    return OperatorRegistry(operators, source)


def parse_profile(text: str, source: str | None = None) -> OperatorRegistry:
    """Parse profile text — JSON when it opens with ``{``, else the line DSL."""
    return (_parse_json(text, source) if text.lstrip().startswith("{")
            else _parse_dsl(text, source))


def load_profile(path: str) -> OperatorRegistry:
    with open(path, encoding="utf-8") as handle:
        return parse_profile(handle.read(), source=path)


# -------------------------------------------------------------- the decision


@dataclass(frozen=True)
class Decision:
    """The outcome of gating one verb dispatch.

    * ``gated`` — did the operator profile apply at all? (False = no profile,
      or a read-only verb: the call proceeds unchanged.)
    * ``allowed`` — was the action authorized? (meaningful only when gated.)
    * ``operator`` / ``verb`` / ``subjects`` — the *who* and *what*, recorded
      into the causal trace on an authorized management action.
    * ``why`` — the policy-style refusal trace, when refused.
    * ``message`` — the human refusal line.
    """

    gated: bool
    allowed: bool = True
    operator: str | None = None
    verb: str | None = None
    subjects: tuple[str, ...] = ()
    why: WhyTrace | None = None
    message: str | None = None


def _labels_for(ir: dict, name: str, realms) -> frozenset[str]:
    return frozenset({name}) | frozenset(realms)


def _live_targets(ir: dict | None) -> list[tuple[str, frozenset[str]]] | None:
    """Every live component as a (name, labels) target, labels being its name
    plus every realm it is isolated into. None when nothing is loaded (the
    handler will report that; gating stays out of its way)."""
    if not ir:
        return None
    from ..policy import component_realms  # noqa: PLC0415 — read-only reuse of item 33

    manifest = ir.get("manifest") or {}
    targets = []
    for entry in manifest.get("components") or []:
        name = entry.get("name")
        realms = component_realms(manifest, name)
        targets.append((name, _labels_for(ir, name, realms)))
    return targets or [("*", frozenset({WHOLE}))]


def _semantic(entry: dict) -> dict:
    """A component IR entry without its provenance — the semantic content that
    decides whether a swap actually *touched* it. `source` is only the origin
    filename, and the body carries no line numbers, so two compiles of the same
    component (even shifted by an edit elsewhere) compare equal."""
    return {k: v for k, v in entry.items() if k != "source"}


def _changed_targets(old_ir: dict, new_ir: dict) \
        -> list[tuple[str, frozenset[str]]]:
    """The components a swap actually touches: those added, removed, or whose
    IR entry differs (modulo provenance) between the running composition and
    the candidate. Realms come off whichever side declares the component."""
    from ..policy import component_realms  # noqa: PLC0415 — read-only reuse of item 33

    old = {c["name"]: _semantic(c) for c in old_ir.get("components") or []}
    new = {c["name"]: _semantic(c) for c in new_ir.get("components") or []}
    old_man = old_ir.get("manifest") or {}
    new_man = new_ir.get("manifest") or {}
    changed: list[tuple[str, frozenset[str]]] = []
    for name in sorted(set(old) | set(new)):
        if old.get(name) == new.get(name):
            continue  # untouched by this swap
        man = new_man if name in new else old_man
        source_ir = new_ir if name in new else old_ir
        changed.append((name, _labels_for(source_ir, name,
                                          component_realms(man, name))))
    return changed


def _snapshot_targets(snap: dict | None) \
        -> list[tuple[str, frozenset[str]]]:
    """A restore's targets, read from the snapshot manifest without compiling
    (a snapshot is re-admitted by the handler; gating only needs the names)."""
    if not isinstance(snap, dict):
        return [("*", frozenset({WHOLE}))]
    manifest = (snap.get("manifest") or {})
    from ..policy import component_realms  # noqa: PLC0415 — read-only reuse of item 33

    targets = []
    for entry in manifest.get("components") or []:
        name = entry.get("name")
        targets.append((name, frozenset({name}) | component_realms(manifest, name)))
    return targets or [("*", frozenset({WHOLE}))]


def _compile_candidate(arguments: dict, manifest: dict | None = None,
                       replacing: tuple = ()):
    """Compile inline source for target derivation. Pure frontend, no runtime.
    Returns None when the candidate does not compile — an *undecidable* target
    set, which every caller must then fail CLOSED on (see `_targets`).

    The compile must be the SAME SHAPE the handler's compile is, or the gate
    scopes a different action than the one that runs. Two inputs were missing,
    and their absence was an authority bypass rather than a scoping bug:

      * `replacing` — `revl_swap`'s handler passes it, so a candidate that
        RENAMES the component it replaces links cleanly there. Without it the
        derivation compile sees the running provider *and* the renamed one and
        dies on a G2 provision conflict, so the target set came back
        undecidable and the swap sailed past both the operator gate and an
        enforced lease on the component it was replacing.
      * the operator's sanctioned `providers` — merged UNDER the agent's own
        `modules` exactly as `server.compile_under_authoring` merges them, so a
        composition that resolves for the handler resolves here too.

    Deliberately UNPROFILED: this derives *which components an action touches*,
    and it neither lowers a host body nor boots anything. The authoring trust is
    enforced positionally before this runs (`server._authoring_refusal`) and
    again by the handler's own compile; running it here would only turn refused
    source into an undecidable target set."""
    from ..compiler import compile_files, compile_source  # noqa: PLC0415 — see docstring
    from ..errors import RevlError as _RevlError  # noqa: PLC0415

    from .server import AUTHORING  # noqa: PLC0415 — cycle

    merged = dict(AUTHORING.providers or {})
    for path, text in (arguments.get("modules") or {}).items():
        merged.setdefault(path, text)
    try:
        if arguments.get("source") is not None:
            return compile_source(arguments["source"], "<candidate>.rvl",
                                  manifest=manifest, replacing=replacing,
                                  modules=merged or None)
        if arguments.get("files"):
            return compile_files(list(arguments["files"]), manifest=manifest,
                                 replacing=replacing)
    except _RevlError:
        return None
    return None


def _targets(verb: str, session, arguments: dict) \
        -> list[tuple[str, frozenset[str]]] | None:
    """The components a management verb touches, as (name, labels) targets.

    ``None`` means *undecidable here* — the target set cannot be determined
    without running the action. It is NOT a licence to proceed. Every caller
    must fail CLOSED on it, because "I could not work out what this touches" is
    the one answer an authority gate may never read as "so let it through":

      * :func:`decide` scopes an undecidable action to the unnameable whole
        composition, which only a literal ``may <verb> on *`` grant authorizes;
      * :func:`leases.check_swap` refuses an undecidable swap against ANY
        component another operator holds.

    Deferring instead was the bypass: a `revl_swap` whose candidate renamed the
    component it replaced compiled cleanly for the handler and not for the
    derivation (which dropped `replacing`), so an undecidable target set turned
    both the operator gate and an enforced lease into no-ops on exactly the
    swap that needed them most."""
    ir = session.ir
    if verb == "swap":
        inline = any(arguments.get(k) is not None
                     for k in ("source", "files", "modules"))
        if not inline or ir is None:
            return _live_targets(ir)  # server-side re-admit / cold: whole comp
        candidate = _compile_candidate(
            arguments, manifest=ir,
            replacing=tuple(arguments.get("replacing") or ()))
        if candidate is None:
            return None  # undecidable — callers fail closed
        changed = _changed_targets(ir, candidate)
        return changed or _live_targets(ir)  # a no-op swap re-admits everything
    if verb == "load":
        candidate = _compile_candidate(arguments)
        if candidate is None:
            return None
        return _live_targets(candidate)
    if verb == "restore":
        return _snapshot_targets(arguments.get("snapshot"))
    if verb == "approve":
        return _approve_targets(session, arguments)
    # unload / edit / snapshot / undo operate on the whole running composition
    return _live_targets(ir)


def _approve_targets(session, arguments: dict) \
        -> list[tuple[str, frozenset[str]]] | None:
    """A `revl_approve`'s target: the crossing component the approval names,
    resolved WITHOUT running anything (the same resolve-without-running pattern
    `_snapshot_targets` uses for `restore`). A ticket `hash` resolves against the
    session's outstanding-ticket table; a proactive item-344 `capability` grant
    resolves against the live class map. Either way the target scopes to the
    crossing component and its realms, so a subject-scoped `may approve on
    payments` grant is not defeated by other live components (Decision 2's
    approve branch). Undecidable inputs — an unknown hash, or a capability that
    resolves to more than one component — defer (None): the handler refuses them
    by the outstanding-ticket table / the ambiguity guard before minting
    anything, so gating never spuriously scopes an input that will not be
    honoured."""
    from ..policy import component_realms  # noqa: PLC0415 — read-only reuse
    ticket_hash = arguments.get("hash")
    tickets = getattr(session, "_tickets", None) or {}
    ticket = tickets.get(ticket_hash) if ticket_hash else None
    name = None
    if ticket is not None:
        name = ticket.get("component")
    elif arguments.get("requestId") is not None:
        # item 379: a revoke naming one grant by id — scope to that grant's
        # component (the same subject-scoped gating a mint got), resolved off the
        # session's grant store without running anything. An unknown id defers.
        for g in getattr(session, "_grants", None) or []:
            if g.get("requestId") == arguments["requestId"]:
                name = g.get("component")
                break
    elif arguments.get("capability") is not None:
        # item 344/379: a proactive capability grant or a capability-wide revoke —
        # scope to the crossing component when the capability resolves to exactly
        # one, else defer.
        class_map = getattr(session, "_class_map", None)
        if class_map is not None:
            resolved = class_map.crossings_for_capability(arguments["capability"])
            components = {t["component"] for t in resolved}
            if len(components) == 1:
                name = next(iter(components))
    elif arguments.get("offerId") is not None \
            or arguments.get("rule") is not None:
        # item 251: apply/revoke a distilled rule - scope to the components the
        # rule's glob selects, so the operator must hold `approve` over EVERY one
        # (all-or-nothing, like a multi-component swap). An offer resolves through
        # the session's fold; a bare rule string resolves through its glob. When no
        # component matches (nothing live under the glob), defer.
        return _distillation_targets(session, arguments)
    if name is None:
        return None  # unknown hash/id / ambiguous capability — handler refuses it
    manifest = (session.ir or {}).get("manifest") or {}
    realms = component_realms(manifest, name)
    return [(name or WHOLE, frozenset({name or WHOLE}) | frozenset(realms))]


def _distillation_targets(session, arguments: dict) \
        -> list[tuple[str, frozenset[str]]] | None:
    """The components an `apply_distillation` / `revoke_distillation` touches: the
    live members of the rule's component glob (item 251, gated by `approve`). An
    offer id resolves to its rule's glob; a bare `rule` string parses to its glob.
    Returns one `(name, labels)` per selected component (all-or-nothing gating), or
    None to defer when nothing resolves (the handler refuses it)."""
    from ..policy import component_realms  # noqa: PLC0415
    glob = None
    offer_id = arguments.get("offerId")
    if offer_id is not None and hasattr(session, "_offer_by_id"):
        offer = session._offer_by_id(offer_id)
        if offer is not None:
            glob = offer.rule.component
    if glob is None and arguments.get("rule") is not None:
        try:
            from ..policy import parse_policy  # noqa: PLC0415
            rules = parse_policy(arguments["rule"]).auto_approve_rules
            if rules:
                glob = rules[0].component
        except Exception:  # noqa: BLE001 - a non-DSL fragment defers
            glob = None
    if glob is None:
        return None
    members = sorted(session._glob_members(glob)) \
        if hasattr(session, "_glob_members") else []
    if not members:
        return None
    manifest = (session.ir or {}).get("manifest") or {}
    out = []
    for name in members:
        realms = component_realms(manifest, name)
        out.append((name, frozenset({name}) | frozenset(realms)))
    return out


def _refusal(operator: Operator, verb: str,
             offender: tuple[str, frozenset[str]], grant: Grant | None,
             ) -> tuple[WhyTrace, str]:
    name, labels = offender
    subject = name if name != WHOLE else "the whole composition"
    named = sorted(l for l in labels if l != WHOLE) or ["*"]
    if grant is not None:  # an explicit `may not`
        head = (f"operator `{operator.token}` is denied `{verb}` on `{subject}` "
                f"— a `may not {verb}` rule prohibits it")
    else:
        head = (f"operator `{operator.token}` may not `{verb}` `{subject}` "
                f"— no grant in its profile permits `{verb}` there")
    message = (head + f" (operator capabilities, item 55; subject labels "
               f"[{', '.join(named)}])")
    why = WhyTrace(
        kind="operator-authority", subject=operator.token, shape=CHAIN,
        steps=[
            TraceStep(operator.token, "operator", None, None,
                      f"attempts `{verb}`"),
            TraceStep(subject, "component", None, None,
                      f"target of `{verb}`", tuple(named)),
        ])
    return why, message


def decide(session, tool_name: str, arguments: dict) -> Decision:
    """Gate one MCP verb dispatch against the session's bound operator.

    Returns a :class:`Decision`. When no operator is bound (no profile) or the
    verb is read-only, ``gated`` is False and the call proceeds unchanged —
    today's behaviour. Otherwise the operator must be authorized for the verb
    on *every* component the action touches, or the whole action is refused
    with a policy-style why-trace (all-or-nothing, like admission).

    Exception: the **initial cold** ``revl_load`` is ungated (roadmap item
    300). A cold load boots a candidate into an empty session so it can be
    inspected and gauntleted; nothing is live, activated, or swapped by it, so
    it is not yet a privileged mutation and the authority gate does not belong
    on it. ``session.ir is None`` is exactly "nothing live yet": the field is
    None until :meth:`Session.load` sets it, and a subsequent ``revl_load``
    against a running composition is refused by the handler outright (`load`
    never replaces or activates a live composition). The gate is kept for that
    already-live case, and for every state-changing verb (swap/edit/unload/
    restore/undo)."""
    verb = TOOL_VERB.get(tool_name)
    if verb is None and tool_name in COMPOSED_TOOL_VERB:
        # a composed verb: gated as the operation it performs underneath, and
        # only in the mode that actually performs it
        if composed_applies(tool_name, arguments):
            verb = COMPOSED_TOOL_VERB[tool_name]
            arguments = swap_arguments(tool_name, arguments)
    operator = getattr(session, "operator", None)
    if verb is None or operator is None:
        return Decision(gated=False)

    if verb == "load" and session.ir is None:
        return Decision(gated=False)  # cold load: not yet a privileged mutation

    targets = _targets(verb, session, arguments)
    if targets is None:
        # UNDECIDABLE — fail closed. The gate cannot work out which components
        # this action touches, so it scopes the action to the unnameable whole
        # composition: only a literal `may <verb> on *` grant authorizes it, and
        # every subject-scoped operator is refused. Deferring here (returning
        # `Decision(gated=False)`) is what let a swap whose target derivation
        # failed run with no authority at all; "I cannot tell what this touches"
        # is a reason to refuse, never a reason to ungate. An operator who does
        # hold `*` proceeds and gets the handler's own diagnostic.
        targets = [(WHOLE, frozenset({WHOLE}))]

    for offender in targets:
        _name, labels = offender
        allowed, grant = operator.allows(verb, labels)
        if not allowed:
            why, message = _refusal(operator, verb, offender, grant)
            return Decision(gated=True, allowed=False, operator=operator.token,
                            verb=verb, why=why, message=message)

    subjects = tuple(sorted(name for name, _ in targets))
    return Decision(gated=True, allowed=True, operator=operator.token,
                    verb=verb, subjects=subjects)
