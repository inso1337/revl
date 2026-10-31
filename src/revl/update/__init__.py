"""Check for, verify and stage a revl release (docs/updates.md).

The package is the language side of the update mechanism. It answers four
questions and nothing else:

* what version is running (:mod:`revl.update.identity`);
* what does the feed say the latest release is (:mod:`revl.update.feed`);
* is the release manifest well formed (:mod:`revl.update.manifest`);
* where are its verified bytes staged (:mod:`revl.update.fetch`).

It never downloads into a composition root and never compiles or executes a
release. Applying a staged release to a running composition is the operator
surface, ``revl_update_apply``, which re-admits through the same gate a
``revl_swap`` runs.
"""

from __future__ import annotations

from .errors import UpdateError
from .feed import ResolvedRelease, resolve_manifest_source, resolve_release, \
    source_archive_url
from .fetch import StagedRelease, extract_archive, find_staged, stage_release, \
    update_home
from .identity import compare_identity, current_identity
from .journal import Journal
from .manifest import ARTIFACT_KINDS, SCHEMA, Artifact, ReleaseManifest, \
    parse_manifest
from .semver import Version, VersionError, bump_kind

__all__ = [
    "ARTIFACT_KINDS",
    "Artifact",
    "Journal",
    "ReleaseManifest",
    "ResolvedRelease",
    "SCHEMA",
    "StagedRelease",
    "UpdateError",
    "Version",
    "VersionError",
    "bump_kind",
    "compare_identity",
    "current_identity",
    "extract_archive",
    "find_staged",
    "parse_manifest",
    "resolve_manifest_source",
    "resolve_release",
    "source_archive_url",
    "stage_release",
    "update_home",
]
