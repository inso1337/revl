"""CLI: revl <cmd> (the documented happy path) — `python -P -m revl <cmd>`
is the absolute-interpreter fallback for callers that always run a venv's
interpreter by its full path (the `-P` is the PYTHONSAFEPATH safety bit,
issue #317); both spellings reach this module, and the issue #317 / #336
window that bare `-m` has is closed by `drop_cwd_entry` (and fully
avoided by the `revl` console script this file is the entry point of).

`main()` is argument-parser assembly (`revl.cli.parser.build_parser`) plus
dispatch. The per-command handlers live in `revl.cli.{change,interop,observe}`;
the shared-`ir` commands (test / query / erase-report / audit / version /
compile) and the G8 boundary analysis stay here — the latter because
`_boundary`/`_extern_reachability` are the one authoritative boundary walk that
`plan`, `query`, `audit_diff`, `registry`, `profile`, and the mcp server all
import from `revl.__main__`.
"""

from __future__ import annotations

import json
import sys

# issue #317: `-m` puts the working directory at sys.path[0], so a `cordis.py`
# or `yaml.py` next to a composition would be imported instead of the real
# module. Drop it before anything else this entry point pulls in.
from ._safepath import drop_cwd_entry

drop_cwd_entry()

from .compiler import compile_files
from .diagnostics import obligations, report
from .distribute import distributability
from .errors import RevlError
from .holes import render as render_holes
from .run import run_command
from .test import test_command

from .cli.parser import build_parser
from .cli.change import (
    _run_apply, _run_branch, _run_canary, _run_compare, _run_estop, _run_plan,
    _run_quarantine, _run_recover, _run_repair, _run_undo)
from .cli.interop import (
    _run_contract, _run_export, _run_fmt, _run_import, _run_mcp, _run_serve)
from .cli.observe import (
    _run_attest, _run_changelog, _run_dash, _run_diff, _run_explain,
    _run_history_query, _run_metrics, _run_profile, _run_trace, _run_why)


# The G8 boundary walk lives in `revl.boundary` (a CLI-free module) so the
# modules that need it need not import this whole command-dispatch graph
# (issue #543 P-10). Re-exported here because `plan`, `query`, `audit_diff`,
# `registry`, `profile`, `placement`, and the mcp server historically reach
# these names as `revl.__main__._boundary` / `._extern_reachability` / etc.
from .boundary import (  # noqa: F401 — re-exported for back-compat
    _UNKNOWN_DISPATCH, _boundary, _extern_reachability, _fn_call_names,
    _ref_provenance)


def _run_query(args, ir: dict) -> int:
    """`revl query <question> <target> <files...>` — the composition query
    layer (docs/queries.md). A miss (unknown component/service/key) is a
    non-zero exit with the known names, not a crash."""
    from .query import QUERIES, render

    handler = QUERIES[args.query_command]
    if args.query_command == "drift":
        result = handler(ir, args.target, gains=args.gains, losses=args.loses)
    else:
        result = handler(ir, args.target)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result))
    return 0 if result.get("ok") else 1


def _run_test(args, ir: dict) -> int:
    """`revl test` — compile and run the composition's `test` blocks."""
    return test_command(ir, args.backend, sweep=getattr(args, "sweep", False),
                        mock_requires=getattr(args, "mock_requires", False),
                        schedule_seed=getattr(args, "schedule_seed", None),
                        schedule_seeds=getattr(args, "schedule_seeds", None))


def _run_erase_report(args, ir: dict) -> int:
    """`revl erase-report --realm R` — right-to-erasure evidence (docs/erase-report.md)."""
    from .erase_report import build_report, render  # noqa: PLC0415
    report_doc = build_report(
        ir, args.realm,
        prove_residue=not getattr(args, "no_residue_proof", False))
    if args.json:
        print(json.dumps(report_doc, indent=2))
    else:
        print(render(report_doc))
    if not report_doc.get("ok"):
        return 1
    # a proven state-gone + untouched other realms is a clean report;
    # a bare crossing does not fail (it is enumerated, by design), but an
    # unproven teardown or a breached other realm does.
    state = report_doc["inProcessStateGone"]["noResidueProof"]
    residue_bad = state.get("available") and not state.get("proven")
    breached = not report_doc["otherRealmsUntouched"]["untouched"]
    return 1 if (residue_bad or breached) else 0


def _run_policy(args) -> int:
    """`revl policy evaluate` (item 290) — the dry-run explain verb. Runs the
    SAME `policy.evaluate` and reports, per component, which rules select it and
    which clauses pass/fail with the recorded fact vs the threshold. Never
    admits, refuses, or mutates: exit 0 clean, 1 when anything would be refused,
    2 on a parse/usage error."""
    from .audit_diff import audit_report  # noqa: PLC0415
    from .compiler import compile_source  # noqa: PLC0415
    from .policy import (PolicyError, explain, load_policy,  # noqa: PLC0415
                         render_explain)

    try:
        policy = load_policy(args.policy_file)
    except (PolicyError, RevlError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    key = None
    if getattr(args, "key", None):
        from .attest import load_key  # noqa: PLC0415
        key = load_key(args.key)
    trusted = frozenset(getattr(args, "trusted_publisher", []) or [])

    evidence: dict = {}
    origins: dict = {}
    evidence_ir: dict = {}
    recompute = bool(getattr(args, "recompute", False))
    recompute_ir: dict = {}
    recompute_gauntlet = None
    # the source set the cold gauntlet producer compiles under --recompute
    # (§4): the .rvl files in source mode, the entry's source text in registry
    # mode. Only assembled when --recompute is on.
    gauntlet_args: dict | None = None

    try:
        if getattr(args, "registry", None):
            # registry mode: evaluate a published entry as if admitted (§7).
            from . import registry as reg  # noqa: PLC0415
            if not args.candidate:
                print("error: --registry needs --candidate NAME",
                      file=sys.stderr)
                return 2
            registry = reg.Registry.from_dir(args.registry)
            entry = next((e for e in registry.entries
                          if e.name == args.candidate), None)
            if entry is None:
                print(f"error: no registry entry named {args.candidate!r}",
                      file=sys.stderr)
                return 2
            ir = compile_source(entry.source, "component.rvl")
            audit = audit_report(ir)
            name = next(iter(audit.get("boundary") or {}), args.candidate)
            evidence[name] = entry.evidence_bundle or reg.EvidenceBundle()
            origins[name] = "registry"
            evidence_ir[name] = reg._normalize_ir_for_attest(
                compile_source(entry.source, "component.rvl"))
            if recompute:
                recompute_ir[name] = ir
                gauntlet_args = {"source": entry.source}
        else:
            if not args.files:
                print("error: policy evaluate needs a POLICY and PROGRAM.rvl "
                      "(or --registry --candidate)", file=sys.stderr)
                return 2
            ir = compile_files(args.files)
            audit = audit_report(ir)
            comps = list(audit.get("boundary") or {})
            for name in comps:
                origins[name] = "source"
                if recompute:
                    recompute_ir[name] = ir
            if recompute:
                gauntlet_args = {"files": list(args.files)}
            if getattr(args, "evidence", None):
                from . import registry as reg  # noqa: PLC0415
                bundle = reg.load_evidence_bundle(args.evidence)
                target = args.component or (comps[0] if len(comps) == 1 else None)
                if target is None:
                    print("error: --evidence needs --component NAME when the "
                          "composition has more than one component",
                          file=sys.stderr)
                    return 2
                evidence[target] = bundle
                origins[target] = "registry"
                evidence_ir[target] = reg._normalize_ir_for_attest(ir)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    # §4: the cold gauntlet is operator-run at evaluation time (needs no
    # attestation root). Guarded broadly — the gauntlet grades rather than
    # raises, but a runtime/toolchain gap must degrade to "no dossier", never
    # crash the dry-run; every other facet still recomputes.
    if recompute and gauntlet_args is not None:
        try:
            from .mcp import gauntlet as _gauntlet  # noqa: PLC0415
            from .mcp.session import Session  # noqa: PLC0415
            recompute_gauntlet = _gauntlet.run(Session(), gauntlet_args,
                                               over_the_transport=False)
        except Exception:  # noqa: BLE001 — a producer gap is not a dry-run crash
            recompute_gauntlet = None

    scope = getattr(args, "mcp_scope", []) or []
    mcp_components = (frozenset(audit.get("boundary") or {})
                     if "*" in scope else frozenset(scope))

    result = explain(policy, audit, mcp_components, evidence=evidence,
                     origins=origins, trusted_publishers=trusted, key=key,
                     evidence_ir=evidence_ir, recompute=recompute,
                     recompute_ir=recompute_ir,
                     recompute_gauntlet=recompute_gauntlet,
                     component=args.component)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_explain(result))
    return 1 if result["refused"] else 0


def _run_goal(args, ir: dict) -> int:
    """`revl goal audit` — the blind-spot report (item 441 / issue #120, S2).

    A pure function of the compiled IR: the class-(c) capabilities the run
    reaches that the termination contract's cone does not observe
    (`goal_audit.blind_spot`). A composition with no goal service has no
    contract, and the command says so and exits 0 (there is nothing to audit;
    §5.3's empty-contract refusal is a session freeze rule, not a `revl check`
    fact, and is S3's)."""
    from .goal_audit import blind_spot, render  # noqa: PLC0415 — lazy
    if args.goal_command == "audit":
        report = blind_spot(ir)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(render(report))
        return 0
    raise AssertionError(f"unknown goal subcommand {args.goal_command!r}")


def _run_audit(args, ir: dict) -> int:
    """`revl audit` — composition manifest + G8 boundary surface, with the
    item-33 policy gate (`--policy`) and the authority-drift gate (`--diff`)."""
    if getattr(args, "policy", None):
        # the third leg of the gate (item 33): absolute authority. The policy
        # is evaluated as set operations over the same audit graph `--diff`
        # and `--json` already build; a violation refuses admission with a
        # why-trace naming the offending chain.
        from .audit_diff import audit_report  # noqa: PLC0415
        from .policy import evaluate, load_policy, render_report  # noqa: PLC0415

        policy = load_policy(args.policy)
        audit = audit_report(ir)
        scope = args.mcp_scope
        mcp_components = (frozenset(audit.get("boundary") or {})
                         if "*" in scope else frozenset(scope))
        # item 290 plumbing: give the gate the evidence bundle, key, and trust
        # set when the policy carries evidence rules. Absent otherwise, so a
        # policy with none evaluates exactly as before.
        evidence: dict = {}
        origins: dict = {}
        evidence_ir: dict = {}
        key = None
        if getattr(args, "key", None):
            from .attest import load_key  # noqa: PLC0415
            key = load_key(args.key)
        trusted = frozenset(getattr(args, "trusted_publisher", []) or [])
        if getattr(args, "evidence", None):
            from . import registry as reg  # noqa: PLC0415
            comps = list(audit.get("boundary") or {})
            target = comps[0] if len(comps) == 1 else None
            if target is not None:
                evidence[target] = reg.load_evidence_bundle(args.evidence)
                origins[target] = "registry"
                evidence_ir[target] = reg._normalize_ir_for_attest(ir)
        violations = evaluate(policy, audit, mcp_components=mcp_components,
                              evidence=evidence, origins=origins,
                              trusted_publishers=trusted, key=key,
                              evidence_ir=evidence_ir)
        if args.json:
            print(json.dumps(
                {"policy": args.policy,
                 "violations": [{"kind": v.kind, "component": v.component,
                                 "token": v.token, "message": v.message,
                                 "why": v.why.to_json()} for v in violations],
                 "refused": bool(violations)}, indent=2))
        else:
            print(render_report(policy, violations))
        return 1 if violations else 0
    if getattr(args, "diff", None):
        from .audit_diff import audit_report, evaluate, render  # noqa: PLC0415
        with open(args.diff, encoding="utf-8") as handle:
            prev = json.load(handle)
        new = audit_report(ir)
        result = evaluate(prev, new, accepted=set(args.accept),
                          accept_all=args.accept_all)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(render(result, args.diff))
        return 1 if result["widened"] else 0
    boundary = _boundary(ir)
    distribution = distributability(ir)
    from .cardinality import cardinality  # noqa: PLC0415
    card = cardinality(ir)
    manifest = ir.get("manifest") or {}
    declared_externs = [
        {"name": ext["name"], "class": ext.get("class"),
         # item 396: a ref-only extern has no `bodies` key but DOES cross on its
         # ref tier, so union the ref backends in — otherwise a ref-only extern
         # would audit as "no bodies" while emitting fine.
         "backends": sorted(set(ext.get("bodies") or {})
                            | set(ext.get("refs") or {})),
         # item 373: carry the reach onto the audit extern entry so the surface
         # can name what a crossing is bounded to. Absent unless declared, so the
         # `--json` audit of every existing composition is byte-identical.
         **({"reach": ext["reach"]} if ext.get("reach") else {}),
         # item 396 option B: the ref provenance (`tier: "path#symbol"`), so a
         # review can see WHERE a crossing's implementation lives when it moved
         # out of the audited document. item 410 prefixes the root KIND
         # (`stdlib:path#symbol`) so a reviewer sees which trust domain the
         # crossing reaches into at a glance. Absent unless a ref is used.
         **({"refs": {tier: _ref_provenance(r)
                      for tier, r in (ext.get("refs") or {}).items()}}
            if ext.get("refs") else {})}
        for ext in ir.get("externs") or []
    ]
    if args.json:
        # The manifest + G8 audit as the versioned interchange format
        # (docs/interchange-format.md, roadmap item 28): `stamp` adds the
        # `schema_version`/`kind` header additively, over the same body
        # earlier consumers already read.
        from .interchange import stamp  # noqa: PLC0415
        # item 309 added capability_registers + recovery_surface to
        # audit_report; the interchange body must carry them too so it stays the
        # byte-for-byte unstamped audit report (test_version_is_additive_body_unchanged).
        from .audit_diff import (  # noqa: PLC0415
            _capability_registers, _env_surface, _recovery_surface,
            _retention_surface, _secrets_surface)
        from .cardinality import cardinality  # noqa: PLC0415
        print(json.dumps(stamp(
            {"manifest": manifest, "boundary": boundary,
             "externs": declared_externs,
             "distributability": distribution,
             "capability_registers": _capability_registers(ir),
             "recovery_surface": _recovery_surface(ir),
             # item 260: the per-component crossing-count ceilings, next to
             # distributability. Must match audit_report byte-for-byte
             # (test_version_is_additive_body_unchanged), so it is the same call.
             "cardinality": cardinality(ir),
             # item 256 Slice 2: the audit secrets table (name + capability only).
             # ADDITIVE and present only when a secret is bound, so this must match
             # audit_report byte-for-byte (test_version_is_additive_body_unchanged);
             # `_secrets_surface` spreads `{}` for a secret-free composition.
             **_secrets_surface(ir),
             # item 350: the environment-contract table (the `boot` component's
             # config schema — name/type/required/secret/bound, never a value).
             # ADDITIVE and present only when a boot component is declared, so
             # this must match audit_report byte-for-byte
             # (test_version_is_additive_body_unchanged).
             **_env_surface(ir),
             # item 308 F10: the report-only retaining-extern surface. ADDITIVE
             # and present only when a resource handle reaches a non-inverse
             # callee; must match audit_report byte-for-byte
             # (test_version_is_additive_body_unchanged), so it is the same call.
             **_retention_surface(ir)}), indent=2))
        return 0
    print("composition (providers first):", " -> ".join(manifest.get("loadOrder") or []))
    # item 350: the environment contract, printed before the components — it is
    # what the host must inject BEFORE any of them can load. Name, type,
    # requiredness and the author-written bound only; a value never reaches the
    # audit (it arrives at `revl run --env` time and never enters the IR).
    from .audit_diff import _env_table  # noqa: PLC0415
    contract = _env_table(ir)
    if contract:
        print(f"\nenvironment contract (boot component {contract['component']}):")
        for row in contract["fields"] or []:
            bound = row.get("bound")
            if not bound:
                shape = "unbounded"
            elif bound.get("kind") == "under":
                shape = f'under "{bound.get("prefix")}"'
            else:
                shape = "in [" + ", ".join(repr(v) for v in bound.get("values") or []) + "]"
            marks = ["required" if row["required"] else "optional", shape]
            if row["secret"]:
                marks.append("secret")
            print(f"  {row['name']}: {row['type']}  ({', '.join(marks)})")
        if not contract["fields"]:
            print("  — (declares no fields: the host injects nothing)")
    for entry in manifest.get("components") or []:
        name = entry["name"]
        isolate = entry.get("isolate") or {}

        def _decorate(key: str) -> str:
            return f"{key}@{isolate[key]}" if key in isolate else key

        print(f"\ncomponent {name}  ({entry.get('file') or '?'})")
        print(f"  requires: {', '.join(_decorate(k) for k in entry.get('inject') or []) or '—'}")
        print(f"  provides: {', '.join(_decorate(k) for k in entry.get('provides') or []) or '—'}")
        for key, metadata in (entry.get("intercept") or {}).items():
            print(f"  intercept: {key} {metadata}")
        stats = boundary.get(name)
        if stats is None:
            continue
        host = stats.get("externs") or []
        if stats["emissions"] or stats["awaits"] or host:
            detail = []
            if stats["emissions"]:
                caps = stats.get("capabilities") or {}

                def _scoped(label: str) -> str:
                    scope = caps.get(label) or ["*"]
                    return (f"{label} [{', '.join(scope)}]"
                            if scope != ["*"] else label)

                # the union is the G8 answer to "where can this component
                # reach"; `*` in it means some dependency's declaration
                # makes no promise, which is the thing worth seeing
                reach = sorted({c for scope in caps.values() for c in scope})
                detail.append(f"emissions: {', '.join(_scoped(e) for e in stats['emissions'])}"
                              f" ({stats['compensated']} compensated)")
                if reach:
                    detail.append(f"capabilities: {', '.join(reach)}")
            if stats["awaits"]:
                detail.append(f"iteration boundaries: {stats['awaits']}")
            if host:
                def _host_extern(e: dict) -> str:
                    if e.get("class") == "first-class dispatch":
                        return (f"{e['name']} (reached through first-class "
                                "function dispatch — what runs is not statically "
                                "boundable)")
                    # item 247: a scoped extern renders its declared token the
                    # same way a scoped emission does (`label [db]` above),
                    # because that token — not the name below it — is what a
                    # `capability <glob>` policy rule selects on. An unscoped
                    # extern has no scope to show and renders as before.
                    scope = (f" [{', '.join(e['capabilities'])}]"
                             if e.get("capabilities") else "")
                    base = (f"{e['name']}{scope} ({e['class']}, "
                            f"{'+'.join(e['backends']) or 'no bodies'})")
                    # item 396 option B: name WHERE a ref crossing's host code
                    # lives, so a review sees the implementation moved out of the
                    # audited document.
                    if e.get("refs"):
                        prov = ", ".join(f"@{tier} ref {loc}"
                                         for tier, loc in sorted(e["refs"].items()))
                        base += f" [{prov}]"
                    return base

                rendered = ", ".join(_host_extern(e) for e in host)
                detail.append(f"host code: {rendered}")
            # item 260: the per-capability crossing-count ceiling, under the
            # capabilities line. Bounded tokens join one clause; every unbounded
            # token is loud on its own clause, never folded into a comma list
            # (docs/design/260 §1.2).
            card_entry = card.get(name)
            if card_entry:
                per_cap = card_entry.get("per_capability") or {}
                bounded = [f"{token} <= {info['bound']} per activation"
                           for token, info in per_cap.items()
                           if info.get("kind") == "bounded"]
                # a certified iteration whose initial fuel is still a config
                # field: the ceiling is symbolic until composition pins it
                # (docs/design/260 §2.2, §1.2).
                bounded += [f"{token} <= {info['expr']} per activation "
                            f"({info['per_iter']} per iteration)"
                            for token, info in per_cap.items()
                            if info.get("kind") == "bounded-symbolic"]
                if bounded:
                    detail.append(f"cardinality: {', '.join(bounded)}")
                for token, info in per_cap.items():
                    if info.get("kind") == "unbounded":
                        detail.append(
                            f"cardinality: {token} UNBOUNDED "
                            f"({info.get('reason')})")
            print(f"  boundary: {'; '.join(detail)}")
        else:
            print("  boundary: none — fully revertible (G8)")
    templates = manifest.get("templates") or []
    if templates:
        # G8: the instance dimension is dynamic (docs/design-v2-instances.md,
        # decision 7). These are spawn targets — runtime instances, not
        # static composition members — so their multiplicity is `× dynamic`.
        print("\ninstance-parametric components (× dynamic — spawned at runtime, "
              "each in its own local realm):")
        for name in templates:
            stats = boundary.get(name) or {}
            emissions = stats.get("emissions") or []
            surface = (f"emissions: {', '.join(emissions)}"
                       if emissions else "no emissions")
            print(f"  {name} × dynamic  ({surface})")
    instances = manifest.get("instances") or []
    if instances:
        # Capability attenuation per instance (item 66,
        # docs/capability-attenuation.md): the spawner → child narrowing.
        # A child's granted set is a checked subset of what the spawner
        # holds; `attenuated` is the authority dropped on the way down —
        # the least-authority proof, per lineage edge.
        print("\ncapability attenuation (per instance — lineage narrows, "
              "never widens):")
        for edge in instances:
            holds = ", ".join(edge.get("holds") or []) or "—"
            granted = ", ".join(edge.get("granted") or []) or "—"
            dropped = ", ".join(edge.get("attenuated") or [])
            tail = f"  (dropped: {dropped})" if dropped else ""
            print(f"  {edge['parent']} → {edge['child']}: "
                  f"holds [{holds}] ⊇ grants [{granted}]{tail}")
    if declared_externs:
        print("\nexterns (verbatim host code — unchecked inside, typed at the boundary):")
        for ext in declared_externs:
            # item 373: name the REACH between the classification and the
            # backends, so a confined crossing is visibly distinct from an
            # unconfined one (`engine_run [emission] reach: confined(cwd)
            # backends: py, ts`). A bare emission carries no reach and prints
            # exactly as before — byte-compatible.
            reach = ext.get("reach")
            reach_str = (f"  reach: {reach['kind']}({reach['target']})"
                         if reach else "")
            print(f"  {ext['name']}  [{ext['class']}]{reach_str}  backends: "
                  f"{', '.join(ext['backends']) or '—'}")
    if distribution:
        print("\ndistributability (interop-bridge §4: which services may cross a process seam):")
        width = max(len(name) for name in distribution)
        for name in sorted(distribution):
            verdict = distribution[name]
            print(f"  {name:<{width}}  {verdict['verdict']:<20} "
                  f"{'; '.join(verdict['reasons'])}")
    # item 308 F10 (docs/design/308-effect-ownership-modes.md, "The honest
    # limitation: retaining externs"): the report-only retention surface. B1
    # refuses a borrow that escapes through a revl position; a host body that
    # keeps the handle it was handed escapes through a surface no clause can
    # see, because the declaration does not say "retains". So the frontier is
    # LISTED for a human instead of refused, and the section is printed only
    # when there is one — a handle-free composition renders exactly as before.
    retention = _retention_surface_rows(ir)
    if retention:
        print("\nretention surface (item 308 F10 — report-only: a host body may "
              "keep a handle it is handed; revl cannot see inside one):")
        width = max(len(row["callee"] or "") for row in retention)
        for row in retention:
            print(f"  {row['callee']:<{width}}  [{row['class']}]  "
                  f"{row['kind']}  param {row['index']} `{row['param']}`: "
                  f"{row['type']} (carries {row['resource']})")
        print("  a declared inverse is excluded — teardown closing a handle is "
              "the contract working, not a retention hazard")
    # item 411, Slice 1: the sandbox envelope + effective reach per sandboxed
    # process, when a placement is supplied. `net=none` is printed alongside
    # each seam-served key's provider reach, so it is never readable as a
    # total-egress claim about the composition.
    if getattr(args, "placement", None):
        from .placement import _load_placement, sandbox_audit_view  # noqa: PLC0415
        lines, sb_err = sandbox_audit_view(ir, _load_placement(args.placement))
        if sb_err:
            print(f"\nsandbox placement: error: {sb_err}")
            return 1
        if lines:
            print()
            for line in lines:
                print(line)
    # item 309: `revl audit --recovery` — the replay-class view. Every inverse,
    # deferred emission, and compensation with its replay class (`replay: free`
    # for a declared/keyed idempotent entry, `replay: fenced` for an undeclared
    # inverse, `recovery: human-finish` for an unkeyed owed emission) and its
    # register (`declared`/`keyed`/item 440's `read`).
    if getattr(args, "recovery", None):
        for line in _recovery_audit_view(ir):
            print(line)
    return 0


def _retention_surface_rows(ir: dict) -> list:
    """The item-308 F10 retention rows, through the one implementation the
    `--json` document uses, so the two views cannot drift."""
    from .resources import retention_surface  # noqa: PLC0415
    return retention_surface(ir.get("externs"), ir.get("types"),
                             ir.get("services"))


def _recovery_audit_view(ir: dict) -> list:
    """The `--recovery` replay-class lines (item 309 §"question 4", point 1)."""
    from .audit_diff import _recovery_surface  # noqa: PLC0415
    surface = _recovery_surface(ir)
    lines = ["recovery surface (item 309): replay class per boundary crossing"]
    if not surface:
        lines.append("  (none — no inverse, deferred emission, or compensation)")
        return lines
    for entry in surface:
        register = entry.get("register")
        kind = entry.get("kind")
        if kind == "inverse":
            # item 440: the read tier is its own class — a `undo pure` inverse is
            # re-dispatched with no key and no fence, and never escalates.
            cls = ("replay: read" if register == "read"
                   else "replay: free" if register else "replay: fenced")
        elif kind == "owed-emission":
            # item 440 §(b): the seam can now ACT on this classification, but only
            # under the operator's `recovery may re-issue owed emissions` knob, so
            # the class says "re-issuable" rather than promising a fire.
            cls = ("replay: re-issuable" if register in ("read", "keyed")
                   else "recovery: human-finish")
        else:  # compensation
            cls = ("compensate: keyed-retry" if register == "keyed"
                   else "compensate: best-effort")
        reg = f"idempotent: {register}" if register else "idempotent: none (fenced)"
        lines.append(f"  {entry['name']:<24} {kind:<14} {cls:<24} {reg}")
    return lines


def _run_version(args, ir: dict) -> int:
    """`revl version` — derive the required semver bump from the interface diff
    (docs/derived-versioning.md)."""
    from .version import derive, render  # noqa: PLC0415
    if args.emit_manifest:
        # the diff input for a later `--against`: the compiled composition,
        # which (unlike an audit report) carries the `services` table.
        print(json.dumps(ir, indent=2))
        return 0
    if not args.against:
        print("error: `revl version` needs --against PREV.json (a previous "
              "compiled composition) or --emit-manifest", file=sys.stderr)
        return 2
    try:
        with open(args.against, encoding="utf-8") as handle:
            previous = json.load(handle)
    except OSError as error:
        print(f"error: cannot read {args.against}: {error}", file=sys.stderr)
        return 1
    try:
        result = derive(previous, ir, previous_version=args.current_version)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result, args.against))
    return 0


def _run_compile(args, ir: dict) -> int:
    """`revl compile` — the default: write the compiled IR document."""
    rendered = json.dumps(ir, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)
    return 0


def _run_emit(args) -> int:
    """`revl emit` — render a backend's emitter in a chosen target (item 253).

    A target is a dimension orthogonal to `--backend`: it selects a RENDERING of
    the backend's emitter, defaulting to that backend's native runtime.
    `--target temporal` renders the typescript emitter for the Temporal TS SDK
    (docs/design/253-temporal-target.md §4). Wiring it as a target rather than a
    seventh runnable backend is what keeps it a variant, not a tier."""
    from .bundle import _emitter  # noqa: PLC0415 — lazy, the shared emitter loader

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    module = _emitter(args.backend)
    if module is None or not hasattr(module, "emit"):
        print(f"error: no emitter for backend {args.backend!r}", file=sys.stderr)
        return 2

    target = args.target
    if target is not None and args.backend != "typescript":
        print(f"error: --target {target!r} is only available for the typescript "
              f"backend in this release (item 253)", file=sys.stderr)
        return 2

    try:
        rendered = module.emit(ir, target=target) if target is not None else module.emit(ir)
    except (RevlError, ValueError) as error:
        # ValueError covers the emitter's own EmitError channel (a closed-
        # allowlist refusal travels here with its why-trace).
        print(f"error: {error}", file=sys.stderr)
        return 1

    if isinstance(rendered, dict):  # a multi-file emitter (wasm)
        rendered = "\n".join(f"// === {name} ===\n{text}"
                             for name, text in sorted(rendered.items()))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


def _run_grammar(args) -> int:
    """`revl grammar` — the language surface small enough to carry in a prompt.

    Default prints the short human-readable summary (the same text `revl_grammar`
    hands back over MCP); `--prompt` prints the dense, complete grammar meant
    for direct injection into an LLM authoring system prompt (roadmap item 346,
    also shipped verbatim as docs/syntax-2.0.prompt.txt). Both live in
    `grammar_summary.py`, not duplicated here."""
    from .grammar_summary import PROMPT_GRAMMAR, PROSE_GRAMMAR

    sys.stdout.write(PROMPT_GRAMMAR if args.prompt else PROSE_GRAMMAR)
    return 0


def _run_scaffold(args) -> int:
    """`revl scaffold` — a typed, holed skeleton from a spec (docs/scaffold.md).

    The generator is the new work; the rest reuses the same compile and
    obligation path every other verb shares. Human output writes the `.rvl`
    (or prints it) and lists the open holes on stderr, exactly as `revl
    compile` does for a draft; `--json` hands back the skeleton, its
    obligations, and each hole's fill spec in one document."""
    from .scaffold import ScaffoldError, build_spec, build_skeleton, scaffold_document

    try:
        spec = build_spec(
            service=args.service, provides=args.provides,
            component=args.component, requires=args.requires,
            capabilities=args.capabilities, methods=args.method,
            emits=args.emits, config=args.config, effect=not args.no_effect,
            resource_type=args.resource)
    except ScaffoldError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    filename = args.out or f"{spec.component}.rvl"

    if args.json:
        document = scaffold_document(spec, filename)
        document["path"] = args.out
        print(json.dumps(document, indent=2))
        if args.out:
            with open(args.out, "w") as handle:
                handle.write(document["source"])
        return 0

    source = build_skeleton(spec)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(source)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(source, end="")

    # The obligations are the point: a scaffold is a draft, so report the holes
    # the way `revl compile` reports a draft's — on stderr, so a redirected
    # skeleton stays exactly the skeleton.
    holes = compile_source_holes(source, filename)
    if holes:
        plural = "s" if len(holes) > 1 else ""
        print(f"{len(holes)} open hole{plural} — a scaffold is a draft: it "
              f"compiles, admission refuses it until every hole is filled "
              f"(docs/holes.md)", file=sys.stderr)
        for rendered in render_holes(holes):
            print(f"  {rendered}", file=sys.stderr)
    return 0


def compile_source_holes(source: str, filename: str) -> list[dict]:
    """The open holes in a freshly compiled skeleton, or [] if it somehow has
    none. The generator only ever emits drafts, so this is the obligation list
    the human output renders."""
    from .compiler import compile_source
    return compile_source(source, filename).get("holes") or []


def _parse_overlay(overrides: list[str]) -> dict:
    """`--set @db.pool=16` into the invocation overlay (426 S2, §3.1 level 3).

    VALUES ONLY, never structure: the spelling reaches a field of a row that
    already exists and there is no `--add` or `--remove`, because the last level
    is where dynamic configuration lives and structure is the part that is
    declared, checked and diffable.
    """
    overlay: dict = {}
    for raw in overrides or []:
        target, sep, value = raw.partition("=")
        label, dot, field_name = target.strip().lstrip("@").partition(".")
        if not sep or not dot or not label or not field_name:
            raise RevlError("<invocation>", 1,
                            f"`--set {raw}` is not `@row.field=value`",
                            hint="the overlay names one field of one row: "
                                 "`--set @db.pool=16`")
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value                    # a bare word is a string
        overlay[(label, field_name)] = parsed
    return overlay


def _print_table(table, document=None, provenance: bool = False) -> None:
    """The ROWS / WIRING panels, shared by `revl composition` and
    `revl layer check`."""
    from .composition import claim_str  # noqa: PLC0415

    print(f"COMPOSITION  {table.name}  (origin `{table.origin}`, "
          f"{len(table.rows)} rows)")
    print(f"             {table.source}")
    print()
    print("ROWS")
    for row in table.rows:
        print(f"  {row.qualified:<24} {row.component}  ({row.source})")
        if row.extra_claims:
            # 426 §1.4: upstream ADDED a provision. The row keeps its label —
            # that is decision 1 earning its keep — and the addition is loud
            # rather than silent.
            gained = ", ".join(claim_str(c) for c in row.extra_claims)
            print(f"  {'':<24}   also provides {gained}, not asserted here")
        if row.granted is not None:
            listed = ", ".join(f"`{k}`" for k in row.granted) or "<empty>"
            print(f"  {'':<24}   granted {listed}")
        if provenance and any(level for level, _, _ in row.provenance):
            # 426 §3.3 step 5: which (level, layer, op) touched this row, in
            # order. Recording it is what makes a row's provider never a
            # mystery — every layer that reached the row is named.
            trail = " -> ".join(f"{op} by `{layer}` (L{level})"
                                for level, layer, op in row.provenance)
            print(f"  {'':<24}   {trail}")
        if row.remote is not None:
            # item 424 C2. The peer, the reach and the failure mode are the
            # ADMISSION facts a remote row adds; the WIRING panel below prints
            # nothing different for it, which is D-424c.1 visible in the output.
            info = row.remote
            print(f"  {'':<24}   REMOTE peer {info['peer']}  "
                  f"reach `{info['capability']}`  "
                  f"on_failure {info['onFailure']}  "
                  f"redirect {info.get('redirect', 'refuse')}")
            print(f"  {'':<24}   synthesized from service "
                  f"`{info['service']}` ({info['serviceSource']}); "
                  "NO inverse — a remote effect survives unwind")
    print()
    print("WIRING")
    for label, edges in table.wiring().items():
        claims = ", ".join(edges["claims"]) or "nothing"
        needs = ", ".join(f"`{k}`" for k in edges["requires"]) or "nothing"
        print(f"  {label:<24} claims {claims}; requires {needs}")
    if table.uses:
        print()
        print("USES")
        for path in table.uses:
            print(f"  {path}")
    print()
    if document is None:
        print("RESOLVED     header-only: no component body was lowered. "
              "Re-run with --admit to compile the rows.")
    else:
        print("ADMITTED     load order "
              f"{' -> '.join(document['manifest']['loadOrder'])}")


def _run_layer(args) -> int:
    """`revl layer check FILE` — fold a composition's declared layers into its
    row table (roadmap item 426, slice S2).

    426 exit test 12: every row id resolves and the whole wiring renders with no
    component body lowered. The fold itself never calls the gate (§3.3), so a
    bug here can only over-refuse; `_link` still decides admission."""
    from .composition import resolve_file  # noqa: PLC0415

    try:
        table = resolve_file(args.file, args.root, _parse_overlay(args.overrides))
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(table.to_ir(), indent=2))
        return 0
    _print_table(table, provenance=True)
    return 0


def _run_composition(args) -> int:
    """`revl composition FILE` — resolve a composition document's ROW TABLE
    (roadmap item 426, slice S1), with its declared layers folded in (S2).

    Header-only by default: every row id resolves and the whole wiring renders
    without lowering a single component body (426 exit test 12). `--admit` also
    compiles the rows the table names, which is where `_link` runs G2/G3 — the
    resolver itself never calls the gate (§3.3). Admission is INCREMENTAL by
    default (S3): a composition with layers admits only the resolved delta into
    its already-admitted base; `--full` forces the whole composition, to the
    same verdict."""
    from .composition import admit_composition, resolve_file  # noqa: PLC0415

    try:
        overlay = _parse_overlay(getattr(args, "overrides", []))
        table = resolve_file(args.file, args.root, overlay)
        document = admit_composition(args.file, args.root, overlay,
                                     full=getattr(args, "full", False)) \
            if args.admit else None
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        out = table.to_ir()
        if document is not None:
            out["loadOrder"] = document["manifest"]["loadOrder"]
        print(json.dumps(out, indent=2))
        return 0

    _print_table(table, document, provenance=True)
    return 0


def _run_analyze(args) -> int:
    """`revl analyze` — Petri-net reachability liveness (roadmap item 438).

    Report-only. Compiles the composition sources (or loads a precompiled IR
    with `--ir`), derives the net in `revl.liveness`, and prints a per-
    composition verdict: live, deadlock (naming the stranded activation), or
    bounded-inconclusive. Only a PROVEN deadlock exits nonzero — the check
    reports the cycle, it does not refuse admission (question 3)."""
    from .liveness import analyze_document, render, to_json  # noqa: PLC0415 — lazy

    if args.ir:
        try:
            with open(args.ir, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read IR document {args.ir}: {error}", file=sys.stderr)
            return 1
        name = args.ir
    else:
        if not args.files:
            print("error: `revl analyze` needs source files or --ir DOC.json",
                  file=sys.stderr)
            return 1
        try:
            document = compile_files(args.files)
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        name = args.files[0]

    report = analyze_document(
        document, name=name,
        max_states=args.max_states, max_tokens=args.max_tokens)
    if args.json:
        print(json.dumps(to_json(report), indent=2))
    else:
        for line in render(report):
            print(line)
    return report.exit_code()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "truc":
        from .truc import main as _truc_main  # noqa: PLC0415 — lazy, pulls cordis
        return _truc_main(args.truc_args)
    if args.command == "bundle":
        from .bundle import run_bundle  # noqa: PLC0415 — lazy, pulls the compiler
        return run_bundle(args)
    if args.command == "verify":
        from .bundle import run_verify  # noqa: PLC0415 — lazy
        return run_verify(args)
    if args.command == "deploy":
        from .deploy import deploy_command  # noqa: PLC0415 — lazy
        return deploy_command(args)
    if args.command == "composition":
        return _run_composition(args)
    if args.command == "analyze":
        return _run_analyze(args)
    if args.command == "layer":
        return _run_layer(args)
    if args.command == "emit":
        return _run_emit(args)
    if args.command == "explain":
        return _run_explain(args)
    if args.command == "grammar":
        return _run_grammar(args)
    if args.command == "adapt":
        from .cli.adapt import _run_adapt
        return _run_adapt(args)
    if args.command == "doctor":
        from .doctor import doctor_command  # noqa: PLC0415 — lazy: no heavy imports
        return doctor_command(args)
    if args.command == "scaffold":
        return _run_scaffold(args)
    if args.command == "repair":
        return _run_repair(args)
    if args.command == "fmt":
        return _run_fmt(args)
    if args.command == "run":
        return run_command(args)
    if args.command == "why":
        return _run_why(args)
    if args.command == "metrics":
        return _run_metrics(args)
    if args.command == "trace":
        return _run_trace(args)
    if args.command == "profile":
        return _run_profile(args)
    if args.command == "attest":
        return _run_attest(args)
    if args.command == "dash":
        return _run_dash(args)
    if args.command == "recover":
        return _run_recover(args)
    if args.command == "estop":
        return _run_estop(args)
    if args.command == "branch":
        return _run_branch(args)
    if args.command == "compare":
        return _run_compare(args)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "mcp":
        return _run_mcp(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "plan":
        return _run_plan(args)
    if args.command == "apply":
        return _run_apply(args)
    if args.command == "undo":
        return _run_undo(args)
    if args.command == "canary":
        return _run_canary(args)
    if args.command == "quarantine":
        return _run_quarantine(args)
    if args.command == "contract":
        return _run_contract(args)
    if args.command == "diff":
        return _run_diff(args)
    # `revl changelog` has its own two-input loader (like `diff`), so it is
    # routed before the shared single-source compile step.
    if args.command == "changelog":
        return _run_changelog(args)
    # historical query mode reads a recorded run (files, not source), so it is
    # routed before the compile-from-source step every other command shares
    if args.command == "query" and args.query_command in ("emitted-between",
                                                           "touched"):
        return _run_history_query(args)

    # `revl policy evaluate` (item 290) compiles its own sources (or reads a
    # registry entry), so it is routed before the shared compile step, like the
    # history query.
    if args.command == "policy":
        return _run_policy(args)

    try:
        profile = None
        if getattr(args, "taint_strict", False):
            from .admit_profile import AdmissionProfile  # noqa: PLC0415 — lazy
            profile = AdmissionProfile(taint_strict=True)
        ir = compile_files(args.files, profile=profile)
    except RevlError as error:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(report(error), indent=2))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1

    # Open obligations go to stderr so a piped/redirected IR document stays
    # exactly the IR document; the same list is in `ir["holes"]` for anything
    # reading the JSON (docs/holes.md).
    open_holes = ir.get("holes") or []
    if open_holes:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(obligations(open_holes), indent=2), file=sys.stderr)
        else:
            plural = "s" if len(open_holes) > 1 else ""
            print(f"{len(open_holes)} open hole{plural} — this is a draft: it "
                  f"compiles, admission will refuse it (docs/holes.md)",
                  file=sys.stderr)
            for rendered in render_holes(open_holes):
                print(f"  {rendered}", file=sys.stderr)

    # roadmap 422 F7: a `use "stdlib/..."` that did not land on the stdlib this
    # compiler ships. Same channel and the same reason as the holes report -
    # stderr, so a piped IR document stays exactly the IR document, with the
    # fact also on `ir["stdlib_shadow"]` for anything reading the JSON. Not a
    # refusal: shadowing is supported (a local `stdlib/` wins by design, item
    # 319, and vendoring is stamped and drift-checked, item 389). It is said out
    # loud because concluding "confined witnessed fs" from the SPELLING of
    # `use "stdlib/fs.rvl"` is unsound while the file it names is unpinned.
    for shadow in ir.get("stdlib_shadow") or []:
        print(f"note: `use \"{shadow['written']}\"` resolved to "
              f"{shadow['resolved']} ({shadow['origin']}), not the stdlib this "
              f"compiler ships, read that module, not the import line, for "
              f"what its externs are classified and confined to",
              file=sys.stderr)

    if args.command == "test":
        return _run_test(args, ir)
    if args.command == "query":
        return _run_query(args, ir)
    if args.command == "erase-report":
        return _run_erase_report(args, ir)
    if args.command == "audit":
        return _run_audit(args, ir)
    if args.command == "goal":
        return _run_goal(args, ir)
    if args.command == "version":
        return _run_version(args, ir)
    return _run_compile(args, ir)


if __name__ == "__main__":
    sys.exit(main())
