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
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .errors import RevlError
from .why import CHAIN, SET, TraceStep, WhyTrace

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


@dataclass(frozen=True)
class Policy:
    """A parsed boundary policy: rules, the tenants switch, the sandbox."""
    rules: tuple[Rule, ...] = ()
    tenants_isolated: bool = False           # `tenants never reach each other`
    mcp_allow: tuple[str, ...] | None = None  # the agent-sandbox allow-list
    leases_enforced: bool = False            # `leases enforced` (item 61)
    quarantine_required: bool = False        # `quarantine required` (item 45)
    source: str | None = None                # file path, for messages

    def is_empty(self) -> bool:
        return not self.rules and not self.tenants_isolated \
            and self.mcp_allow is None and not self.leases_enforced \
            and not self.quarantine_required


# ------------------------------------------------------------------- parsing

class PolicyError(RevlError):
    """A malformed policy file (a parse error, not a violation)."""


def _split_caps(text: str) -> tuple[str, ...]:
    caps = tuple(part.strip() for part in text.split(",") if part.strip())
    return caps


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
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low == "tenants never reach each other":
            tenants = True
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
    return Policy(tuple(rules), tenants, mcp_allow, leases_enforced,
                  quarantine_required, source)


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
    return Policy(tuple(rules), tenants, mcp_allow, leases_enforced,
                  quarantine_required, source)


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
    via: str                   # the emission label / "host code" it came through
    kind: str                  # "emission" | "host"


def component_reach(audit: dict, name: str) -> list[Reach]:
    """The set of boundaries a component reaches, off the G8 audit graph.

    Two sources, both already enumerated by `revl audit`:
      * emission capabilities — the scope each emission call site may cross;
      * reached host externs — host code the component's body reaches.
    A `*` on either side is the unnameable boundary, carried through verbatim.
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
        add(ext.get("name"), "host code", "host")
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
    component: str
    token: str
    message: str
    why: WhyTrace

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
    detail = (f"reaches `{reach.token}` via emission `{reach.via}`"
              if reach.kind == "emission"
              else f"reaches host code `{reach.token}`")
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


def evaluate(policy: Policy, audit: dict,
             mcp_components: frozenset[str] | set[str] | None = None) \
        -> list[Violation]:
    """Evaluate `policy` against an audit graph — the whole gate decision.

    Returns every `Violation`; an empty list is a clean pass. `mcp_components`
    are the components admitted through the MCP session, to which the sandbox
    (`mcp_allow`) profile applies.
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
                    violations.append(_deny_violation(manifest, name, r, rule))
            # a closed component/realm allow-list
            if allow is not None and not _allowed(r.token, allow):
                violations.append(
                    _allow_violation(manifest, name, r, allow, "capability"))
            # the MCP / agent sandbox allow-list
            if sandbox is not None and not _allowed(r.token, sandbox):
                violations.append(
                    _allow_violation(manifest, name, r, sandbox, "mcp-sandbox"))

    if policy.tenants_isolated:
        violations.extend(_tenant_violations(audit, manifest))
    return violations


def _allow_violation(manifest: dict, name: str, reach: Reach,
                     allow: tuple[str, ...], kind: str) -> Violation:
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
    return Violation(kind, name, reach.token, message, why)


def _deny_violation(manifest: dict, name: str, reach: Reach,
                    rule: Rule) -> Violation:
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
    return Violation("deny", name, reach.token, message, why)


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
    return RevlError(file, None, v.message, hint=hint, why=v.why)


def enforce(policy: Policy, audit: dict,
            mcp_components: frozenset[str] | set[str] | None = None) -> None:
    """Evaluate and raise on the first violation. Admission calls this; a
    clean policy returns silently."""
    error = first_error(evaluate(policy, audit, mcp_components))
    if error is not None:
        raise error


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
