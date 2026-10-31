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


def compare_identity(manifest, identity: dict | None = None) -> dict:
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
    running = Version.parse_or_none(identity.get("version"))
    if running is None:
        update_available = False
        bump = "none"
    else:
        update_available = latest > running
        bump = bump_kind(running, latest) if update_available else "none"
    requirements: list[str] = []
    release_compat = manifest.compat
    declared_gate = release_compat.get("gateApi")
    running_gate = (identity.get("gate") or {}).get("api")
    if declared_gate and running_gate and declared_gate != running_gate:
        requirements.append(
            f"gateApi: the release was built for gate {declared_gate}, this "
            f"toolchain serves gate {running_gate}")
    declared_language = release_compat.get("language")
    if declared_language and running is not None:
        try:
            if Version.parse(declared_language) > running:
                requirements.append(
                    f"language: the release declares language "
                    f"{declared_language}, this toolchain is {running}")
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


def _tag_fallback_comparison(resolved, identity: dict, channel: str) -> dict:
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


def compare_release(resolved, identity: dict | None = None,
                    channel: str = "stable") -> dict:
    """Compare a resolved release with the running identity.

    A tag fallback carries no compatibility statement; it is reported as
    compatible ``None`` and never live, which is the honest answer rather than
    a guessed yes.
    """
    identity = identity or current_identity()
    if resolved.manifest is None:
        return _tag_fallback_comparison(resolved, identity, channel)
    report = compare_identity(resolved.manifest, identity)
    report["tagFallback"] = False
    return report
