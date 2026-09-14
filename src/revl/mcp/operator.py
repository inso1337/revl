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
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .. import cap_order
from .. import intent as _intent
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
    # item 471 Slice 2: escalating a multi-party question CLOSES its vote path,
    # which only ever narrows authority (the remaining path is the separately
    # granted override), so it needs no authority beyond the one to answer the
    # question — `approve`, the same verb a vote is cast under. Its
    # `_approve_targets` branch resolves the ticket `hash` to the crossing
    # component, so a subject-scoped `may approve on payments` grant governs who
    # may hand a payments question up exactly as it governs who may vote on one.
    "revl_escalate": "approve",
    # item 471 Slice 2: the EMERGENCY OVERRIDE gets its own verb, deliberately
    # NOT folded into `approve`. It is the one path that admits a class-(c)
    # crossing WITHOUT the count its rule demands, and an operator trusted to cast
    # one of N votes is not thereby trusted to stand in for all of them — that is
    # the whole content of `require N of {...}`, and folding the override into
    # `approve` would hand every voter a one-operator bypass of the rule they vote
    # under. So a profile addresses the emergency path separately: `may approve on
    # payments` authorizes votes on payments and no override at all, and `may
    # override on payments` is what an on-call operator holds to break the glass.
    # Its target set is the crossing component (the `_approve_targets` branch),
    # not the whole composition: an override decides ONE question about one
    # candidate.
    "revl_override": "override",
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
    # item 476: `revl_deploy` reconfigures a running composition ACROSS MACHINES
    # (via = ssh). It reaches the same privileged operation as `revl deploy`'s
    # COMMIT path (`deploy.deploy_ssh_map` -> `run_deploy`), which hot-swaps the
    # composition on a second host — the swap authority, extended over a machine
    # boundary. It gets its OWN verb (`deploy`) rather than folding into `swap`
    # because the boundary it opens is the machine boundary, the authority
    # address an operator must be able to hold SEPARATELY from a same-host swap:
    # an operator trusted to hot-swap a component locally is not automatically
    # trusted to push a composition onto a second host and drive its teardown.
    # Its target set is the WHOLE running composition (`_targets`' fall-through
    # to `_live_targets`), because a deploy reconfigures the composition as one
    # coordinated unit across the boundary: a subject-scoped grant authorizes
    # only while EVERY live component is within it, so an operator who must be
    # able to deploy always needs `may deploy on *`. (docs/operator-capabilities.md,
    # issue #830.)
    "revl_deploy": "deploy",
    # Session-plane mutations are operator authority too.  The related tool
    # variants share one capability so profiles do not need implementation
    # details such as the two-step fork protocol.
    "revl_call": "call",
    "revl_lease": "lease",
    "revl_fork": "fork",
    "revl_fork_confirm": "fork",
    "revl_step_back": "replay",
    "revl_replay_forward": "replay",
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


# ------------------------------------------- what an operator may MINT
#
# `Grant` above is verb globs over subject globs: an AUTHORITY, not a declared
# intent. It answers "may this operator call `approve` on this component", and
# that is all it can answer, because there is nothing on it to compare a
# capability, a ceiling or a `uses` count against. So an operator holding
# `approve` could mint a standing grant (item 344) over ANY capability, with any
# ceiling and any number of uses, and nothing checked the mint against anything:
# the class-(c) gate refines a crossing against what the grant declared (item 470
# stage 1) while the grant itself was minted unchecked — bounded downstream,
# unbounded upstream.
#
# `MintBound` is the declaration the grant side lacked. It is deliberately NOT a
# `Grant` with extra fields: the two answer different questions and compose
# rather than overlap. `may approve on payments` still decides WHO may say yes
# and WHERE (the subject dimension, checked in `decide` before dispatch); a
# `may mint` line decides WHAT MAY BE MINTED there (the capability cone, its
# ceilings, the uses and the window). Neither subsumes the other and neither
# states the other's dimension twice.

#: The capability spelling that declares the whole capability surface in a
#: `may mint` line. It is the ONE unbounded object a profile can state, and it
#: has to be WRITTEN to hold: a declaration is never inferred from silence.
ANY_CAPABILITY = "*"

#: The spelling that declares one numeric axis explicitly unbounded
#: (`uses *`, `ttl *`). Same rule: unbounded is statable, never assumed.
UNBOUNDED = "*"

#: The verb both sides of a mint comparison state.
#:
#: A mint is compared DECLARATION-to-DECLARATION — the profile's `may mint` line
#: against the spelling the operator is minting — and neither side names an
#: operation performed at a boundary, so `refine`'s verb dimension is the
#: identity here exactly as it is at the class-(c) gate (`session._GATE_VERB`).
#: Guessing a verb would be inferring an intent nobody stated, which item 470's
#: scope note forbids.
MINT_VERB = "mint"

#: The ceiling parameter the `uses` axis already meters, dropped from both sides
#: of the capability comparison.
#:
#: `_mint_grant` folds a `calls=N` in the minted spelling into the shipped
#: `remainingUses` counter, so `calls` IS the uses axis. Comparing it in the
#: ceiling dimension as well would bound one quantity by two rules and let the
#: weaker one win — the hazard `Intent.__post_init__` refuses a ceiling parameter
#: on an object for. A `may mint` DECLARATION may not spell it at all (see
#: `_parse_mint`): the uses axis has one spelling, `uses N`.
_USES_METERED = frozenset({"calls"})

_MINT_CLAUSE = re.compile(r"\s+(uses|ttl)\s+(\S+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class MintBound:
    """One `may mint` line: the standing grants this operator may mint.

    Three axes, all three STATED, because each of them is one of the three the
    unbounded mint left free:

    * ``capability`` is the declared cone, compared by ``cap_order.covers``
      through ``intent.refine`` — the tree's one partial order over capability
      spellings, read here over a declaration instead of over an authority. It
      carries its own ceilings (``size``/``time``), which is the ceiling axis.
      ``*`` is the whole surface, and it is the only glob: relatedness between
      two capabilities is never inferred from a shared prefix (item 470's scope
      note), so a profile that means two cones writes two lines.
    * ``uses`` is the largest ``remainingUses`` a grant minted here may carry.
      ``None`` is the explicit ``uses *``.
    * ``ttl_ms`` is the longest window it may live for. ``None`` is ``ttl *``.

    Both numeric clauses are REQUIRED on the line. A declaration that could omit
    one would have an unstated axis, and an unstated axis is exactly what this
    record exists to remove: `uses *` is a deployment saying so, and silence is
    not. The same rule in the other direction is why a mint that states no bound
    on an axis the declaration bounds is refused rather than defaulted — an
    unbounded grant cannot be shown to be within a stated bound.
    """

    capability: str
    uses: int | None
    ttl_ms: int | None

    def to_dsl(self) -> str:
        """The canonical line, as a refusal renders it."""
        uses = UNBOUNDED if self.uses is None else str(self.uses)
        ttl = UNBOUNDED if self.ttl_ms is None else f"{self.ttl_ms}ms"
        return f"may mint {self.capability} uses {uses} ttl {ttl}"


@dataclass(frozen=True)
class Operator:
    """One operator identity: a token, the grants bound to it, and — when the
    profile declares one — the digest of the vote credential that binds a cast
    to this identity.

    ``vote_key`` is the SHA-256 hex digest of a secret the deployer issues to
    this operator out of band, never the secret itself: the profile file is a
    configuration artifact that gets read, copied and diffed, and a file that
    carried the secrets would hand every voter identity to anyone who can read
    it. The session verifies a presented credential by hashing it and comparing
    against this digest (:func:`revl.mcp.quorum.resolve_cast`).

    ``None`` means the profile declares no credential for this operator, which
    is not a default-open: a cast attributed to an operator with no declared
    credential cannot be proven and is refused (roadmap item 471 / issue #979).

    ``sign_key`` is the stronger form and the one a deployment should prefer: the
    raw ``X || Y`` hex of the operator's ECDSA P-256 PUBLIC key. The operator
    keeps the private half and never transmits it; a cast signs the question's
    own binding and presents the signature. The difference from ``vote_key`` is
    not cosmetic. A ``vote_key`` cast hands the session the secret itself, so
    everything that can observe one honest cast — the session process, the
    transport, a log, the proposer — can afterwards cast as that operator on
    every future question. A ``sign_key`` cast hands over a value that verifies
    exactly one question and nothing else.

    An operator declares one or the other, never both: two credential kinds on
    one identity would mean the weaker one is always available, so the stronger
    would bound nothing (a profile that declares both is a parse error).

    ``not_after`` (epoch ms) and ``revoked`` are the credential's LIFETIME. A
    credential with neither is a permanent grant, which is the honest reading of
    the profile and usually not what the deployer meant; both are checked at the
    cast, and an expired or revoked credential REFUSES rather than counts.
    """

    token: str
    grants: tuple[Grant, ...] = ()
    vote_key: str | None = None
    sign_key: str | None = None
    not_after: int | None = None
    revoked: bool = False
    # issue #1062 / item 470 stage 3: what this operator may MINT. Appended
    # last so every positional construction of an `Operator` keeps its meaning.
    mints: tuple[MintBound, ...] = ()

    def bounds_mints(self) -> bool:
        """Does this operator's profile DECLARE what it may mint?

        The migration hinge, and the one place the answer is read. An operator
        that declares no `may mint` line is UNCHANGED: it mints exactly what it
        minted before, because refusing every profile written before this
        grammar existed would refuse every deployment that has one, and a fix
        nobody can adopt bounds nothing. An operator that declares ONE line is
        closed over the whole mint surface from that line on — the set of `may
        mint` lines is the whole of what it may mint, and anything outside them
        refuses. So adopting the bound is per-operator and its first line is
        already load-bearing, rather than a flag that has to be flipped for the
        declaration to mean anything.
        """
        return bool(self.mints)

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


def _parse_vote_key(value: str, source: str | None, lineno: int) -> str:
    """One operator's vote-credential DIGEST, normalised to bare lowercase hex.

    Accepts `<64 hex>` or `sha256:<64 hex>`; the algorithm prefix is a migration
    slot, so a profile written today says which hash it meant. Anything else is a
    profile error rather than a credential nothing can match: a typo that parsed
    as an unmatchable digest would silently make the operator unable to vote, and
    an operator who cannot be proven is refused at the cast — a refusal is the
    right outcome for a missing credential and the wrong one for a malformed
    line the author could have been told about.
    """
    digest = value.strip()
    if digest.lower().startswith("sha256:"):
        digest = digest[len("sha256:"):].strip()
    digest = digest.lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ProfileError(
            source, lineno,
            f"a vote credential is the SHA-256 HEX DIGEST of the operator's "
            f"secret (64 hex characters, optionally `sha256:`-prefixed), never "
            f"the secret itself: {value.strip()!r}")
    return digest


#: The signature suite a `sign` line declares. One spelling, validated rather
#: than recorded, so a profile cannot name one curve and carry another's bytes.
#: It is a migration slot in the same sense `sha256:` is: a second suite would
#: be a second prefix, and a profile written today says which one it meant.
SIGN_ALG = "p256"


def _parse_sign_key(value: str, source: str | None, lineno: int) -> str:
    """One operator's cast-signing PUBLIC key, normalised to bare lowercase hex.

    Accepts ``p256:<128 hex>`` (raw ``X || Y``) and the SEC1 uncompressed
    spelling ``p256:04<128 hex>``, because that is what most key tooling prints.

    The point is checked to be ON THE CURVE here, at parse time, rather than at
    the cast. An off-curve key is the VERIFIER's own misconfiguration, not a
    peer's record, and a deployer who typo'd one should be told when the profile
    loads instead of discovering it as an unexplainable refusal in the middle of
    a quorum. Verifying against a point that is not on the curve is not
    verification, so there is no tolerant reading available."""
    raw = value.strip()
    low = raw.lower()
    if not low.startswith(SIGN_ALG + ":"):
        raise ProfileError(
            source, lineno,
            f"a cast-signing key names its suite: `sign {SIGN_ALG}:<hex>`, the "
            f"raw X||Y of the operator's ECDSA P-256 PUBLIC key (128 hex "
            f"characters, or 130 with the SEC1 `04` prefix). Got {raw!r}")
    body = low[len(SIGN_ALG) + 1:].strip()
    if len(body) == 130 and body.startswith("04"):
        body = body[2:]
    if len(body) != 128 or any(c not in "0123456789abcdef" for c in body):
        raise ProfileError(
            source, lineno,
            f"a P-256 cast-signing key is 128 hex characters of raw X||Y (the "
            f"PUBLIC key, never the private scalar): {raw!r}")
    # imported here, not at module import: `tee_quote` is pure-integer ECC that
    # nothing else in the management plane needs, and a profile with no `sign`
    # line should not pay for it.
    from ..tee_quote import CURVE_P256, QuoteFormatError, _decode_public_key
    try:
        _decode_public_key(CURVE_P256, bytes.fromhex(body))
    except QuoteFormatError as exc:
        raise ProfileError(
            source, lineno,
            f"the declared cast-signing key for this operator is not a usable "
            f"P-256 public key ({exc}). A signature checked against it would "
            f"verify nothing, so the profile is refused rather than loaded")
    return body


def _parse_not_after(value: str, source: str | None, lineno: int) -> int:
    """The `until <timestamp>` clause: when this credential stops binding casts.

    Accepts an ISO-8601 instant (`2026-01-01T00:00:00Z`) or bare epoch
    milliseconds. A naive timestamp — one with no offset — is REFUSED rather
    than read as local time: the session clock is epoch-anchored, and a
    credential whose expiry moves with the reader's timezone is a credential
    whose lifetime nobody can state."""
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    from datetime import datetime, timezone
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00")
                                        if raw.endswith("Z") else raw)
    except ValueError:
        raise ProfileError(
            source, lineno,
            f"`until` takes an ISO-8601 instant with an offset "
            f"(`2026-01-01T00:00:00Z`) or bare epoch milliseconds: {raw!r}")
    if moment.tzinfo is None:
        raise ProfileError(
            source, lineno,
            f"`until {raw}` carries no UTC offset, so the instant it names "
            f"depends on who reads it. Write it as `{raw}Z` or with an explicit "
            f"offset")
    return int(moment.astimezone(timezone.utc).timestamp() * 1000)


def _split_until(rest: str, source: str | None, lineno: int):
    """Peel an optional trailing `until <timestamp>` off a credential line."""
    lowered = rest.lower()
    marker = lowered.rfind(" until ")
    if marker < 0:
        return rest, None
    return (rest[:marker],
            _parse_not_after(rest[marker + len(" until "):], source, lineno))


def _parse_ttl_ms(value: str, source: str | None, lineno: int) -> int:
    """A `ttl` duration, in the ONE spelling the tree already reads.

    `policy._parse_ttl` is the grammar a `requires approval ttl` rule and a
    distilled rule are written in (`30s`, `10m`, `1h`, `500ms`, and a bare
    number as seconds), and the window a `may mint` line bounds is the same
    window those set. A second duration grammar in the same configuration file
    would be one an author has to remember which of two rules they are under.
    """
    from ..policy import _parse_ttl  # noqa: PLC0415 — one ttl grammar for the tree
    try:
        return _parse_ttl(value)
    except ValueError as exc:
        raise ProfileError(source, lineno, str(exc))


def _parse_mint_bound(value: str, kind: str, source: str | None,
                      lineno: int) -> int | None:
    """One `uses N` / `ttl D` clause, or `None` for the explicit `*`."""
    raw = value.strip()
    if raw == UNBOUNDED:
        return None
    if kind == "ttl":
        ms = _parse_ttl_ms(raw, source, lineno)
        if ms < 1:
            raise ProfileError(source, lineno,
                               f"`ttl {raw}` is not a window a grant can live "
                               f"in: write a positive duration, or `ttl *`")
        return ms
    if not raw.isdigit() or int(raw) < 1:
        raise ProfileError(
            source, lineno,
            f"`uses {raw}` is not a count of crossings: write a positive "
            f"integer (the largest `uses` a grant minted here may carry), or "
            f"`uses *` to declare the axis unbounded")
    return int(raw)


def _parse_mint(clause: str, source: str | None, lineno: int) -> MintBound:
    """One `may mint <capability> uses <N|*> ttl <D|*>` line.

    The two numeric clauses are peeled off the END (each at most once, in either
    order) because the capability spelling in front of them may itself contain
    spaces: `fs.write(path="/tmp", size="1MB")`. Both are REQUIRED. A line that
    could leave one out would declare an axis by silence, and silence is what
    this whole record exists to remove — `uses *` says it.
    """
    rest = clause.strip()
    seen: dict[str, int | None] = {}
    while True:
        match = _MINT_CLAUSE.search(rest)
        if match is None:
            break
        name = match.group(1).lower()
        if name in seen:
            raise ProfileError(
                source, lineno,
                f"`{name}` is stated twice on one `may mint` line: a bound is "
                f"one number, and the second spelling would silently win")
        seen[name] = _parse_mint_bound(match.group(2), name, source, lineno)
        rest = rest[:match.start()]
    missing = [name for name in ("uses", "ttl") if name not in seen]
    if missing:
        raise ProfileError(
            source, lineno,
            f"a `may mint` line states every axis it bounds: it is missing "
            f"`{'` and `'.join(missing)}`. Write "
            f"`may mint <capability> uses <N|*> ttl <D|*>` — an axis left out "
            f"would be a bound nobody stated, and an unstated bound is the "
            f"thing this line exists to replace (use `*` to declare one "
            f"explicitly unbounded)")
    spelling = rest.strip()
    if not spelling:
        raise ProfileError(source, lineno,
                           "a `may mint` line names no capability: write the "
                           "cone it may mint over, or `*` for every capability")
    if spelling != ANY_CAPABILITY:
        if ANY_CAPABILITY in spelling:
            raise ProfileError(
                source, lineno,
                f"`{spelling}` is a glob, and a `may mint` line names a POINT "
                f"in the capability order (`fs.write(path=\"/tmp\")`), which "
                f"is what a mint is compared against. A bare token already "
                f"tops its own cone, and two cones that are not one cone are "
                f"two lines: relatedness between capabilities is declared, "
                f"never inferred from a shared prefix")
        try:
            cap = cap_order.parse_cap(spelling)
        except cap_order.CapError as exc:
            raise ProfileError(
                source, lineno,
                f"`{spelling}` is not a capability spelling ({exc.args[0] if exc.args else exc}). "
                f"A `may mint` line names a point in the capability order "
                f"(`fs.write(path=\"/tmp\")`), which is what a mint is compared "
                f"against; it is not a glob")
        for name, _value in cap.params:
            if cap_order.is_ceiling(name) and name in _USES_METERED:
                raise ProfileError(
                    source, lineno,
                    f"`{name}` on a `may mint` capability would bound the same "
                    f"quantity the `uses` clause bounds, by two rules, and the "
                    f"weaker one would win: a mint folds `{name}=N` into the "
                    f"grant's `remainingUses`. State it once, as `uses N`")
        spelling = cap.to_str()
    return MintBound(spelling, seen["uses"], seen["ttl"])


def _parse_dsl(text: str, source: str | None) -> OperatorRegistry:
    """The line DSL (blank lines and ``#`` comments ignored). Grammar:

        operator <token> may     <verb>[, ...] on <subject>[, ...]
        operator <token> may not <verb>[, ...] on <subject>[, ...]
        operator <token> may     <verb>[, ...]                       # on *
        operator <token> may mint <cap> uses <N|*> ttl <D|*>         # issue #1062
        operator <token> key     sha256:<hex> [until <ts>]           # item 471
        operator <token> sign    p256:<hex>   [until <ts>]           # issue #979
        operator <token> revoked                                     # issue #979

    A verb of ``*`` matches every management verb; a subject of ``*`` matches
    every component and realm. ``on`` may be omitted to mean ``on *``.

    The ``key`` line declares the DIGEST of this operator's BEARER vote
    credential and the ``sign`` line the PUBLIC half of its cast-signing key, so
    a multi-party cast attributed to the operator can be proven rather than
    asserted (issue #979). They are orthogonal to the grants: an operator may
    have grants and no credential (it can act on this session's own authority but
    cannot be named by a cast from elsewhere), a credential and no grants (it can
    be proven as a voter without holding any management verb), or both.

    An operator declares ``key`` or ``sign``, never both. Both would leave the
    bearer path permanently open beside the signed one, so the signed one would
    bound nothing: an attacker who holds the secret does not care that a stronger
    credential also exists.

    ``until`` bounds the credential's lifetime and ``revoked`` ends it now. Both
    are checked at the cast, and both refuse.

    ``may mint`` is the declaration the GRANT side of a standing approval had
    none of (issue #1062). It is orthogonal to the verb grants in the same way
    the credential lines are: ``may approve on payments`` decides who may say
    yes and where, and ``may mint`` decides what may be minted when they do.
    There is deliberately no ``may not mint``: the set of ``may mint`` lines IS
    the whole of what an operator may mint, so a narrower bound is written by
    narrowing the line rather than by a second rule that could disagree with it.
    """
    operators: dict[str, list[Grant]] = {}
    mints: dict[str, list[MintBound]] = {}
    keys: dict[str, str] = {}
    sign_keys: dict[str, str] = {}
    not_after: dict[str, int] = {}
    revoked: set[str] = set()

    def _claim_credential(token: str, lineno: int) -> None:
        if token in keys or token in sign_keys:
            raise ProfileError(
                source, lineno,
                f"operator `{token}` already declares a vote credential: two "
                f"credentials for one identity would make it ambiguous which "
                f"principal a cast proved, and the weaker of the two would be "
                f"the one that actually bounds it")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 2)
        # `operator <token> revoked` is the one three-word form with no clause
        # after it, so the arity check is "at least a token and something to say
        # about it" and the emptiness is caught below with the same message.
        if len(parts) < 3 or parts[0].lower() != "operator":
            raise ProfileError(source, lineno,
                               f"expected `operator <token> may ...`: {raw.strip()!r}")
        token = parts[1]
        rest = parts[2]
        low = rest.lower()
        if low == "revoked":
            revoked.add(token)
            operators.setdefault(token, [])
            continue
        if low.startswith("may not mint"):
            raise ProfileError(
                source, lineno,
                f"there is no `may not mint` rule: the `may mint` lines an "
                f"operator declares are the WHOLE of what it may mint, and "
                f"anything outside them is already refused. Narrow the `may "
                f"mint` line instead of adding a rule that could disagree with "
                f"it: {raw.strip()!r}")
        if low.startswith("may mint "):
            mints.setdefault(token, []).append(
                _parse_mint(rest[len("may mint "):], source, lineno))
            operators.setdefault(token, [])
            continue
        if low.startswith("key ") or low.startswith("sign "):
            _claim_credential(token, lineno)
            kind, _, body = rest.partition(" ")
            body, until = _split_until(body, source, lineno)
            if kind.lower() == "key":
                keys[token] = _parse_vote_key(body, source, lineno)
            else:
                sign_keys[token] = _parse_sign_key(body, source, lineno)
            if until is not None:
                not_after[token] = until
            operators.setdefault(token, [])
            continue
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
        {t: Operator(t, tuple(g), keys.get(t), sign_keys.get(t),
                     not_after.get(t), t in revoked,
                     tuple(mints.get(t, ())))
         for t, g in operators.items()},
        source)


def _parse_json_mint(token: str, entry, source: str | None) -> MintBound:
    """One `mints` entry, read through the SAME line the DSL is read through.

    The two parsers state one grammar or they are two grammars: a JSON profile
    that admitted a bound the DSL refuses would be the shape an author reaches
    for when the DSL says no. So the entry is rendered back into a `may mint`
    line and parsed by `_parse_mint`, which makes every refusal above reachable
    from here with the same wording.
    """
    if not isinstance(entry, dict):
        raise ProfileError(source, 1,
                           f"a `mints` entry for `{token}` must be an object "
                           f"naming a `capability`, `uses` and a window")
    capability = entry.get("capability") or entry.get("cap")
    if not capability:
        raise ProfileError(source, 1,
                           f"a `mints` entry for `{token}` names no "
                           f"`capability` (use \"*\" for every capability)")
    if "uses" not in entry:
        raise ProfileError(
            source, 1,
            f"a `mints` entry for `{token}` states no `uses`: write a positive "
            f"integer, or \"*\" to declare the axis unbounded. An axis left out "
            f"would be a bound nobody stated")
    if "ttlMs" not in entry and "ttl" not in entry:
        raise ProfileError(
            source, 1,
            f"a `mints` entry for `{token}` states no window: write `ttlMs` "
            f"(milliseconds) or `ttl` (a duration), either of them \"*\". An "
            f"axis left out would be a bound nobody stated")
    uses = entry["uses"]
    if "ttlMs" in entry:
        ttl = entry["ttlMs"]
        ttl = ttl if ttl == UNBOUNDED else f"{ttl}ms"
    else:
        ttl = entry["ttl"]
    return _parse_mint(f"{capability} uses {uses} ttl {ttl}", source, 1)


def _parse_json(text: str, source: str | None) -> OperatorRegistry:
    """The JSON equivalent — same model, machine-authored.

    { "operators": [
        { "token": "alice",
          "sign": "p256:<128 hex>",
          "notAfter": "2026-01-01T00:00:00Z",
          "grants": [ {"verbs": ["swap"], "on": ["tenant_a*"]},
                      {"verbs": ["unload"], "on": ["*"], "deny": true} ],
          "mints": [ {"capability": "fs.write(path=\"/tmp\")",
                      "uses": 10, "ttl": "30m"} ] } ] }

    ``key`` (or ``voteKey``) is the digest of the operator's bearer vote
    credential and ``sign`` (or ``signKey``) the public half of its cast-signing
    key, exactly as in the DSL's ``key`` and ``sign`` lines — one or the other,
    never both. ``notAfter`` and ``revoked`` are the DSL's ``until`` clause and
    ``revoked`` line.

    ``mints`` is the DSL's ``may mint`` lines (issue #1062): one entry per
    declared cone, each stating both numeric axes. ``uses`` is a positive
    integer or ``"*"``; the window is ``ttlMs`` (milliseconds) or ``ttl`` (the
    DSL's duration spelling), either of them ``"*"``. Both axes are REQUIRED on
    an entry for the reason the DSL requires both clauses: a key left out would
    declare a bound by silence, and this record exists to remove exactly that.
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
        raw_key = entry.get("key") or entry.get("voteKey")
        raw_sign = entry.get("sign") or entry.get("signKey")
        if raw_key and raw_sign:
            raise ProfileError(
                source, 1,
                f"operator `{token}` declares both a bearer vote credential and "
                f"a cast-signing key: two credentials for one identity would "
                f"leave the weaker one permanently open beside the stronger, so "
                f"the stronger would bound nothing. Declare one")
        raw_not_after = entry.get("notAfter") or entry.get("not_after")
        mints = tuple(_parse_json_mint(token, m, source)
                      for m in entry.get("mints") or ())
        operators[token] = Operator(
            token, tuple(grants),
            _parse_vote_key(raw_key, source, 1) if raw_key else None,
            _parse_sign_key(raw_sign, source, 1) if raw_sign else None,
            (_parse_not_after(str(raw_not_after), source, 1)
             if raw_not_after is not None else None),
            bool(entry.get("revoked")), mints)
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


def _cold_load_activation_emits(arguments: dict) -> bool:
    """Whether a candidate's ACTIVATION body reaches a class-(c) crossing.

    The cold-load exemption in :func:`decide` rests on "nothing is live,
    activated, or swapped by it". That is true of a candidate whose body only
    wires provisions, and false of one whose component-scope body EMITS: a cold
    load boots the candidate, activation runs, and an ``emit`` there is a
    one-way boundary crossing with no inverse (item 246's class (c)). Reaching
    class (c) at activation is therefore exactly what makes a cold load a
    privileged mutation, and it is the case the exemption's justification does
    not cover — so this is the predicate that narrows the exemption to its
    reason.

    Derived from the same :class:`~revl.mcp.approval.ClassMap` the activation
    gate reads, built directly from the candidate IR. The class map needs no
    approval policy here and the check must not depend on one: the operator
    gate is item 55, an independent opt-in.

    Undecidable reads as ``True``. A candidate that does not compile cannot
    boot and cannot emit, but the gate must never widen on an answer it could
    not derive. The compile is the same UNPROFILED derivation ``_targets``
    performs for this verb, so the reach checked here is the reach that runs.
    """
    ir = _compile_candidate(arguments)
    if ir is None:
        return True  # undecidable — do not widen the exemption
    from .approval import ClassMap  # noqa: PLC0415 — lazy, no cordis
    try:
        return any(reach.get("class") == "c"
                   for reach in ClassMap(ir).activation_reaches())
    except Exception:  # noqa: BLE001 - a failed derivation must not widen
        return True


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
    if verb in {"approve", "override"}:
        # item 471: the `override` verb scopes to the crossing component exactly
        # as `approve` does, through the same ticket-hash resolution — an override
        # decides ONE question, so it must not widen to the whole composition.
        return _approve_targets(session, arguments)
    if verb == "lease":
        component = arguments.get("component")
        if not component or not ir:
            return _live_targets(ir)
        return [target for target in _live_targets(ir) or []
                if target[0] == component] or None
    if verb in {"fork", "replay"}:
        component = arguments.get("component")
        if component and ir:
            return [target for target in _live_targets(ir) or []
                    if target[0] == component] or None
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


# ------------------------------------------------- the mint decision (#1062)


def _mint_records(spelling: str):
    """One capability spelling read as BOTH sides of a mint comparison.

    A mint is compared declaration-to-declaration, so the profile's line and the
    operator's mint spelling go through the identical reading: `split_ceilings`
    peels the ceiling parameters off the resource valuation, the uses-metered
    ceiling is dropped (`_USES_METERED`), and what is left is the object and the
    ceiling dimension. Reading the two sides by two routes is how a declaration
    and the thing compared against it drift apart, which is the whole reason
    `Intent.from_cap` / `Action.from_cap` exist.

    Returns `(Intent, Action)`, or `(None, None)` when the spelling is not a
    point in the capability order at all (the caller then falls back to byte
    equality, never to a match).
    """
    try:
        cap = cap_order.parse_cap(spelling)
    except cap_order.CapError:
        return None, None
    obj, ceilings = cap_order.split_ceilings(cap)
    bounds = _intent.ceiling_params(
        {name: value for name, value in ceilings.items()
         if name not in _USES_METERED})
    return (_intent.Intent(obj, frozenset({MINT_VERB}), bounds, None),
            _intent.Action(obj, MINT_VERB, bounds, None))


def _mint_bound_refusal(bound: MintBound, capability: str,
                        uses: int | None, ttl_ms: int | None) -> str | None:
    """Why this one `may mint` line does not admit this mint, or None when it
    does. Checked from the narrowest authority outward, exactly as `refine`
    orders its own dimensions: the capability cone and its ceilings first, then
    the two numeric axes."""
    if bound.capability != ANY_CAPABILITY:
        declared, _ = _mint_records(bound.capability)
        _, requested = _mint_records(capability)
        if declared is None or requested is None:
            if bound.capability != capability:
                return (f"the mint names {capability!r} and the declaration "
                        f"names {bound.capability!r}, and the two cannot both "
                        f"be read as points in the capability order, so "
                        f"neither covers the other")
        else:
            refusal = _intent.refine(declared, requested)
            if refusal is not None:
                return refusal.message

    # The two numeric axes. Their asymmetry is the kernel's: a mint that states
    # NO bound on an axis the declaration bounds is an unbounded grant on that
    # axis, and an unbounded grant can never be shown to be within a stated
    # bound. It refuses rather than defaulting to the declared number, because
    # defaulting would silently mint something the operator did not ask for.
    if bound.uses is not None:
        if uses is None:
            return (f"the mint bounds no number of uses (it is bounded only by "
                    f"its window), and the declaration permits at most "
                    f"`uses {bound.uses}`")
        if uses > bound.uses:
            return (f"the mint carries `uses {uses}`, and the declaration "
                    f"permits at most `uses {bound.uses}`")
    if bound.ttl_ms is not None:
        if ttl_ms is None:
            return (f"the mint bounds no window (it is bounded only by its "
                    f"uses and the session), and the declaration permits at "
                    f"most `ttl {bound.ttl_ms}ms`")
        if ttl_ms > bound.ttl_ms:
            return (f"the mint carries `ttl {ttl_ms}ms`, and the declaration "
                    f"permits at most `ttl {bound.ttl_ms}ms`")
    return None


def mint_refusal(operator: Operator | None, *, capability: str,
                 uses: int | None, ttl_ms: int | None) -> str | None:
    """WHY this operator may not mint this standing grant, or None when it may.

    The grant side of a standing approval as a DECLARATION (issue #1062, item
    470 stage 3). The class-(c) gate already refines a crossing against what a
    grant declared; this is the same reading one step upstream, where the mint
    itself is refined against what the operator's profile declared it may mint.
    Both numbers on this side are the grant's own bounds rather than a spend, so
    the comparison is declaration-to-declaration and the verb dimension is the
    identity (`MINT_VERB`).

    Three answers, and only the middle one is new:

    * NO PROFILE (`operator is None`) — not gated, exactly as every other verb
      in this module is ungated without a profile. Item 55 is opt-in.
    * a profile that declares NO `may mint` line — unchanged, and this is the
      migration hinge (`Operator.bounds_mints`). Every profile written before
      this grammar existed keeps minting what it minted, which is what makes
      the change adoptable; what it leaves open is stated in
      `docs/operator-capabilities.md` rather than hidden.
    * a profile that declares ONE OR MORE — closed over the mint surface. The
      mint must refine SOME declared line, and it is refused otherwise. Several
      lines are alternatives rather than a conjunction, because a profile that
      means two cones has to write two lines (relatedness between capabilities
      is declared, never inferred), and requiring a mint to satisfy every line
      at once would make the second line refuse everything the first admits.

    The refusal names the declaration it violated, in `errors.RevlError`'s
    message-plus-hint shape, because item 470's criterion is a refusal that
    names the intent and a bare boolean carries none.
    """
    if operator is None or not operator.bounds_mints():
        return None
    findings = [(bound, _mint_bound_refusal(bound, capability, uses, ttl_ms))
                for bound in operator.mints]
    if any(finding is None for _bound, finding in findings):
        return None
    lines = "\n".join(
        f"    `{bound.to_dsl()}`: {finding}" for bound, finding in findings)
    plural = "" if len(findings) == 1 else "s"
    return (f"operator `{operator.token}` may not mint a standing grant over "
            f"`{capability}`: its profile declares what it may mint, and no "
            f"declaration admits this one.\n"
            f"  against {len(findings)} `may mint` declaration{plural}:\n"
            f"{lines}\n"
            f"  a standing grant is minted against what the profile DECLARES, "
            f"not against the `approve` verb alone (issue #1062, roadmap item "
            f"470 stage 3). Widen the `may mint` line, or mint within it")


def decide(session, tool_name: str, arguments: dict) -> Decision:
    """Gate one MCP verb dispatch against the session's bound operator.

    Returns a :class:`Decision`. When no operator is bound (no profile) or the
    verb is read-only, ``gated`` is False and the call proceeds unchanged —
    today's behaviour. Otherwise the operator must be authorized for the verb
    on *every* component the action touches, or the whole action is refused
    with a policy-style why-trace (all-or-nothing, like admission).

    Exception: the **initial cold** ``revl_load`` is ungated (roadmap item
    300), but only for as long as that item's justification holds. A cold load
    boots a candidate into an empty session so it can be inspected and
    gauntleted, and a candidate whose body only wires provisions is not yet a
    privileged mutation. A candidate whose ACTIVATION body EMITS is a different
    thing: the load runs it, and a component-scope ``emit`` is a one-way
    boundary crossing with no inverse. That IS a privileged mutation, and it is
    the case the exemption was never argued to cover, so the exemption is
    narrowed to candidates whose activation reaches no class-(c) crossing
    (:func:`_cold_load_activation_emits`). An emitting candidate falls through
    to the ordinary gate below, where ``may load on ...`` authorizes it and
    ``may not load on *`` refuses it.

    ``session.ir is None`` is exactly "nothing live yet": the field is None
    until :meth:`Session.load` sets it, and a subsequent ``revl_load`` against a
    running composition is refused by the handler outright (`load` never
    replaces or activates a live composition). The gate is kept for that
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

    # Cold load (roadmap item 300): ungated ONLY while it is not yet a
    # privileged mutation. A candidate whose activation body reaches a
    # class-(c) crossing performs an irreversible emission when this load boots
    # it, so it is a privileged mutation and answers to the same gate as every
    # other one — a grant-less operator is refused, and an explicit
    # `may not load on *` is honoured rather than silently ignored.
    if verb == "load" and session.ir is None \
            and not _cold_load_activation_emits(arguments):
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
