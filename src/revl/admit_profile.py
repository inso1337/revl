"""The untrusted-author admission profile (roadmap item 329).

The composition gate already refuses a turn that reaches an *undeclared*
service (G1) and holds an ungranted `requires` inert against a granted-only
provider set (R2). That is enough for capability composition, but it BREAKS on
host blocks: a model-authored

    extern pure fn exfil(t: Str) -> Str = @py { import os; ... }

ADMITS and RUNS arbitrary host code, because G8's boundary is "verbatim host
code — unchecked inside" (item 24: "the gate does not sandbox host code"). When
the AUTHOR of the source is untrusted — the lighthouse code-mode direction,
where a model emits an arbitrary revl program every turn — that is an injection
hole: the turn is composed against a granted tool surface, but nothing stops it
from declaring its own escape hatch.

This module is the CHEAPEST-FIRST cut item 329 names, a *compile refusal* (not a
runtime policy check):

  (a) `no_extern` — the admitted source may not DECLARE new `extern`/host-block
      declarations. The untrusted author may only COMPOSE pre-granted services;
      it cannot smuggle verbatim host code past the boundary. Refused
      structurally, before any host body could run.

  (b) `granted` — an allowlist of the service NAMES the admitted program may
      reach. Even a *declared* service is refused unless it is in the granted
      set (or provided internally by the same turn). This closes the gap where
      an admitted turn, wired against a running composition, could reach ANY
      ambient service; the profile restricts it to an explicit subset.

Deferred (item 45, the fuller answer): compiling the turn to the wasm substrate
with the granted tools wired in as host imports, so confinement is *physical*
and even an admitted host body cannot escape its linear memory. That path is a
larger change (see `docs/design/329-untrusted-author-profile.md` and the TODO in
`quarantine.py`); the no-extern + allowlist cut here is item 329's deliverable
and is sufficient to make "model-emitted revl, admitted through the gate"
injection-resistant for BOTH capability reach and smuggled host code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError
from .parser import Program


@dataclass(frozen=True)
class AdmissionProfile:
    """How much to trust the AUTHOR of the source being admitted.

    `no_extern`  — forbid new `extern`/host-block declarations in the admitted
                   source (the untrusted author may only compose, not declare
                   host code).
    `granted`    — the allowlist of service names the admitted program may
                   reach. `None` means the allowlist is OFF (unrestricted, the
                   trusted-author default); a set — even the empty set — turns it
                   ON and every externally-reached service must be a member.

    The default `AdmissionProfile()` is inert: no_extern off, allowlist off, so
    a caller that passes no profile compiles exactly as before.
    """

    no_extern: bool = False
    granted: frozenset[str] | None = None
    # item 249, Slice C: forbid the admitted ROOT source from minting its own
    # declassifiers — no `endorse` in any form, and no root-declared
    # `Trusted`-returning `verified fn`. On by default in `untrusted_author`, so
    # the whole Slice A/B taint discipline cannot be opted out of by the one
    # author item 329 refuses to trust. Off (inert) for a trusted author.
    no_declassify: bool = False
    # item 249, Slice D (D3): derive taint sinks and sources with NO annotation —
    # a shell/exec/terminal-scoped crossing is a sink, a web/net/fs/model/input
    # emission mints its origin. On by default in `untrusted_author` (a
    # model-authored turn annotates nothing), and byte-identical when off, so a
    # plain compile never moves. Also reachable via `revl compile --taint-strict`.
    taint_strict: bool = False

    @staticmethod
    def untrusted_author(granted) -> "AdmissionProfile":
        """The profile for a model-authored per-turn source: no new host code,
        reach bounded to an explicit granted service set, no self-minted
        declassifier (item 249 Slice C), and derived taint sinks/sources so the
        defense exists with zero annotations (item 249 Slice D)."""
        return AdmissionProfile(no_extern=True,
                                granted=frozenset(granted or ()),
                                no_declassify=True,
                                taint_strict=True)

    @property
    def active(self) -> bool:
        return (self.no_extern or self.granted is not None
                or self.no_declassify or self.taint_strict)


def check_no_extern(root_programs: list[Program], profile: AdmissionProfile) -> None:
    """(a) Refuse if the untrusted-authored source declares any `extern`.

    The check is scoped to the ROOT programs — the source actually being
    admitted — not the imported closure: a pre-granted module the turn `use`s is
    trusted host code that was granted deliberately, but an `extern` written in
    the admitted source itself is exactly the escape hatch this forbids. It is a
    purely structural check on the parsed AST, so it fires BEFORE any host body
    is lowered or run — a compile refusal, per the item.
    """
    if not profile.no_extern:
        return
    for program in root_programs:
        if program.externs:
            decl = program.externs[0]
            has_body = bool(decl.bodies)
            what = (f"host-block extern `{decl.name}` (@"
                    f"{decl.bodies[0].backend} body)" if has_body
                    else f"extern `{decl.name}`")
            raise RevlError(
                program.filename, decl.line,
                f"admission refused: the untrusted-author profile forbids new "
                f"`extern`/host-block declarations, but this source declares "
                f"{what}",
                hint="verbatim host code is unchecked inside the boundary (G8, "
                     "item 24: the gate does not sandbox host code). An untrusted "
                     "author may only COMPOSE pre-granted services, never declare "
                     "its own host code — remove the extern and reach a granted "
                     "service instead",
                code="G8", category="admission",
            )


def _iter_var_refs(node, out: set) -> None:
    """Collect every bare-name reference (`ExprVar.name`) reachable from a parsed
    AST fragment, by the same structural dataclass walk as `_iter_endorse`. A
    named-imported extern is in a body's scope only as a bare callable (an
    alias-qualified `mod.sh(...)` is rejected by lower — externs are not reachable
    through a module alias), so a bare-name sweep of the root bodies is exactly
    the reach surface. Structural, so it runs on the parsed source before any host
    body could be lowered or run."""
    from .parser import ExprVar  # noqa: PLC0415 — lazy, avoids import cycle
    if isinstance(node, ExprVar):
        out.add(node.name)
    if isinstance(node, (list, tuple)):
        for item in node:
            _iter_var_refs(item, out)
        return
    fields = getattr(node, "__dict__", None)
    if fields:
        for value in fields.values():
            _iter_var_refs(value, out)


def check_no_host_extern_reach(root_programs: list[Program],
                               merged_fn_decls: list,
                               host_externs: dict,
                               profile: AdmissionProfile) -> None:
    """(a2) Refuse if an untrusted-authored turn REACHES an extern that carries a
    host body: the import-and-call bypass of `check_no_extern`, closed across the
    WHOLE transitive module closure (items 330 and 329/transitive).

    `check_no_extern` refuses a root that DECLARES an extern, but nothing stopped
    a root from `use "stdlib/shell.rvl" { sh }` and calling `sh(...)`: the extern
    is DECLARED in a search-path module (trusted host code, granted deliberately)
    yet REACHED from the untrusted turn. That runs arbitrary host code under the
    profile whose whole purpose is to forbid it. The design's intent is "compose
    granted SERVICES, never reach host code"; a host-body extern is host code just
    the same (item 24: the gate does not sandbox host code).

    The 330 cut walked only the ROOT's directly-imported extern surface and swept
    only the ROOT bodies. But `compile_files` MERGES and LOWERS the entire
    transitive `included` closure into one program, so an untrusted turn reaches
    arbitrary host code through a `pub fn` wrapper in a NON-root module: the wrapper
    is bare-callable from a root body and its own body reaches the host extern. The
    reach is real (the merged doc lowers and carries the host body) but was neither
    SEEN by the root-`uses`-only feeder nor SWEPT by the root-only reference sweep.

    So the feeder (`_included_host_externs`) now keys every host-body extern in ANY
    included module by name, and this sweep is a transitive call-graph reachability
    pass: starting from every root component / provide-method body and every root
    fn body (the admitted source), it follows bare-name calls into imported `pub
    fn`s across `merged_fn_decls` and refuses the moment any reached name is a
    host-body extern. This is the PRECISE call graph, not a closure over-
    approximation: an included module whose host extern is never actually reached
    from a root does NOT false-refuse (matching `test_untrusted_importing_a_pure_fn`
    additivity). Refusal is otherwise conservative and sound: ANY bare reference
    to a host-body extern anywhere on a reachable path is a reach and is refused,
    full stop, with no extern allowlist and across all classifications, exactly
    `check_no_extern`'s stance. It fires at COMPILE (admission), before any host
    body runs, and names the reached extern, the module it came from, and the rule.
    """
    if not profile.no_extern or not host_externs:
        return
    host_names = set(host_externs)
    fn_by_name: dict = {}
    for fn in merged_fn_decls:
        # first binding wins; the merge already refused a genuine pub-name
        # duplicate, and privates are mangled apart, so a name maps to one body.
        fn_by_name.setdefault(fn.name, fn)

    for program in root_programs:
        # seed the reach set from the admitted source: every bare name referenced
        # from a root component / provide body and from a root fn body.
        reached_names: set = set()
        _iter_var_refs(program.components, reached_names)
        _iter_var_refs(program.fn_decls, reached_names)
        # transitively follow bare-name calls into imported `pub fn` bodies across
        # the merged closure; a fn body's bare refs join the reach set.
        worklist = [n for n in reached_names if n in fn_by_name]
        visited_fns: set = set()
        while worklist:
            fn_name = worklist.pop()
            if fn_name in visited_fns:
                continue
            visited_fns.add(fn_name)
            body_refs: set = set()
            _iter_var_refs(fn_by_name[fn_name], body_refs)
            for ref in body_refs:
                if ref not in reached_names:
                    reached_names.add(ref)
                    if ref in fn_by_name:
                        worklist.append(ref)

        reached_host = reached_names & host_names
        if not reached_host:
            continue
        name = sorted(reached_host)[0]
        decl, module_path = host_externs[name]
        backend = decl.bodies[0].backend if decl.bodies else "?"
        raise RevlError(
            program.filename, decl.line,
            f"admission refused: the untrusted-author profile forbids reaching "
            f"host code, but this source transitively reaches host-block extern "
            f"`{name}` ({decl.classification} @{backend} body, from "
            f"`{module_path}`): a host-body `extern`, reached through the merged "
            f"module closure, is verbatim host code the gate does not sandbox "
            f"(G8, item 24)",
            hint="`no_extern` forbids DECLARING host code; REACHING a host extern "
                 "is the same escape hatch by another door: directly, or through "
                 "a `pub fn` wrapper in an imported module the merge lowers into "
                 "the same program. An untrusted author may only COMPOSE pre-"
                 "granted services, never reach a host extern; drop the import "
                 "and reach a granted service instead (items 330, 329/transitive)",
            code="G8", category="admission",
        )


def _iter_endorse(node, out: list) -> None:
    """Collect every `ExprEndorse` node reachable from a parsed AST fragment, by
    a structural walk over dataclass fields and containers. Structural (like
    `check_no_extern`) so it fires on the parsed source, before lowering."""
    from .parser import ExprEndorse  # noqa: PLC0415 — lazy, avoids import cycle
    if isinstance(node, ExprEndorse):
        out.append(node)
    if isinstance(node, (list, tuple)):
        for item in node:
            _iter_endorse(item, out)
        return
    fields = getattr(node, "__dict__", None)
    if fields:
        for value in fields.values():
            _iter_endorse(value, out)


def check_no_declassify(root_programs: list[Program],
                        profile: AdmissionProfile) -> None:
    """(c) Refuse if the untrusted-authored source mints its own declassifier
    (item 249, Slice C, closing hole 2 for the untrusted author).

    Two doors, both refused structurally on the parsed ROOT AST, before lowering
    (like `no_extern`): an `endorse` in any form, and a `verified fn` whose
    return mentions `Trusted[...]`. Declassification for an admitted turn then
    comes only from the pre-granted closure (a granted checked parser) or a human
    approval — never from the turn's own source. Refused loudly, so the model
    gets the repair signal rather than a mystery G9 downstream."""
    if not profile.no_declassify:
        return
    from .taint import _mentions_trusted  # noqa: PLC0415 — lazy, avoids cycle
    for program in root_programs:
        # door 1: a self-declared laundering `verified fn`.
        for fn in program.fn_decls:
            if getattr(fn, "verified", False) and _mentions_trusted(fn.returns):
                raise RevlError(
                    program.filename, fn.line,
                    f"admission refused: the untrusted-author profile forbids "
                    f"self-minted declassifiers, but `verified fn {fn.name}` "
                    f"returns `Trusted[...]` — a laundering parser the turn "
                    f"declares itself",
                    hint="`verified` proves totality, not validation quality; an "
                         "untrusted author may not mint its own declassifier. "
                         "Reach a granted checked parser, or gate the downgrade on "
                         "a human approval (item 249, Slice C)",
                    code="G9", category="admission")
        # door 2: an `endorse` anywhere in the admitted source.
        endorses: list = []
        _iter_endorse(program.fn_decls, endorses)
        _iter_endorse(program.components, endorses)
        if endorses:
            first = endorses[0]
            raise RevlError(
                program.filename, getattr(first, "line", 0),
                f"admission refused: the untrusted-author profile forbids "
                f"declassification, but this source calls `endorse[{first.origin}]"
                f"(...)` — an untrusted author may not downgrade taint",
                hint="declassification for an admitted turn comes only from the "
                     "pre-granted closure (a granted checked parser) or a human "
                     "approval, never from the turn's own `endorse` (item 249, "
                     "Slice C)",
                code="G9", category="admission")


def check_allowlist(document: dict, profile: AdmissionProfile) -> None:
    """(b) Refuse if the admitted program reaches a service outside `granted`.

    A component "reaches" a service by `requires`-ing it. A service the turn's
    own components also PROVIDE is internal wiring, not an outward reach, so it
    is exempt; every other required service must be in the granted allowlist.
    This is what bounds an admitted turn to a subset of a running composition's
    ambient services instead of all of them.
    """
    if profile.granted is None:
        return
    granted = profile.granted
    components = document.get("components") or []
    provided_internally: set[str] = set()
    for comp in components:
        provides = comp.get("provides")
        if isinstance(provides, dict):
            provided_internally.update(provides.values())
    for comp in components:
        requires = comp.get("requires") or {}
        for key, service in requires.items():
            if service in granted or service in provided_internally:
                continue
            allowed = ", ".join(f"`{s}`" for s in sorted(granted)) or "<nothing>"
            raise RevlError(
                comp.get("source") or document.get("filename") or "<candidate>",
                0,
                f"admission refused: component `{comp.get('name')}` reaches "
                f"service `{service}` (via `requires {key}`), which is not in the "
                f"granted set",
                hint=f"the untrusted-author profile grants [{allowed}] and "
                     f"nothing else; an admitted turn may only reach a granted "
                     f"service or one it provides itself (item 329 allowlist)",
                code="R2", category="admission",
            )


def enforce_source(root_programs: list[Program],
                   profile: AdmissionProfile | None) -> None:
    """Pre-lowering half of the profile: the structural no-extern check on the
    parsed source. Runs BEFORE lowering, so an untrusted `extern` is refused
    with the profile's own message before any host body is even lowered — never
    a runtime check. A `None`/inert profile is a no-op."""
    if profile is None:
        return
    if profile.no_extern:
        check_no_extern(root_programs, profile)
    if profile.no_declassify:
        check_no_declassify(root_programs, profile)


def enforce_document(document: dict, profile: AdmissionProfile | None) -> None:
    """Post-lowering half of the profile: the granted-allowlist check on the
    lowered document (it needs the resolved `requires`/`provides`). A
    `None`/inert profile is a no-op, so a trusted-author compile is
    byte-identical."""
    if profile is None or profile.granted is None:
        return
    check_allowlist(document, profile)
