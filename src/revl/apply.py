"""`revl apply` — a plan made executable, with a rollback that is a theorem.

`revl plan` answers *what would happen*. Today that answer is printed and
thrown away, and the change is made by hand. This turns the plan into an
artifact you can hand back to the tool:

    revl plan candidate.rvl --manifest running.json -o change.plan
    revl apply change.plan

`apply` does three things ordinary plan/apply does not:

  (a) **refuses on drift.** The running composition may have moved since the
      plan was computed. The plan carries a *basis* — a fingerprint of the
      composition it was computed against — and apply re-derives the live
      fingerprint and compares. A mismatch is refused, not papered over.

  (b) **verifies reality against the prediction at every step.** Each ordered
      operation carries the effect the plan predicted (a component gone, a
      component ACTIVE and providing a key). After apply performs it, it reads
      the live composition back and checks. A divergence stops the apply.

  (c) **rolls back by derived LIFO inverses.** When a step fails — its own
      execution errors, or its result contradicts the prediction — apply
      unwinds the steps it already made, last-in-first-out, each inverse
      derived from the same IR the plan was: a load's inverse is a dispose, a
      teardown's inverse is a re-load of the withdrawn body. It then proves the
      composition is back exactly where it started (no residue).

This module is the *pure* half: it builds the artifact from a plan result,
checks drift, and decides whether a step matched. It touches no runtime. The
effectful half — driving the fibers, and the LIFO unwind — lives on
`mcp.session.Session.apply`, next to the cordis lifecycle it reuses.
"""

from __future__ import annotations

# The artifact's format version. `apply` refuses a plan it does not recognise
# rather than misreading a future one; bump this when the shape below changes.
PLAN_FORMAT = 1
PLAN_KEY = "revlPlan"


class ApplyError(RuntimeError):
    """The plan cannot be applied as written (unrecognised, or drifted)."""


# ---------------------------------------------------------------- fingerprint

def fingerprint(ir: dict | None) -> dict:
    """A composition's identity, for drift detection: which components, in
    what load order, and which key each provides. Derived the same way from
    the plan's running IR and from the live session, so the two are directly
    comparable — staleness is *re-derived*, never assumed.
    """
    ir = ir or {}
    manifest = ir.get("manifest") or ir or {}
    entries = manifest.get("components") or []
    provisions = []
    for entry in entries:
        for key in entry.get("provides") or []:
            provisions.append({"key": key, "provider": entry["name"]})
    return {
        "components": sorted(e["name"] for e in entries),
        "loadOrder": list(manifest.get("loadOrder") or []),
        "provisions": sorted(({"key": p["key"], "provider": p["provider"]}
                              for p in provisions),
                             key=lambda p: (p["key"], p["provider"])),
    }


def _canon(fp: dict) -> tuple:
    return (
        tuple(sorted(fp.get("components") or [])),
        tuple(fp.get("loadOrder") or []),
        tuple(sorted((p["key"], p["provider"])
                     for p in fp.get("provisions") or [])),
    )


def drift(basis: dict, live: dict) -> dict | None:
    """`None` when the live composition is exactly the plan's basis, else a
    structured diff naming what moved: components that appeared or vanished,
    and provisions whose provider (or presence) changed. This is the diagnostic
    apply refuses with."""
    if _canon(basis) == _canon(live):
        return None
    basis_c, live_c = set(basis.get("components") or []), set(live.get("components") or [])
    basis_p = {(p["key"], p["provider"]) for p in basis.get("provisions") or []}
    live_p = {(p["key"], p["provider"]) for p in live.get("provisions") or []}
    return {
        "componentsAppeared": sorted(live_c - basis_c),
        "componentsVanished": sorted(basis_c - live_c),
        "loadOrderChanged": (list(basis.get("loadOrder") or [])
                             != list(live.get("loadOrder") or [])),
        "provisionsAppeared": sorted(f"{k} <- {p}" for k, p in live_p - basis_p),
        "provisionsVanished": sorted(f"{k} <- {p}" for k, p in basis_p - live_p),
    }


def render_drift(diff: dict) -> str:
    """One-line-per-fact rendering of a drift diff for the CLI/diagnostic."""
    out = ["the running composition has DRIFTED since this plan was computed "
           "— refusing to apply it:"]
    for label, items in (("components appeared", diff["componentsAppeared"]),
                         ("components vanished", diff["componentsVanished"]),
                         ("provisions appeared", diff["provisionsAppeared"]),
                         ("provisions vanished", diff["provisionsVanished"])):
        if items:
            out.append(f"  {label}: {', '.join(items)}")
    if diff.get("loadOrderChanged"):
        out.append("  the load order changed")
    out.append("  re-run `revl plan` against the current composition to get a "
               "fresh plan.")
    return "\n".join(out)


# ---------------------------------------------------------------- the artifact

def build_artifact(plan_result: dict, running_ir: dict | None) -> dict:
    """Turn an *admitted* plan into the self-contained `.plan` artifact.

    Refuses a rejected plan: there is nothing to apply. The artifact carries
    the basis (for drift), the resulting fingerprint, the ordered operations
    with their per-step predictions, and the resulting IR bodies apply loads.
    """
    if not plan_result.get("admissible"):
        raise ApplyError("cannot serialize a plan that was not admitted — "
                         "`apply` executes changes, and this candidate is "
                         "rejected (run `revl plan` to see why)")
    resulting_ir = plan_result.get("resultingIR")
    if resulting_ir is None:
        raise ApplyError("this plan carries no resulting IR — recompute it with "
                         "`plan(..., include_ir=True)` (the CLI does this for -o)")

    # the plan payload is kept for `apply`'s summary and for re-rendering, but
    # the heavy IR lives once at the top of the artifact, not twice.
    payload = {k: v for k, v in plan_result.items() if k != "resultingIR"}
    return {
        PLAN_KEY: PLAN_FORMAT,
        "basis": fingerprint(running_ir),
        "resulting": fingerprint(resulting_ir),
        "operations": _operations(plan_result),
        "runningIR": running_ir or {},
        "resultingIR": resulting_ir,
        "replacing": list(plan_result.get("candidate", {}).get("replacing") or []),
        "plan": payload,
    }


def validate_artifact(artifact: object) -> dict:
    """Confirm this is a plan artifact of a format we execute; raise otherwise."""
    if not isinstance(artifact, dict) or PLAN_KEY not in artifact:
        raise ApplyError("not a revl plan artifact (missing the "
                         f"`{PLAN_KEY}` marker) — apply takes a file written by "
                         "`revl plan -o`")
    version = artifact.get(PLAN_KEY)
    if version != PLAN_FORMAT:
        raise ApplyError(f"this plan is format {version}, but this revl speaks "
                         f"format {PLAN_FORMAT} — recompute it with `revl plan -o`")
    for field in ("basis", "resulting", "operations", "resultingIR"):
        if field not in artifact:
            raise ApplyError(f"the plan artifact is missing `{field}` — it is "
                             "corrupt or truncated")
    return artifact


def _after_providers(plan_result: dict) -> dict[str, str]:
    """key -> the provider it has in the *resulting* composition."""
    provisions = plan_result.get("provisions") or {}
    after: dict[str, str] = {}
    for record in (provisions.get("gained") or []) + (provisions.get("retained") or []):
        after[record["key"]] = record["provider"]
    for record in provisions.get("rebound") or []:
        after[record["key"]] = record["to"]
    return after


def _operations(plan_result: dict) -> list[dict]:
    """The ordered, executable steps — each with the effect it predicts.

    Two physical op kinds, because they are the two the live session can make
    reversibly on a single component:

      dispose  a withdrawn or replaced component's fiber tears down (its
               inverses replay); predicts the fiber gone and, for a true
               withdrawal, its provisions no longer served.
      load     an added or replaced component's fiber comes up from the
               resulting bodies; predicts its state and the keys it provides.

    Survivors that merely *rebind* or go *diverted* (PENDING) are not stepped
    explicitly — the runtime reacts to the provider changes on its own; the
    final whole-composition check confirms they landed where predicted.
    """
    comps = plan_result.get("components") or {}
    cascade = plan_result.get("cascade") or {}
    diverted = {d["name"] for d in cascade.get("diverted") or []}
    withdrawn = set(comps.get("withdrawn") or [])
    replaced = {r["name"] for r in comps.get("replaced") or []}
    added = list(comps.get("added") or [])
    basis_provider = {p["key"]: p["provider"]
                      for p in (plan_result.get("running") or {}).get("provisions") or []}
    after = _after_providers(plan_result)

    ops: list[dict] = []
    # teardown first, in the plan's derived LIFO order (consumers before
    # providers); only components we physically dispose.
    for name in plan_result.get("teardownOrder") or []:
        if name in withdrawn or name in replaced:
            keys = sorted(k for k, prov in basis_provider.items() if prov == name)
            ops.append({
                "op": "dispose", "name": name,
                "predict": {"absent": True,
                            "withdrawnKeys": keys if name in withdrawn else []},
            })
    # then bring up the added and replaced components, in resulting load order.
    load_set = set(added) | replaced
    for name in (plan_result.get("resulting") or {}).get("loadOrder") or []:
        if name in load_set:
            keys = sorted(k for k, prov in after.items() if prov == name)
            state = "PENDING" if name in diverted else "ACTIVE"
            ops.append({
                "op": "load", "name": name,
                "predict": {"state": state, "keys": keys},
            })
    return ops


# ---------------------------------------------------------------- verification

def verify_step(op: dict, observed: dict) -> str | None:
    """`None` if the live effect of `op` matches its prediction, else the
    reason it did not — the sentence apply reports before rolling back."""
    predict = op.get("predict") or {}
    if op["op"] == "dispose":
        if not observed.get("absent"):
            return (f"tearing down `{op['name']}` should have removed it, but it "
                    f"is still present ({observed.get('state')})")
        for key in predict.get("withdrawnKeys") or []:
            if key in observed.get("providedKeys") or []:
                return (f"`{op['name']}` was predicted to withdraw `{key}`, but "
                        f"the key is still provided after its teardown")
        return None
    if op["op"] == "load":
        expected = predict.get("state")
        actual = observed.get("state")
        if actual != expected:
            return (f"loading `{op['name']}` was predicted to leave it {expected}, "
                    f"but it is {actual}")
        if expected == "ACTIVE":
            for key in predict.get("keys") or []:
                if key not in (observed.get("providedKeys") or []):
                    return (f"`{op['name']}` was predicted to provide `{key}`, but "
                            f"it is not among the provided keys")
        return None
    return f"unknown operation `{op['op']}`"


def verify_final(artifact: dict, live_fingerprint: dict,
                 provided_keys: list[str]) -> str | None:
    """The whole-composition check after every step succeeds: the live
    composition must be the resulting one the plan promised. Catches the
    reactive fallout (diverted/rebound survivors) the per-step checks leave to
    the runtime.
    """
    resulting = artifact["resulting"]
    diff = drift(resulting, live_fingerprint)
    if diff is not None:
        appeared = diff["componentsAppeared"] or diff["provisionsAppeared"]
        vanished = diff["componentsVanished"] or diff["provisionsVanished"]
        return ("the applied composition is not the one the plan predicted — "
                f"unexpected present: {appeared or '—'}; "
                f"unexpected missing: {vanished or '—'}")
    expected_keys = sorted({p["key"] for p in resulting.get("provisions") or []}
                           & set(_live_active_keys(artifact)))
    # `expected_keys` are the resulting provisions whose provider is not
    # predicted diverted; every one of them must actually be provided.
    for key in expected_keys:
        if key not in provided_keys:
            return (f"the resulting composition should provide `{key}`, but the "
                    f"live session does not")
    return None


def _live_active_keys(artifact: dict) -> set:
    """Keys whose resulting provider is expected to be ACTIVE (not diverted)."""
    diverted = {d["name"] for d in
                (artifact["plan"].get("cascade") or {}).get("diverted") or []}
    after = _after_providers(artifact["plan"])
    return {key for key, provider in after.items() if provider not in diverted}
