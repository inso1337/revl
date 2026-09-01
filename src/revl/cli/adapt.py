"""`revl adapt` (roadmap item 296, slice 1): surface a proposed safe adapter
between a consumer's required service and a candidate's provided service.

Proposed, NOT silent (design section 3): `--check` reports whether the pair is
`compatible-with-adapter`, printing the bridge plan or the named refusals;
`--emit` additionally renders the synthesized adapter `.rvl` source (the
section-4 artifact) that the author commits and the compiler re-admits through
the ordinary gate. Synthesis is never auto-applied.

TODO(296-slice3): fold this into `revl resolve` so a candidate that fails the
direct `_service_compatible` filter is reported inline as compatible-with-
adapter, ranked below direct-compatible at equal authority.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..adapt import (bridge_plan, derivation_hash, navigate_for_refusals,
                     render_adapter)
from ..admission import _service_from_ir
from ..compiler import compile_source


def _load(path: str) -> dict:
    text = Path(path).read_text()
    return compile_source(text, path)


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
        "methods": [
            {"method": mp.method,
             "steps": [{"position": s.position,
                        "transformation": s.transformation,
                        "detail": s.detail,
                        "merge_shape": s.merge_shape}
                       for s in mp.steps]}
            for mp in res.methods],
    }
    if args.emit:
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
            prov_types=prov_types)
        plan["derivation"] = derivation_hash(
            json.dumps(need_ir["services"][rs], sort_keys=True),
            json.dumps(cand_ir["services"][ps], sort_keys=True),
            args.candidate, json.dumps(opt_ins, sort_keys=True))
        plan["source"] = source
    print(json.dumps(plan, indent=2))
    return 0
