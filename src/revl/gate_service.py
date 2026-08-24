"""The py provider body behind the `Gate` bridge service (roadmap item 144,
Path A — the compiler/gate reachable as a bridge SERVICE).

`src/revl/truc/_host.py::admit_all` made revl's own admission gate reachable
*in-process* on the py tier: a `@py` extern body that calls
`compiler.compile_files(sources, manifest=running)` — the exact call
docs/registry.md §"install is admission" names as the install step. That gate
only serves a consumer living in the same Python process.

This module is the same gate, exposed as the body of a *bridge* service so a
consumer on a **different tier** (a ts component, over the interop bridge of
roadmap item 56) can obtain an admission verdict by emitting to it. The gate
is unchanged — same `compile_files`, same guarantees (G2/G3/G4 span the
running composition) — only the *reach* changes: in-process extern → bridge
provider.

`admit` is a *pure* function of (candidate sources, running manifest): with the
sources supplied in memory it reads no disk and writes none, so the extern that
wraps it is `pure`, and the `Gate.admit` operation stays `async fn` with
value-typed (`Str`) parameters — i.e. **transport-safe** by the
docs/interop-bridge.md §4 verdict, which is exactly what lets it cross a
process seam. A gate marked `emission` (as truc's in-process one is) would be
address-space-bound and could not be a bridge service; that the compile is
genuinely effect-free is what makes Path A possible.

The JSON contract (values only, so it crosses the wire by copy):

  admit(sources_json, manifest_json) -> verdict_json

  sources_json  : {abspath: source, ...}  — the candidate composition's files
  manifest_json : the running composition IR as {"manifest":..., "services":...}
                  (what a previous compile returned), or "" for a fresh admit
  verdict_json  : {"ok": Bool, "diagnostic": Str, "admitted": [name...],
                   "manifest": running-manifest-after}

A refused candidate carries the compiler's why-trace verbatim in `diagnostic`
(the same string `revl compile` prints), so the refusal — and the reason —
survive the round trip back to the cross-tier consumer.
"""

from __future__ import annotations

import json


def admit(sources_json: str, manifest_json: str) -> str:
    """Admit a candidate composition through revl's own gate, against an
    optional running manifest. Returns a verdict JSON string (see module
    docstring). Never raises across the boundary: a compiler refusal (G2/G3/G4)
    is returned as ``ok: false`` with the diagnostic, so the seam stays total.
    """
    from revl.compiler import compile_files  # noqa: PLC0415 — the gate, in-process
    from revl.errors import RevlError  # noqa: PLC0415

    sources = json.loads(sources_json) if sources_json else {}
    running = json.loads(manifest_json) if manifest_json else None
    paths = list(sources.keys())
    try:
        ir = compile_files(paths, manifest=running, sources=sources)
    except RevlError as error:
        return json.dumps({
            "ok": False,
            "diagnostic": str(error),
            "admitted": [],
            "manifest": (running or {}).get("manifest", {}),
        })
    return json.dumps({
        "ok": True,
        "diagnostic": "",
        "admitted": [c["name"] for c in ir.get("components") or []],
        "manifest": ir.get("manifest") or {},
    })


# --------------------------------------------------------------------------
# admit_case: a fixture-driven convenience for the *probe-driven* cross-tier
# proof. A placement probe (src/revl/placement.py::_parse_probe,
# backends/typescript/placement_runner.ts) admits only `key.method(literal,
# ...)` with comma-separated literal arguments — it cannot carry a raw JSON
# composition through as a string. `admit_case` takes one short case name and
# builds the running manifest + candidate here, so a node consumer can drive a
# real cross-tier admission from a placement file. It calls the *same* `admit`
# gate above; only the argument marshalling differs.
# --------------------------------------------------------------------------

# The running composition every case is admitted *against*: one component
# provides the key `thing`.
_RUNNING_SOURCES = {
    "/gate_demo/base.rvl": (
        "service Thing { fn ping() -> Int }\n"
        "component Base provides thing: Thing { provide thing { fn ping() = 1 } }\n"
    ),
}

# Candidate compositions, keyed by case name:
#   collide — provides the *same* key `thing`: refused by G2 (a provision
#             conflict), the verdict + why-trace must survive the round trip.
#   clean   — provides a *new* key `other`: admitted.
_CANDIDATES = {
    "collide": {
        "/gate_demo/dup.rvl":
            "component Dup provides thing: Thing { provide thing { fn ping() = 2 } }\n",
    },
    "clean": {
        "/gate_demo/extra.rvl":
            "component Extra provides other: Thing { provide other { fn ping() = 3 } }\n",
    },
}


def admit_case(case_name: str) -> str:
    """Admit a named fixture candidate against the fixture running composition,
    through the same `admit` gate. `collide` is refused by G2; `clean` admits.
    The verdict JSON is identical in shape to `admit`'s."""
    from revl.compiler import compile_files  # noqa: PLC0415

    running_ir = compile_files(list(_RUNNING_SOURCES), sources=_RUNNING_SOURCES)
    running = json.dumps({
        "manifest": running_ir.get("manifest") or {},
        "services": running_ir.get("services") or {},
    })
    sources = _CANDIDATES.get(case_name)
    if sources is None:
        return json.dumps({
            "ok": False,
            "diagnostic": f"unknown gate case {case_name!r} "
                          f"(known: {', '.join(sorted(_CANDIDATES))})",
            "admitted": [],
            "manifest": {},
        })
    return admit(json.dumps(sources), running)
