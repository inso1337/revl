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
import tempfile
import threading
import time

from ..update.errors import UpdateError
from ..update.feed import resolve_release
from ..update.fetch import (find_staged, stage_release, staged_intact,
                            update_home)
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
#: The releases this server session applied, oldest first. A release is the
#: running one while the session's composition is the one that apply produced
#: (its document digest), so a hand swap or a reload forgets it.
_APPLIED: list[dict] = []
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
        _APPLIED.clear()


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


def _running_release() -> dict | None:
    """The release the running composition was updated to by this session.

    ``None`` when nothing is loaded, when this session applied no release, or
    when the running composition is no longer one an apply produced. The
    comparison is with the composition, never with the toolchain's own
    version: an application feed versions the application.
    """
    server = _server()
    if not server.SESSION.loaded:
        return None
    digest = _ir_digest(server.SESSION.ir)
    with _LOCK:
        for entry in reversed(_APPLIED):
            if entry["irDigest"] == digest:
                return dict(entry)
    return None


def _running_version() -> str | None:
    running = _running_release()
    return running["version"] if running is not None else None


def _files_digest(paths: list[str]) -> tuple[str, list[dict], dict]:
    """Read each staged file ONCE: its digest, and the bytes that digest is of.

    A caller that goes on to use the files uses these bytes, never a second
    read of the path, so what is used is what was hashed.
    """
    digest = hashlib.sha256()
    entries: list[dict] = []
    bodies: dict[str, bytes] = {}
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
        bodies[path] = body
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest(), entries, bodies


def _materialise(plan: dict, bodies: dict[str, bytes]) -> list[str]:
    """Write the hashed bytes into a fresh private directory, return its files.

    The swap compiles from these paths, so a write to the staging directory
    after the digest check cannot reach the swap. The directory is kept: it
    is the on-disk source of the generation the swap starts.
    """
    root = update_home(_CONFIG["home"]) / "applied"
    root.mkdir(parents=True, exist_ok=True)
    target = tempfile.mkdtemp(prefix=f"{plan['planHash'][:16]}-", dir=root)
    source_root = plan["sourceRoot"]
    paths: list[str] = []
    for path in plan["files"]:
        relative = os.path.relpath(path, source_root)
        if relative.startswith(os.pardir) or os.path.isabs(relative):
            raise UpdateError(
                "UPDATE_FILES",
                f"the planned source {path} is outside its staging root",
                fix="stage the release again, then plan it again")
        destination = os.path.join(target, relative)
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        descriptor = os.open(destination,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(bodies[path])
        paths.append(destination)
    return paths


def _plan_hash(binding: dict) -> str:
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _widening_from_audits(before: dict, after: dict) -> dict:
    """Every authority axis that widened between two audits.

    `audit_diff` carries seven differs: the crossing set and its reach,
    per-emission capability scopes, host backends, recovery inverses,
    capability registers and emission cardinality. A release can widen any of
    them while adding no crossing, so all of them feed the acknowledgement
    decision.
    """
    from ..audit_diff import (diff_backends,  # noqa: PLC0415
                              diff_capability_scopes, diff_cardinality,
                              diff_recovery, diff_registers, evaluate)

    drift = evaluate(before, after)
    scopes = diff_capability_scopes(before, after)
    backends = diff_backends(before, after)
    recovery = diff_recovery(before, after)
    registers = diff_registers(before, after)
    cardinality = diff_cardinality(before, after)
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


def _widening(old_ir: dict, files: list[str]) -> dict:
    """The widening report for a staged release, conservative on failure."""
    from ..audit_diff import audit_report  # noqa: PLC0415
    from .server import compile_under_authoring  # noqa: PLC0415

    try:
        # The same compiler door every verb uses. Release sources are files,
        # so they compile as operator-sanctioned code, exactly as the admit
        # and swap stages above compiled them.
        candidate_ir = compile_under_authoring(None, list(files))
        return _widening_from_audits(audit_report(old_ir),
                                     audit_report(candidate_ir))
    except Exception as error:  # noqa: BLE001 - fail closed, name the reason
        return {"widened": True, "unknown":
                f"the audit diff could not be computed: {error}"}


def _keys_served(before: list[str], after: list[str]) -> bool:
    """Whether the successor still serves every key the predecessor served.

    An EMPTY run is healthy: a composition that provides no keys is not a
    failed successor. Only a key the predecessor served and the successor no
    longer does counts.
    """
    return set(before).issubset(set(after))


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
    running = _running_release()
    comparison = compare_release(
        resolved, identity, cfg["channel"],
        running=running["version"] if running is not None else None)
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
        and comparison.get("compatible") is not False
        and artifact is not None
        and artifact.kind == "source")
    return {
        "ok": True,
        "feed": {"repo": cfg["repo"], "channel": cfg["channel"]},
        "identity": identity,
        "release": resolved.as_report(),
        "runningRelease": running,
        "comparison": comparison,
        "applyAvailable": apply_available,
        "applyReason": None if apply_available else (
            "nothing is loaded" if not server.SESSION.loaded else
            "the release carries no composition source artifact" if (
                artifact is None or artifact.kind != "source") else
            "no newer release on the channel" if not comparison.get(
                "updateAvailable") else
            "the release declares requirements this host does not meet"),
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
            # the extracted files are what compiles, so they are checked
            # against the digests recorded when the verified archive was
            # extracted; a staging edited since is fetched again, not used
            if not staged_intact(staged):
                journal.append("refusal", stage="reuse",
                               project=staged.project, version=staged.version,
                               directory=str(staged.directory))
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
        "runningRelease": _running_release(),
        "staged": staged,
        "plans": plan_documents,
        "journal": _journal(cfg).tail(20) if cfg["enabled"] else [],
    }


#: The caller's explicit consents, each to one thing. `force` only re-plans the
#: release that is already running; it opts into nothing else.
_CONSENTS = ("force", "allowDowngrade", "acceptNotLive", "acceptIncompatible")


def _consents(arguments: dict) -> dict:
    return {name: arguments.get(name) is True for name in _CONSENTS}


def _nothing_to_plan(comparison: dict, note: str) -> dict:
    return {"ok": True, "planned": False, "applied": False,
            "comparison": comparison, "live": comparison.get("live"),
            "restartRequired": comparison.get("restartRequired"),
            "note": note}


def _ordering_gate(resolved, comparison: dict, consents: dict) -> dict | None:
    relation = comparison.get("relation")
    running = comparison.get("running")
    if relation == "older" and not consents["allowDowngrade"]:
        if consents["force"]:
            return _refusal(
                UpdateError(
                    "UPDATE_DOWNGRADE",
                    f"release {resolved.version} is older than release "
                    f"{running}, which the running composition was updated "
                    "to; force re-plans the running release and never "
                    "consents to a downgrade",
                    fix="pass allowDowngrade true to plan the older release"),
                comparison=comparison)
        return _nothing_to_plan(
            comparison, f"the running composition is on release {running}, "
                        f"newer than the channel's {resolved.version}; nothing "
                        "was staged or planned (allowDowngrade plans it)")
    if relation == "same" and not consents["force"]:
        return _nothing_to_plan(
            comparison, f"the running composition is already on release "
                        f"{running}, the newest on the channel; nothing was "
                        "staged or planned")
    return None


def _plan_gate(resolved, comparison: dict, consents: dict) -> dict | None:
    """Refuse, before anything is staged, what the caller did not consent to."""
    refusal = _ordering_gate(resolved, comparison, consents)
    if refusal is not None:
        return refusal
    if comparison.get("compatible") is False and not consents[
            "acceptIncompatible"]:
        return _refusal(
            UpdateError(
                "UPDATE_INCOMPATIBLE",
                f"release {resolved.version} declares requirements this host "
                "does not meet: " + "; ".join(comparison.get("requirements")
                                              or ()),
                fix="update the toolchain or the host first, or pass "
                    "acceptIncompatible true to let the running gate decide"),
            comparison=comparison)
    if (resolved.manifest is not None and not resolved.manifest.live
            and not consents["acceptNotLive"]):
        return _refusal(
            UpdateError(
                "UPDATE_NOT_LIVE",
                f"release {resolved.version} does not declare itself live-safe "
                "(`live` is false), so this host may stage it but not apply it "
                "to a running composition",
                fix="stage it and apply it at the next start, or pass "
                    "acceptNotLive true to apply a release the feed has not "
                    "certified live"),
            comparison=comparison)
    return None


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
    comparison = compare_release(resolved, identity, cfg["channel"],
                                 running=_running_version())
    consents = _consents(arguments)
    refusal = _plan_gate(resolved, comparison, consents)
    if refusal is not None:
        return refusal
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
    # Hash the staged bytes BEFORE anything reads them, and bind those bytes:
    # the plan admits the digest it was made from, and apply re-hashes. Hashing
    # only after admit/plan/widening would bind an edit made during planning
    # without that edit ever being the bytes the gate admitted.
    records_digest, entries, _bodies = _files_digest(files)
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
    # a change during planning is caught here, before the plan is minted
    if _files_digest(files)[0] != records_digest:
        return _refusal(UpdateError(
            "UPDATE_PLAN_RACED",
            "the staged release changed while it was being admitted and "
            "planned; the plan would not describe the bytes the gate saw",
            fix="stage the release again, then plan it again"))
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
        "consents": consents,
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
        "sourceRoot": str(staged.source_root),
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
        "consents": consents,
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
        current_digest, _entries, bodies = _files_digest(plan["files"])
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
    try:
        swap_files = _materialise(plan, bodies)
    except (OSError, UpdateError) as error:
        return _refusal(error if isinstance(error, UpdateError) else
                        UpdateError("UPDATE_FILES",
                                    f"the verified sources could not be "
                                    f"written for the swap: {error}"))
    swap = server._tool_swap({"files": swap_files,
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
               and _keys_served(before_keys, after_keys))
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
        _APPLIED.append({"planHash": plan_hash,
                         "version": plan["release"]["version"],
                         "release": dict(plan["release"]),
                         "irDigest": _ir_digest(server.SESSION.ir),
                         "generation": after_generation})
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


def _pending_apply(journal: Journal) -> dict | None:
    """This session's newest applied release that no rollback consumed."""
    entries = journal.read()
    consumed = {entry.get("consumes") for entry in entries
                if entry.get("event") == "rollback"
                and entry.get("sessionId") == _SESSION_ID}
    for entry in reversed(entries):
        if (entry.get("event") == "apply"
                and entry.get("sessionId") == _SESSION_ID
                and entry.get("planHash") not in consumed):
            return entry
    return None


def _forget_applied(plan_hash: str | None) -> None:
    with _LOCK:
        _APPLIED[:] = [entry for entry in _APPLIED
                       if entry["planHash"] != plan_hash]


def _rollback_refused(answer: dict, record: dict) -> dict:
    """The undo's own answer, kept whole so an approval two-step reads as it
    does from revl_undo; a plain refusal also carries the update code."""
    payload = {**answer, "ok": False, "rolledBack": False, "stoppedAt": "undo",
               "planHash": record.get("planHash")}
    if not answer.get("approvalRequired") and "refusal" not in answer:
        payload["refusal"] = UpdateError(
            "UPDATE_ROLLBACK_REFUSED",
            "the undo to the generation before the applied release was "
            "refused; the running composition is untouched",
            fix="the previous generation may have aged out of the bounded "
                "history; inspect revl_state and recover by hand").as_dict()
    return payload


def _tool_update_rollback(arguments: dict) -> dict:
    del arguments
    disabled = _require_enabled()
    if disabled is not None:
        return disabled
    server = _server()
    cfg = _CONFIG
    journal = _journal(cfg)
    record = _pending_apply(journal)
    if record is None:
        return _refusal(UpdateError(
            "UPDATE_NOTHING_APPLIED",
            "this server session has no applied release left to roll back in "
            "this update home (none was applied, or its rollback already ran)",
            fix="apply a release first, or use revl_undo for a hand-made "
                "generation. A plan or apply recorded by another server "
                "session is not this session's to undo"))
    target = record.get("generationBefore")
    if not server.SESSION.loaded:
        return _refusal(UpdateError(
            "UPDATE_NOTHING_LOADED",
            "nothing is running now; the generation to roll back to is not "
            "live", fix="load the composition first"))
    generation = _generation()
    if generation != record.get("generationAfter"):
        return _refusal(UpdateError(
            "UPDATE_ROLLBACK_STALE",
            f"release {record.get('version')} started generation "
            f"{record.get('generationAfter')}, but generation {generation} is "
            "running; rolling back to the generation before the release would "
            "also discard every change made since",
            fix="use revl_undo to choose the generation to return to"),
            planHash=record.get("planHash"))
    # revl_undo's own path: the authoring profile, and an approval ticket
    # answered as the two-step it is
    answer = server._tool_undo({"to": target})
    if not answer.get("ok"):
        journal.append("refusal", stage="rollback",
                       planHash=record.get("planHash"), sessionId=_SESSION_ID)
        return _rollback_refused(answer, record)
    _forget_applied(record.get("planHash"))
    journal.append("rollback", consumes=record.get("planHash"), release={
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
        "undo": answer,
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
                          "description": "plan the release the running "
                                         "composition is already on again; "
                                         "consents to nothing else"},
                "allowDowngrade": {"type": "boolean",
                                   "description": "plan a release older than "
                                                  "the one the running "
                                                  "composition was updated to"},
                "acceptNotLive": {"type": "boolean",
                                  "description": "plan a release whose "
                                                 "manifest says live false"},
                "acceptIncompatible": {"type": "boolean",
                                       "description": "plan a release whose "
                                                      "declared gate, language "
                                                      "or Python requirements "
                                                      "this host does not meet; "
                                                      "the running gate still "
                                                      "admits or refuses it"},
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
                       "same admitted undo revl_undo runs. Reads the update "
                       "journal for the target generation, consumes the apply "
                       "it rolls back, and refuses by name when no release is "
                       "left to roll back, when a later generation is running, "
                       "or when the target has aged out of the bounded "
                       "history.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_update_rollback,
    },
]
