"""Branch lineage, offline enumeration and comparison over durable WALs
(roadmap item 250, Slice 2).

Slice 1 shipped the fork as a LIVE primitive: `revl_fork` enumerates the honest
partition of a session's tail, `revl_fork_confirm` runs the scope-gated rewind,
freezes the parent and mints the branch. Everything it knows, it knows from the
in-process timeline — and the in-process timeline dies with the process. So the
capability the roadmap item asks for after the fork (*"branch a run at a step and
diverge: `revl branch`, `revl compare`"*) had no substrate: a WAL on disk could
not say it was a branch, could not name where it diverged, and could not
reproduce the partition a fork at step k would produce.

This module is that substrate, and it is deliberately tier-agnostic — it reads
through :func:`revl.wal.read_wal` with no backend on the path, exactly as
:mod:`revl.recovery` does (item 322), so a branch written by the py in-process
driver and one written by a go/rust/java/wasm subprocess read identically.

Three surfaces:

* :func:`lineage` — what ONE WAL is: a standalone session, a parent frozen at k,
  a branch of another session, or a branch that was itself later forked. Plus the
  provenance the branch inherited and, stated explicitly, the provenance it did
  not.
* :func:`topology` — the branch tree over a SET of WALs, with the edges it cannot
  close reported as orphans and dangling children rather than guessed at.
* :func:`partition` — the fork partition of a recorded tail, derived from the
  durable classification inputs, plus the three Slice-1 refusals as findings.
* :func:`compare` — what two recorded histories did differently after the point
  they diverged at, and (the honest half) what a comparison cannot yet say.

What this module does NOT do, and will not pretend to
-----------------------------------------------------
It never RUNS anything. An offline reader has no live component, no workspace
handle and no fiber; the only honest post-mortem verbs are *enumerate* and
*compare*. Since item 250 Slice 3a the WAL carries one ``model-decision`` record
per completion crossing (model, tokens, cost, latency, attempts), and
:func:`compare` lists them per side; what it still does not carry is the
prompt/response digest, the tool calls and the request parameters (temperature,
seed), so re-executing a branch (``revl replay branch``) and the exact /
tool-only / model-substitute / counterfactual replay modes remain out of reach;
see :data:`NOT_COMPARABLE` and the design doc's Slice 3b.
"""

from __future__ import annotations

import os
from typing import Optional

from .wal import (KIND_BOUNDARY, KIND_COMPENSATION, KIND_EFFECT, KIND_EMISSION,
                  KIND_HINGE, KIND_OPAQUE, KIND_PROVISION, WALIntegrityError,
                  model_decisions, read_wal, scope_host_confined)


class BranchError(RuntimeError):
    """A WAL could not be read, or a branch question cannot be answered from it."""


#: What a comparison of two recorded histories cannot say, and why. Reported on
#: every compare document. A diff of two branches can say WHAT each did
#: differently, and since Slice 3a WHICH model answered at what cost; it cannot
#: say WHY, because what the model was asked and what it answered are not on the
#: record. Emptying this list is a claim that the WAL now carries those too.
NOT_COMPARABLE = (
    {"axis": "decisionCause",
     "why": "a model decision records the model, usage, latency and attempts "
            "(item 250 Slice 3a) but no prompt/response digest, tool call, "
            "temperature or seed, so a comparison shows which model each side "
            "asked and what it cost, never the reasoning that chose between them"},
    {"axis": "counterfactual",
     "why": "neither side can be re-executed from the WAL, so `what would have "
            "happened if` is not answerable here — only `what did happen` on "
            "each side is"},
)

#: The reader's one blind spot, stated on every partition document. The fork's
#: classification inputs became durable in Slice 2; a WAL written before that
#: cannot distinguish "this step declared no boundary-crossing capability" from
#: "this step's scope was never written down". Both read as the former, which is
#: what the LIVE classifier does with an absent scope too — so the offline
#: partition agrees with the live one on every WAL written by a runtime that
#: records scopes, and is stated as an assumption on any older one.
SCOPE_NOTE = (
    "a record with no `scope` reads as 'no declared boundary-crossing "
    "capability', exactly as the live classifier reads a scope of None. A WAL "
    "written before the fork's classification inputs became durable (item 250, "
    "Slice 2) cannot distinguish that from a scope that was never recorded."
)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _load(path: str) -> dict:
    try:
        return read_wal(path)
    except OSError as error:
        raise BranchError(f"cannot read WAL {path}: {error}") from None
    except WALIntegrityError as error:
        raise BranchError(str(error)) from None


def _first(records: list, kind: str) -> Optional[dict]:
    return next((r for r in records if r.get("record") == kind), None)


def _steps(records: list) -> list:
    """The per-step effect records, in recorded order. These are the only records
    the fork partition classifies; approval, deferral and commit records describe
    the session, not its timeline."""
    return [r for r in records if r.get("record") == "effect"]


def _entry(record: dict) -> dict:
    """One classified step as a report entry. Keyed on `seq` — the durable WAL
    position — not on the live timeline's step index, which a post-mortem reader
    has no way to bind to and which is absent on an explicit boundary record."""
    out = {
        "seq": record.get("seq"),
        "component": record.get("component"),
        "kind": record.get("kind"),
        "label": record.get("label"),
    }
    if record.get("stepIndex") is not None:
        out["stepIndex"] = record["stepIndex"]
    if record.get("scope") is not None:
        out["scope"] = record["scope"]
    return out


# ---------------------------------------------------------------------------
# lineage — what one WAL IS
# ---------------------------------------------------------------------------


def lineage(wal_path: str) -> dict:
    """The lineage facts of ONE WAL, read from its durable records alone.

    ``role`` is one of ``standalone`` (no fork touched this session),
    ``forked-parent`` (it was frozen at k when a branch diverged from it),
    ``branch`` (it IS a branch of another session), or ``forked-branch`` (a branch
    that was itself later forked — the serial N-branch exploration the design's
    Decision 4 describes, where each branch is forked again from its own fork
    point).
    """
    wal = _load(wal_path)
    records = wal["records"]
    branch_rec = _first(records, "fork-branch")
    frozen = _first(records, "fork-frozen")
    begin = _first(records, "fork-begin")
    complete = _first(records, "fork-complete")

    if branch_rec is not None and frozen is not None:
        role = "forked-branch"
    elif branch_rec is not None:
        role = "branch"
    elif frozen is not None:
        role = "forked-parent"
    else:
        role = "standalone"

    doc: dict = {
        "kind": "revl.branch-lineage",
        "schemaVersion": "1.0",
        "wal": os.path.abspath(wal_path),
        "role": role,
        "session": (branch_rec or {}).get("branch") or (frozen or {}).get("parent"),
        "generation": wal["header"].get("generation"),
        "complete": wal["complete"],
        "torn": wal["torn"],
        "steps": len(_steps(records)),
    }

    if branch_rec is not None:
        doc["parent"] = branch_rec.get("parent")
        doc["divergedAt"] = branch_rec.get("at")
        doc["parentWal"] = branch_rec.get("parentWal")
        doc["preserved"] = branch_rec.get("preserved") or {}
        doc["notPreserved"] = branch_rec.get("notPreserved") or []
        doc["inheritedResidue"] = _residue(branch_rec.get("crossed") or [],
                                           branch_rec.get("wouldCross") or [],
                                           inherited=True)
    if frozen is not None:
        doc["frozenAt"] = frozen.get("at")
        doc["child"] = (complete or {}).get("branch")
        doc["forkComplete"] = complete is not None
        doc["residue"] = _residue((begin or {}).get("crossed") or [],
                                  (begin or {}).get("wouldCross") or [])
        if complete is None:
            doc["midFork"] = (
                "this WAL carries `fork-begin` with no `fork-complete`: the "
                "process died mid-fork and the workspace may be half rewound. "
                "`revl recover` completes the parent rollback (item 250, "
                "Decision 5) — this reader states the window, it does not close "
                "it")
    return doc


def _residue(crossed: list, would_cross: list, *, inherited: bool = False) -> dict:
    outstanding = ([e.get("index") for e in crossed]
                   + [e.get("index") for e in would_cross])
    subject = "the branch stands on" if inherited else "the fork left"
    return {
        "clean": not outstanding,
        "outstanding": outstanding,
        "crossed": list(crossed),
        "wouldCross": list(would_cross),
        "proof": (
            f"{len(crossed)} emission(s) crossed the boundary above the fork "
            f"point and cannot be undone; {len(would_cross)} inverse(s) whose "
            "declared scope crosses the boundary were enumerated and never "
            f"fired. That is the residue {subject}; nothing else is claimed."
            if outstanding else
            "nothing crossed above the fork point and no inverse whose scope "
            "crosses the boundary was skipped — the rewind to the fork point was "
            "exact over host-confined state."),
    }


# ---------------------------------------------------------------------------
# topology — the tree over a SET of WALs
# ---------------------------------------------------------------------------


def topology(wal_paths: list) -> dict:
    """Reconstruct the branch tree across the supplied WALs.

    Every edge is read off a durable record; none is inferred from a filename or
    a timestamp. An edge whose other end was not supplied is reported — as an
    ``orphan`` (a branch whose parent WAL is missing) or as ``dangling`` (a parent
    naming a branch WAL that is missing) — rather than dropped, because a tree
    that quietly omits the half it could not see is the one way this view could
    mislead.
    """
    nodes: dict = {}
    unidentified = []
    for path in wal_paths:
        doc = lineage(path)
        session = doc.get("session")
        if session is None:
            unidentified.append({"wal": doc["wal"], "role": doc["role"],
                                 "why": "no fork record names this session, so it "
                                        "has no identity in the tree"})
            continue
        nodes[session] = doc

    orphans, dangling, edges = [], [], []
    for session, doc in nodes.items():
        parent = doc.get("parent")
        if parent is None:
            continue
        if parent in nodes:
            edges.append({"parent": parent, "branch": session,
                          "at": doc.get("divergedAt")})
        else:
            orphans.append({"wal": doc["wal"], "branch": session,
                            "parent": parent, "at": doc.get("divergedAt"),
                            "why": "the parent's WAL was not supplied, so the "
                                   "edge is named but not closed"})
    for session, doc in nodes.items():
        child = doc.get("child")
        if child is not None and child not in nodes:
            dangling.append({"wal": doc["wal"], "parent": session,
                             "branch": child, "at": doc.get("frozenAt"),
                             "why": "this session was frozen for a branch whose "
                                    "WAL was not supplied"})

    children: dict = {}
    for edge in edges:
        children.setdefault(edge["parent"], []).append(edge["branch"])
    roots = sorted(s for s, doc in nodes.items()
                   if doc.get("parent") not in nodes)
    return {
        "kind": "revl.branch-topology",
        "schemaVersion": "1.0",
        "sessions": [nodes[s] for s in sorted(nodes)],
        "edges": sorted(edges, key=lambda e: (e["parent"], e["branch"])),
        "children": {p: sorted(c) for p, c in sorted(children.items())},
        "roots": roots,
        "orphans": sorted(orphans, key=lambda e: e["branch"]),
        "dangling": sorted(dangling, key=lambda e: e["branch"]),
        "unidentified": unidentified,
    }


# ---------------------------------------------------------------------------
# partition — the fork partition of a RECORDED tail
# ---------------------------------------------------------------------------


def partition(wal_path: str, at: int) -> dict:
    """The honest fork partition of the tail above WAL position ``at``, derived
    from the durable records (item 250, Decision 3 — the same total partition over
    all seven step kinds the live fork enumerates, off the same classification
    inputs, now durable).

    ``at`` is a WAL ``seq``, the durable position; ``-1`` is the whole recorded
    tail. Nothing is run and nothing is rewound: this says what a fork HERE would
    put back and what it could not, over a session that is no longer live.

    The three Slice-1 refusals (a `KIND_OPAQUE` tail, a non-idempotent inverse in
    the span, a committed boundary below the point) are reported as FINDINGS, all
    of them, rather than raised on the first. The live fork refuses up front
    because it is about to touch a workspace; nothing here is about to touch
    anything, so enumerating every reason is strictly more useful than stopping at
    the first one.
    """
    wal = _load(wal_path)
    records = wal["records"]
    steps = _steps(records)
    tail = [r for r in steps if (r.get("seq") if r.get("seq") is not None else -1) > at]

    buckets: dict = {name: [] for name in (
        "hostConfined", "provisions", "crossed", "compensated",
        "wouldCross", "unrestored")}
    for record in reversed(tail):          # newest-first: the order a rewind runs
        kind = record.get("kind")
        if kind == KIND_EMISSION:
            (buckets["compensated"] if record.get("compensated")
             else buckets["crossed"]).append(record)
        elif kind == KIND_COMPENSATION:
            buckets["wouldCross"].append(record)
        elif kind == KIND_PROVISION:
            buckets["provisions"].append(record)
        elif kind == KIND_EFFECT:
            (buckets["hostConfined"] if scope_host_confined(record.get("scope"))
             else buckets["wouldCross"]).append(record)
        elif kind == KIND_OPAQUE:
            buckets["unrestored"].append(record)
        elif kind in (KIND_BOUNDARY, KIND_HINGE):
            continue                       # provably empty: no undo, no crossing
        else:
            buckets["unrestored"].append(record)   # a future kind stays covered

    crossed = [_entry(r) for r in buckets["crossed"]]
    compensated = [_entry(r) for r in buckets["compensated"]]
    would = [dict(_entry(r), why=_why_crosses(r)) for r in buckets["wouldCross"]]
    unrestored = [_entry(r) for r in buckets["unrestored"]]
    clean = not (crossed or compensated or would or unrestored)

    at_label = next((r.get("label") for r in steps if r.get("seq") == at),
                    "(before the first recorded step)")
    findings = _findings(wal, records, buckets)
    return {
        "kind": "revl.branch-partition",
        "schemaVersion": "1.0",
        "wal": os.path.abspath(wal_path),
        "at": at,
        "atLabel": at_label,
        "performed": False,
        "wouldRewind": [_entry(r) for r in buckets["hostConfined"]],
        "wouldWithdraw": [_entry(r) for r in buckets["provisions"]],
        "emissionsCrossed": crossed,
        "emissionsCompensated": compensated,
        "wouldCrossOnRewind": would,
        "unrestored": unrestored,
        "findings": findings,
        "forkable": not findings,
        "residue": {
            "clean": clean,
            "outstanding": sorted(
                e["seq"] for e in crossed + compensated + would + unrestored
                if e["seq"] is not None),
            "proof": (
                f"a fork at WAL position {at} would put back "
                f"{len(buckets['hostConfined'])} host-confined inverse(s) and "
                f"{len(buckets['provisions'])} provision(s). "
                f"{len(crossed) + len(compensated)} emission(s) already crossed "
                f"and cannot be undone; {len(would)} inverse(s) whose declared "
                f"scope crosses the boundary would be enumerated and never fired; "
                f"{len(unrestored)} step(s) could not be restored."
                if not clean else
                f"the tail above WAL position {at} is only host-confined "
                "inverses and provisions: a fork here would be an exact rewind."),
        },
        "readerNote": SCOPE_NOTE,
    }


def _why_crosses(record: dict) -> str:
    if record.get("kind") == KIND_COMPENSATION:
        return ("a compensation is a second boundary crossing chosen to offset "
                "the first — outbound by definition, so it is enumerated and "
                "never fired by a fork rewind")
    scope = record.get("scope") or {}
    caps = ", ".join(scope.get("caps") or []) or "(unnamed)"
    return (f"the inverse's declared capability scope [{caps}] is not provably "
            "host-confined, so running it would itself cross the boundary "
            "mid-fork")


def _findings(wal: dict, records: list, buckets: dict) -> list:
    """The Slice-1 refusals, as findings over a recorded tail."""
    findings = []
    if any(r.get("record") in ("flushed", "commit-approved") for r in records):
        findings.append({
            "finding": "committed-boundary",
            "why": "this WAL carries a durably committed crossing (a `flushed` / "
                   "`commit-approved` record). A fork cannot rewind to a point "
                   "before a send that already committed and closed — the "
                   "rewindable window is [last commit boundary, head] (item 250, "
                   "Decision 6)"})
    opaque = [_entry(r) for r in buckets["unrestored"]
              if r.get("kind") == KIND_OPAQUE]
    if opaque:
        findings.append({
            "finding": "opaque-tail",
            "steps": opaque,
            "why": "the tail contains a step the recorder cannot restore, so a "
                   "fork must not claim the state below it (item 250, Decision 3)"})
    non_idempotent = [_entry(r) for r in buckets["hostConfined"]
                      if r.get("undoIdempotent") is False
                      or (r.get("inverse") or {}).get("undo_idempotent") is False]
    if non_idempotent:
        findings.append({
            "finding": "non-idempotent-span",
            "steps": non_idempotent,
            "why": "the span holds a declared non-idempotent-total inverse; a "
                   "crash mid-fork could re-run it against a partially rewound "
                   "workspace and double-apply it (item 250, Decision 5)"})
    if wal["torn"]:
        findings.append({
            "finding": "torn-tail",
            "why": "the final record is half written (the crash itself), so the "
                   "recorded tail may be shorter than what actually ran"})
    return findings


# ---------------------------------------------------------------------------
# compare — two recorded histories, after the point they diverged
# ---------------------------------------------------------------------------


def compare(left_path: str, right_path: str) -> dict:
    """Compare two recorded histories that share a fork point.

    Establishes the relation from durable lineage records only — ``parent-branch``
    (the right WAL is a branch of the left, or the reverse) or ``siblings`` (both
    forked from the same session at the same step, the serial exploration Slice 1
    supports). Two WALs with no recorded relation are NOT compared against an
    invented common point: the document says ``comparable: false`` and stops,
    because a divergence point is the only thing that makes a per-side tail mean
    anything.
    """
    left, right = lineage(left_path), lineage(right_path)
    relation, diverged, note = _relate(left, right)
    doc: dict = {
        "kind": "revl.branch-compare",
        "schemaVersion": "1.0",
        "relation": relation,
        "comparable": relation != "unrelated",
        "divergedAt": diverged,
        "left": _side(left_path, left, diverged),
        "right": _side(right_path, right, diverged),
        "notComparable": [dict(e) for e in NOT_COMPARABLE],
    }
    if relation == "unrelated":
        doc["why"] = note
        return doc
    doc["delta"] = _delta(doc["left"], doc["right"])
    return doc


def _relate(left: dict, right: dict) -> tuple:
    """(relation, divergedAt, why) from the two lineage documents."""
    if left.get("session") is not None and right.get("parent") == left["session"]:
        return "parent-branch", right.get("divergedAt"), ""
    if right.get("session") is not None and left.get("parent") == right["session"]:
        return "parent-branch", left.get("divergedAt"), ""
    lp, rp = left.get("parent"), right.get("parent")
    if lp is not None and lp == rp:
        if left.get("divergedAt") == right.get("divergedAt"):
            return "siblings", left.get("divergedAt"), ""
        return ("siblings", None,
                "both are branches of the same parent but at different steps, so "
                "there is no single divergence point to compare against")
    return ("unrelated", None,
            "neither WAL's durable lineage names the other, and they share no "
            "recorded parent. There is no divergence point, so a per-side tail "
            "would be a diff of two unrelated runs dressed up as a branch "
            "comparison")


def _side(path: str, doc: dict, diverged: Optional[int]) -> dict:
    """One side of the comparison: its identity and the tail it recorded after the
    divergence point. For a BRANCH the whole WAL is the tail (it began at the fork
    point); for the frozen PARENT the tail is what it recorded above ``at`` — the
    history the fork rewound, which is exactly the alternative the branch replaced.
    """
    wal = _load(path)
    steps = _steps(wal["records"])
    if doc["role"] in ("branch", "forked-branch"):
        tail = steps
    elif diverged is None:
        tail = steps
    else:
        tail = [r for r in steps
                if (r.get("seq") if r.get("seq") is not None else -1) > diverged]
    return {
        "wal": doc["wal"],
        "session": doc.get("session"),
        "role": doc["role"],
        "steps": [_entry(r) for r in tail],
        "emissionsCrossed": [_entry(r) for r in tail
                             if r.get("kind") == KIND_EMISSION],
        "modelDecisions": _decisions(wal["records"], tail),
        "capabilities": (doc.get("preserved") or {}).get("capabilities"),
        "complete": doc.get("complete"),
    }


def _decisions(records: list, tail: list) -> list:
    """The model decisions made in `tail` (item 250 Slice 3a), one entry per
    `model-decision` record whose crossing is a step of the tail, in the tail's
    order. Joined on the crossing identity the writer keyed the record by,
    ``(component, stepIndex)``; a tail step with no record made no completion
    (or was written before Slice 3a, which the reader cannot tell apart and
    :data:`NOT_COMPARABLE` says so)."""
    by_crossing = model_decisions(records)
    out = []
    for step in tail:
        record = by_crossing.get((step.get("component"), step.get("stepIndex")))
        if record is None:
            continue
        llm = record.get("llm") or {}
        out.append({
            "seq": step.get("seq"),
            "label": step.get("label"),
            "outcome": record.get("outcome"),
            "model": llm.get("model"),
            "tokensIn": llm.get("tokensIn"),
            "tokensOut": llm.get("tokensOut"),
            "cost": llm.get("cost"),
            "latencySeconds": llm.get("latencySeconds"),
            "attempts": llm.get("attempts"),
            "attemptCeiling": llm.get("attemptCeiling"),
        })
    return out


def _delta(left: dict, right: dict) -> dict:
    """What the two sides did differently, keyed on (component, kind, label) —
    the step identity a WAL actually carries. Seq numbers are per-session and
    would make two identical histories look different, so they are not the key."""
    def key(entry):
        return (entry.get("component"), entry.get("kind"), entry.get("label"))

    lsteps, rsteps = left["steps"], right["steps"]
    first = None
    for index in range(max(len(lsteps), len(rsteps))):
        lhs = lsteps[index] if index < len(lsteps) else None
        rhs = rsteps[index] if index < len(rsteps) else None
        if lhs is None or rhs is None or key(lhs) != key(rhs):
            first = {"position": index,
                     "left": lhs, "right": rhs,
                     "why": ("the histories agree up to this position and differ "
                             "from it on")}
            break
    lkeys = [key(e) for e in lsteps]
    rkeys = [key(e) for e in rsteps]
    only_left = [e for e in lsteps if key(e) not in rkeys]
    only_right = [e for e in rsteps if key(e) not in lkeys]
    lcaps = set(left.get("capabilities") or [])
    rcaps = set(right.get("capabilities") or [])
    return {
        "sharedPrefix": first["position"] if first else min(len(lsteps), len(rsteps)),
        "firstDifference": first,
        "onlyLeft": only_left,
        "onlyRight": only_right,
        "emissions": {
            "left": len(left["emissionsCrossed"]),
            "right": len(right["emissionsCrossed"]),
        },
        "modelDecisions": {
            "left": len(left.get("modelDecisions") or []),
            "right": len(right.get("modelDecisions") or []),
        },
        "capabilities": {
            "onlyLeft": sorted(lcaps - rcaps),
            "onlyRight": sorted(rcaps - lcaps),
        },
        "identical": not (only_left or only_right or first),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(doc: dict) -> str:
    """The human view of any document this module produces."""
    return {
        "revl.branch-lineage": _render_lineage,
        "revl.branch-topology": _render_topology,
        "revl.branch-partition": _render_partition,
        "revl.branch-compare": _render_compare,
    }[doc["kind"]](doc)


def _render_lineage(doc: dict) -> str:
    lines = [f"{doc['role'].upper()}  {doc.get('session') or '(unidentified)'}",
             f"  wal        {doc['wal']}",
             f"  steps      {doc['steps']}  generation {doc.get('generation')}"]
    if doc.get("parent"):
        lines.append(f"  branch of  {doc['parent']} at step {doc.get('divergedAt')}")
        if doc.get("parentWal"):
            lines.append(f"  parent wal {doc['parentWal']}")
    if doc.get("child"):
        lines.append(f"  forked to  {doc['child']} at step {doc.get('frozenAt')}")
    if doc.get("midFork"):
        lines += ["", f"  MID-FORK: {doc['midFork']}"]
    preserved = doc.get("preserved") or {}
    if preserved:
        lines += ["", "  preserved:"]
        for name in sorted(preserved):
            lines.append(f"    {name:<14} {preserved[name]}")
    for entry in doc.get("notPreserved") or []:
        lines.append(f"    NOT {entry['axis']:<10} {entry['why']}")
    for name in ("residue", "inheritedResidue"):
        residue = doc.get(name)
        if residue:
            tag = "CLEAN" if residue["clean"] else "RESIDUE"
            lines += ["", f"  {name} [{tag}]: {residue['proof']}"]
    return "\n".join(lines)


def _render_topology(doc: dict) -> str:
    lines = ["branch topology"]
    by_id = {node.get("session"): node for node in doc["sessions"]}

    def walk(session: str, depth: int) -> None:
        node = by_id.get(session) or {}
        at = node.get("divergedAt")
        suffix = f"  (forked at step {at})" if at is not None else ""
        lines.append(f"{'  ' * (depth + 1)}{session}  [{node.get('role')}]{suffix}")
        for child in doc["children"].get(session) or []:
            walk(child, depth + 1)

    for root in doc["roots"]:
        walk(root, 0)
    if not doc["roots"]:
        lines.append("  (no identified session in the supplied WALs)")
    for entry in doc["orphans"]:
        lines.append(f"  ORPHAN   {entry['branch']} -> parent {entry['parent']}: "
                     f"{entry['why']}")
    for entry in doc["dangling"]:
        lines.append(f"  DANGLING {entry['parent']} -> branch {entry['branch']}: "
                     f"{entry['why']}")
    for entry in doc["unidentified"]:
        lines.append(f"  UNNAMED  {entry['wal']}: {entry['why']}")
    return "\n".join(lines)


def _render_partition(doc: dict) -> str:
    lines = [f"fork partition at WAL position {doc['at']}  ({doc['atLabel']})",
             f"  wal {doc['wal']}", ""]
    for entry in doc["wouldRewind"]:
        lines.append(f"  would rewind    {entry['label']}")
    for entry in doc["wouldWithdraw"]:
        lines.append(f"  would withdraw  {entry['label']}")
    for entry in doc["emissionsCrossed"] + doc["emissionsCompensated"]:
        lines.append(f"  CROSSED         {entry['label']}  (cannot be undone)")
    for entry in doc["wouldCrossOnRewind"]:
        lines.append(f"  NOT FIRED       {entry['label']}  {entry['why']}")
    for entry in doc["unrestored"]:
        lines.append(f"  UNRESTORED      {entry['label']}")
    if not (doc["wouldRewind"] or doc["wouldWithdraw"] or doc["emissionsCrossed"]
            or doc["emissionsCompensated"] or doc["wouldCrossOnRewind"]
            or doc["unrestored"]):
        lines.append("  (the tail above this position is empty)")
    for finding in doc["findings"]:
        lines.append(f"  REFUSES [{finding['finding']}] {finding['why']}")
    residue = doc["residue"]
    lines += ["", f"residue proof [{'CLEAN' if residue['clean'] else 'RESIDUE'}]:",
              f"  {residue['proof']}", "", f"note: {doc['readerNote']}"]
    return "\n".join(lines)


def _render_compare(doc: dict) -> str:
    lines = [f"relation: {doc['relation'].upper()}"]
    if not doc["comparable"]:
        lines += [f"  {doc['why']}", "",
                  f"  left  {doc['left']['wal']}",
                  f"  right {doc['right']['wal']}"]
        return "\n".join(lines)
    lines.append(f"  diverged at step {doc['divergedAt']}")
    delta = doc["delta"]
    for side in ("left", "right"):
        entry = doc[side]
        lines.append(f"  {side:<5} {entry.get('session') or entry['wal']}  "
                     f"[{entry['role']}]  {len(entry['steps'])} step(s), "
                     f"{len(entry['emissionsCrossed'])} emission(s), "
                     f"{len(entry.get('modelDecisions') or [])} model decision(s)")
        for decision in entry.get("modelDecisions") or []:
            lines.append(f"        model {decision.get('model') or '(unreported)'}"
                         f"  {decision['label']}  {decision['outcome']}  "
                         f"attempts {decision.get('attempts')}/"
                         f"{decision.get('attemptCeiling')}")
    lines += ["", f"  shared prefix: {delta['sharedPrefix']} step(s)"]
    first = delta["firstDifference"]
    if first is None:
        lines.append("  the two histories are identical after the fork point")
    else:
        left = (first["left"] or {}).get("label", "(nothing)")
        right = (first["right"] or {}).get("label", "(nothing)")
        lines.append(f"  first difference at position {first['position']}: "
                     f"left {left} / right {right}")
    for entry in delta["onlyLeft"]:
        lines.append(f"    only left   {entry['label']}")
    for entry in delta["onlyRight"]:
        lines.append(f"    only right  {entry['label']}")
    lines.append("")
    for entry in doc["notComparable"]:
        lines.append(f"  cannot say [{entry['axis']}]: {entry['why']}")
    return "\n".join(lines)
