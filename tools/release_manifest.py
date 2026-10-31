#!/usr/bin/env python3
"""Write the release manifest a GitHub release publishes (docs/updates.md).

A release is not just a tag and a file: the updater needs the release's
identity, the sha256 of every artifact, the pins the tree was built against,
and the compatibility statement the running host compares with its own gate
before it stages anything. This script writes that document from a tagged
checkout and validates it with the same parser the updater uses, so a release
whose manifest the updater would refuse cannot be published by this tool.

    tools/release_manifest.py dist/* --out release.json
    tools/release_manifest.py --validate release.json

The `--validate` form is what the release dry run checks on every scheduled
run and on a PR that touches the release path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.update.errors import UpdateError  # noqa: E402
from revl.update.manifest import (  # noqa: E402
    Artifact, ReleaseManifest, parse_manifest)


def _version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit("error: pyproject.toml declares no [project] version")
    return match.group(1)


def _min_python(root: Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\s*requires-python\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        return None
    version = re.search(r"(\d+\.\d+)", match.group(1))
    return version.group(1) if version else None


def _commit(root: Path, explicit: str | None) -> str:
    if explicit:
        commit = explicit.lower()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SystemExit(f"error: --commit {explicit!r} is not a 40 character sha")
        return commit
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=30,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        raise SystemExit(
            "error: cannot read the release commit; pass --commit (a release "
            "manifest always names the tree it was built from)")
    return result.stdout.strip().lower()


def _compat(min_python: str | None) -> dict:
    from revl.gate import gate_version  # noqa: PLC0415
    from revl.lower import IR_VERSION  # noqa: PLC0415

    gate = gate_version()
    compat = {
        "language": gate["language"],
        "gateApi": gate["api"],
        "frontier": gate["frontier"],
        "irVersion": IR_VERSION,
    }
    if min_python:
        compat["minPython"] = min_python
    return compat


def _artifact(path: Path) -> Artifact:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    name = path.name
    if name.endswith(".whl"):
        kind = "wheel"
    elif name.endswith((".tar.gz", ".tgz")):
        kind = "sdist"
    else:
        kind = "bundle"
    return Artifact(name=name, kind=kind, url="https://github.com/inso1337/revl/"
                   "releases/download/PLACEHOLDER/" + name,
                   sha256=digest.hexdigest(), size=path.stat().st_size)


def _fill_artifact_urls(document: dict, tag: str,
                        repository: str) -> None:
    """The release asset URL is only known once the tag exists."""
    base = f"https://github.com/{repository}/releases/download/{tag}/"
    for artifact in document.get("artifacts", []):
        artifact["url"] = base + artifact["name"]


def build_manifest(*, root: Path = ROOT, artifacts: list[str],
                   version: str | None = None, tag: str | None = None,
                   project: str = "revl", repository: str = "inso1337/revl",
                   commit: str | None = None, channel: str = "stable",
                   live: bool = False, host_changes: bool = True,
                   notes_file: str | None = None,
                   changes_file: str | None = None,
                   published_at: str | None = None) -> ReleaseManifest:
    version = version or _version(root)
    tag = tag or f"v{version}"
    commit = _commit(root, commit)
    parsed = [Path(path) for path in artifacts]
    missing = [str(path) for path in parsed if not path.is_file()]
    if missing:
        raise SystemExit(f"error: release artifact(s) not found: {', '.join(missing)}")
    if not parsed:
        raise SystemExit("error: name at least one release artifact")
    built = ReleaseManifest(
        schema="revl.release/1", project=project, version=version, tag=tag,
        commit=commit,
        artifacts=tuple(_artifact(path) for path in parsed),
        compat=_compat(_min_python(root)), pins={"revl": commit},
        live=live, host_changes=host_changes, channel=channel,
        published_at=published_at or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime()),
        url=f"https://github.com/{repository}/releases/tag/{tag}",
        changes=_json_file(changes_file) if changes_file else None,
        notes=Path(notes_file).read_text(encoding="utf-8") if notes_file else None,
    )
    document = built.document()
    _fill_artifact_urls(document, tag, repository)
    # Re-parse exactly what will be published, so the writer and the updater
    # cannot disagree about what a valid manifest is.
    return parse_manifest(document)


def _json_file(path: str) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"error: {path} must hold a JSON object")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="write or validate the release manifest (docs/updates.md)")
    parser.add_argument("artifacts", nargs="*",
                        help="the built release files (wheel, sdist, ...)")
    parser.add_argument("--out", default="release.json", metavar="FILE",
                        help="where to write the manifest (default release.json)")
    parser.add_argument("--validate", metavar="FILE", default=None,
                        help="validate an existing release manifest and exit")
    parser.add_argument("--root", default=str(ROOT), metavar="DIR",
                        help="the checkout the manifest describes")
    parser.add_argument("--version", default=None, metavar="X.Y.Z",
                        help="the release version (default: pyproject)")
    parser.add_argument("--tag", default=None, metavar="TAG",
                        help="the release tag (default: v<version>)")
    parser.add_argument("--project", default="revl", metavar="NAME")
    parser.add_argument("--repository", default="inso1337/revl",
                        metavar="OWNER/NAME")
    parser.add_argument("--commit", default=None, metavar="SHA")
    parser.add_argument("--channel", default="stable",
                        choices=("stable", "prerelease"))
    parser.add_argument("--live", action="store_true",
                        help="declare the release safe to apply to a running "
                             "composition")
    parser.add_argument("--no-host-changes", dest="host_changes",
                        action="store_false", default=True,
                        help="the release changes no host Python, compiler or "
                             "venv bytes")
    parser.add_argument("--notes-file", default=None, metavar="FILE")
    parser.add_argument("--changes-file", default=None, metavar="FILE")
    parser.add_argument("--published-at", default=None, metavar="ISO")
    args = parser.parse_args(argv)
    try:
        if args.validate:
            manifest = parse_manifest(Path(args.validate).read_bytes())
            print(json.dumps({
                "ok": True,
                "project": manifest.project,
                "version": manifest.version,
                "tag": manifest.tag,
                "artifacts": [artifact.name for artifact in manifest.artifacts],
                "live": manifest.live,
                "hostChanges": manifest.host_changes,
            }, indent=2))
            return 0
        manifest = build_manifest(
            root=Path(args.root), artifacts=args.artifacts,
            version=args.version, tag=args.tag, project=args.project,
            repository=args.repository, commit=args.commit,
            channel=args.channel, live=args.live,
            host_changes=args.host_changes, notes_file=args.notes_file,
            changes_file=args.changes_file, published_at=args.published_at)
    except UpdateError as error:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        if error.fix:
            print(f"  fix: {error.fix}", file=sys.stderr)
        return 1
    document = manifest.document()
    Path(args.out).write_text(json.dumps(document, indent=2) + "\n",
                              encoding="utf-8")
    print(f"wrote {args.out} ({manifest.project} {manifest.version}, "
          f"{len(manifest.artifacts)} artifact(s), sha256 pinned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
