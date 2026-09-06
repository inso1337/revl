"""Boundary policy — the third leg of the gate (roadmap item 33).

Everything on the G8 surface (`revl audit`) answers *what does this reach?*
Nothing there lets the composition operator state *what may anything here
reach.* This module adds that: a policy file, evaluated against the audit
graph at admission, expressing **absolute authority** over the boundary.

The triad it completes:

    admission (DESIGN §5)   correctness — running consumers stay valid
    audit --diff (item 21)  drift       — a regenerated component didn't widen
    policy (this module)    authority   — nothing reaches what it may not

A policy is a set of allow/deny rules over the *capabilities* a component
reaches — the emission scopes (docs/capabilities.md) and host externs the G8
audit already enumerates per component. Evaluation is pure set operations
over that graph: a component's *reach* is the union of its emission
capabilities and reached externs; a rule constrains that set; a violation
**refuses admission** carrying a why-trace (`revl.why`) that names the
offending chain — which component reaches what it may not, and how.

Three rule families, one file:

  * per **component pattern** — ``component Agent* may reach llm, kv*`` binds
    an allow-list to every component whose name matches the glob; a reach
    outside the union of a component's allow-lists is refused. ``may not
    reach`` is a deny-list that refuses regardless of any allow.
  * per **realm** — ``realm billing may reach db`` binds the same to every
    component isolated into that realm; plus the built-in ``tenants never
    reach each other``, which refuses when two components in *different*
    realms reach a common named boundary (their isolation is not real).
  * the **MCP / agent sandbox** — ``mcp may reach llm, kv*`` is the sandbox
    profile for agent-generated code admitted through the MCP session
    (`revl.mcp.session`): "agent output may reach [llm, kv*] and nothing
    else", enforced as a machine-checked invariant instead of a review
    convention.

The file is a small line DSL (below) or the equivalent JSON; either parses to
the same `Policy`. Patterns are globs (`fnmatch`) over capability tokens; a
reach of ``*`` — an unbounded ``emission`` or a first-class dispatch, a
boundary with no name — never satisfies a closed allow-list unless the
allow-list literally contains ``*``, because an unnameable reach can never be
proven in-bounds.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase

from . import cap_order
from .errors import RevlError
from .why import CHAIN, SET, TraceStep, WhyTrace


class InertTaintPolicyWarning(UserWarning):
    """A taint policy rule (`<origin>-taint may not reach ...` /
    `... may not declassify <origin>`) was evaluated against an audit that
    carries NO taint surface at all (item 249, Finding 3).

    The taint walk mints an origin only when the program is annotated
    (`Untrusted[T]`) or compiled with derived sources on (`--taint-strict` / the
    untrusted-author profile). Over an unannotated, no-profile program the walk is
    inert, so a taint rule silently matches nothing — an operator must not mistake
    an inert rule for a protecting one. This warns loudly rather than passing clean."""

# a reach of `*` is an unbounded emission or a first-class dispatch — a
# boundary the analysis cannot name (docs/capabilities.md). It is the token no
# allow-list can bound implicitly, exactly as G4 treats it.
UNBOUNDED = "*"


# --------------------------------------------------------------------- model

@dataclass(frozen=True)
class Rule:
    """One allow-or-deny rule, bound to a component glob or a realm.

    Exactly one of `component`/`realm` selects the subjects; `allow` is True
    for ``may reach`` (a closed allow-list contribution), False for ``may not
    reach`` (a deny-list contribution). `patterns` are globs over capability
    tokens.
    """
    scope: str                       # "component" | "realm"
    selector: str                    # the glob (component) or realm name
    allow: bool                      # True = allow-list, False = deny-list
    patterns: tuple[str, ...]

    def selects(self, name: str, realms: frozenset[str]) -> bool:
        if self.scope == "component":
            return fnmatchcase(name, self.selector)
        return self.selector in realms


# the units a `ttl` duration may name — parsed to milliseconds, the unit the
# ledger's `expiresAt` clock runs in (docs/design/246-auto-approve.md, invariant
# 3: expiry is checked at the crossing, so the token carries an absolute deadline
# minted from this).
_TTL_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}


def _parse_ttl(text: str) -> int:
    """Parse a `ttl` duration (`10m`, `30s`, `1h`, `500ms`) to milliseconds. A
    bare integer is seconds, matching the human reading of `ttl 30`."""
    raw = text.strip().lower()
    for unit in ("ms", "s", "m", "h"):   # ms before s so `500ms` wins over `s`
        if raw.endswith(unit) and raw[: -len(unit)].strip().isdigit():
            return int(raw[: -len(unit)].strip()) * _TTL_UNITS[unit]
    if raw.isdigit():
        return int(raw) * 1000
    raise ValueError(
        f"malformed ttl {text!r} — expected `<n>[ms|s|m|h]` (a bare number is "
        f"seconds)")


@dataclass(frozen=True)
class ApprovalRule:
    """A ``capability C requires approval [ttl D]`` rule (item 246, Slice 2).

    The operator-owned floor: a capability whose crossing needs a human `yes`,
    minted as a typed `Approval[C]` (the language surface) or a per-call ticket
    (the operator layer). Author code cannot waive it by omission — the rule
    lives in the boundary policy the operator writes, not the source. `pattern`
    is a glob over capability tokens (the same tokens `may reach` constrains);
    `ttl_ms` is the approval's lifetime, defaulted at the crossing when None.
    """
    pattern: str
    ttl_ms: int | None = None

    def covers(self, token: str) -> bool:
        return fnmatchcase(token, self.pattern)


@dataclass(frozen=True)
class TaintFlowRule:
    """item 249, Slice D (D2): ``<origin>-taint may not reach <cap>[, ...]
    [without approval]`` — the operator's power over a legitimate-but-dangerous
    taint flow (the exfiltration edge of the lethal trifecta). Read over the
    `taint:<component>:<origin>` audit tokens: a component that carries `origin`
    taint to an emission AND reaches a capability matching `patterns` is refused,
    unless `without_approval` and the reached flow is approval-covered (the
    item-246 surface, declassifier three)."""
    origin: str
    patterns: tuple[str, ...]
    without_approval: bool = False


# --------------------------------------------- item 251: approval distillation
#
# The five taint-fold origins an `AutoApproveRule` may `admit` - exactly the set
# the item-249 taint fold enforces (`taint._SOURCE_CLASS_SCOPES`, the set
# `test_taint_fold_visits_every_in_scope_kind` asserts). `secret` is NOT one of
# them: `taint._ORIGIN_CLASSES` lists it, but it is enforced by the separate
# G-SECRET mechanism (item 256) as a REFUSAL at the crossing, never a recorded
# origin, so it can never appear in a `taintOrigins` set and is never
# `admit`-able by any rule (design §2.1, §3.2). Kept here rather than imported
# from `taint` to avoid pulling the type-checker into the policy parser; the
# 414 taint row pins the two to the same five.
TAINT_FOLD_ORIGINS: frozenset[str] = frozenset(
    {"web", "net", "fs", "model", "input"})


@dataclass(frozen=True)
class AutoApproveRule:
    """A distilled standing-auto-approve rule (roadmap item 251, Slice 1).

    The typed policy diff item 251 emits: a rule an operator could have written
    by hand, scoped over exactly the tuple an item-33 rule scopes over - a
    component glob, an optional realm, one or more item-294 capability spellings
    (each carrying its resource scope, `gateway.send(host="api.stripe.com")`),
    and the taint origins it `admit`s. Slice 1 is PARSE-ONLY: the rule round-trips
    (`to_dsl`/`to_json` re-parse equal) and is added to `Policy`, but no
    evaluation is wired (that is Slice 2's runtime consume path).

    DSL:

        component <glob> may auto-approve <cap>[, <cap> ...]
            [in realm <name>]
            [admitting <origin>-taint[, <origin>-taint ...]]
            [ttl <D>] [uses <N>]

    `caps` are canonical `cap_order` spellings (parsed and re-rendered through
    `Cap`, so two spellings of one cone compare equal). `admitting` ranges over
    the five taint-fold origins only; `admitting secret-taint` is REFUSED at
    parse - `secret` is structurally never admit-able. Every origin not named is
    excluded (the negative guarantee, design §3.2). `ttl_ms`/`uses` bound the
    rule's lifetime exactly as a 344 grant's do."""
    component: str                          # glob over component names
    caps: tuple[str, ...]                   # canonical capability spellings
    realm: str | None = None                # the item-33 `in realm <name>` scope
    admitting: frozenset[str] = frozenset()  # admitted taint origins (of the five)
    ttl_ms: int | None = None
    uses: int | None = None

    def negative_guarantee(self) -> frozenset[str]:
        """The taint origins this rule can NEVER admit - the complement of
        `admitting` over the five taint-fold origins (design §3.2). `secret` is
        not in this set: it is refused by G-SECRET at the crossing, an absolute
        the render sources separately, not one of five symmetric origins."""
        return TAINT_FOLD_ORIGINS - self.admitting

    def to_dsl(self) -> str:
        """Render the rule back to its canonical DSL line (round-trips through
        `parse_policy`). `ttl` is emitted in milliseconds (`<n>ms`), the one
        form `_parse_ttl` reads back without unit ambiguity."""
        out = [f"component {self.component} may auto-approve {', '.join(self.caps)}"]
        if self.realm is not None:
            out.append(f"in realm {self.realm}")
        if self.admitting:
            origins = ", ".join(f"{o}-taint" for o in sorted(self.admitting))
            out.append(f"admitting {origins}")
        if self.ttl_ms is not None:
            out.append(f"ttl {self.ttl_ms}ms")
        if self.uses is not None:
            out.append(f"uses {self.uses}")
        return " ".join(out)

    def to_json(self) -> dict:
        """Render the rule to its JSON entry (round-trips through
        `parse_policy`). Origins are the bare names, not the `-taint` spelling."""
        entry: dict = {"component": self.component, "capabilities": list(self.caps)}
        if self.realm is not None:
            entry["realm"] = self.realm
        if self.admitting:
            entry["admitting"] = sorted(self.admitting)
        if self.ttl_ms is not None:
            entry["ttl"] = f"{self.ttl_ms}ms"
        if self.uses is not None:
            entry["uses"] = self.uses
        return entry


def _canon_cap_spelling(text: str, source, lineno) -> str:
    """Parse one capability spelling through `cap_order` and return its canonical
    string, so a hand-written `gateway.send(host="api.stripe.com")` and the
    distiller's projection of the same cone render byte-identically and compare
    equal. A malformed spelling is a `PolicyError`, not a silent pass-through."""
    try:
        return cap_order.parse_cap(text.strip()).to_str()
    except cap_order.CapError as exc:
        raise PolicyError(source, lineno,
                          f"malformed auto-approve capability {text.strip()!r}: "
                          f"{exc}") from exc


def _parse_admitting(origins: tuple[str, ...], source, lineno) -> frozenset[str]:
    """Parse an `admitting <origin>-taint, ...` list to a validated origin set.

    Each entry is `<origin>-taint`; `<origin>` must be one of the five taint-fold
    origins. `admitting secret-taint` is REFUSED: `secret` is enforced by G-SECRET
    at the crossing and is structurally never admit-able (design §2.1, §3.2)."""
    out: set[str] = set()
    for raw in origins:
        token = raw.strip()
        low = token.lower()
        # the DSL spells `<origin>-taint`; JSON carries the bare origin name.
        # Accept both, so the two forms round-trip to the same rule.
        origin = low[:-len("-taint")] if low.endswith("-taint") else low
        if origin == "secret":
            raise PolicyError(
                source, lineno,
                "`admitting secret-taint` is refused - a bound-secret crossing is "
                "refused by G-SECRET (item 256) at the crossing regardless of any "
                "rule, so no auto-approve rule can ever admit `secret`; it is not "
                "one of the five admit-able taint-fold origins")
        if origin not in TAINT_FOLD_ORIGINS:
            allowed = ", ".join(f"{o}-taint" for o in sorted(TAINT_FOLD_ORIGINS))
            raise PolicyError(
                source, lineno,
                f"unknown admitting origin {token!r} - an `admitting` clause names "
                f"the taint-fold origins {allowed}")
        out.add(origin)
    return frozenset(out)


def _split_top_level(text: str) -> list[str]:
    """Split a capability list on the top-level commas - the commas NOT inside a
    capability's own `(...)` parameter list or a quoted value. A naive comma
    split would tear `gateway.send(host="a",path="/x")` apart."""
    parts: list[str] = []
    depth = 0
    in_str = False
    current: list[str] = []
    for ch in text:
        if ch == '"':
            in_str = not in_str
            current.append(ch)
        elif ch == "(" and not in_str:
            depth += 1
            current.append(ch)
        elif ch == ")" and not in_str:
            depth -= 1
            current.append(ch)
        elif ch == "," and not in_str and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


# the clause keywords that terminate the capability list of an `auto-approve`
# line, in the order the grammar writes them. `in realm` is a two-word marker.
_AUTO_APPROVE_CLAUSES = ("in realm", "admitting", "ttl", "uses")


def _parse_auto_approve(head: str, tail: str, source, lineno) -> AutoApproveRule:
    """Parse one ``component <glob> may auto-approve ...`` line (item 251).

    `head` is the text before ` may auto-approve`; `tail` is the caps list and
    its optional `in realm` / `admitting` / `ttl` / `uses` clauses. The clauses
    are located by their keyword markers so the caps list (which may carry
    resource params with their own commas) is not mis-split."""
    parts = head.split()
    if len(parts) != 2 or parts[0].lower() != "component":
        raise PolicyError(
            source, lineno,
            f"an auto-approve rule names one component glob: `component <glob> "
            f"may auto-approve <cap>[, ...] [in realm <r>] [admitting "
            f"<o>-taint,...] [ttl <D>] [uses <N>]`, got {head.strip()!r}")
    component = parts[1]

    # carve the tail into the caps segment and each named clause, scanning the
    # lowercased text for the earliest remaining clause marker.
    segments: dict[str, str] = {}
    rest = tail.strip()
    order: list[str] = ["caps"]
    # find clause positions
    low = rest.lower()
    marks: list[tuple[int, str, str]] = []  # (index, clause-key, literal)
    for clause in _AUTO_APPROVE_CLAUSES:
        idx = 0
        while True:
            found = low.find(clause, idx)
            if found < 0:
                break
            # a marker is a whole word (bounded by start/space on the left, space
            # on the right), so a `ttl`/`uses` substring inside a value is ignored.
            left_ok = found == 0 or low[found - 1].isspace()
            right = found + len(clause)
            right_ok = right >= len(low) or low[right].isspace()
            if left_ok and right_ok:
                marks.append((found, clause, rest[found:found + len(clause)]))
                break
            idx = found + len(clause)
    marks.sort()
    caps_end = marks[0][0] if marks else len(rest)
    caps_text = rest[:caps_end].strip()
    for i, (start, clause, _literal) in enumerate(marks):
        body_start = start + len(clause)
        body_end = marks[i + 1][0] if i + 1 < len(marks) else len(rest)
        segments[clause] = rest[body_start:body_end].strip()
        order.append(clause)

    caps_raw = _split_top_level(caps_text)
    if not caps_raw:
        raise PolicyError(source, lineno,
                          f"an auto-approve rule names at least one capability: "
                          f"{head.strip()!r} may auto-approve <cap>[, ...]")
    caps = tuple(_canon_cap_spelling(c, source, lineno) for c in caps_raw)

    realm: str | None = None
    if "in realm" in segments:
        realm_body = segments["in realm"].split()
        if len(realm_body) != 1:
            raise PolicyError(source, lineno,
                              f"`in realm` names exactly one realm, got "
                              f"{segments['in realm']!r}")
        realm = realm_body[0]

    admitting: frozenset[str] = frozenset()
    if "admitting" in segments:
        admitting = _parse_admitting(
            tuple(_split_top_level(segments["admitting"])), source, lineno)

    ttl_ms: int | None = None
    if "ttl" in segments:
        ttl_body = segments["ttl"].split()
        if len(ttl_body) != 1:
            raise PolicyError(source, lineno,
                              f"`ttl` names exactly one duration, got "
                              f"{segments['ttl']!r}")
        try:
            ttl_ms = _parse_ttl(ttl_body[0])
        except ValueError as exc:
            raise PolicyError(source, lineno, str(exc))

    uses: int | None = None
    if "uses" in segments:
        uses_body = segments["uses"].split()
        if len(uses_body) != 1 or not uses_body[0].isdigit():
            raise PolicyError(source, lineno,
                              f"`uses` names one positive integer, got "
                              f"{segments['uses']!r}")
        uses = int(uses_body[0])

    return AutoApproveRule(component, caps, realm, admitting, ttl_ms, uses)


# ---------------------------------------------- item 290: evidence / register
#
# The closed facet vocabulary, taken VERBATIM from the shipped item-293
# assessment (`registry.assess_evidence`). 290 introduces no new grading: a
# threshold is "at least this recorded status", a hard predicate over an
# objective fact. The status sets are the thresholds an operator may name (the
# strongest few, since a threshold weaker than the bottom status is vacuous);
# `fault-sweep` additionally accepts the numeric `N/N` size floor.
_EVIDENCE_STATUS_THRESHOLDS = {
    "fault-sweep": ("full",),                 # plus the numeric N/N form
    "attestation": ("valid", "present"),
    "inverse-roundtrip": ("pass",),
    "gauntlet": ("admissible", "present"),
    "publisher": ("trusted",),
    "capabilities": ("present",),
}

# The facets whose evidence is author-produced and therefore UNROOTED unless a
# binding-covering `attestation valid` clause roots them (item 290, §6.2/6.3).
# `attestation` and `publisher` are checked against operator-held inputs (a key,
# a trust set), so they are never self-attested.
_SELF_ATTESTING_FACETS = frozenset({"fault-sweep", "inverse-roundtrip",
                                    "gauntlet"})

# The declaration-strength floors (item 290, §3.2, adopting 309's order). Item
# 309 records `declared`/`keyed` in the IR (lower.py `_idempotent_register`) and
# item 440 adds `read`, so every floor is recordable: `declared` is the trust-me
# floor, `keyed` is the strong emission register, and `strong` is the floor
# meaning "any register above the trust-me floor" (today `keyed` or `read`).
_REGISTER_LEVELS = ("declared", "keyed", "strong")
_REGISTER_RECORDABLE = frozenset(_REGISTER_LEVELS)

# Floor spellings item 207 RETIRED. `shape-proven` named a register nothing ever
# produced, and over the whole producible vocabulary it was an exact synonym for
# `strong`. Rejected at parse naming the replacement rather than silently
# accepted, which is 290's own precedent for closing a vocabulary in time.
_RETIRED_REGISTER_LEVELS = {"shape-proven": "strong"}


@dataclass(frozen=True)
class EvidenceRule:
    """A ``<subject> requires evidence [<facet> <threshold>, ...]`` rule (item
    290). A hard predicate over the item-293 evidence facets: a conjunction of
    clauses (no scores, no partial credit), fail-closed (a missing facet fails),
    refuse-only (it can append a `Violation`, never widen past a deny or waive
    an approval). `origin` is ``"registry"`` for the origin-scoped
    ``component registry:<glob>`` selector (§3.2), else None."""
    scope: str                        # "component" | "realm" | "mcp" | "capability"
    selector: str                     # glob / realm name ("" for mcp)
    origin: str | None                # "registry" for origin-scoped rules, else None
    require: tuple                    # ((facet, threshold), ...) conjunction
    self_attested: bool = False       # explicit unrooted acknowledgment (§6.3)

    def unrooted_facets(self) -> frozenset:
        """The named facets that are UNROOTED — self-attesting evidence not
        covered by a binding `attestation valid` clause in this rule, and not
        operator-run at evaluation time (the mcp-session gauntlet). An unrooted
        facet is a `PolicyError` unless acknowledged (§6.3)."""
        has_valid_attest = any(
            f == "attestation" and t == "valid" for f, t in self.require)
        out = set()
        for facet, _ in self.require:
            if facet not in _SELF_ATTESTING_FACETS:
                continue
            if facet == "gauntlet" and self.scope == "mcp":
                continue  # operator-run session dossier — rooted by construction
            if has_valid_attest:
                continue  # the binding-covering attestation roots it (§6.2)
            out.add(facet)
        return frozenset(out)


@dataclass(frozen=True)
class RegisterRule:
    """A ``capability <glob> requires register <level>`` rule (item 290, §3.2):
    the declaration-strength floor over the item-44/309 honesty ledger. Slice 1
    parses only ``declared`` (the sole register the IR records today); a higher
    floor is a parse error until 309's ledger lands."""
    capability: str                   # glob over capability reach tokens
    at_least: str                     # "declared" | "keyed" | "strong"


@dataclass(frozen=True)
class TeardownRule:
    """A ``requires idempotent-teardown[(strength: <level>)]`` rule (item 309,
    §"question 4" point 3): the operator's unattended-recovery floor. Refuses a
    composition whose recovery surface (any inverse, deferred emission, or
    compensation) contains an entry whose register is below the strength floor,
    under 309's partial order. The bare form is ``strength = "declared"``:
    refuse a fenced/unregistered entry, admit any of the three registers."""
    strength: str                     # "declared" | "keyed" | "strong"


@dataclass(frozen=True)
class ReissueRule:
    """A ``recovery may re-issue owed emissions [(strength: <level>)]`` rule
    (item 440 §(b), item 309 §3b): the operator's knob for the crash-recovery
    RE-ISSUE SEAM.

    Absent — the default — `revl recover` auto-fires NOTHING, which is item 245's
    v1 rule ("recover never auto-fires an owed emission"). Present, it lets
    recovery re-fire an owed deferred emission whose WAL descriptor carries a
    register at or above `strength`, under 309's partial order:

    * bare (``strength = "keyed"``) — only the dedup-safe registers (`keyed`,
      and 440's `read`, which crosses nothing at all). The fire is safe BY
      CONSTRUCTION even if the pre-crash flush actually landed.
    * ``(strength: declared)`` — additionally the author's unverified trust-me
      claim, which an operator may choose to accept.

    An owed emission with NO register is never auto-fired under any strength: the
    ambiguous case stays human-finish, because nothing about it can be proven."""
    strength: str                     # "declared" | "keyed" | "strong"


@dataclass(frozen=True)
class Policy:
    """A parsed boundary policy: rules, the tenants switch, the sandbox."""
    rules: tuple[Rule, ...] = ()
    tenants_isolated: bool = False           # `tenants never reach each other`
    mcp_allow: tuple[str, ...] | None = None  # the agent-sandbox allow-list
    leases_enforced: bool = False            # `leases enforced` (item 61)
    quarantine_required: bool = False        # `quarantine required` (item 45)
    approval_rules: tuple[ApprovalRule, ...] = ()  # `requires approval` (246)
    # item 249, Slice C: `<subject> may not declassify <origin>[, ...]` — the
    # operator's power to forbid a taint downgrade for a component/realm. A
    # deny-list `Rule` (allow=False) whose `patterns` are origin tokens, checked
    # against the `declassify:<origin>` audit surface.
    declassify_rules: tuple[Rule, ...] = ()
    # item 249, Slice D (D2): `<origin>-taint may not reach <cap> [without
    # approval]` — the policy-gated tier over the landed taint tokens.
    taint_flow_rules: tuple[TaintFlowRule, ...] = ()
    source: str | None = None                # file path, for messages
    # item 290: the confidence/evidence admission rules. Defaulting empty so
    # every existing policy file parses byte-identically and evaluates the same.
    evidence_rules: tuple[EvidenceRule, ...] = ()
    register_rules: tuple[RegisterRule, ...] = ()
    # item 309: `requires idempotent-teardown[(strength: <level>)]` — the
    # unattended-recovery floor over the recovery surface. Empty by default so
    # every existing policy parses/evaluates byte-identically.
    teardown_rules: tuple[TeardownRule, ...] = ()
    # item 440: `recovery may re-issue owed emissions [(strength: <level>)]` —
    # the crash-recovery re-issue seam's operator knob. Empty by default so every
    # existing policy parses/evaluates byte-identically AND so a recover with no
    # policy auto-fires nothing.
    reissue_rules: tuple[ReissueRule, ...] = ()
    evidence_root_local: bool = False        # `evidence-root: local` (§6.3)
    # item 251, Slice 1: distilled `component <glob> may auto-approve <caps>`
    # rules. PARSE-ONLY this slice (no evaluation wiring); empty by default so
    # every existing policy parses/evaluates byte-identically.
    auto_approve_rules: tuple[AutoApproveRule, ...] = ()

    def is_empty(self) -> bool:
        return not self.rules and not self.tenants_isolated \
            and self.mcp_allow is None and not self.leases_enforced \
            and not self.quarantine_required and not self.approval_rules \
            and not self.declassify_rules and not self.taint_flow_rules \
            and not self.evidence_rules and not self.register_rules \
            and not self.teardown_rules \
            and not self.reissue_rules \
            and not self.auto_approve_rules \
            and not self.evidence_root_local

    def reissue_strength(self) -> str | None:
        """The re-issue seam's strength floor for `revl recover`, or None when no
        rule turns the seam on (item 440 §(b)).

        Several rules take the WEAKEST floor, because a policy that says both
        "keyed only" and "declared too" has authorized the wider one; the
        recovery side then admits a descriptor iff it meets that floor. `None`
        means the seam is OFF, which is what every policy written before this
        item — and every recover run with no policy at all — gets."""
        if not self.reissue_rules:
            return None
        from .lower import _REGISTER_RANK  # noqa: PLC0415 — the partial order
        return min((r.strength for r in self.reissue_rules),
                   key=lambda level: _REGISTER_RANK.get(level, 0))

    def requires_approval(self) -> bool:
        """Whether this policy names any approval-required capability — the
        signal that a policy FILE enables the item-246 approval gate (Decision 3:
        `a policy file that names approval-required capabilities`)."""
        return bool(self.approval_rules)

    def approval_rule_for(self, token: str) -> ApprovalRule | None:
        """The first approval rule whose glob covers `token`, or None. The
        crossing reads this to decide whether it needs an `Approval` and, when it
        does, the ttl its token carries."""
        for rule in self.approval_rules:
            if rule.covers(token):
                return rule
        return None


# ------------------------------------------------------------------- parsing

class PolicyError(RevlError):
    """A malformed policy file (a parse error, not a violation)."""


def _split_caps(text: str) -> tuple[str, ...]:
    caps = tuple(part.strip() for part in text.split(",") if part.strip())
    return caps


# ------------------------------------------------ item 290: evidence parsing

def _parse_facet_clause(clause: str, source, lineno) -> tuple[str, str]:
    """Parse one ``<facet> <threshold>`` clause into a validated pair.

    The vocabulary is CLOSED: an unknown facet, an unknown status, a numeric
    sweep floor whose numerator differs from its denominator, or any numeric-
    confidence spelling is a `PolicyError` at parse time, so a typo can never
    become a rule that silently requires nothing (§3.3)."""
    parts = clause.split()
    if len(parts) < 2:
        raise PolicyError(source, lineno,
                          f"an evidence clause is `<facet> <threshold>`, got "
                          f"{clause.strip()!r}")
    facet = parts[0]
    threshold = " ".join(parts[1:])
    if facet == "confidence" or _is_float(facet) or _is_float(threshold):
        raise PolicyError(
            source, lineno,
            f"evidence is a hard predicate, not a score: {clause.strip()!r} "
            f"names a numeric confidence. There is no `confidence`, weight, or "
            f"float in the vocabulary (item 290, §2) — threshold an objective "
            f"facet instead, e.g. `fault-sweep full` or `attestation valid`")
    if facet not in _EVIDENCE_STATUS_THRESHOLDS:
        raise PolicyError(
            source, lineno,
            f"unknown evidence facet {facet!r} — the closed vocabulary is "
            f"[{', '.join(sorted(_EVIDENCE_STATUS_THRESHOLDS))}] (item 293's "
            f"assessment facets)")
    if facet == "fault-sweep" and "/" in threshold:
        num, _, den = threshold.partition("/")
        num, den = num.strip(), den.strip()
        if not (num.isdigit() and den.isdigit()):
            raise PolicyError(source, lineno,
                              f"malformed fault-sweep floor {threshold!r} — "
                              f"expected `full` or `<n>/<n>`")
        if int(num) != int(den):
            raise PolicyError(
                source, lineno,
                f"fault-sweep floor {threshold!r} has numerator below "
                f"denominator: the only semantics is all-passed (no partial "
                f"credit, item 290 §2/§3.1), so the numerator must equal the "
                f"denominator — write `{den}/{den}`")
        return ("fault-sweep", f"{int(den)}/{int(den)}")
    allowed = _EVIDENCE_STATUS_THRESHOLDS[facet]
    if threshold not in allowed:
        raise PolicyError(
            source, lineno,
            f"unknown threshold {threshold!r} for facet {facet!r} — expected "
            f"one of [{', '.join(allowed)}]"
            + (" or `<n>/<n>`" if facet == "fault-sweep" else ""))
    return (facet, threshold)


def _is_float(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _parse_require_list(text: str, source, lineno) -> tuple:
    """Parse ``[<facet> <threshold>, ...]`` into a validated conjunction."""
    inner = text.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        raise PolicyError(source, lineno,
                          f"a `requires evidence` list is `[<facet> <threshold>"
                          f", ...]`, got {text.strip()!r}")
    clauses = [c for c in inner[1:-1].split(",") if c.strip()]
    if not clauses:
        raise PolicyError(source, lineno,
                          "a `requires evidence` rule names no facet")
    return tuple(_parse_facet_clause(c, source, lineno) for c in clauses)


def _parse_evidence_subject(head: str, source, lineno) -> tuple[str, str, str | None]:
    """Parse an evidence-rule subject into ``(scope, selector, origin)``.

    Supports ``component <glob>``, the origin-scoped ``component registry:<glob>``
    (§3.2), ``realm <name>``, ``mcp``, and ``capability <glob>``."""
    parts = head.split()
    if len(parts) == 1 and parts[0].lower() == "mcp":
        return ("mcp", "", None)
    if len(parts) != 2:
        raise PolicyError(
            source, lineno,
            f"unrecognised evidence subject {head!r} — expected `component "
            f"<glob>`, `component registry:<glob>`, `realm <name>`, `mcp`, or "
            f"`capability <glob>`")
    kind, sel = parts[0].lower(), parts[1]
    if kind == "component":
        if sel.startswith("registry:"):
            return ("component", sel[len("registry:"):], "registry")
        if ":" in sel:
            raise PolicyError(
                source, lineno,
                f"unknown origin prefix in {sel!r} — the only origin selector "
                f"is `registry:` (item 290, §3.2); a bare `component <glob>` "
                f"selects by name at any origin")
        return ("component", sel, None)
    if kind == "realm":
        return ("realm", sel, None)
    if kind == "capability":
        return ("capability", sel, None)
    raise PolicyError(
        source, lineno,
        f"unrecognised evidence subject {head!r} — expected `component <glob>`, "
        f"`component registry:<glob>`, `realm <name>`, `mcp`, or `capability "
        f"<glob>`")


def _parse_register_level(level: str, source, lineno) -> str:
    """Validate a `requires register <level>` floor (§3.2). Every level in
    `_REGISTER_LEVELS` is recordable now that 309's ledger and 440's read tier
    have landed. A level item 207 RETIRED is rejected with its replacement named,
    so a policy written against the old vocabulary fails loudly rather than
    grading nothing."""
    lvl = level.strip()
    if lvl in _RETIRED_REGISTER_LEVELS:
        raise PolicyError(
            source, lineno,
            f"register level {lvl!r} was removed (item 207) — write "
            f"{_RETIRED_REGISTER_LEVELS[lvl]!r} instead, which is what it "
            f"graded")
    if lvl not in _REGISTER_LEVELS:
        raise PolicyError(
            source, lineno,
            f"unknown register level {lvl!r} — expected one of "
            f"[{', '.join(_REGISTER_LEVELS)}] (item 290/309)")
    if lvl not in _REGISTER_RECORDABLE:  # pragma: no cover — all levels recordable
        raise PolicyError(
            source, lineno,
            f"register level {lvl!r} is not recordable (item 290, §3.2)")
    return lvl


def _parse_teardown_strength(tail: str, source, lineno) -> str:
    """Parse the `(strength: <level>)` argument of `requires idempotent-teardown`
    (item 309). The bare rule (no argument) is `declared`; an explicit argument
    names a level in `_REGISTER_LEVELS`."""
    inner = tail.strip()
    if not (inner.startswith("(") and inner.endswith(")")):
        raise PolicyError(
            source, lineno,
            f"`requires idempotent-teardown` takes either no argument or "
            f"`(strength: <level>)`, got {tail.strip()!r} (item 309)")
    inner = inner[1:-1].strip()
    key, _, level = inner.partition(":")
    if key.strip().lower() != "strength" or not level.strip():
        raise PolicyError(
            source, lineno,
            f"the `requires idempotent-teardown` argument is "
            f"`(strength: <level>)`, got `({inner})` (item 309)")
    return _parse_register_level(level.strip(), source, lineno)


def _parse_reissue_strength(tail: str, source, lineno) -> str:
    """Parse the `(strength: <level>)` argument of `recovery may re-issue owed
    emissions` (item 440 §(b)). The bare rule is `keyed` — only the dedup-safe
    registers — and an explicit argument names a level in `_REGISTER_LEVELS`."""
    inner = tail.strip()
    if not (inner.startswith("(") and inner.endswith(")")):
        raise PolicyError(
            source, lineno,
            f"`recovery may re-issue owed emissions` takes either no argument or "
            f"`(strength: <level>)`, got {tail.strip()!r} (item 440)")
    inner = inner[1:-1].strip()
    key, _, level = inner.partition(":")
    if key.strip().lower() != "strength" or not level.strip():
        raise PolicyError(
            source, lineno,
            f"the `recovery may re-issue owed emissions` argument is "
            f"`(strength: <level>)`, got `({inner})` (item 440)")
    return _parse_register_level(level.strip(), source, lineno)


def _validate_evidence_rooting(rules, root_local: bool) -> None:
    """After the whole policy is parsed, refuse any evidence rule that
    thresholds an UNROOTED self-attesting facet without an acknowledgment — per
    rule (`self-attested`) or policy-wide (`evidence-root: local`) — per §6.3.
    Deferred to here because `evidence-root: local` may appear on any line."""
    if root_local:
        return
    for rule in rules:
        if rule.self_attested:
            continue
        unrooted = rule.unrooted_facets()
        if unrooted:
            raise PolicyError(
                rule.selector or "<policy>", None,
                f"evidence rule thresholds self-attested facet(s) "
                f"[{', '.join(sorted(unrooted))}] with no root: an author-"
                f"produced dossier is only as good as its trust root (item 290, "
                f"§6). Root it with a binding-covering `attestation valid` "
                f"clause, or acknowledge it explicitly — add `self-attested` to "
                f"the rule, or `evidence-root: local` to the policy")


def _parse_dsl(text: str, source: str | None) -> Policy:
    """The line DSL. Grammar (blank lines and ``#`` comments ignored):

        component <glob>  may reach      <cap>[, <cap> ...]
        component <glob>  may not reach  <cap>[, <cap> ...]
        realm <name>      may reach      <cap>[, <cap> ...]
        realm <name>      may not reach  <cap>[, <cap> ...]
        mcp               may reach      <cap>[, <cap> ...]
        agent             may reach      <cap>[, <cap> ...]   (alias of mcp)
        tenants never reach each other
    """
    rules: list[Rule] = []
    tenants = False
    mcp_allow: tuple[str, ...] | None = None
    leases_enforced = False
    quarantine_required = False
    approval_rules: list[ApprovalRule] = []
    declassify_rules: list[Rule] = []
    taint_flow_rules: list[TaintFlowRule] = []
    evidence_rules: list[EvidenceRule] = []
    register_rules: list[RegisterRule] = []
    teardown_rules: list[TeardownRule] = []
    reissue_rules: list[ReissueRule] = []
    auto_approve_rules: list[AutoApproveRule] = []
    evidence_root_local = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low == "tenants never reach each other":
            tenants = True
            continue
        # item 290, §6.3: the policy-wide unrooted acknowledgment.
        if low in ("evidence-root: local", "evidence-root local"):
            evidence_root_local = True
            continue
        # item 290: `<subject> requires evidence [<facet> <threshold>, ...]
        # [self-attested]`. Checked before the approval/reach loops (it shares
        # neither phrase). A trailing `self-attested` is the per-rule unrooted
        # acknowledgment (§6.3).
        if "requires evidence" in low:
            head, _, tail = line.partition(" requires evidence")
            scope, selector, origin = _parse_evidence_subject(
                head.strip(), source, lineno)
            self_attested = False
            body = tail.strip()
            close = body.rfind("]")
            if close != -1:
                trailer = body[close + 1:].strip().lower()
                if trailer == "self-attested":
                    self_attested = True
                elif trailer:
                    raise PolicyError(
                        source, lineno,
                        f"unexpected trailer after `requires evidence [...]`: "
                        f"{body[close + 1:].strip()!r} — only `self-attested` "
                        f"is allowed (item 290, §6.3)")
                body = body[:close + 1]
            require = _parse_require_list(body, source, lineno)
            evidence_rules.append(
                EvidenceRule(scope, selector, origin, require, self_attested))
            continue
        # item 290, §3.2: `capability <glob> requires register <level>`.
        if "requires register" in low:
            head, _, tail = line.partition(" requires register")
            parts = head.split()
            if len(parts) != 2 or parts[0].lower() != "capability":
                raise PolicyError(
                    source, lineno,
                    f"a `requires register` rule names one capability glob: "
                    f"`capability <glob> requires register <level>`, got "
                    f"{raw.strip()!r}")
            level = _parse_register_level(tail.strip(), source, lineno)
            register_rules.append(RegisterRule(parts[1], level))
            continue
        # item 309 §"question 4", point 3: `requires idempotent-teardown` and its
        # `requires idempotent-teardown(strength: <level>)` form — the operator's
        # unattended-recovery floor. Document-global: it refuses a composition
        # whose recovery surface (an inverse, a deferred emission, a compensation)
        # contains an entry below the named strength floor. The bare form means
        # `strength: declared` (refuse a fenced/unregistered entry).
        if "requires idempotent-teardown" in low:
            _, _, tail = line.partition("requires idempotent-teardown")
            tail = tail.strip()
            strength = "declared"
            if tail:
                strength = _parse_teardown_strength(tail, source, lineno)
            teardown_rules.append(TeardownRule(strength))
            continue
        # item 440 §(b): `recovery may re-issue owed emissions` and its
        # `(strength: <level>)` form — the crash-recovery re-issue seam's
        # operator knob. Document-global, like the teardown floor: it authorizes
        # `revl recover` to auto-fire an OWED deferred emission whose WAL
        # descriptor carries a register at or above the floor. Absent, recover
        # auto-fires nothing (item 245's v1 rule), so the DEFAULT is closed.
        if "recovery may re-issue owed emissions" in low:
            _, _, tail = line.partition("recovery may re-issue owed emissions")
            tail = tail.strip()
            strength = "keyed"
            if tail:
                strength = _parse_reissue_strength(tail, source, lineno)
            reissue_rules.append(ReissueRule(strength))
            continue
        # item 249, Slice D (D2): `<origin>-taint may not reach <cap>[, ...]
        # [without approval]`. Checked before the generic `may not reach` loop
        # (it CONTAINS that phrase) so the taint tier is not misread as a reach
        # rule. The subject is the taint origin, not a component/realm.
        if "-taint may not reach" in low:
            origin = line[:low.find("-taint may not reach")].strip()
            tail = line[low.find("-taint may not reach") + len("-taint may not reach"):]
            without_approval = False
            low_tail = tail.lower()
            if low_tail.rstrip().endswith("without approval"):
                without_approval = True
                tail = tail[:low_tail.rstrip().rfind("without approval")]
            caps = _split_caps(tail)
            if not origin or not caps:
                raise PolicyError(source, lineno,
                                  f"a taint-flow rule is `<origin>-taint may not "
                                  f"reach <cap>[, ...] [without approval]`, got "
                                  f"{raw.strip()!r}")
            taint_flow_rules.append(
                TaintFlowRule(origin, caps, without_approval))
            continue
        # item 249, Slice C: `<subject> may not declassify <origin>[, ...]` — the
        # operator forbids a taint downgrade. `<subject>` is `component <glob>`
        # or `realm <name>`, exactly as the reach rules. Checked before the
        # reach-verb loop so `declassify` is not mistaken for a capability token.
        if "may not declassify" in low:
            idx = low.find("may not declassify")
            head = line[:idx].strip()
            origins = _split_caps(line[idx + len("may not declassify"):])
            if not origins:
                raise PolicyError(source, lineno,
                                  f"policy line names no taint origin: "
                                  f"{raw.strip()!r}")
            parts = head.split()
            if len(parts) == 2 and parts[0].lower() == "component":
                declassify_rules.append(Rule("component", parts[1], False, origins))
            elif len(parts) == 2 and parts[0].lower() == "realm":
                declassify_rules.append(Rule("realm", parts[1], False, origins))
            else:
                raise PolicyError(source, lineno,
                                  f"unrecognised declassify subject {head!r} — "
                                  f"expected `component <glob>` or `realm <name>`")
            continue
        # the approval gate (item 246, Slice 2): `capability <glob> requires
        # approval [ttl <D>]`. Operator-owned — an author cannot waive it by
        # omission, so the requirement lives here, not in the source (Decision 3).
        if "requires approval" in low:
            head, _, tail = line.partition(" requires approval")
            parts = head.split()
            if len(parts) != 2 or parts[0].lower() != "capability":
                raise PolicyError(source, lineno,
                                  f"a `requires approval` rule names one "
                                  f"capability glob: `capability <glob> requires "
                                  f"approval [ttl <D>]`, got {raw.strip()!r}")
            ttl_ms = None
            rest = tail.strip()
            if rest:
                ttl_parts = rest.split()
                if len(ttl_parts) != 2 or ttl_parts[0].lower() != "ttl":
                    raise PolicyError(source, lineno,
                                      f"unexpected trailer after `requires "
                                      f"approval`: {rest!r} — only `ttl <D>` is "
                                      f"allowed")
                try:
                    ttl_ms = _parse_ttl(ttl_parts[1])
                except ValueError as exc:
                    raise PolicyError(source, lineno, str(exc))
            approval_rules.append(ApprovalRule(parts[1], ttl_ms))
            continue
        # the quarantine tier (item 45): require an untrusted candidate to prove
        # itself in the wasm sandbox before it may be admitted to a hosted tier
        # (docs/quarantine-tier.md). Enforced at swap; an operator with
        # `quarantine-bypass` authority (item 55) may override.
        if low in ("quarantine required", "quarantine is required"):
            quarantine_required = True
            continue
        # component leases (item 61): promote the advisory workspace warning to
        # an admission refusal — a swap that replaces a component another
        # operator leases is refused (docs/component-leases.md).
        if low in ("leases enforced", "leases are enforced"):
            leases_enforced = True
            continue
        # item 251, Slice 1: `component <glob> may auto-approve <caps> [in realm
        # <r>] [admitting <o>-taint,...] [ttl <D>] [uses <N>]`. Checked before the
        # `may reach` loop (it shares neither verb, but this keeps the distilled
        # tier visibly separate). Parse-only this slice - no evaluation wiring.
        if "may auto-approve" in low:
            head, _, tail = line.partition(" may auto-approve")
            auto_approve_rules.append(
                _parse_auto_approve(head.strip(), tail, source, lineno))
            continue
        # <head> may [not] reach <caps>
        for verb, allow in (("may not reach", False), ("may reach", True)):
            idx = low.find(verb)
            if idx == -1:
                continue
            head = line[:idx].strip()
            caps = _split_caps(line[idx + len(verb):])
            if not caps:
                raise PolicyError(source, lineno,
                                  f"policy line names no capability: {raw.strip()!r}")
            parts = head.split()
            if len(parts) == 2 and parts[0].lower() == "component":
                rules.append(Rule("component", parts[1], allow, caps))
            elif len(parts) == 2 and parts[0].lower() == "realm":
                rules.append(Rule("realm", parts[1], allow, caps))
            elif len(parts) == 1 and parts[0].lower() in ("mcp", "agent"):
                if not allow:
                    raise PolicyError(source, lineno,
                                      "the mcp/agent sandbox is an allow-list; "
                                      "write `mcp may reach ...`, not `may not`")
                mcp_allow = caps if mcp_allow is None else mcp_allow + caps
            else:
                raise PolicyError(source, lineno,
                                  f"unrecognised policy subject {head!r} — expected "
                                  f"`component <glob>`, `realm <name>`, or `mcp`")
            break
        else:
            raise PolicyError(source, lineno,
                              f"unrecognised policy line: {raw.strip()!r}")
    _validate_evidence_rooting(evidence_rules, evidence_root_local)
    return Policy(tuple(rules), tenants, mcp_allow, leases_enforced,
                  quarantine_required, tuple(approval_rules),
                  tuple(declassify_rules), tuple(taint_flow_rules), source,
                  tuple(evidence_rules), tuple(register_rules),
                  tuple(teardown_rules),
                  tuple(reissue_rules),
                  evidence_root_local,
                  tuple(auto_approve_rules))


def _parse_json(text: str, source: str | None) -> Policy:
    """The JSON equivalent of the DSL — same `Policy`, machine-authored.

    { "components": [{"pattern": "Agent*", "allow": [...], "deny": [...]}],
      "realms":     [{"realm": "billing", "allow": [...], "deny": [...]}],
      "tenants":    {"neverReachEachOther": true},
      "mcp":        {"allow": [...]} }
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(source, exc.lineno, f"invalid policy JSON: {exc.msg}")
    if not isinstance(doc, dict):
        raise PolicyError(source, 1, "a JSON policy must be an object")
    rules: list[Rule] = []
    for entry in doc.get("components") or []:
        pat = entry.get("pattern") or entry.get("component") or "*"
        if entry.get("allow") is not None:
            rules.append(Rule("component", pat, True, tuple(entry["allow"])))
        if entry.get("deny"):
            rules.append(Rule("component", pat, False, tuple(entry["deny"])))
    for entry in doc.get("realms") or []:
        realm = entry.get("realm")
        if realm is None:
            raise PolicyError(source, 1, "a realm rule needs a `realm` name")
        if entry.get("allow") is not None:
            rules.append(Rule("realm", realm, True, tuple(entry["allow"])))
        if entry.get("deny"):
            rules.append(Rule("realm", realm, False, tuple(entry["deny"])))
    tenants = bool((doc.get("tenants") or {}).get("neverReachEachOther"))
    mcp = doc.get("mcp") or {}
    mcp_allow = tuple(mcp["allow"]) if mcp.get("allow") is not None else None
    leases_enforced = bool((doc.get("leases") or {}).get("enforced"))
    quarantine_required = bool((doc.get("quarantine") or {}).get("required"))
    approval_rules: list[ApprovalRule] = []
    for entry in doc.get("approvals") or []:
        cap = entry.get("capability") or entry.get("pattern")
        if not cap:
            raise PolicyError(source, 1,
                              "an approval rule needs a `capability` glob")
        ttl_ms = None
        if entry.get("ttl") is not None:
            try:
                ttl_ms = _parse_ttl(str(entry["ttl"]))
            except ValueError as exc:
                raise PolicyError(source, 1, str(exc))
        approval_rules.append(ApprovalRule(cap, ttl_ms))
    declassify_rules: list[Rule] = []
    for entry in doc.get("declassify") or []:
        sel = entry.get("component") or entry.get("realm")
        scope = "realm" if entry.get("realm") else "component"
        origins = entry.get("deny") or entry.get("origins")
        if not sel or not origins:
            raise PolicyError(source, 1,
                              "a declassify rule needs a `component`/`realm` "
                              "selector and a `deny`/`origins` list")
        declassify_rules.append(Rule(scope, sel, False, tuple(origins)))
    taint_flow_rules: list[TaintFlowRule] = []
    for entry in doc.get("taintFlow") or doc.get("taint_flow") or []:
        origin = entry.get("origin")
        caps = entry.get("reach") or entry.get("deny")
        if not origin or not caps:
            raise PolicyError(source, 1,
                              "a taint-flow rule needs an `origin` and a "
                              "`reach`/`deny` capability list")
        taint_flow_rules.append(
            TaintFlowRule(origin, tuple(caps),
                          bool(entry.get("withoutApproval")
                               or entry.get("without_approval"))))
    evidence_root_local = str(doc.get("evidenceRoot") or "").lower() == "local"
    evidence_rules: list[EvidenceRule] = []
    for entry in doc.get("evidence") or []:
        self_attested = bool(entry.get("selfAttested")
                             or entry.get("self_attested"))
        require_obj = entry.get("require") or {}
        if not isinstance(require_obj, dict) or not require_obj:
            raise PolicyError(source, 1,
                              "an evidence rule needs a non-empty `require` map "
                              "of {facet: threshold}")
        require = tuple(
            _parse_facet_clause(f"{facet} {threshold}", source, 1)
            for facet, threshold in require_obj.items())
        if entry.get("mcp"):
            evidence_rules.append(
                EvidenceRule("mcp", "", None, require, self_attested))
            continue
        if entry.get("capability"):
            evidence_rules.append(
                EvidenceRule("capability", entry["capability"], None, require,
                             self_attested))
            continue
        if entry.get("realm"):
            evidence_rules.append(
                EvidenceRule("realm", entry["realm"], None, require,
                             self_attested))
            continue
        comp = entry.get("component")
        if not comp:
            raise PolicyError(source, 1,
                              "an evidence rule needs a `component`, `realm`, "
                              "`capability`, or `mcp` selector")
        scope, selector, origin = _parse_evidence_subject(
            f"component {comp}", source, 1)
        evidence_rules.append(
            EvidenceRule(scope, selector, origin, require, self_attested))
    register_rules: list[RegisterRule] = []
    for entry in doc.get("registers") or []:
        cap = entry.get("capability") or entry.get("pattern")
        if not cap:
            raise PolicyError(source, 1,
                              "a register rule needs a `capability` glob")
        level = _parse_register_level(
            str(entry.get("atLeast") or entry.get("at_least") or ""), source, 1)
        register_rules.append(RegisterRule(cap, level))
    teardown_rules: list[TeardownRule] = []
    reissue_rules: list[ReissueRule] = []
    for entry in doc.get("idempotentTeardown") or []:
        strength = _parse_register_level(
            str(entry.get("strength") or "declared"), source, 1)
        teardown_rules.append(TeardownRule(strength))
    # item 251, Slice 1: the `autoApprovals` array, the JSON equivalent of the
    # `may auto-approve` DSL line. Parse-only, additive.
    auto_approve_rules: list[AutoApproveRule] = []
    for entry in doc.get("autoApprovals") or []:
        component = entry.get("component")
        caps_raw = entry.get("capabilities") or entry.get("caps")
        if not component or not caps_raw:
            raise PolicyError(source, 1,
                              "an auto-approve rule needs a `component` glob and a "
                              "non-empty `capabilities` list")
        caps = tuple(_canon_cap_spelling(str(c), source, 1) for c in caps_raw)
        realm = entry.get("realm")
        admitting = _parse_admitting(
            tuple(str(o) for o in (entry.get("admitting") or [])), source, 1)
        ttl_ms = None
        if entry.get("ttl") is not None:
            try:
                ttl_ms = _parse_ttl(str(entry["ttl"]))
            except ValueError as exc:
                raise PolicyError(source, 1, str(exc))
        uses = entry.get("uses")
        if uses is not None:
            uses = int(uses)
        auto_approve_rules.append(
            AutoApproveRule(component, caps, realm, admitting, ttl_ms, uses))
    _validate_evidence_rooting(evidence_rules, evidence_root_local)
    return Policy(tuple(rules), tenants, mcp_allow, leases_enforced,
                  quarantine_required, tuple(approval_rules),
                  tuple(declassify_rules), tuple(taint_flow_rules), source,
                  tuple(evidence_rules), tuple(register_rules),
                  tuple(teardown_rules),
                  tuple(reissue_rules),
                  evidence_root_local,
                  tuple(auto_approve_rules))


def parse_policy(text: str, source: str | None = None) -> Policy:
    """Parse policy text — JSON when it opens with ``{``, else the line DSL."""
    return (_parse_json(text, source) if text.lstrip().startswith("{")
            else _parse_dsl(text, source))


def load_policy(path: str) -> Policy:
    """Read and parse a policy file."""
    with open(path, encoding="utf-8") as handle:
        return parse_policy(handle.read(), source=path)


# ---------------------------------------------------------- the audit graph

@dataclass(frozen=True)
class Reach:
    """One boundary a component reaches, with its provenance for the trace."""
    token: str                 # the capability token or extern name (or `*`)
    via: str                   # the emission label / extern it came through
    kind: str                  # "emission" | "host"


def component_reach(audit: dict, name: str) -> list[Reach]:
    """The set of boundaries a component reaches, off the G8 audit graph.

    Two sources, both already enumerated by `revl audit`:
      * emission capabilities — the scope each emission call site may cross;
      * reached host externs — host code the component's body reaches.
    A `*` on either side is the unnameable boundary, carried through verbatim.

    A reached extern contributes its DECLARED capability scope when it has one
    (`extern emission[db] fn pg_write` contributes `db`) and its own NAME when
    it does not (`extern emission fn send` contributes `send`) — the
    `capabilities or (name,)` rule the rest of the system already keys scoped
    crossings by: `lower._extern_emission_caps`, `emission_analysis`'s
    witnessed seed, `audit_diff._capability_registers`, and item 343's approval
    `ClassMap`. Before item 247 this surface used the name unconditionally, so a
    directly-emitted `emission[db]` extern put `pg_write` in reach while
    `capability_registers` and the approval fold both said `db` — and a rule
    written `capability db requires register keyed` selected nothing, giving an
    operator less floor than they asked for with no diagnostic. The audit's
    `externs` enumeration is unchanged and still keyed by NAME: it is the
    host-code table (class, backends, ref provenance), not the token namespace.
    """
    stats = (audit.get("boundary") or {}).get(name) or {}
    out: list[Reach] = []
    seen: set[tuple[str, str]] = set()

    def add(token: str, via: str, kind: str) -> None:
        key = (token, kind)
        if key not in seen:
            seen.add(key)
            out.append(Reach(token, via, kind))

    for label, caps in (stats.get("capabilities") or {}).items():
        for cap in caps:
            add(cap, label, "emission")
    for ext in stats.get("externs") or []:
        for token in ext.get("capabilities") or (ext.get("name"),):
            add(token, ext.get("name"), "host")
    return out


def component_realms(manifest: dict, name: str) -> frozenset[str]:
    """The realms a component is isolated into (its `isolate` map values).

    A component with no isolate lives in the shared realm and is *not* a
    tenant — the tenants rule only pairs components that each name a realm.
    """
    for entry in manifest.get("components") or []:
        if entry.get("name") == name:
            return frozenset((entry.get("isolate") or {}).values())
    return frozenset()


# ----------------------------------------------------------- the evaluation

@dataclass(frozen=True)
class Violation:
    """One way the composition breaches the policy."""
    kind: str                        # capability | deny | tenant | mcp-sandbox
                                     #   | approval | declassify | taint-flow
                                     #   | evidence | register (item 290)
    component: str
    token: str
    message: str
    why: WhyTrace
    # item 274: the navigable-refusal map for this violation, computed from the
    # same tables that refused (navigate.py). Optional and additive — a
    # violation with no navigate threads nothing onto its `RevlError`, so every
    # existing surface is byte-identical. Already redacted for the
    # untrusted-author view at construction.
    navigate: dict | None = None

    def render(self) -> str:
        from .why import render as render_why  # noqa: PLC0415
        return self.message + "\n" + render_why(self.why)


def _matches_any(token: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(token, pat) for pat in patterns)


def _allowed(token: str, allow: tuple[str, ...]) -> bool:
    """Is `token` inside a closed allow-list? `*` (unbounded) is only ever
    allowed by a literal `*` in the list — an unnameable reach can never be
    proven in-bounds by a named pattern."""
    if token == UNBOUNDED:
        return UNBOUNDED in allow
    return _matches_any(token, allow)


def _location(manifest: dict, name: str) -> tuple[str | None, int | None]:
    for entry in manifest.get("components") or []:
        if entry.get("name") == name:
            return entry.get("file"), None
    return None, None


def _reach_step(manifest: dict, name: str, reach: Reach) -> list[TraceStep]:
    """component -> the boundary it reaches, as a two-hop chain."""
    file, _ = _location(manifest, name)
    if reach.kind == "emission":
        detail = f"reaches `{reach.token}` via emission `{reach.via}`"
    elif reach.via and reach.via != reach.token:
        # item 247: a scoped extern's token is not its name, so the trace has
        # to name the host code the token was declared on — otherwise the
        # refusal says `db` and the reader cannot find `db` in the source.
        detail = f"reaches `{reach.token}` via host code `{reach.via}`"
    else:
        detail = f"reaches host code `{reach.token}`"
    tail = (reach.token if reach.token != UNBOUNDED
            else "* (an unnameable boundary)")
    return [
        TraceStep(name, "component", file, None, detail),
        TraceStep(tail, reach.kind, None, None, "boundary crossed",
                  () if reach.token == UNBOUNDED else (reach.token,)),
    ]


def _allow_for(policy: Policy, name: str, realms: frozenset[str]) \
        -> tuple[str, ...] | None:
    """The union of allow-patterns constraining a component, or None when no
    allow rule selects it (then it is unconstrained by allow-lists)."""
    allow: list[str] = []
    constrained = False
    for rule in policy.rules:
        if rule.allow and rule.selects(name, realms):
            constrained = True
            allow.extend(rule.patterns)
    return tuple(dict.fromkeys(allow)) if constrained else None


def _deny_for(policy: Policy, name: str, realms: frozenset[str]) -> list[Rule]:
    return [r for r in policy.rules
            if not r.allow and r.selects(name, realms)]


def _components(audit: dict) -> list[str]:
    return list((audit.get("boundary") or {}).keys())


# ------------------------------------------------ item 290: evidence evaluation

@dataclass(frozen=True)
class ClauseVerdict:
    """One evidence clause evaluated against a component's recorded facts — the
    unit both the gate (a failing clause becomes a `Violation`) and the
    `revl policy evaluate` report (every clause, pass and fail) read, so the
    dry-run can never disagree with the gate (§7, one comparison site)."""
    facet: str
    threshold: str
    fact: str                 # the recorded fact, e.g. "8/12 partial", "valid"
    passed: bool
    standing: str             # "verified" | "operator-run" | "self-attested"
    detail: str = ""          # extra note (hash mismatch, cannot-verify, ...)


@dataclass(frozen=True)
class RuleReport:
    """One evidence rule's verdict for one component: whether it selected the
    component, and every clause verdict when it did."""
    scope: str
    selector: str
    origin: str | None
    self_attested: bool
    selected: bool
    clauses: tuple = ()

    def failed(self) -> list:
        return [c for c in self.clauses if not c.passed]


def _register_clause(token: str, actual: str | None, floor: str) -> "ClauseVerdict":
    """One capability token's register verdict against a `requires register`
    floor (item 309/290 §3.2). Reads 309's partial order via lower's
    `_register_satisfies`; a token with no idempotency claim (`actual is None`)
    fails every floor. The clause carries the token in `facet` so the violation
    names WHICH capability fell short."""
    from .lower import _register_satisfies  # noqa: PLC0415 — the partial order
    ok = _register_satisfies(actual, floor)
    fact = actual or "unregistered"
    return ClauseVerdict(f"register:{token}", floor, fact, ok, "verified",
                         "" if ok else f"{fact} is below the `{floor}` floor")


def _component_origin(audit: dict, origins, name: str) -> str | None:
    """A component's admission ORIGIN (`registry` for a registry-resolved
    admission, `source` for bare-source), recorded by the admission path and
    never asserted by the component (§3.2). Read from an explicit `origins` map
    or from `audit["origins"]`; absent when the admission path did not record
    one, in which case an origin-scoped `registry:` rule selects nothing (it
    never refuses first-party bare-source code by accident)."""
    if origins and name in origins:
        return origins[name]
    graph_origins = audit.get("origins")
    if isinstance(graph_origins, dict):
        return graph_origins.get(name)
    return None


def _evidence_rule_selects(rule: EvidenceRule, name: str,
                           realms: frozenset, reach_tokens: frozenset,
                           origin: str | None,
                           mcp_components: frozenset) -> bool:
    if rule.scope == "mcp":
        return name in mcp_components
    if rule.scope == "realm":
        return rule.selector in realms
    if rule.scope == "capability":
        return any(fnmatchcase(tok, rule.selector) for tok in reach_tokens
                   if tok != UNBOUNDED)
    # component: name glob, plus origin when the rule is origin-scoped
    if not fnmatchcase(name, rule.selector):
        return False
    if rule.origin is not None:
        return origin == rule.origin
    return True


def _sweep_counts(bundle) -> tuple[int, int, int]:
    """The raw (passed, steps, unreachable) a fault-sweep dossier records — read
    directly for the numeric form and the `unreachable == 0` check 290 adds on
    top of 293's grading (§3.1)."""
    dossier = getattr(bundle, "fault_sweep", None) if bundle else None
    counts = (dossier or {}).get("counts") or {} if isinstance(dossier, dict) else {}
    return (int(counts.get("passed") or 0), int(counts.get("steps") or 0),
            int(counts.get("unreachable") or 0))


def _clause_verdict(facet: str, threshold: str, assessment, bundle,
                    *, key_present: bool, standing: str,
                    att_detail: str) -> ClauseVerdict:
    """Evaluate one `<facet> <threshold>` clause against the graded assessment
    and the raw dossier counts. A hard predicate: missing evidence
    (`unavailable`) fails, no partial credit, fail-closed (§2)."""
    status = (assessment.facets.get(facet) if assessment else None) or "unavailable"

    if facet == "fault-sweep":
        passed_n, steps_n, unreachable_n = _sweep_counts(bundle)
        fact = (f"{passed_n}/{steps_n} {status}" if bundle
                and getattr(bundle, "fault_sweep", None) is not None
                else "unavailable")
        if unreachable_n:
            fact += f" ({unreachable_n} unreachable)"
        if threshold == "full":
            ok = status == "full" and unreachable_n == 0
        else:                                   # "N/N"
            floor = int(threshold.split("/")[0])
            ok = (steps_n >= floor and passed_n == steps_n and steps_n > 0
                  and unreachable_n == 0)
        detail = ("some swept effects were unreachable to the scheme"
                  if unreachable_n and status == "full" else "")
        return ClauseVerdict(facet, threshold, fact, ok, standing, detail)

    if facet == "attestation":
        if threshold == "valid":
            if status == "present" and not key_present:
                return ClauseVerdict(
                    facet, threshold, "present (unverified)", False, "self-attested",
                    "cannot verify is not valid — pass a verification key (--key)")
            ok = status == "valid"
            return ClauseVerdict(facet, threshold, status, ok,
                                 "verified" if ok else standing, att_detail)
        # "present"
        ok = status in ("valid", "present")
        return ClauseVerdict(facet, threshold, status, ok,
                             "verified" if status == "valid" else standing,
                             att_detail if not ok else "")

    if facet == "publisher":
        ok = status == "trusted"
        return ClauseVerdict(facet, threshold, status, ok, "verified", "")

    if facet == "inverse-roundtrip":
        ok = status == "pass"
        return ClauseVerdict(facet, threshold, status, ok, standing, "")

    if facet == "gauntlet":
        ok = (status == "admissible" if threshold == "admissible"
              else status in ("admissible", "present"))
        return ClauseVerdict(facet, threshold, status, ok, standing, "")

    if facet == "capabilities":
        ok = status == "present"
        return ClauseVerdict(facet, threshold, status, ok, "verified", "")

    return ClauseVerdict(facet, threshold, status, False, standing, "")


def _recompute_component(ir: dict, name: str, published,
                         *, gauntlet_dossier) -> tuple:
    """Item 290 §4, slice 3 (`--recompute`): run the operator's OWN local
    producers against the component in hand and return
    ``(bundle, recomputed_facets)``.

    The producers are the shipped entry points, reused verbatim (never
    re-derived here): `fault.sweep_dossier` (the per-component fault sweep),
    `fault.roundtrip_dossier` (the composition's inverse round-trip), and the
    caller-supplied cold `mcp.gauntlet.run` dossier. Operator-run evidence needs
    no attestation root — the operator produced it — so the facets it yields are
    marked `recomputed` (§6.3: rooted by construction). A producer whose runtime
    is absent is honestly skipped (never faked): that facet is left as the
    PUBLISHED bundle carried it and stays out of `recomputed_facets`, so the
    report keeps marking it `published`.
    """
    from . import fault  # noqa: PLC0415 — lazy: the runtime-tested producers
    from . import registry as reg  # noqa: PLC0415
    fields: dict = {}
    recomputed: set = set()
    try:
        fields["fault_sweep"] = fault.sweep_dossier(ir, only=name)
        recomputed.add("fault-sweep")
    except (ModuleNotFoundError, ImportError):
        pass  # cordis-py absent: honestly unavailable, never faked
    try:
        fields["inverse_roundtrip"] = fault.roundtrip_dossier(ir)
        recomputed.add("inverse-roundtrip")
    except (ModuleNotFoundError, ImportError):
        pass
    if gauntlet_dossier is not None:
        fields["gauntlet"] = gauntlet_dossier
        recomputed.add("gauntlet")
    base = published if published is not None else reg.EvidenceBundle()
    return replace(base, **fields), frozenset(recomputed)


def _rule_reports(policy: Policy, audit: dict, mcp_components: frozenset,
                  *, evidence, origins, trusted_publishers, key,
                  evidence_ir, recompute=False, recompute_ir=None,
                  recompute_gauntlet=None) -> dict:
    """The single comparison site: every evidence rule against every component,
    every clause verdict (pass and fail). `evaluate` turns failing clauses into
    violations; `revl policy evaluate` renders the whole thing. Register rules
    are reported alongside (slice 1: a `declared` floor is met by every selected
    component; higher floors are parse-rejected until 309's ledger).

    Returns {component: {"evidence": [RuleReport], "registers": [RuleReport]}}.

    Slice 3 (`--recompute`, §4): when `recompute` is set, the operator's OWN
    local producers are run against each component in `recompute_ir` (plus the
    caller-supplied cold gauntlet dossier `recompute_gauntlet`) and the facets
    they yield OVERLAY the published bundle before grading. Those facets are
    marked `recomputed` (standing) — operator-run at evaluation time, rooted by
    construction (§6.3); facets no local producer ran stay as the published
    bundle carried them and keep their published standing.
    """
    from . import registry as reg  # noqa: PLC0415 — lazy, pulls the compiler
    evidence = evidence or {}
    key_bytes = bytes(key) if key is not None else None
    trusted = frozenset(trusted_publishers or ())
    manifest = audit.get("manifest") or {}
    # item 309/290 §3.2: the per-capability-token register floor input.
    cap_registers = audit.get("capability_registers") or {}
    out: dict = {}
    for name in _components(audit):
        realms = component_realms(manifest, name)
        reach_tokens = frozenset(r.token for r in component_reach(audit, name))
        origin = _component_origin(audit, origins, name)
        bundle = evidence.get(name)
        recomputed_facets: frozenset = frozenset()
        if recompute and recompute_ir is not None and name in recompute_ir:
            bundle, recomputed_facets = _recompute_component(
                recompute_ir[name], name, bundle,
                gauntlet_dossier=recompute_gauntlet)
        assessment = None
        att_detail = ""
        ev_reports: list[RuleReport] = []
        reg_reports: list[RuleReport] = []
        for rule in policy.evidence_rules:
            selected = _evidence_rule_selects(
                rule, name, realms, reach_tokens, origin, mcp_components)
            if not selected:
                ev_reports.append(RuleReport(rule.scope, rule.selector,
                                             rule.origin, rule.self_attested,
                                             False, ()))
                continue
            if assessment is None:
                b = bundle if bundle is not None else reg.EvidenceBundle()
                assessment = reg.assess_evidence(
                    b, key=key_bytes, ir=(evidence_ir or {}).get(name),
                    trusted_publishers=trusted)
                mismatch = reg.binding_mismatch(getattr(b, "attestation", None), b)
                if mismatch is not None:
                    att_detail = (f"dossier `{mismatch}` does not hash to the "
                                  f"signed binding — forged or copied")
                elif assessment.facets.get("attestation") == "invalid":
                    att_detail = "attestation signature does not verify"
            has_valid_attest = any(
                f == "attestation" and t == "valid" for f, t in rule.require)
            clauses = []
            for facet, threshold in rule.require:
                if facet in recomputed_facets:
                    # §4/§6.3: run locally against the component in hand, so
                    # rooted in the operator's own run — marked `recomputed`.
                    standing = "recomputed"
                elif facet in _SELF_ATTESTING_FACETS:
                    if facet == "gauntlet" and rule.scope == "mcp":
                        standing = "operator-run"
                    elif (has_valid_attest
                          and assessment.facets.get("attestation") == "valid"):
                        standing = "verified"
                    else:
                        standing = "self-attested"
                else:
                    standing = "verified"
                clauses.append(_clause_verdict(
                    facet, threshold, assessment,
                    bundle if bundle is not None else reg.EvidenceBundle(),
                    key_present=key_bytes is not None, standing=standing,
                    att_detail=att_detail))
            ev_reports.append(RuleReport(rule.scope, rule.selector, rule.origin,
                                         rule.self_attested, True, tuple(clauses)))
        for rrule in policy.register_rules:
            # item 309 §3.2: read the per-token register off the audit's
            # `capability_registers` map (built from the IR's declared registers,
            # WEAKEST-wins per token) and refuse a selected token whose register
            # is below the floor, under 309's order (`declared < keyed < read`;
            # `strong` names any register above the trust-me floor).
            matched = [tok for tok in reach_tokens
                       if tok != UNBOUNDED and fnmatchcase(tok, rrule.capability)]
            selected = bool(matched)
            clauses = tuple(
                _register_clause(tok, cap_registers.get(tok), rrule.at_least)
                for tok in sorted(matched))
            reg_reports.append(RuleReport("capability", rrule.capability, None,
                                          False, selected, clauses))
        out[name] = {"evidence": ev_reports, "registers": reg_reports}
    return out


def _evidence_violations(policy: Policy, reports: dict, manifest: dict,
                         profile=None) -> list[Violation]:
    """Turn every failing evidence clause into a refuse-only `Violation`."""
    violations: list[Violation] = []
    for name, entry in reports.items():
        for report in entry["evidence"]:
            if not report.selected:
                continue
            for clause in report.failed():
                violations.append(
                    _evidence_violation(manifest, name, report, clause, profile))
    return violations


def _teardown_violations(policy: Policy, audit: dict, manifest: dict) \
        -> list[Violation]:
    """Refuse a composition whose recovery surface contains an entry below the
    strongest `requires idempotent-teardown` floor (item 309 §"question 4",
    point 3). Document-global: a single below-floor inverse/emission fails
    admission, so unattended recovery auto-replays only what the floor permits."""
    from .lower import _REGISTER_RANK, _register_satisfies  # noqa: PLC0415
    floor = max((r.strength for r in policy.teardown_rules),
                key=lambda s: _REGISTER_RANK.get(s, 0))
    violations: list[Violation] = []
    for entry in audit.get("recovery_surface") or []:
        register = entry.get("register")
        if _register_satisfies(register, floor):
            continue
        name = entry.get("name")
        kind = entry.get("kind")
        fact = register or "unregistered (fenced)"
        message = (
            f"policy violation: {kind} `{name}` has register `{fact}`, below the "
            f"required `idempotent-teardown` strength `{floor}` — admission "
            f"refused (unattended-recovery floor, item 309). Declare `undo "
            f"idempotent` or add an `idempotent(key: ...)` so recovery can "
            f"auto-replay it")
        steps = [
            TraceStep(name, "extern", None, None,
                      f"{kind}: register `{fact}`"),
            TraceStep(f"idempotent-teardown {floor}", "teardown", None, None,
                      "required strength floor", (name,)),
        ]
        why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                       steps=steps)
        violations.append(Violation("teardown", name, name, message, why))
    return violations


def _register_violations(policy: Policy, reports: dict, manifest: dict) \
        -> list[Violation]:
    """Turn every failing register clause into a refuse-only `Violation` (item
    309/290 §3.2). A `capability <glob> requires register <level>` floor refuses
    a selected capability token whose declaration register is below the floor
    under 309's partial order."""
    violations: list[Violation] = []
    for name, entry in reports.items():
        for report in entry["registers"]:
            if not report.selected:
                continue
            for clause in report.failed():
                file, _ = _location(manifest, name)
                token = clause.facet.split(":", 1)[-1]
                message = (
                    f"policy violation: capability `{token}` on `{name}` has "
                    f"register `{clause.fact}`, below the required "
                    f"`{clause.threshold}` floor — admission refused "
                    f"(declaration-strength floor, item 309/290 §3.2). Declare "
                    f"`undo idempotent` or an `idempotent(key: ...)` so the "
                    f"register meets the floor")
                steps = [
                    TraceStep(name, "component", file, None,
                              f"register:{token}: recorded `{clause.fact}`"),
                    TraceStep(f"register {clause.threshold}", "register", None,
                              None, "required floor", (token,)),
                ]
                why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                               steps=steps)
                violations.append(
                    Violation("register", name, token, message, why))
    return violations


def _inert_evidence_selectors(policy: Policy, reports: dict) -> list:
    """Evidence/register rules that select NO component in the audit graph
    (item 249's inert-taint precedent, §7). Returned for the report body and
    JSON, never a silent nothing."""
    selected_ev = set()
    selected_reg = set()
    for entry in reports.values():
        for i, r in enumerate(entry["evidence"]):
            if r.selected:
                selected_ev.add(i)
        for i, r in enumerate(entry["registers"]):
            if r.selected:
                selected_reg.add(i)
    inert = []
    for i, rule in enumerate(policy.evidence_rules):
        if i not in selected_ev:
            inert.append(("evidence", rule))
    for i, rule in enumerate(policy.register_rules):
        if i not in selected_reg:
            inert.append(("register", rule))
    return inert


# ------------------------------- item 290 slice 2: the resolve-side prediction

#: The rule families whose SELECTION is a property of the assembled composition
#: rather than of a candidate standing alone, so `predict_refusals` cannot
#: evaluate them and says so instead of reporting them as "not selected"
#: (§5, "the marker is a courtesy prediction computed by the same evaluator").
#:
#: * `realm` — a realm placement is written by the composition that isolates the
#:   component; a candidate in a registry sits in no realm.
#: * `capability` — an evidence or register rule scoped by capability selects on
#:   the G8 REACH of the linked graph. A registry entry carries only its
#:   publisher's `index.json` claim about its capabilities, and §5's own
#:   assumption list says that claim is not cross-checked unless the registry was
#:   verified. Predicting reach from a claim would be predicting from the one
#:   input the gate refuses to trust.
#: * `mcp` — whether a component is MCP-admitted is a property of the session
#:   that admits it, decided after resolve returns.
_UNPREDICTABLE_SCOPES = frozenset({"realm", "capability", "mcp"})


def unpredicted_rules(policy: Policy) -> list[str]:
    """Every rule in `policy` that `predict_refusals` cannot evaluate about a
    candidate standing alone, rendered as written (item 290 §5).

    This list is what keeps an empty `wouldBeRefused` from reading as an
    approval: these rules refuse on their own, at the gate, on inputs a resolve
    does not have. It is a property of the POLICY, not of any candidate, so it is
    reported once per resolve rather than per candidate."""
    # `_rule_line` renders a rule off a REPORT, so an unevaluated rule is given
    # its own `require` clauses verbatim (`passed` is never read by the renderer)
    # — the line reads exactly as written in the policy file, facets included.
    out = [_rule_line(RuleReport(
               rule.scope, rule.selector, rule.origin, rule.self_attested, False,
               tuple(ClauseVerdict(f, t, "", False, "") for f, t in rule.require)))
           for rule in policy.evidence_rules
           if rule.scope in _UNPREDICTABLE_SCOPES]
    out += [f"capability {rule.capability} requires register {rule.at_least}"
            for rule in policy.register_rules]
    out += [f"requires idempotent-teardown(strength: {rule.strength})"
            for rule in policy.teardown_rules]
    return out


def predict_refusals(policy: Policy, name: str, *,
                     evidence_bundle=None, evidence_ir: dict | None = None,
                     key=None, trusted_publishers=frozenset()) -> dict:
    """Predict which of `policy`'s evidence rules ALREADY refuse `name` on the
    evidence it publishes today — item 290 §5's resolve-side `wouldBeRefused`
    marker, so an agent does not pick a top-ranked candidate the gate bounces.

    Two properties this function exists to hold, and how it holds them:

    **It never refuses.** It returns data and nothing else: no exception on a
    failing clause, no filtering, no reordering. `resolve` attaches the result to
    a candidate it has already ranked, and the ranking is computed before this
    runs and is not read back. The only path from here to a refusal is the gate
    itself, re-evaluating the assembled composition through `evaluate`.

    **Its silence is never an approval.** A prediction is ONE-SIDED: a
    `wouldBeRefused` entry says the gate refuses this candidate on facts that are
    already recorded, and an EMPTY list says only that no component-scoped
    evidence rule refuses it *on those facts*. It cannot say the gate will admit,
    for three separate reasons, all of them reported rather than assumed:

    1. `unpredicted` names every rule this call could not evaluate — the
       `_UNPREDICTABLE_SCOPES` families plus the register and idempotent-teardown
       floors, which read the audit graph's per-token registers and recovery
       surface. Those rules refuse on their own and are not covered here.
    2. The gate evaluates the ASSEMBLED composition. Realms, reach, G2 key
       collisions and the operator's own key/trust set are inputs resolve does
       not have, and every one of them can turn an admit into a refusal.
    3. The evidence graded here is the candidate's PUBLISHED bundle. §4's
       `--recompute` re-derives facets locally, and a recomputed fact may be
       worse than the published one.

    Returns ``{"wouldBeRefused": [...], "unpredicted": [...]}``. Each
    `wouldBeRefused` entry is one FAILING clause: the rule line as written, the
    facet, its threshold, the recorded fact, the clause's standing, and any
    detail (a hash mismatch, a cannot-verify). The verdicts come from
    `_rule_reports`, the same single comparison site the gate and
    `revl policy evaluate` read, so a prediction can never disagree with the
    gate on a fact both of them can see.
    """
    from . import registry as reg  # noqa: PLC0415 — lazy, pulls the compiler

    unpredicted = unpredicted_rules(policy)
    predictable = [r for r in policy.evidence_rules
                   if r.scope not in _UNPREDICTABLE_SCOPES]
    if not predictable:
        return {"wouldBeRefused": [], "unpredicted": unpredicted}

    # The candidate stands alone: one component, admitted from a registry (which
    # is what an origin-scoped `component registry:*` rule selects on), no realm
    # placement and no reach — the three inputs `_UNPREDICTABLE_SCOPES` withholds
    # from the rules that would read them. `capability_registers` is left absent
    # for the same reason, and no register rule is evaluated below.
    stub = Policy(evidence_rules=tuple(predictable))
    audit = {"boundary": {name: {}}, "manifest": {}, "origins": {name: "registry"}}
    reports = _rule_reports(
        stub, audit, frozenset(),
        evidence={name: evidence_bundle if evidence_bundle is not None
                  else reg.EvidenceBundle()},
        origins={name: "registry"},
        trusted_publishers=frozenset(trusted_publishers or ()),
        key=key, evidence_ir={name: evidence_ir} if evidence_ir else {})

    refused = []
    for report in reports[name]["evidence"]:
        if not report.selected:
            continue
        for clause in report.failed():
            refused.append({
                "rule": _rule_line(report),
                "facet": clause.facet,
                "threshold": clause.threshold,
                "fact": clause.fact,
                "standing": clause.standing,
                **({"detail": clause.detail} if clause.detail else {}),
            })
    return {"wouldBeRefused": refused, "unpredicted": unpredicted}


def evaluate(policy: Policy, audit: dict,
             mcp_components: frozenset[str] | set[str] | None = None,
             *, evidence: dict | None = None,
             origins: dict | None = None,
             trusted_publishers=frozenset(), key=None,
             evidence_ir: dict | None = None,
             recompute: bool = False, recompute_ir: dict | None = None,
             recompute_gauntlet: dict | None = None,
             profile=None) \
        -> list[Violation]:
    """Evaluate `policy` against an audit graph — the whole gate decision.

    Returns every `Violation`; an empty list is a clean pass. `mcp_components`
    are the components admitted through the MCP session, to which the sandbox
    (`mcp_allow`) profile applies.

    Item 290 grows the signature with the evidence inputs, all defaulting empty
    so an evidence-free policy evaluates BYTE-IDENTICALLY to before: `evidence`
    is the per-component `{name: EvidenceBundle}`, `origins` the per-component
    admission origin (`registry`/`source`), `trusted_publishers` the operator
    trust set, `key` the attestation verification key, and `evidence_ir` the
    per-component rebuilt IR an `attestation valid` clause verifies against.

    Slice 3 (`--recompute`, §4): with `recompute` set and `recompute_ir` a
    per-component `{name: ir}` map, the operator's own local producers are run
    against each component and OVERLAY the published evidence before grading, so
    the gate and `revl policy evaluate` threshold the freshly recomputed facts;
    `recompute_gauntlet` is the caller-supplied cold gauntlet dossier. All three
    default off, so an ordinary evaluation is byte-identical to before.
    """
    mcp_components = frozenset(mcp_components or ())
    manifest = audit.get("manifest") or {}
    violations: list[Violation] = []

    for name in _components(audit):
        realms = component_realms(manifest, name)
        reach = component_reach(audit, name)
        allow = _allow_for(policy, name, realms)
        denies = _deny_for(policy, name, realms)
        sandbox = policy.mcp_allow if name in mcp_components else None

        for r in reach:
            # deny-lists refuse regardless of any allow (deny wins)
            for rule in denies:
                if _matches_any(r.token, rule.patterns) or \
                        (r.token == UNBOUNDED and UNBOUNDED in rule.patterns):
                    violations.append(
                        _deny_violation(manifest, name, r, rule, profile))
            # a closed component/realm allow-list
            if allow is not None and not _allowed(r.token, allow):
                violations.append(
                    _allow_violation(manifest, name, r, allow, "capability",
                                     profile))
            # the MCP / agent sandbox allow-list
            if sandbox is not None and not _allowed(r.token, sandbox):
                violations.append(
                    _allow_violation(manifest, name, r, sandbox, "mcp-sandbox",
                                     profile))

        # item 249, Slice C: a forbidden taint downgrade. Read the origins this
        # component declassifies off the `declassify:` audit surface and refuse
        # any a matching `may not declassify` rule forbids.
        taint = ((audit.get("boundary") or {}).get(name) or {}).get("taint", {})
        if policy.declassify_rules:
            for origin in taint.get("declassify") or []:
                for rule in policy.declassify_rules:
                    if rule.selects(name, realms) and \
                            _matches_any(origin, rule.patterns):
                        violations.append(
                            _declassify_violation(manifest, name, origin, rule))

        # item 249, Slice C (C2): an endorse under a `capability
        # declassify.<origin> requires approval` rule must carry a covering
        # `Approval[declassify.<origin>]` edge — the third declassifier, on the
        # landed item-246 surface. The endorse's approval edge is recorded on
        # its declassify record (`approved`).
        if policy.approval_rules:
            for record in taint.get("declassify_records") or []:
                origin = record.get("origin")
                token = f"declassify.{origin}"
                rule = policy.approval_rule_for(token)
                if rule is None:
                    continue
                approved = record.get("approved")
                if approved is None or not _approval_edge_covers(approved, token):
                    violations.append(
                        _declassify_approval_violation(manifest, name, origin,
                                                       token))

        # item 249, Slice D (D2): the policy-gated tier. A component that carries
        # `origin` taint to an emission AND reaches a forbidden capability is the
        # exfiltration edge; refuse it unless the flow is approval-covered under a
        # `without approval` rule. Coarse component-scoped (the design's sound
        # fallback), read off the `taint:` tokens and the same reach graph.
        if policy.taint_flow_rules:
            reached = taint.get("reaches") or []
            approvals = taint.get("reach_approvals") or []
            for rule in policy.taint_flow_rules:
                if rule.origin not in reached:
                    continue
                for r in reach:
                    if not _matches_any(r.token, rule.patterns):
                        continue
                    if rule.without_approval and \
                            any(_approval_edge_covers(a, r.token) for a in approvals):
                        continue
                    violations.append(
                        _taint_flow_violation(manifest, name, rule, r))

    if policy.tenants_isolated:
        violations.extend(_tenant_violations(audit, manifest))

    # item 309: the unattended-recovery floor over the recovery surface.
    if policy.teardown_rules:
        violations.extend(_teardown_violations(policy, audit, manifest))

    # item 290: the confidence/evidence admission rules. Refuse-only and
    # additive — a policy with no evidence/register rules skips this entirely and
    # the result is byte-identical to before. The reports are the single
    # comparison site `revl policy evaluate` also reads (§7).
    if policy.evidence_rules or policy.register_rules:
        reports = _rule_reports(
            policy, audit, mcp_components, evidence=evidence, origins=origins,
            trusted_publishers=trusted_publishers, key=key,
            evidence_ir=evidence_ir, recompute=recompute,
            recompute_ir=recompute_ir, recompute_gauntlet=recompute_gauntlet)
        violations.extend(_evidence_violations(policy, reports, manifest,
                                               profile))
        violations.extend(_register_violations(policy, reports, manifest))

    # item 249, Finding 3: a taint policy rule over an audit with NO taint surface
    # is inert — the derived-source walk is off (an unannotated, no-profile
    # program), so the rule mints nothing and matches nothing. Warn loudly so an
    # operator is not lulled by a rule that protects nothing.
    _warn_if_taint_rules_are_inert(policy, audit)
    return violations


def _warn_if_taint_rules_are_inert(policy: Policy, audit: dict) -> None:
    if not (policy.taint_flow_rules or policy.declassify_rules):
        return
    for stats in (audit.get("boundary") or {}).values():
        taint = stats.get("taint") or {}
        if taint.get("reaches") or taint.get("declassify"):
            return  # a real taint surface exists; the rules can match
    origins = sorted(
        {rule.origin for rule in policy.taint_flow_rules}
        | {pat for rule in policy.declassify_rules for pat in rule.patterns})
    warnings.warn(
        "taint policy rule(s) naming ["
        + ", ".join(origins)
        + "] match nothing: this composition carries no taint surface, so the "
          "rule protects nothing. Derived taint sources are OFF unless the "
          "program is annotated (`Untrusted[T]`) or compiled with `--taint-strict` "
          "(the untrusted-author profile turns it on). An inert rule is not a "
          "protecting one (item 249).",
        InertTaintPolicyWarning, stacklevel=2)


def _allow_violation(manifest: dict, name: str, reach: Reach,
                     allow: tuple[str, ...], kind: str,
                     profile=None) -> Violation:
    permitted = ", ".join(allow) or "nothing"
    if kind == "mcp-sandbox":
        head = (f"agent-sandbox violation: component `{name}` was admitted "
                f"through the MCP session, whose sandbox permits [{permitted}], "
                f"but it reaches `{reach.token}`")
    else:
        head = (f"policy violation: component `{name}` may reach only "
                f"[{permitted}], but it reaches `{reach.token}`")
    detail = (f"via emission `{reach.via}`" if reach.kind == "emission"
              else "through host code")
    message = f"{head} {detail} — admission refused (boundary policy, item 33)"
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                   steps=_reach_step(manifest, name, reach))
    nav = _reach_navigate(reach, kind, allow, profile)
    return Violation(kind, name, reach.token, message, why, navigate=nav)


def _deny_violation(manifest: dict, name: str, reach: Reach,
                    rule: Rule, profile=None) -> Violation:
    where = (f"realm `{rule.selector}`" if rule.scope == "realm"
             else f"components matching `{rule.selector}`")
    detail = (f"via emission `{reach.via}`" if reach.kind == "emission"
              else "through host code")
    message = (f"policy violation: {where} may not reach "
               f"[{', '.join(rule.patterns)}], but `{name}` reaches "
               f"`{reach.token}` {detail} — admission refused "
               f"(boundary policy, item 33)")
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                   steps=_reach_step(manifest, name, reach))
    nav = _deny_navigate(reach, rule, profile)
    return Violation("deny", name, reach.token, message, why, navigate=nav)


# ------------------------------------------------ item 274: navigable refusals
#
# The boundary-policy family's nearest-allowed, derived from the rule that fired
# and the reach that missed — never advisory prose (design §2.2). Every marker
# obeys the HIGH restriction: a reach set is a STATIC fact at the refusal site
# (the audit graph is already computed), so dropping the reach is
# `clears-this-gate`; a policy edit is operator-enacted and therefore always
# `candidate` (the operator decision the compiler does not hold). The record is
# redacted to the single collapsed verdict under the untrusted-author profile.

def _reach_navigate(reach: Reach, kind: str, allow: tuple[str, ...],
                    profile) -> dict:
    """A capability/sandbox allow-list refusal: re-route to an in-policy
    capability, drop the reach, or (operator) extend the allow rule."""
    from . import navigate as nav  # noqa: PLC0415 — lazy, additive
    family = "mcp-sandbox" if kind == "mcp-sandbox" else "policy-capability"
    named_allowed = [t for t in allow if t != UNBOUNDED]
    alts = []
    if named_allowed:
        # re-route: the allowed set is already in hand. Author-enactable, but
        # whether an in-policy capability does the job is a judgment the compiler
        # does not hold, so `candidate`, not `clears`.
        alts.append(nav.alternative(
            enacts=nav.ENACTS_AUTHOR,
            action=(f"re-route to an in-policy capability instead: this "
                    f"component may reach [{', '.join(named_allowed)}]"),
            ref=named_allowed[0]))
    # drop the reach: removing the emission/host edge removes `token` from the
    # reach set, which clears THIS gate by construction (the reach is a static
    # fact at the refusal site — immutable operand, so `clears-this-gate`).
    drop_where = ("the emission" if reach.kind == "emission" else "the host-code")
    alts.append(nav.alternative(
        enacts=nav.ENACTS_AUTHOR,
        action=(f"drop the reach: remove {drop_where} edge that reaches "
                f"`{reach.token}` (the why-trace names it)"),
        ref=reach.token, clears=True))
    # the nearest policy edit: extend the matched allow rule with the token.
    # Operator-enacted (design §2.2), always `candidate` — never predicts
    # approval, and an operator alternative is never author-enactable.
    alts.append(nav.alternative(
        enacts=nav.ENACTS_OPERATOR,
        action=(f"extend the matched `may reach` allow rule with `{reach.token}` "
                f"(a 251-style candidate, referenced not applied)"),
        ref=reach.token))
    return nav.record(family=family,
                      refused={"token": reach.token, "kind": reach.kind,
                               "allowed": list(named_allowed)},
                      blocked=False, alternatives=alts, profile=profile)


def _deny_navigate(reach: Reach, rule: Rule, profile) -> dict:
    """A `may not reach` deny refusal: drop the reach (author), or narrow the
    deny rule (operator). The deny wins over any allow, so there is no re-route
    to an in-policy capability under it."""
    from . import navigate as nav  # noqa: PLC0415 — lazy, additive
    drop_where = ("the emission" if reach.kind == "emission" else "the host-code")
    alts = [
        nav.alternative(
            enacts=nav.ENACTS_AUTHOR,
            action=(f"drop the reach: remove {drop_where} edge that reaches "
                    f"`{reach.token}` (the why-trace names it)"),
            ref=reach.token, clears=True),
        nav.alternative(
            enacts=nav.ENACTS_OPERATOR,
            action=(f"narrow the `may not reach [{', '.join(rule.patterns)}]` "
                    f"deny rule so `{reach.token}` is no longer matched "
                    f"(referenced, not applied)"),
            ref=reach.token),
    ]
    return nav.record(family="policy-deny",
                      refused={"token": reach.token, "kind": reach.kind,
                               "denied": list(rule.patterns)},
                      blocked=False, alternatives=alts, profile=profile)


def _declassify_violation(manifest: dict, name: str, origin: str,
                          rule: Rule) -> Violation:
    """A component declassifies an origin a `may not declassify` rule forbids
    (item 249, Slice C)."""
    where = (f"realm `{rule.selector}`" if rule.scope == "realm"
             else f"components matching `{rule.selector}`")
    file, _ = _location(manifest, name)
    message = (f"policy violation: {where} may not declassify "
               f"[{', '.join(rule.patterns)}], but `{name}` declassifies "
               f"`{origin}` taint — the downgrade is forbidden; admission "
               f"refused (boundary policy, item 249 Slice C)")
    steps = [
        TraceStep(name, "component", file, None,
                  f"declassifies `{origin}` taint (an `endorse[{origin}]`)"),
        TraceStep(origin, "declassify", None, None, "forbidden downgrade",
                  (origin,)),
    ]
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                   steps=steps)
    return Violation("declassify", name, origin, message, why)


def _taint_flow_violation(manifest: dict, name: str, rule: TaintFlowRule,
                          reach: Reach) -> Violation:
    """A component routes `origin` taint into a capability a taint-flow rule
    forbids (item 249, Slice D). The exfiltration edge, named with its chain."""
    file, _ = _location(manifest, name)
    approval = (" without approval" if rule.without_approval else "")
    hint_tail = (" (acquire an approval and thread it on the send: `emit … with a`)"
                 if rule.without_approval else "")
    message = (f"policy violation: `{rule.origin}`-taint may not reach "
               f"[{', '.join(rule.patterns)}]{approval}, but `{name}` carries "
               f"`{rule.origin}`-origin data to `{reach.token}` — the flow is "
               f"forbidden; admission refused (boundary policy, item 249 Slice "
               f"D){hint_tail}")
    steps = [
        TraceStep(name, "component", file, None,
                  f"carries `{rule.origin}`-origin taint to an emission"),
        TraceStep(reach.token, reach.kind, None, None, "forbidden taint flow",
                  (reach.token,)),
    ]
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN, steps=steps)
    return Violation("taint-flow", name, reach.token, message, why)


def _evidence_subject_phrase(report: "RuleReport") -> str:
    if report.scope == "mcp":
        return "MCP-admitted components"
    if report.scope == "realm":
        return f"realm `{report.selector}`"
    if report.scope == "capability":
        return f"components reaching `{report.selector}`"
    if report.origin == "registry":
        return f"registry-resolved components matching `{report.selector}`"
    return f"components matching `{report.selector}`"


# item 274, design §2.6: the closed facet registry, one producer per facet. The
# refusal names the producer that RECORDS the missing fact; it never predicts the
# outcome (a re-run may still fail). Off-table facets fall back to a generic
# producer phrase (`--recompute` for a stale-standing fact).
_EVIDENCE_PRODUCER = {
    "fault-sweep": "the fault gauntlet run that records the sweep dossier",
    "attestation": "the attestation registration for this component",
    "publisher": "publishing this component under a trusted publisher",
    "register": "the registration that records the fact",
}


def _evidence_producer(facet: str) -> str:
    return _EVIDENCE_PRODUCER.get(
        facet, f"the `{facet}` producer (or `--recompute` when stale-standing)")


def _evidence_violation(manifest: dict, name: str, report: "RuleReport",
                        clause: "ClauseVerdict", profile=None) -> Violation:
    """A component fails an evidence threshold a rule requires (item 290). The
    why-trace is a CHAIN: component -> the failing facet with its recorded fact
    -> the rule's threshold (§3.4)."""
    file, _ = _location(manifest, name)
    where = _evidence_subject_phrase(report)
    tail = (f" ({clause.detail})" if clause.detail else "")
    message = (f"policy violation: {where} require evidence "
               f"[{clause.facet} {clause.threshold}], but `{name}` "
               f"{clause.facet} is `{clause.fact}` — admission refused "
               f"(confidence/evidence admission, item 290){tail}")
    steps = [
        TraceStep(name, "component", file, None,
                  f"{clause.facet}: recorded `{clause.fact}` "
                  f"({clause.standing})"),
        TraceStep(f"{clause.facet} {clause.threshold}", "evidence", None, None,
                  "required threshold", (clause.facet,)),
    ]
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                   steps=steps)
    from . import navigate as nav  # noqa: PLC0415 — lazy, additive
    nav_rec = nav.evidence_navigate(
        facet=clause.facet, threshold=clause.threshold, fact=clause.fact,
        producer=_evidence_producer(clause.facet), rule_line=_rule_line(report),
        profile=profile)
    return Violation("evidence", name, clause.facet, message, why,
                     navigate=nav_rec)


def _approval_edge_covers(edge_scope: str, token: str) -> bool:
    """Whether an endorse's threaded `Approval[edge_scope]` covers a required
    `declassify.<origin>` token — the same glob/equality rule the emit approval
    coverage uses."""
    return fnmatchcase(token, edge_scope) or edge_scope == token


def _declassify_approval_violation(manifest: dict, name: str, origin: str,
                                   token: str) -> Violation:
    file, _ = _location(manifest, name)
    message = (f"policy violation: capability `{token}` requires approval, but "
               f"component `{name}` endorses `{origin}` taint with no covering "
               f"`with` approval edge — admission refused (item 249 Slice C, on "
               f"the item 246 surface). Acquire an approval (`let a = await "
               f"approval[{token}] {{ ... }}`) and thread it "
               f"(`endorse[{origin}](v, reason = \"...\") with a`)")
    steps = [
        TraceStep(name, "component", file, None,
                  f"endorses `{origin}` taint without approval"),
        TraceStep(token, "declassify", None, None, "approval-required downgrade",
                  (token,)),
    ]
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                   steps=steps)
    return Violation("declassify-approval", name, token, message, why)


def _tenant_violations(audit: dict, manifest: dict) -> list[Violation]:
    """`tenants never reach each other`: two components in *different* realms
    that reach a common named boundary. Their isolation is not real — one
    tenant's world touches a boundary the other's does too.

    A pure set operation: partition the reach of each realm-bearing component,
    then any named token in two disjoint realms' reach is a cross-tenant leak.
    `*` is excluded — an unnameable reach is caught by the allow-lists, and it
    would pair every tenant with every other for no actionable reason.
    """
    tenants: list[tuple[str, frozenset[str], set[str]]] = []
    for name in _components(audit):
        realms = component_realms(manifest, name)
        if not realms:
            continue  # shared-realm component is not a tenant
        tokens = {r.token for r in component_reach(audit, name)
                  if r.token != UNBOUNDED}
        tenants.append((name, realms, tokens))

    seen: set[tuple[str, str, str]] = set()
    out: list[Violation] = []
    for i in range(len(tenants)):
        a_name, a_realms, a_tokens = tenants[i]
        for j in range(i + 1, len(tenants)):
            b_name, b_realms, b_tokens = tenants[j]
            if a_realms & b_realms:
                continue  # co-tenant (share a realm) — not a cross-tenant pair
            shared = a_tokens & b_tokens
            for token in sorted(shared):
                key = tuple(sorted((a_name, b_name))) + (token,)
                if key in seen:
                    continue
                seen.add(key)
                out.append(_tenant_violation(manifest, a_name, a_realms,
                                             b_name, b_realms, token))
    return out


def _tenant_violation(manifest: dict, a: str, a_realms: frozenset[str],
                      b: str, b_realms: frozenset[str], token: str) -> Violation:
    a_r = ", ".join(sorted(a_realms))
    b_r = ", ".join(sorted(b_realms))
    message = (f"policy violation: tenants never reach each other, but `{a}` "
               f"(realm {a_r}) and `{b}` (realm {b_r}) both reach `{token}` "
               f"— their isolation is not real; admission refused "
               f"(boundary policy, item 33)")
    fa, _ = _location(manifest, a)
    fb, _ = _location(manifest, b)
    steps = [
        TraceStep(a, "component", fa, None, f"realm {a_r}, reaches `{token}`"),
        TraceStep(token, "boundary", None, None, "shared across tenants",
                  (token,)),
        TraceStep(b, "component", fb, None, f"realm {b_r}, reaches `{token}`"),
    ]
    why = WhyTrace(kind="cross-tenant-reach", subject=token, shape=SET,
                   steps=steps)
    return Violation("tenant", a, token, message, why)


# ------------------------------------------------- item 246: approval admission

def _component_approval_edges(comp: dict) -> set:
    """The set of `Approval[C']` scopes a component threads — the `with e`
    capability recorded on each of its `emit` steps by lowering. A crossing to a
    required capability is covered iff one of these scopes covers it."""
    scopes: set = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("step") == "emit" and isinstance(node.get("approval"), dict):
                cap = node["approval"].get("capability")
                if cap:
                    scopes.add(cap)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(comp.get("body") or [])
    return scopes


def approval_admission(policy: Policy, ir: dict, profile=None) -> list[Violation]:
    """Refuse admission for a component that reaches a POLICY-approval-required
    capability with no covering `with` edge (item 246, Slice 2, Decision 3 rule
    2). Evaluated at admission over the audit graph, the same place the sandbox
    refuses, before any runtime is touched. Operator-owned: the requirement lives
    in the policy, so an author cannot waive it by omission.

    v1 is component-scoped (the design's sound coarser fallback): a component
    reaching required `C` with NO covering edge anywhere is refused; the runtime
    frame check is the per-crossing defense-in-depth. An unnameable `*` reach is
    NOT subject to this rule — a `*` crossing can never be approved into and
    receives only the per-call ticket (Decision 1, the `*` row)."""
    if not policy.approval_rules:
        return []
    from .audit_diff import audit_report  # noqa: PLC0415 — lazy, no cordis
    audit = audit_report(ir)
    manifest = audit.get("manifest") or {}
    violations: list[Violation] = []
    by_name = {c["name"]: c for c in ir.get("components") or []}
    for name in _components(audit):
        comp = by_name.get(name)
        if comp is None:
            continue
        edges = _component_approval_edges(comp)
        for reach in component_reach(audit, name):
            token = reach.token
            if token == UNBOUNDED:
                continue
            rule = policy.approval_rule_for(token)
            if rule is None:
                continue
            if any(fnmatchcase(token, scope) or scope == token for scope in edges):
                continue
            violations.append(
                _approval_violation(manifest, name, reach, rule, profile))
    return violations


def _approval_violation(manifest: dict, name: str, reach: Reach,
                        rule: ApprovalRule, profile=None) -> Violation:
    detail = (f"via emission `{reach.via}`" if reach.kind == "emission"
              else "through host code")
    message = (f"policy violation: capability `{reach.token}` requires approval, "
               f"but component `{name}` reaches it {detail} with no `with` "
               f"approval edge — admission refused (item 246, unreachable-"
               f"without). Acquire an approval (`let a = await approval"
               f"[{reach.token}] {{ ... }}`) and thread it (`emit … with a`)")
    why = WhyTrace(kind="policy-authority", subject=name, shape=CHAIN,
                   steps=_reach_step(manifest, name, reach))
    from . import navigate as nav  # noqa: PLC0415 — lazy, additive
    # the grant ledger is a RUNTIME surface (mcp/session), not held here at
    # admission; a covering standing grant, when a caller threads one, is always
    # a `candidate`/`live` alternative (TOCTOU, the HIGH fix), never a promise.
    nav_rec = nav.approval_navigate(
        token=reach.token, ttl_ms=rule.ttl_ms if rule is not None else None,
        profile=profile)
    return Violation("approval", name, reach.token, message, why,
                     navigate=nav_rec)


# ------------------------------------------------------------- enforcement

def first_error(violations: list[Violation]) -> RevlError | None:
    """The first violation as a raisable `RevlError` (admission is all-or-
    nothing) — with the why-trace attached, like every other §5 refusal."""
    if not violations:
        return None
    v = violations[0]
    file = v.why.steps[0].file if v.why.steps else None
    hint = ("state what a component may reach in the policy file; a boundary "
            "outside its allow-list, a denied one, or a cross-tenant reach "
            "refuses admission (boundary policy, docs/boundary-policy.md)")
    # item 274: thread the violation's navigable-refusal map onto the raised
    # error so `classify()`/`--json` carry it (additive; None for a violation
    # that built none, so every existing surface is byte-identical).
    return RevlError(file, None, v.message, hint=hint, why=v.why,
                     navigate=v.navigate)


def enforce(policy: Policy, audit: dict,
            mcp_components: frozenset[str] | set[str] | None = None,
            *, profile=None) -> None:
    """Evaluate and raise on the first violation. Admission calls this; a
    clean policy returns silently. `profile` (item 274) is the untrusted-author
    admission profile, threaded so the navigable-refusal map is redacted to the
    collapsed verdict for an untrusted author."""
    error = first_error(evaluate(policy, audit, mcp_components, profile=profile))
    if error is not None:
        raise error


def _rule_line(report: "RuleReport") -> str:
    facets = ", ".join(f"{c.facet} {c.threshold}" for c in report.clauses) \
        if report.clauses else ""
    prefix = {"mcp": "mcp", "realm": f"realm {report.selector}",
              "capability": f"capability {report.selector}"}.get(
                  report.scope,
                  (f"component registry:{report.selector}"
                   if report.origin == "registry"
                   else f"component {report.selector}"))
    tail = f" [{facets}]" if facets else ""
    sa = " self-attested" if report.self_attested else ""
    return f"{prefix} requires evidence{tail}{sa}"


def explain(policy: Policy, audit: dict,
            mcp_components: frozenset[str] | set[str] | None = None,
            *, evidence: dict | None = None, origins: dict | None = None,
            trusted_publishers=frozenset(), key=None,
            evidence_ir: dict | None = None,
            recompute: bool = False, recompute_ir: dict | None = None,
            recompute_gauntlet: dict | None = None,
            component: str | None = None) -> dict:
    """The `revl policy evaluate` dry-run: run the SAME `evaluate` (the gate's
    refusal set is authoritative), plus the per-clause reports for the explain
    body. Returns a JSON-able dict; `render_explain` prints it. Nothing is
    admitted, refused, or mutated (§7).

    With `recompute` (slice 3, §4), the same recompute inputs feed BOTH the
    authoritative `evaluate` and the report, so the recomputed facts the report
    shows are exactly the facts the verdict was reached on (one comparison
    site)."""
    mcp_components = frozenset(mcp_components or ())
    violations = evaluate(
        policy, audit, mcp_components, evidence=evidence, origins=origins,
        trusted_publishers=trusted_publishers, key=key, evidence_ir=evidence_ir,
        recompute=recompute, recompute_ir=recompute_ir,
        recompute_gauntlet=recompute_gauntlet)
    reports = _rule_reports(
        policy, audit, mcp_components, evidence=evidence, origins=origins,
        trusted_publishers=trusted_publishers, key=key,
        evidence_ir=evidence_ir, recompute=recompute,
        recompute_ir=recompute_ir,
        recompute_gauntlet=recompute_gauntlet) if (
            policy.evidence_rules or policy.register_rules) else {}
    refused = {v.component for v in violations}
    inert = _inert_evidence_selectors(policy, reports) if reports else []
    comps = []
    for name in _components(audit):
        if component and name != component:
            continue
        entry = reports.get(name, {"evidence": [], "registers": []})
        rule_objs = []
        n_selected_clauses = 0
        n_selecting = 0
        for r in entry["evidence"]:
            clause_objs = [
                {"facet": c.facet, "threshold": c.threshold, "fact": c.fact,
                 "pass": c.passed, "standing": c.standing,
                 **({"detail": c.detail} if c.detail else {})}
                for c in r.clauses]
            if r.selected:
                n_selecting += 1
                n_selected_clauses += len(r.clauses)
            rule_objs.append({"rule": _rule_line(r), "selected": r.selected,
                              "selfAttested": r.self_attested,
                              "clauses": clause_objs})
        for r in entry["registers"]:
            rule_objs.append(
                {"rule": f"capability {r.selector} requires register "
                         f"{r.clauses[0].threshold if r.clauses else '?'}",
                 "selected": r.selected, "clauses": []})
            if r.selected:
                n_selecting += 1
        refused_here = name in refused
        if refused_here:
            verdict = "would be REFUSED"
        elif n_selecting == 0:
            verdict = "admitted: no evidence rule selects this component"
        else:
            verdict = (f"admitted: all {n_selected_clauses} clause(s) across "
                       f"{n_selecting} selecting rule(s) hold")
        comps.append({"component": name, "origin": _component_origin(
            audit, origins, name), "selected": n_selecting > 0,
            "rules": rule_objs, "refused": refused_here, "verdict": verdict})
    return {
        "policy": policy.source,
        "recomputed": bool(recompute),
        "components": comps,
        "inertSelectors": [
            {"kind": kind,
             "rule": (_rule_line(RuleReport(
                 r.scope, r.selector, r.origin, r.self_attested, False, ()))
                 if kind == "evidence"
                 else f"capability {r.capability} requires register {r.at_least}")}
            for kind, r in inert],
        "refused": bool(violations),
        "violations": [{"kind": v.kind, "component": v.component,
                        "token": v.token, "message": v.message} for v in violations],
    }


def render_explain(result: dict) -> str:
    """Human-readable `revl policy evaluate` report (§7)."""
    lines: list[str] = []
    where = f" ({result['policy']})" if result.get("policy") else ""
    recomputed = (", evidence recomputed locally" if result.get("recomputed")
                  else "")
    lines.append(f"policy evaluate{where}: dry-run, nothing admitted or "
                 f"refused{recomputed}")
    lines.append("")
    for comp in result["components"]:
        origin = f" [origin: {comp['origin']}]" if comp.get("origin") else ""
        lines.append(f"component {comp['component']}{origin}")
        selecting = [r for r in comp["rules"] if r["selected"]]
        if not selecting:
            lines.append("  (no evidence rule selects this component)")
        for r in comp["rules"]:
            if not r["selected"]:
                continue
            sa = "  [self-attested]" if r.get("selfAttested") else ""
            lines.append(f"  rule: {r['rule']}{sa}")
            for c in r["clauses"]:
                glyph = "PASS" if c["pass"] else "FAIL"
                detail = f"   {c['detail']}" if c.get("detail") else ""
                lines.append(
                    f"    {c['facet']:<18}{c['fact']:<24}"
                    f"required {c['threshold']:<10}({c['standing']}) "
                    f"{glyph}{detail}")
        lines.append(f"  verdict: {comp['verdict']}")
        lines.append("")
    for inert in result["inertSelectors"]:
        lines.append(f"inert selector ({inert['kind']}): `{inert['rule']}` "
                     f"selects no component in this composition — it requires "
                     f"nothing (item 290, §7; item 249 inert-taint precedent)")
    if result["inertSelectors"]:
        lines.append("")
    verdict = ("REFUSED — at least one component would be refused"
               if result["refused"]
               else "clean — every selected component clears its thresholds")
    lines.append(f"gate verdict: {verdict}")
    return "\n".join(lines).rstrip()


def render_report(policy: Policy, violations: list[Violation]) -> str:
    """Human-readable gate report for the CLI."""
    if not violations:
        where = f" ({policy.source})" if policy.source else ""
        return (f"boundary policy{where}: clean — every component's reach is "
                f"within its declared authority.")
    lines = [f"boundary policy: {len(violations)} violation(s) — admission "
             f"REFUSED:", ""]
    for v in violations:
        lines.append(v.render())
        lines.append("")
    return "\n".join(lines).rstrip()
