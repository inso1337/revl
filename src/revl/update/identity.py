"""What version is running, and what a release means against it (docs/updates.md).

The identity is the same one the rest of the toolchain reports:
``compiler_version()`` for the language version, ``gate_version()`` for the
gate surface and frontier, the running IR schema revision, and the interpreter
version the release must be able to run on. Nothing here is hand maintained,
so an identity cannot drift from the build it describes.
"""

from __future__ import annotations

import sys

from .semver import Version, VersionError, bump_kind


def current_identity() -> dict:
    """The running toolchain, as the updater compares it with a release."""
    from ..attest import compiler_version  # noqa: PLC0415 - keeps the CLI light
    from ..gate import gate_version  # noqa: PLC0415
    from ..lower import IR_VERSION  # noqa: PLC0415

    return {
        "version": compiler_version(),
        "gate": gate_version(),
        "irVersion": IR_VERSION,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}."
                  f"{sys.version_info.micro}",
    }


def _python_at_least(have: str, want: str) -> bool:
    have_parts = tuple(int(part) for part in have.split(".") if part.isdigit())
    want_parts = tuple(int(part) for part in want.split(".") if part.isdigit())
    return have_parts[:len(want_parts)] >= want_parts


#: ``running`` default: compare the release with the running toolchain, which
#: is what ``revl update check`` asks about the toolchain's own feed.
TOOLCHAIN = object()


def _relation(running: Version | None, latest: Version) -> str:
    if running is None:
        return "unknown"
    if latest > running:
        return "newer"
    if latest == running:
        return "same"
    return "older"


def _running_version(identity: dict, running) -> tuple[Version | None, str]:
    """The version a release is compared with, and what that version is of.

    The toolchain basis is the compiler's own version. The composition basis
    is the release a running composition was last updated to: a session that
    has applied none runs a composition of no known release version, so any
    verified release is a candidate for it (``relation`` is ``unknown``).
    """
    if running is TOOLCHAIN:
        return Version.parse_or_none(identity.get("version")), "toolchain"
    return (Version.parse_or_none(running) if running else None), "composition"


def compare_identity(manifest, identity: dict | None = None, *,
                     running=TOOLCHAIN) -> dict:
    """Compare a release manifest with the running identity.

    The answer separates three questions, because they are three answers:

    * ``updateAvailable``: is the release a newer version, and by what bump;
    * ``compatible``: does the release declare a gate or IR surface this
      toolchain does not have (a host fact, decided from the manifest);
    * ``live`` / ``restartRequired``: may this host apply the release to a
      running composition, or must the host part wait for the next start.

    Admission is not decided here. Whether the running gate actually admits
    the release's sources is answered by compiling them (the plan stage), and
    the declaration is only a hint until that compile runs.
    """
    identity = identity or current_identity()
    latest = manifest.semver
    toolchain = Version.parse_or_none(identity.get("version"))
    current, basis = _running_version(identity, running)
    relation = _relation(current, latest)
    update_available, bump = _availability(current, latest, basis)
    requirements: list[str] = []
    release_compat = manifest.compat
    declared_gate = release_compat.get("gateApi")
    running_gate = (identity.get("gate") or {}).get("api")
    if declared_gate and running_gate and declared_gate != running_gate:
        requirements.append(
            f"gateApi: the release was built for gate {declared_gate}, this "
            f"toolchain serves gate {running_gate}")
    declared_language = release_compat.get("language")
    if declared_language and toolchain is not None:
        try:
            if Version.parse(declared_language) > toolchain:
                requirements.append(
                    f"language: the release declares language "
                    f"{declared_language}, this toolchain is {toolchain}")
        except VersionError:
            requirements.append(
                f"language: the release declares {declared_language!r}, which "
                "is not a version this updater can compare")
    min_python = release_compat.get("minPython")
    running_python = identity.get("python")
    if min_python and running_python and not _python_at_least(running_python,
                                                              min_python):
        requirements.append(
            f"python: the release needs Python {min_python}, this host runs "
            f"{running_python}")
    compatible = not requirements
    live = bool(manifest.live) and compatible and update_available
    return {
        "current": identity,
        "basis": basis,
        "running": str(current) if current is not None else None,
        "relation": relation,
        "latest": {
            "version": manifest.version,
            "tag": manifest.tag,
            "commit": manifest.commit,
            "channel": manifest.channel,
        },
        "updateAvailable": update_available,
        "bump": bump,
        "compatible": compatible,
        "requirements": requirements,
        "live": live,
        "hostChanges": manifest.host_changes,
        "restartRequired": bool(manifest.host_changes) or not live,
        "manifestDigest": manifest.digest(),
    }


def _availability(current: Version | None, latest: Version,
                  basis: str) -> tuple[bool, str]:
    if current is None:
        # an unknown toolchain version offers nothing; a composition no
        # release was applied to may take any verified release
        return basis == "composition", "unknown" if basis == "composition" \
            else "none"
    if latest > current:
        return True, bump_kind(current, latest)
    return False, "none"


def _tag_fallback_comparison(resolved, identity: dict, channel: str,
                             running=TOOLCHAIN) -> dict:
    current, basis = _running_version(identity, running)
    latest = Version.parse(resolved.version)
    available, bump = _availability(current, latest, basis)
    return {
        "current": identity,
        "basis": basis,
        "running": str(current) if current is not None else None,
        "relation": _relation(current, latest),
        "latest": {"version": resolved.version, "tag": resolved.tag,
                   "commit": resolved.commit, "channel": channel},
        "updateAvailable": available,
        "bump": bump,
        "compatible": None,
        "requirements": [],
        "live": False,
        "hostChanges": True,
        "restartRequired": True,
        "manifestDigest": None,
        "tagFallback": True,
    }


def compare_release(resolved, identity: dict | None = None,
                    channel: str = "stable", *, running=TOOLCHAIN) -> dict:
    """Compare a resolved release with the running identity.

    ``running`` is the version the release would replace: the toolchain's
    own by default (the CLI), or the release a running composition was last
    updated to (the operator verbs pass it, ``None`` when no release was
    applied). The language, gate and Python requirements are always checked
    against the toolchain.

    A tag fallback carries no compatibility statement; it is reported as
    compatible ``None`` and never live, which is the honest answer rather than
    a guessed yes.
    """
    identity = identity or current_identity()
    if resolved.manifest is None:
        return _tag_fallback_comparison(resolved, identity, channel, running)
    report = compare_identity(resolved.manifest, identity, running=running)
    report["tagFallback"] = False
    return report
