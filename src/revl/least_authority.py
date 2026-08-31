"""The wasm least-authority chain (roadmap item 289).

    host imports  subset-of  declared caps  subset-of  policy-allowed

For a wasm component every one of those three sets is statically decidable:

  * the *host imports* are literally the emitted module's import section (a
    coeffect/route channel per required key the body reached);
  * the *declared caps* are the G8 boundary reach (the same capability tokens
    `revl audit` enumerates, docs/capabilities.md);
  * the *policy-allowed* set is the item-33 boundary allow-list.

A `@py`/`@ts` host body is G8-opaque, so its host calls are not enumerable and
this full chain is enforceable only at the wasm tier -- which is exactly why
the check lives against the wasm emitter. The wasm cell's import section is
GENERATED from the reached requires (docs/design/411-sandbox-placement.md),
so an ungranted reach is a missing import refused by the substrate itself.

The two legs reuse the subset machinery already in the tree rather than a
third copy of it: `admission._caps_widen` (None = top, `*` a distinct
unnameable token) decides `import subset-of declared`, and the predicate
`policy.evaluate` already uses (`policy._allowed`) decides
`declared subset-of policy`. This module only composes them over the emitted
modules and names which leg failed, the offending capability, and the
component.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .admission import _caps_widen
from .audit_diff import audit_report
from .errors import RevlError
from .policy import (
    Policy,
    _allow_for,
    _allowed,
    component_realms,
    component_reach,
)
from .why import CHAIN, TraceStep, WhyTrace

# A wasm host import names a capability iff its module is a coeffect/route
# channel for a required key. Everything else in the import section is ABI
# scaffolding -- the async job pump (`host.job_run`), the config/spawn/dispose/
# instance-accessor seams, and the durable-WAL framing channel -- and grants no
# named host authority, so it is not a capability for this gate.
_STRUCTURAL_COEFFECTS = frozenset({"revl:wal"})

_IMPORT_MODULE = re.compile(r'\(import "([^"]+)"')


def wasm_import_capabilities(wat: str) -> set[str]:
    """The capability tokens a wasm module's import section actually names.

    A required-key crossing lowers to ``(import "coeffect:<key>" ...)`` or, for
    a routed key, ``(import "route:<key>" ...)``; the capability is the KEY --
    the same token the G8 reach and the policy allow-list use -- so a
    realm-scoped ``coeffect:<realm>/<key>`` still contributes ``<key>``. The
    structural imports above are excluded.
    """
    caps: set[str] = set()
    for module in _IMPORT_MODULE.findall(wat):
        if module.startswith("coeffect:"):
            rest = module[len("coeffect:") :]
            if rest in _STRUCTURAL_COEFFECTS:
                continue
            caps.add(rest.rsplit("/", 1)[-1])
        elif module.startswith("route:"):
            caps.add(module[len("route:") :].rsplit("/", 1)[-1])
    return caps


@dataclass(frozen=True)
class LeastAuthorityBreach:
    """One way a wasm component breaches the least-authority chain."""

    leg: str  # "import>declared" | "declared>policy"
    component: str
    capabilities: tuple[str, ...]  # the offending capability tokens

    def _named(self) -> str:
        return ", ".join(f"`{c}`" for c in self.capabilities)

    def message(self) -> str:
        if self.leg == "import>declared":
            return (
                f"least-authority (289): wasm component `{self.component}` "
                f"imports host {_plural('capability', self.capabilities)} "
                f"{self._named()} it does not declare -- the emitted module's "
                f"import section exceeds its declared/reached capabilities "
                f"(host imports subset-of declared caps FAILED)"
            )
        return (
            f"least-authority (289): wasm component `{self.component}` declares "
            f"{_plural('capability', self.capabilities)} {self._named()} the "
            f"boundary policy does not allow -- a wasm cell may reach no more "
            f"host authority than the policy grants "
            f"(declared caps subset-of policy-allowed FAILED)"
        )

    def hint(self) -> str:
        if self.leg == "import>declared":
            return (
                "the wasm import section is generated from the reached "
                "requires, so this is a by-construction invariant; a failure "
                "is an emitter regression (docs/design/411-sandbox-placement.md)"
            )
        return (
            "widen the boundary policy's allow-list for this component, or "
            "drop the capability from the component -- a wasm cell gets least "
            "authority (docs/boundary-policy.md)"
        )

    def as_error(self, filename: str | None = None) -> RevlError:
        tail = ("the declared capabilities" if self.leg == "import>declared"
                else "the policy allow-list")
        steps = [
            TraceStep(self.component, "component", filename, None,
                      f"wasm cell reaching {self._named()}"),
            TraceStep(self.capabilities[0], "emission", None, None,
                      f"outside {tail}"),
        ]
        why = WhyTrace(kind="least-authority", subject=self.component,
                       shape=CHAIN, steps=steps)
        return RevlError(filename, None, self.message(), hint=self.hint(), why=why)


def _plural(word: str, items: tuple[str, ...]) -> str:
    if len(items) == 1:
        return word
    return word[:-1] + "ies" if word.endswith("y") else word + "s"


def component_breaches(
    component: str,
    import_caps: set[str],
    declared_caps: set[str],
    allow: tuple[str, ...] | None,
) -> list[LeastAuthorityBreach]:
    """Every least-authority breach for one wasm component, in chain order.

    ``import subset-of declared`` first (the wasm-tier invariant), then
    ``declared subset-of policy`` (the least-authority gate). ``allow`` is the
    component's resolved allow-list, or ``None`` when no allow rule constrains
    it (then the policy leg is vacuous -- an unconstrained component reaches
    what it likes, exactly as `policy.evaluate` treats it).
    """
    breaches: list[LeastAuthorityBreach] = []

    declared = tuple(sorted(declared_caps))
    imports = tuple(sorted(import_caps))
    # `import subset-of declared`, via the same widen predicate admission uses.
    if _caps_widen(declared, imports):
        extra = tuple(sorted(set(imports) - set(declared)))
        breaches.append(LeastAuthorityBreach("import>declared", component, extra))

    # `declared subset-of policy`, via the predicate `policy.evaluate` uses.
    if allow is not None:
        beyond = tuple(c for c in declared if not _allowed(c, allow))
        if beyond:
            breaches.append(LeastAuthorityBreach("declared>policy", component, beyond))

    return breaches


def least_authority_breaches(
    ir: dict,
    policy: Policy | None,
    modules: dict[str, str],
) -> list[LeastAuthorityBreach]:
    """Every least-authority breach across a composition's emitted wasm modules.

    ``modules`` is the wasm emitter's output (component name -> WAT). The
    non-component ``functions`` module carries no requires and no reach, so it
    contributes nothing. When ``policy`` is ``None`` only the import-subset-of-
    declared invariant is checked (there is no allow-list to bound against).
    """
    audit = audit_report(ir)
    manifest = audit.get("manifest") or {}
    reachable = set((audit.get("boundary") or {}).keys())

    breaches: list[LeastAuthorityBreach] = []
    for name in sorted(set(modules) & reachable):
        declared = {r.token for r in component_reach(audit, name)}
        import_caps = wasm_import_capabilities(modules[name])
        allow = None
        if policy is not None:
            allow = _allow_for(policy, name, component_realms(manifest, name))
        breaches.extend(component_breaches(name, import_caps, declared, allow))
    return breaches


def enforce_wasm_least_authority(
    ir: dict,
    policy: Policy | None,
    modules: dict[str, str],
    filename: str | None = None,
) -> None:
    """Refuse a composition whose wasm modules breach the least-authority chain.

    Raises on the FIRST breach (admission is all-or-nothing), like every other
    boundary refusal; returns silently on a clean chain. Additive: with no
    policy and a conformant emitter (the invariant holds by construction) this
    never fires, so an ordinary wasm compile/run is unchanged.
    """
    breaches = least_authority_breaches(ir, policy, modules)
    if breaches:
        raise breaches[0].as_error(filename or ir.get("filename"))
