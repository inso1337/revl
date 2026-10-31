"""``revl update check|fetch|status`` (docs/updates.md).

The CLI answers three questions and never mutates a running system:

* ``check``: is there a newer release on the configured channel;
* ``fetch``: put the release under the update home, verified;
* ``status``: what is running, what is staged, what the journal recorded.

Applying a fetched release to a live composition is the operator MCP surface
(``revl_update_apply``), not a CLI side effect: a CLI process has no handle on
a running session.
"""

from __future__ import annotations

import json
import os
import sys

from .errors import UpdateError
from .feed import resolve_release
from .fetch import find_staged, stage_release, update_home
from .identity import compare_identity, current_identity
from .journal import Journal
from .semver import Version, bump_kind

DEFAULT_REPO = "inso1337/revl"
DEFAULT_CHANNEL = "stable"

EXIT_UP_TO_DATE = 0
EXIT_UPDATE_AVAILABLE = 10
EXIT_ERROR = 2


def _repo(args) -> str:
    return getattr(args, "repo", None) or os.environ.get("REVL_UPDATE_REPO") \
        or DEFAULT_REPO


def _channel(args) -> str:
    return getattr(args, "channel", None) or os.environ.get(
        "REVL_UPDATE_CHANNEL") or DEFAULT_CHANNEL


def _token(args) -> tuple[str | None, str | None]:
    token_env = getattr(args, "token_env", None) or "REVL_UPDATE_TOKEN"
    return os.environ.get(token_env), token_env


def _resolve(args):
    token, token_env = _token(args)
    return resolve_release(
        _repo(args), channel=_channel(args),
        project=getattr(args, "project", None), token=token,
        token_env=token_env, api_base_override=getattr(args, "api_base", None),
        manifest_source=getattr(args, "manifest", None),
        timeout=float(getattr(args, "timeout", 20.0)))


def _tag_fallback_comparison(resolved, identity: dict,
                             channel: str) -> dict:
    current = Version.parse_or_none(identity.get("version"))
    latest = Version.parse(resolved.version)
    available = current is not None and latest > current
    return {
        "current": identity,
        "latest": {"version": resolved.version, "tag": resolved.tag,
                   "commit": resolved.commit, "channel": channel},
        "updateAvailable": available,
        "bump": bump_kind(current, latest) if available else "none",
        "compatible": None,
        "requirements": [],
        "live": False,
        "hostChanges": True,
        "restartRequired": True,
        "manifestDigest": None,
        "tagFallback": True,
    }


def _comparison(resolved, identity: dict, channel: str) -> dict:
    if resolved.manifest is None:
        return _tag_fallback_comparison(resolved, identity, channel)
    report = compare_identity(resolved.manifest, identity)
    report["tagFallback"] = False
    return report


def _error(args, error: UpdateError) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "refusal": error.as_dict()}, indent=2))
    else:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        if error.fix:
            print(f"  fix: {error.fix}", file=sys.stderr)
    return EXIT_ERROR


def _print_check(report: dict) -> None:
    current = report["current"]
    latest = report["latest"]
    print(f"current: {current.get('version', 'unknown')} "
          f"(gate {current.get('gate', {}).get('api', '?')}, "
          f"ir {current.get('irVersion', '?')}, "
          f"python {current.get('python', '?')})")
    print(f"latest:  {latest['version']} ({latest['tag']}, "
          f"{latest['commit'][:12]}, {latest.get('channel', '?')})")
    if not report["updateAvailable"]:
        print(f"up to date on {report['feed']['channel']}: "
              f"{current.get('version', 'unknown')} is the latest release")
        return
    print(f"update available: {report['bump']} bump")
    if report.get("tagFallback"):
        print("feed: tag fallback (no release manifest), apply is not "
              "automatic")
        return
    print(f"feed: {report['feed']['repo']} ({report['feed']['channel']}, "
          "release manifest)")
    print(f"live: {'yes' if report['live'] else 'no'}; "
          f"restart required: {'yes' if report['restartRequired'] else 'no'}")
    for requirement in report.get("requirements") or []:
        print(f"  requirement: {requirement}")
    for artifact in latest.get("artifacts") or []:
        size = f", {artifact['size']} bytes" if artifact.get("size") else ""
        print(f"artifact: {artifact['name']} ({artifact['kind']}{size})")


def _check(args) -> int:
    identity = current_identity()
    resolved = _resolve(args)
    report = {
        "ok": True,
        "feed": {"repo": resolved.repo, "channel": _channel(args)},
        **_comparison(resolved, identity, _channel(args)),
        "release": resolved.as_report(),
    }
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_check(report)
    return EXIT_UPDATE_AVAILABLE if report["updateAvailable"] else EXIT_UP_TO_DATE


def _fetch(args) -> int:
    resolved = _resolve(args)
    token, _ = _token(args)
    staged = stage_release(
        resolved, home_dir=getattr(args, "home", None), token=token,
        timeout=float(getattr(args, "timeout", 20.0)),
        api_base_override=getattr(args, "api_base", None))
    report = {"ok": True, "feed": {"repo": resolved.repo,
                                   "channel": _channel(args)},
              "release": resolved.as_report(),
              "staged": staged.as_report()}
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"staged: {staged.directory}")
        print(f"release: {staged.project} {staged.version} "
              f"({staged.tag}, {staged.commit[:12]})")
        print(f"artifact: {staged.artifact_name} "
              f"sha256 {staged.digest}")
        print(f"sources: {len(staged.files)} .rvl file(s) under "
              f"{staged.source_root}")
    return EXIT_UP_TO_DATE


def _status(args) -> int:
    identity = current_identity()
    home = update_home(getattr(args, "home", None))
    journal = Journal(getattr(args, "home", None))
    staged = [release.as_report()
              for release in find_staged(getattr(args, "home", None))]
    report = {
        "ok": True,
        "current": identity,
        "home": str(home),
        "staged": staged,
        "journal": journal.tail(20),
    }
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True))
        return EXIT_UP_TO_DATE
    print(f"current: {identity['version']} "
          f"(gate {identity['gate']['api']}, ir {identity['irVersion']}, "
          f"python {identity['python']})")
    print(f"update home: {home}")
    if not staged:
        print("staged: none")
    else:
        print(f"staged: {len(staged)} release(s)")
        for release in staged:
            print(f"  {release['project']} {release['version']} "
                  f"({release['tag']}, sha256 {release['artifact']['sha256'][:12]})")
    tail = report["journal"]
    if tail:
        print("journal:")
        for record in tail:
            detail = record.get("detail") or record.get("version") or ""
            print(f"  {record.get('ts')} {record.get('event')} {detail}".rstrip())
    return EXIT_UP_TO_DATE


def _run_update(args) -> int:
    command = getattr(args, "update_command", None)
    try:
        if command == "check":
            return _check(args)
        if command == "fetch":
            return _fetch(args)
        if command == "status":
            return _status(args)
        print(f"error: unknown update command {command!r}", file=sys.stderr)
        return EXIT_ERROR
    except UpdateError as error:
        return _error(args, error)
