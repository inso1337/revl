"""Semantic versions for the update mechanism (docs/updates.md).

Small and dependency free on purpose: the updater has to run under a bare
stdlib interpreter (the release check is the first thing a broken install
runs), so this module imports nothing but ``re`` and ``dataclasses``.

The comparison follows the semver precedence rules that matter here: a
release is newer by major, then minor, then patch, a prerelease is older than
the release it precedes, and prerelease identifiers compare numerically when
they are numbers. Build metadata is ignored, as the specification requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z][0-9A-Za-z.-]*))?"
    r"(?:\+(?P<build>[0-9A-Za-z][0-9A-Za-z.-]*))?$")


class VersionError(ValueError):
    """A string that is not a semantic version."""


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: str | None = None

    @classmethod
    def parse(cls, text: object) -> "Version":
        if not isinstance(text, str):
            raise VersionError(f"a version must be a string, got {type(text).__name__}")
        match = _VERSION.match(text.strip())
        if match is None:
            raise VersionError(
                f"`{text}` is not a semantic version; expected MAJOR.MINOR.PATCH "
                "with optional -prerelease and +build")
        pre = match.group("pre")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(pre.split(".")) if pre else (),
            build=match.group("build"),
        )

    @classmethod
    def parse_or_none(cls, text: object) -> "Version | None":
        try:
            return cls.parse(text)
        except VersionError:
            return None

    @property
    def stable(self) -> bool:
        return not self.prerelease

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        if self.build:
            base += "+" + self.build
        return base

    def _precedence(self) -> tuple:
        # A release sorts after every prerelease of the same triple. Numeric
        # identifiers sort numerically and rank below alphanumeric ones, which
        # is the one semver rule a plain tuple comparison gets wrong.
        if not self.prerelease:
            return (1,)
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in self.prerelease)
        return (0, identifiers)

    def _triple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):  # pragma: no cover - defensive
            return NotImplemented
        if self._triple() != other._triple():
            return self._triple() < other._triple()
        return self._precedence() < other._precedence()

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        return not self <= other

    def __ge__(self, other: "Version") -> bool:
        return not self < other


def bump_kind(current: Version, latest: Version) -> str:
    """What a move from ``current`` to ``latest`` changes.

    One of ``major``, ``minor``, ``patch``, ``none``. A move backwards is
    reported as ``none``: the update decision is ``latest > current``, and a
    caller that reaches this function with a downgrade is asking a different
    question.
    """
    if latest._triple() == current._triple():
        if latest == current:
            return "none"
        # same triple, prerelease boundary: a patch-level event.
        return "patch" if latest > current else "none"
    if latest < current:
        return "none"
    if (latest.major, latest.minor) != (current.major, current.minor):
        return "major" if latest.major != current.major else "minor"
    return "minor" if latest.minor != current.minor else "patch"
