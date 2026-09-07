"""`revl adapt` (roadmap item 296, slice 1): surface a proposed safe adapter
between a consumer's required service and a candidate's provided service.

Proposed, NOT silent (design section 3): `--check` reports whether the pair is
`compatible-with-adapter`, printing the bridge plan or the named refusals;
`--emit` additionally renders the synthesized adapter `.rvl` source (the
section-4 artifact) that the author commits and the compiler re-admits through
the ordinary gate. Synthesis is never auto-applied.

Slice 3 landed the resolver half: `registry.resolve` probes a candidate the
direct `_service_compatible` filter refused and reports it inline as
compatible-with-adapter, ranked below direct-compatible at equal authority, with
the chain depth and the outcome-merge evidence discount. Both surfaces derive
the SAME adapter identity for the same pair (`adapt.service_surface` plus the
candidate source's sha).

`--check` also FLATTENS a chain (design section 6.4): when the candidate is
itself a committed adapter, the output carries `chainDepth` and a `chain`
listing the proposed hop and the committed inner hop end to end, so what gets
reviewed is the actual composed loss (every merge, default and drop across all
hops), not the last hop's slice of it. TODO(296-slice3, remaining): `revl diff`
and the federation pin.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..adapt import (bridge_plan, chain_depth_for, derivation_hash,
                     flatten_committed_hop, navigate_for_refusals,
                     render_adapter, service_surface)
from ..admission import _service_from_ir
from ..compiler import compile_source


def _load(path: str) -> dict:
    text = Path(path).read_text()
    return compile_source(text, path)


def _methods_json(res) -> list:
    """The per-method step listing shared by the plan and by each flattened
    chain hop, so a hop re-displays every merge, default and drop in the same
    shape the top-level plan uses (design section 6.4)."""
    return [
        {"method": mp.method,
         "steps": [{"position": s.position,
                    "transformation": s.transformation,
                    "detail": s.detail,
                    "merge_shape": s.merge_shape}
                   for s in mp.steps]}
        for mp in res.methods]


def _pick_service(ir: dict, name: str | None, role: str) -> str:
    services = ir.get("services") or {}
    if name is not None:
        if name not in services:
            raise SystemExit(
                f"adapt: {role} service `{name}` not found "
                f"(declared: {', '.join(sorted(services)) or 'none'})")
        return name
    if len(services) == 1:
        return next(iter(services))
    raise SystemExit(
        f"adapt: {role} file declares "
        f"{len(services)} services ({', '.join(sorted(services))}); "
        f"name one with --{role}-service")


def _run_adapt(args) -> int:
    need_ir = _load(args.need)
    cand_ir = _load(args.candidate)
    rs = _pick_service(need_ir, args.need_service, "need")
    ps = _pick_service(cand_ir, args.candidate_service, "candidate")
    req = _service_from_ir(rs, need_ir["services"][rs])
    prov = _service_from_ir(ps, cand_ir["services"][ps])
    req_types = need_ir.get("types") or {}
    prov_types = cand_ir.get("types") or {}

    opt_ins: dict = {}
    if args.adapt:
        opt_ins = json.loads(Path(args.adapt).read_text())

    res = bridge_plan(req, prov, opt_ins,
                      req_types=req_types, prov_types=prov_types)

    if not res.ok:
        out = {
            "verdict": "refuse",
            "need": rs,
            "candidate": ps,
            "refusals": [
                {"method": r.method, "position": r.position,
                 "transformation": r.transformation, "clause": r.clause,
                 "reason": r.reason, "hint": r.hint}
                for r in res.refusals],
            # item 274: the same refusal list projected into the shared
            # `navigate` record (family `adapter`), so a harness reads one shape.
            "navigate": navigate_for_refusals(res.refusals),
        }
        print(json.dumps(out, indent=2))
        return 1

    plan = {
        "verdict": "compatible-with-adapter",
        "need": rs,
        "candidate": ps,
        "merges": list(res.merges),
        "methods": _methods_json(res),
    }

    # Section 6.4: flatten a chain. `chainDepth` is the total composed depth a
    # fresh bridge onto this candidate would carry (1 onto ordinary code); when
    # the candidate is itself a committed adapter, `chain` re-displays the
    # proposed hop and the committed inner hop end to end, so the review sees
    # the composed loss rather than just this hop's slice of it.
    cand_text = Path(args.candidate).read_text()
    depth = chain_depth_for(cand_text)
    plan["chainDepth"] = depth
    if depth > 1:
        inner = flatten_committed_hop(cand_ir, ps)
        chain = [{
            "hop": depth,
            "kind": "proposed",
            "from": rs,
            "to": ps,
            "merges": list(res.merges),
            "methods": _methods_json(res),
        }]
        if inner is not None:
            hop = {
                "hop": depth - 1,
                "kind": "committed",
                "requireKey": inner.require_key,
                "from": inner.provided_service,
                "to": inner.backing_service,
            }
            if inner.opaque is not None:
                hop["opaque"] = inner.opaque
            else:
                hop["merges"] = list(inner.result.merges)
                hop["methods"] = _methods_json(inner.result)
            chain.append(hop)
        plan["chain"] = chain

    if args.emit:
        # The derivation pins the two SURFACES (not the two IR documents they
        # arrived in) plus the candidate source's sha, so `revl adapt` and
        # `registry.resolve` name the same adapter for the same pair. Hashing
        # the candidate PATH here, as an earlier spelling did, made the identity
        # depend on where the file happened to sit - the opposite of the
        # byte-stable identity section 4 asks for.
        derivation = derivation_hash(
            service_surface(req), service_surface(prov),
            hashlib.sha256(cand_text.encode("utf-8")).hexdigest(),
            json.dumps(opt_ins, sort_keys=True))
        # the alias carries the consumer-facing tokens: the union of the
        # required service's declared capability tokens (item 296, S2).
        carried: list[str] = []
        for m in req.methods.values():
            for cap in (m.capabilities or ()):
                if cap not in carried:
                    carried.append(cap)
        source = render_adapter(
            args.name, req, prov, opt_ins,
            provide_key=args.provide_key or rs.lower(),
            require_key=args.require_key,
            carried_tokens=tuple(carried),
            prov_types=prov_types, req_types=req_types,
            derivation=derivation, chain_depth=depth)
        plan["derivation"] = derivation
        plan["source"] = source
    print(json.dumps(plan, indent=2))
    return 0
