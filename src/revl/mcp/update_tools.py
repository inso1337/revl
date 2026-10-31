"""The operator update surface: check a release feed, apply it through the gate.

`revl update` (the CLI) reads and stages a release; it cannot apply one,
because a separate process has no handle on a running session. These verbs are
that half. They fit beside `revl_ship` and reuse the same primitives: the
candidate is compiled and admitted against the running manifest, planned, and
only then swapped. Nothing here opens a second mutation path.

The verbs are off unless the operator starts the server with ``--update`` and
names the feed. That flag is the grant: a caller over the transport cannot
point the session at a different repository, token or staging directory. A
plan is bound to a hash over the release identity, the staged bytes and the
running generation, so apply cannot act on a plan made against a system that
has since moved.

The trust boundary is docs/updates.md's: the manifest validates, the artifact
hash matches, and the staged release is the bytes the manifest names. Once
verified they are operator-sanctioned code, compiled as files (so they may
carry host bodies) exactly as the operator's own checkout would be. Admission
still decides whether the running composition may take them.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from ..update.errors import UpdateError
from ..update.feed import resolve_release
from ..update.fetch import find_staged, stage_release
from ..update.identity import compare_release, current_identity
from ..update.journal import Journal

#: How many plans one server keeps. A plan is small; the bound is there so a
#: long-lived session cannot grow it without limit.
PLAN_LIMIT = 8

_CONFIG: dict = {
    "enabled": False,
    "repo": "inso1337/revl",
    "project": None,
    "channel": "stable",
    "token_env": "REVL_UPDATE_TOKEN",
    "home": None,
    "api_base": None,
    "trust_tags": False,
    "replacing": (),
}
_PLANS: dict[str, dict] = {}
_LOCK = threading.RLock()
#: Identifies this server process in the journal and in a plan: a second
#: server sharing the update home must not consume another session's rollback
#: target, and a plan is bound to the session it was made in.
_SESSION_ID = f"{os.getpid()}:{time.time_ns()}"


def configure(**values: object) -> None:
    """Configure the update surface. Only the server's own flags call this."""
    unknown = set(values) - set(_CONFIG)
    if unknown:
        raise ValueError(f"unknown update option(s): {', '.join(sorted(unknown))}")
    with _LOCK:
        _CONFIG.update(values)
        _CONFIG["enabled"] = bool(_CONFIG["enabled"])
        _CONFIG["replacing"] = tuple(_CONFIG["replacing"] or ())
        if not _CONFIG["enabled"]:
            _PLANS.clear()


def config() -> dict:
    with _LOCK:
        return dict(_CONFIG)


def reset() -> None:
    """Tests and a fresh server: back to disabled, no plans."""
    with _LOCK:
        _CONFIG.update({
            "enabled": False, "repo": "inso1337/revl", "project": None,
            "channel": "stable", "token_env": "REVL_UPDATE_TOKEN",
            "home": None, "api_base": None, "trust_tags": False,
            "replacing": ()})
        _PLANS.clear()


def _server():
    from . import server  # noqa: PLC0415 - avoids a circular import
    return server


def _journal(cfg: dict) -> Journal:
    return Journal(cfg["home"])


def _token(cfg: dict) -> str | None:
    return os.environ.get(cfg["token_env"]) if cfg["token_env"] else None


def _refusal(error: UpdateError, **extra) -> dict:
    payload = {"ok": False, "refusal": error.as_dict()}
    payload.update(extra)
    return payload


def _disabled() -> dict:
    return _refusal(UpdateError(
        "UPDATE_DISABLED",
        "this server was not started with update enabled, so no release feed "
        "is configured and no release may be applied",
        fix="restart `revl mcp serve` with --update, --update-repo OWNER/NAME "
            "and, for a private feed, --update-token-env VAR"))


def _require_enabled() -> dict | None:
    return None if _CONFIG["enabled"] else _disabled()


def _resolve(cfg: dict):
    return resolve_release(
        cfg["repo"], channel=cfg["channel"], project=cfg["project"],
        token=_token(cfg), token_env=cfg["token_env"],
        api_base_override=cfg["api_base"])


def _generation() -> int | None:
    server = _server()
    if not server.SESSION.loaded:
        return None
    return server.SESSION.state(drain=False).get("generation")


def _ir_digest(ir: dict | None) -> str | None:
    """The running composition's document digest, so a plan is bound to it.

    The generation counter alone is not enough: a session that unloads and
    loads a different composition starts again at generation 1.
    """
    if ir is None:
        return None
    encoded = json.dumps(ir, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _provided_keys() -> list[str]:
    server = _server()
    if not server.SESSION.loaded:
        return []
    return list(server.SESSION.state(drain=False).get("providedKeys") or [])


def _files_digest(paths: list[str]) -> tuple[str, list[dict]]:
    digest = hashlib.sha256()
    entries: list[dict] = []
    for path in sorted(paths):
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError as error:
            raise UpdateError(
                "UPDATE_FILES",
                f"the staged source {path} can no longer be read: {error}",
                fix="stage the release again, then plan it again") from None
        sha = hashlib.sha256(body).hexdigest()
        entries.append({"path": path, "sha256": sha, "size": len(body)})
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest(), entries


def _plan_hash(binding: dict) -> str:
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _widening(old_ir: dict, files: list[str]) -> dict:
    """Every authority axis the release widens, not only its crossings.

    `audit_diff` carries seven differs: the crossing set and its reach,
    per-emission capability scopes, host backends, recovery inverses,
    capability registers and emission cardinality. A release can widen any of
    them while adding no crossing, so all of them feed the acknowledgement
    decision. A comparison that cannot be computed is reported as widened: an
    apply pends rather than proceeding on a guess.
    """
    from ..audit_diff import (audit_report, diff_backends,  # noqa: PLC0415
                              diff_capability_scopes, diff_cardinality,
                              diff_recovery, diff_registers, evaluate)
    from .server import compile_under_authoring  # noqa: PLC0415

    try:
        # The same compiler door every verb uses. Release sources are files,
        # so they compile as operator-sanctioned code, exactly as the admit
        # and swap stages above compiled them.
        candidate_ir = compile_under_authoring(None, list(files))
        before = audit_report(old_ir)
        after = audit_report(candidate_ir)
        drift = evaluate(before, after)
        scopes = diff_capability_scopes(before, after)
        backends = diff_backends(before, after)
        recovery = diff_recovery(before, after)
        registers = diff_registers(before, after)
        cardinality = diff_cardinality(before, after)
    except Exception as error:  # noqa: BLE001 - fail closed, name the reason
        return {"widened": True, "unknown":
                f"the audit diff could not be computed: {error}"}
    buckets = {
        "added": list(drift["added"]),
        "reachWeakened": list(drift["reach_weakened"]),
        "scopesWidened": list(scopes["scope_widened"]),
        "backendsAdded": list(backends["backends_added"]),
        "recoveryDropped": list(recovery["recovery_dropped"]),
        "recoveryWeakened": list(recovery["recovery_weakened"]),
        "registersWeakened": list(registers["registers_weakened"]),
        "cardinalityWidened": list(cardinality["cardinality_widened"]),
    }
    return {"widened": any(buckets.values()), **buckets}


def _store_plan(plan: dict) -> None:
    with _LOCK:
        _PLANS[plan["planHash"]] = plan
        while len(_PLANS) > PLAN_LIMIT:
            oldest = min(_PLANS.values(), key=lambda item: item["createdAt"])
            _PLANS.pop(oldest["planHash"], None)


def _require_plan(plan_hash: str) -> dict:
    with _LOCK:
        plan = _PLANS.get(plan_hash)
    if plan is None:
        raise UpdateError(
            "UPDATE_PLAN_UNKNOWN",
            f"no update plan named {plan_hash!r} is held by this server; a "
            "plan is server side and is forgotten after it applies",
            fix="call revl_update_apply with no planHash to plan the release "
                "again")
    return plan


def _check_arguments(arguments: dict) -> dict:
    del arguments  # the feed is the operator's configuration, not the caller's
    cfg = _CONFIG
    journal = _journal(cfg)
    resolved = _resolve(cfg)
    identity = current_identity()
    comparison = compare_release(resolved, identity, cfg["channel"])
    journal.append("check", repo=cfg["repo"], channel=cfg["channel"],
                   version=resolved.version, updateAvailable=bool(
                       comparison.get("updateAvailable")))
    server = _server()
    artifact = (resolved.manifest.unpackable_artifact()
                if resolved.manifest is not None else None)
    apply_available = (
        server.SESSION.loaded
        and resolved.manifest is not None
        and comparison.get("updateAvailable", False)
        and artifact is not None
        and artifact.kind == "source")
    return {
        "ok": True,
        "feed": {"repo": cfg["repo"], "channel": cfg["channel"]},
        "identity": identity,
        "release": resolved.as_report(),
        "comparison": comparison,
        "applyAvailable": apply_available,
        "applyReason": None if apply_available else (
            "nothing is loaded" if not server.SESSION.loaded else
            "the release carries no composition source artifact" if (
                artifact is None or artifact.kind != "source") else
            "no newer release on the channel"),
        "note": "check reads the feed and compares identities; it fetches and "
                "compiles nothing. revl_update_apply with no planHash stages "
                "and plans the release",
    }


def _tag_fallback_refusal() -> dict:
    return _refusal(UpdateError(
        "UPDATE_TAG_FALLBACK",
        "the feed has no release manifest, so the release's bytes have no "
        "declared sha256 and the new version is not pinned to a manifest; a "
        "tag fallback is never applied automatically",
        fix="publish a release.json asset on the release, or start the server "
            "with --update-trust-tags to accept a tag fallback explicitly"))


def _stage_for_apply(resolved, cfg: dict, journal: Journal):
    artifact = None
    if resolved.manifest is None:
        if not cfg["trust_tags"]:
            return None, _tag_fallback_refusal()
    else:
        artifact = resolved.manifest.unpackable_artifact()
        if artifact is None or artifact.kind != "source":
            return None, _refusal(UpdateError(
                "UPDATE_NOT_COMPOSITION",
                f"release {resolved.version} carries no `source` artifact; a "
                "live apply needs the release's composition source tree, not "
                "an installer or a toolchain sdist",
                fix="publish an artifact of kind `source` with the release"))
    for staged in find_staged(cfg["home"], project=resolved.project):
        if (staged.version == resolved.version
                and staged.commit == resolved.commit):
            if resolved.manifest is not None:
                # a staging is reused only when its bytes are the ones the
                # manifest names. A tag-fallback fetch of the same commit was
                # never checked against a declared sha256, so it is not.
                if staged.tag_fallback or staged.digest != artifact.sha256:
                    continue
            return staged, None
    try:
        staged = stage_release(resolved, home_dir=cfg["home"],
                               token=_token(cfg),
                               api_base_override=cfg["api_base"])
    except UpdateError as error:
        return None, _refusal(error)
    journal.append("fetch", project=staged.project, version=staged.version,
                   digest=staged.digest, directory=str(staged.directory))
    return staged, None


def _tool_update_check(arguments: dict) -> dict:
    disabled = _require_enabled()
    if disabled is not None:
        return disabled
    try:
        return _check_arguments(arguments)
    except UpdateError as error:
        return _refusal(error)


def _tool_update_status(arguments: dict) -> dict:
    del arguments
    cfg = _CONFIG
    server = _server()
    loaded = server.SESSION.loaded
    plan_documents = []
    with _LOCK:
        for plan in _PLANS.values():
            plan_documents.append({
                "planHash": plan["planHash"],
                "release": plan["release"],
                "generation": plan["generation"],
                "widening": plan["widening"]["widened"],
            })
    staged = [release.as_report()
              for release in find_staged(cfg["home"], project=cfg["project"])]
    return {
        "ok": True,
        "enabled": cfg["enabled"],
        "config": {key: cfg[key] for key in
                   ("repo", "project", "channel", "home", "trust_tags")},
        "identity": current_identity(),
        "loaded": loaded,
        "generation": _generation(),
        "staged": staged,
        "plans": plan_documents,
        "journal": _journal(cfg).tail(20) if cfg["enabled"] else [],
    }


def _plan_apply(arguments: dict) -> dict:
    server = _server()
    cfg = _CONFIG
    journal = _journal(cfg)
    if not server.SESSION.loaded:
        return _refusal(UpdateError(
            "UPDATE_NOTHING_LOADED",
            "nothing is running, so there is no composition to update; the "
            "live apply replaces a running generation",
            fix="load the composition first (revl_load), then plan the update"))
    try:
        resolved = _resolve(cfg)
    except UpdateError as error:
        return _refusal(error)
    identity = current_identity()
    comparison = compare_release(resolved, identity, cfg["channel"])
    if not comparison.get("updateAvailable") and not arguments.get("force"):
        return {"ok": True, "planned": False, "applied": False,
                "comparison": comparison, "live": comparison.get("live"),
                "restartRequired": comparison.get("restartRequired"),
                "note": "the running version is the newest release on the "
                        "channel; nothing was staged or planned"}
    if (resolved.manifest is not None and not resolved.manifest.live
            and not arguments.get("force")):
        return _refusal(
            UpdateError(
                "UPDATE_NOT_LIVE",
                f"release {resolved.version} does not declare itself live-safe "
                "(`live` is false), so this host may stage it but not apply it "
                "to a running composition",
                fix="stage it and apply it at the next start, or pass force to "
                    "apply a release the feed has not certified live"),
            comparison=comparison)
    staged, refusal = _stage_for_apply(resolved, cfg, journal)
    if refusal is not None:
        return refusal
    files = staged.source_paths()
    if not files:
        return _refusal(UpdateError(
            "UPDATE_NO_SOURCES",
            f"the staged release {staged.project} {staged.version} carries no "
            ".rvl sources, so it is not a composition tree",
            fix="publish the release's composition source files"))
    replacing = list(cfg["replacing"])
    admission = server._tool_admit({"files": files,
                                    "manifest": server.SESSION.ir,
                                    "replacing": replacing})
    if not admission.get("ok"):
        journal.append("refusal", stage="admit", project=resolved.project,
                       version=resolved.version, sessionId=_SESSION_ID)
        return {"ok": False, "planned": False, "applied": False,
                "stoppedAt": "admit", "admission": admission,
                "note": "the running admission gate refused the release; the "
                        "running composition is untouched"}
    rehearsal = server._tool_plan({"files": files, "replacing": replacing})
    widening = _widening(server.SESSION.ir, files)
    records_digest, entries = _files_digest(files)
    generation = _generation()
    ir_digest = _ir_digest(server.SESSION.ir)
    binding = {
        "protocol": "revl.update.plan/1",
        "feed": {"repo": cfg["repo"], "channel": cfg["channel"]},
        "release": {"project": resolved.project, "version": resolved.version,
                    "commit": resolved.commit,
                    "manifestDigest": (resolved.manifest.digest()
                                       if resolved.manifest is not None
                                       else None)},
        "artifact": {"sha256": staged.digest,
                     "name": staged.artifact_name},
        "sources": records_digest,
        "generation": generation,
        "irDigest": ir_digest,
        "replacing": replacing,
        "trustTags": bool(cfg["trust_tags"]),
        "sessionId": _SESSION_ID,
    }
    plan_hash = _plan_hash(binding)
    plan = {
        "planHash": plan_hash,
        "binding": binding,
        "release": {"project": resolved.project,
                    "version": resolved.version,
                    "tag": resolved.tag,
                    "commit": resolved.commit,
                    "manifestDigest": binding["release"]["manifestDigest"],
                    "artifactSha256": staged.digest},
        "files": files,
        "sourcesDigest": records_digest,
        "entries": entries,
        "replacing": replacing,
        "generation": generation,
        "irDigest": ir_digest,
        "sessionId": _SESSION_ID,
        "widening": widening,
        "live": comparison.get("live", False),
        "restartRequired": comparison.get("restartRequired", True),
        "createdAt": time.time(),
    }
    _store_plan(plan)
    journal.append("plan", planHash=plan_hash, project=resolved.project,
                   version=resolved.version, generation=generation,
                   sessionId=_SESSION_ID, widening=widening["widened"])
    return {
        "ok": True,
        "planned": True,
        "applied": False,
        "planHash": plan_hash,
        "release": plan["release"],
        "staged": staged.as_report(),
        "comparison": comparison,
        "generation": generation,
        "live": plan["live"],
        "restartRequired": plan["restartRequired"],
        "admission": {"ok": True,
                      "note": "the running gate admitted the release against "
                              "the running manifest"},
        "plan": {"against": rehearsal.get("against"),
                 "provisions": rehearsal.get("provisions"),
                 "withdrawals": rehearsal.get("withdrawals"),
                 "note": rehearsal.get("note")},
        "widening": widening,
        "approvalRequired": widening["widened"],
        "next": (f"call revl_update_apply with planHash {plan_hash} and "
                 "acknowledgeWidening true" if widening["widened"] else
                 f"call revl_update_apply with planHash {plan_hash}"),
        "note": "nothing was applied; the plan is bound to the running "
                "generation and composition, to the staged bytes, and to this "
                "server session",
    }


def _apply_plan(arguments: dict) -> dict:
    server = _server()
    cfg = _CONFIG
    journal = _journal(cfg)
    plan_hash = arguments.get("planHash")
    try:
        plan = _require_plan(plan_hash)
    except UpdateError as error:
        return _refusal(error)
    if not server.SESSION.loaded:
        return _refusal(UpdateError(
            "UPDATE_NOTHING_LOADED",
            "nothing is running now; the plan was made against a loaded "
            "composition", fix="plan the update again"))
    generation = _generation()
    if (generation != plan["generation"]
            or plan.get("sessionId") != _SESSION_ID
            or _ir_digest(server.SESSION.ir) != plan["irDigest"]):
        return _refusal(UpdateError(
            "UPDATE_PLAN_STALE",
            f"the plan was made against generation {plan['generation']} of a "
            f"different running composition (now generation {generation}); "
            "what would be applied is no longer what was planned",
            fix="call revl_update_apply with no planHash to plan against the "
                "running generation"), stalePlan=plan_hash)
    if plan["widening"]["widened"] and not arguments.get("acknowledgeWidening"):
        return {"ok": False, "planned": True, "applied": False,
                "approvalRequired": True, "planHash": plan_hash,
                "widening": plan["widening"],
                "refusal": UpdateError(
                    "UPDATE_WIDENS",
                    "the release adds or widens the composition's "
                    "irreversible reach; apply needs that acknowledged",
                    fix="call revl_update_apply again with the same planHash "
                        "and acknowledgeWidening true").as_dict()}
    # The plan binds the bytes, not just the path: a staged file edited after
    # the plan was admitted is refused here, never swapped.
    try:
        current_digest, _entries = _files_digest(plan["files"])
    except UpdateError as error:
        return _refusal(error)
    if current_digest != plan["sourcesDigest"]:
        return _refusal(UpdateError(
            "UPDATE_PLAN_TAMPERED",
            "the staged release changed after it was admitted and planned; "
            "what would be applied is not what the gate admitted",
            fix="discard the staging and plan the verified release again"),
            stalePlan=plan_hash)
    before_generation = generation
    before_keys = _provided_keys()
    swap = server._tool_swap({"files": plan["files"],
                              "replacing": plan["replacing"]})
    if not swap.get("ok"):
        journal.append("refusal", stage="swap", planHash=plan_hash,
                       sessionId=_SESSION_ID)
        return {"ok": False, "applied": False, "stoppedAt": "swap",
                "swap": swap,
                "note": "the swap was refused; the running composition is "
                        "untouched"}
    after_generation = _generation()
    after_keys = _provided_keys()
    healthy = (server.SESSION.loaded
               and set(before_keys).issubset(set(after_keys)))
    if not healthy:
        reverted = False
        try:
            server.SESSION.rollback()
            reverted = True
        except Exception:  # noqa: BLE001 - report the uncertain outcome
            reverted = False
        journal.append("auto-revert", planHash=plan_hash,
                       release=plan["release"], reverted=reverted,
                       sessionId=_SESSION_ID,
                       before=before_generation, after=after_generation)
        return {"ok": False, "applied": False, "autoReverted": reverted,
                "stoppedAt": "health",
                "health": {"before": before_keys, "after": after_keys},
                "note": ("the successor did not serve the running keys; "
                         + ("the previous generation was restored" if reverted
                            else "the automatic restore FAILED and the "
                                 "session is in the updated generation"))}
    with _LOCK:
        _PLANS.pop(plan_hash, None)
    journal.append("apply", planHash=plan_hash, **plan["release"],
                   sessionId=_SESSION_ID,
                   generationBefore=before_generation,
                   generationAfter=after_generation)
    return {
        "ok": True,
        "applied": True,
        "planHash": plan_hash,
        "release": plan["release"],
        "generationBefore": before_generation,
        "generation": after_generation,
        "widening": plan["widening"],
        "live": plan.get("live"),
        "restartRequired": plan.get("restartRequired"),
        "providedKeys": after_keys,
        "note": "the release is the running generation; the previous "
                "generation is retained, so revl_update_rollback can restore it",
    }


def _tool_update_apply(arguments: dict) -> dict:
    disabled = _require_enabled()
    if disabled is not None:
        return disabled
    if arguments.get("planHash"):
        with _LOCK:
            return _apply_plan(arguments)
    try:
        return _plan_apply(arguments)
    except UpdateError as error:
        return _refusal(error)


def _tool_update_rollback(arguments: dict) -> dict:
    del arguments
    disabled = _require_enabled()
    if disabled is not None:
        return disabled
    server = _server()
    cfg = _CONFIG
    journal = _journal(cfg)
    record = None
    for entry in reversed(journal.read()):
        if (entry.get("event") == "apply"
                and entry.get("sessionId") == _SESSION_ID):
            record = entry
            break
    if record is None:
        return _refusal(UpdateError(
            "UPDATE_NOTHING_APPLIED",
            "this server session has applied no release recorded in this "
            "update home, so there is no release generation to roll back to",
            fix="apply a release first, or use revl_undo for a hand-made "
                "generation. A plan or apply recorded by another server "
                "session is not this session's to undo"))
    target = record.get("generationBefore")
    if not server.SESSION.loaded:
        return _refusal(UpdateError(
            "UPDATE_NOTHING_LOADED",
            "nothing is running now; the generation to roll back to is not "
            "live", fix="load the composition first"))
    from .session import SessionError  # noqa: PLC0415

    try:
        result = server.SESSION.undo(to=target)
    except SessionError as error:
        return _refusal(UpdateError(
            "UPDATE_ROLLBACK_REFUSED", str(error),
            fix="the previous generation may have aged out of the bounded "
                "history; inspect revl_state and recover by hand"))
    journal.append("rollback", release={
        key: record.get(key) for key in
        ("project", "version", "tag", "commit", "manifestDigest")},
        sessionId=_SESSION_ID,
        generationBefore=record.get("generationAfter"),
        generationTo=target)
    return {
        "ok": True,
        "rolledBack": True,
        "generation": _generation(),
        "release": {key: record.get(key) for key in
                    ("project", "version", "tag", "commit")},
        "undo": result,
        "note": "the generation before the applied release is running again; "
                "the rollback was itself admitted through the gate",
    }


TOOLS = [
    {
        "name": "revl_update_check",
        "description": "Check the operator-configured release feed for a newer "
                       "release and compare it with the running toolchain: "
                       "version, semver bump, whether the release declares "
                       "itself live-safe, and whether this host satisfies its "
                       "declared gate, language and Python requirements. Reads "
                       "the feed only; fetches and compiles nothing. The feed "
                       "is the server's configuration (`--update`, "
                       "`--update-repo`, `--update-channel`), never a caller "
                       "argument. A server started without --update refuses by "
                       "name.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_update_check,
    },
    {
        "name": "revl_update_apply",
        "description": "Apply a verified release to the RUNNING composition "
                       "through the admission gate, or rehearse that apply. "
                       "Called with no planHash it stages the newest release, "
                       "compiles and admits it against the running manifest, "
                       "plans the swap and returns a server-side planHash; "
                       "nothing is mutated. Called with planHash it refuses a "
                       "stale plan, requires acknowledgeWidening when the "
                       "release adds or widens irreversible reach, swaps the "
                       "release in and verifies the successor serves the "
                       "running keys, reverting automatically when it does "
                       "not. The feed is the server's configuration; a caller "
                       "cannot name another repository or token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "planHash": {"type": "string",
                             "description": "the planHash from a previous call "
                                            "with no planHash; applying is "
                                            "bound to it and to the running "
                                            "generation"},
                "force": {"type": "boolean",
                          "description": "plan even when the running version "
                                         "is already the newest on the channel"},
                "acknowledgeWidening": {"type": "boolean",
                                        "description": "true: apply a release "
                                                       "whose irreversible "
                                                       "reach is wider than "
                                                       "the running "
                                                       "composition's; the "
                                                       "widening is reported in "
                                                       "the plan answer"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_update_apply,
    },
    {
        "name": "revl_update_status",
        "description": "What the update surface knows: whether it is enabled, "
                       "its feed configuration, the running identity and "
                       "generation, the staged releases under the update home, "
                       "the plans held server side, and the tail of the update "
                       "journal. Read-only, and answers even when updates are "
                       "disabled so an operator can see why.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_update_status,
    },
    {
        "name": "revl_update_rollback",
        "description": "Roll the running composition back to the generation "
                       "that ran before the last applied release, through the "
                       "same admitted undo a hand-made generation uses. Reads "
                       "the update journal for the target generation and "
                       "refuses by name when no release was applied or the "
                       "target has aged out of the bounded history.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_update_rollback,
    },
]
