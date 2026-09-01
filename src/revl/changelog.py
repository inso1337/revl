"""`revl changelog` - the release note is computed, not written (item 261).

This module renders a human-readable release note from the STRUCTURAL delta
between two generations of a composition. It computes NO delta of its own that
an existing helper already computes: it folds three shipped differs into one
ordered, classified, honest document -

  * `composition_diff.diff` (item 123): the IR-level structural delta
    (membership, wiring, and the crossing set it already reuses from
    `audit_diff.diff_crossings`);
  * `version.derive` (item 64): the computed semver bump and its per-operation
    major/minor classification;
  * `audit_diff.diff_reach` (item 373) plus the Slice-2 audit-surface differs
    (`diff_capability_scopes`, `diff_backends`, `diff_recovery`, `diff_registers`,
    `diff_cardinality`): the authority drift a crossing-set diff cannot see.

Every rendered line carries a mandatory provenance `fact` token drawn from one
of those differs. A line with no backing fact is a defect, not a feature, and
`ChangelogLine` refuses to construct one.

Slice 2 hardens item 64's `audit_diff` foundation: the audit surfaces that Slice
1 could only HONESTY-LINE (`recovery_surface`, `capability_registers`,
`cardinality`, the per-component `boundary[*].capabilities` scope map, and
`externs[*].backends`) are now CLASSIFIED by their own differ - a widened scope,
a new backend host body, a dropped recovery inverse, a weakened register floor,
and a raised emission ceiling each become a first-class BREAKING line, and their
leaf paths move into `CONSUMED_PATHS` so the guard no longer double-reports them.
The completeness guard (`_completeness_guard`) still structurally diffs the WHOLE
`audit_report(before)` against `audit_report(after)` and honesty-lines any
RESIDUAL differing path (a surface no differ yet reads - `parallel_plan`,
`distributability`, `externs[*].{class,register,...}`), so a genuinely
unclassified change is still never dropped and still forces the headline
non-clean.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .audit_diff import (
    audit_report, diff_backends, diff_capability_scopes, diff_cardinality,
    diff_reach, diff_recovery, diff_registers)
from .composition_diff import diff as composition_diff

# --------------------------------------------------------------------------
# the bump lattice (a local copy so this module never depends on version.py's
# internals; the join is `any major -> major, else any minor -> minor, else
# patch`, item 64's own rule).
MAJOR, MINOR, PATCH = "major", "minor", "patch"
_RANK = {PATCH: 1, MINOR: 2, MAJOR: 3}

# category -> the bump level it floors the headline at.
_CATEGORY_LEVEL = {"breaking": MAJOR, "added": MINOR, "internal": PATCH}

# the fixed category order an operator reads (§3, §4).
_CATEGORY_ORDER = ["breaking", "added", "internal", "unclassified"]


# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ChangelogLine:
    """One rendered release-note line and the upstream fact it derives from.

    `fact` is mandatory and non-empty by construction: a line is only ever
    built inside a loop over one upstream fact, and the constructor asserts it.
    There is no code path that appends free text, so "every line traces to a
    fact" is a structural guarantee, not a convention.
    """

    fact: str          # provenance token: the exact upstream fact this derives from
    category: str      # breaking | added | internal | unclassified
    text: str          # the rendered sentence
    lede: bool = False  # authority-relevant -> sorts to the top of its category
    # honesty-line only: a typed/boolean delta over an undiffed surface.
    changed: bool | None = None
    paths: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.fact:
            raise ValueError("a ChangelogLine must carry a non-empty `fact` "
                             "token - a line with no backing fact is a defect")
        if self.category not in ("breaking", "added", "internal", "unclassified"):
            raise ValueError(f"unknown changelog category {self.category!r}")


# --------------------------------------------------------------------------
# The completeness guard (§4): coverage is a property of the LEAF a differ
# reads, not of a top-level key. `CONSUMED_PATHS` is the allowlist of leaf-path
# PATTERNS a declared differ demonstrably reads; a `*` segment matches any dict
# key or list index. A pattern matches a leaf path when it is a prefix of it
# (so `boundary[*].emissions` consumes every label under an emissions list).
# Each entry names the differ that reads it, and `test_consumed_paths_*`
# asserts that correspondence so the allowlist cannot rot.
_STAR = "*"

CONSUMED_PATHS: dict[tuple, tuple[str, str]] = {
    # read by `audit_diff.crossings` (through `composition_diff.diff`, which
    # stores `diff_crossings` output):
    ("boundary", _STAR, "emissions"): ("audit_diff", "crossings"),
    ("boundary", _STAR, "externs", _STAR, "name"): ("audit_diff", "crossings"),
    ("boundary", _STAR, "taint"): ("audit_diff", "crossings"),
    ("secrets", _STAR, "capability"): ("audit_diff", "crossings"),
    ("secrets", _STAR, "name"): ("audit_diff", "crossings"),
    # read by `audit_diff.diff_reach` (via `_reach_map`):
    ("externs", _STAR, "reach"): ("audit_diff", "_reach_map"),
    # Slice 2: the audit-surface differs now READ these nested leaves, so a delta
    # on them is a CLASSIFIED line, not a guard honesty line. Each entry names
    # the differ whose source `test_every_consumed_path_*` asserts reads the leaf.
    ("boundary", _STAR, "capabilities"): ("audit_diff", "_capability_scopes"),
    ("externs", _STAR, "backends"): ("audit_diff", "diff_backends"),
    ("recovery_surface",): ("audit_diff", "diff_recovery"),
    ("capability_registers",): ("audit_diff", "diff_registers"),
    ("cardinality",): ("audit_diff", "diff_cardinality"),
    # the composition membership/wiring surface, owned by `composition_diff`
    # (which reports every component add/remove/change and provide/require edge
    # from `_components`); the audit `manifest` is the same membership data, so
    # a delta on it is already a first-class structural line, never dropped.
    ("manifest",): ("composition_diff", "_components"),
}

_MISSING = object()


def _leaf_paths(value, prefix: tuple = ()):
    """Yield `(path_tuple, scalar_value)` for every leaf of a JSON-ish value.

    Recurses into dicts (sorted by key, so iteration order never affects the
    result) and lists (by index), stopping at scalars. An empty dict/list is
    itself a leaf (a sentinel), so adding or removing a whole empty subtree is a
    detectable move. This is a canonical structural decomposition: it never
    hashes a dict or list, so it is safe over the unhashable `recovery_surface`
    (`list[dict]`) and `capability_registers` (`dict`) surfaces (§4, finding 4).
    """
    if isinstance(value, dict):
        if not value:
            yield (prefix, ("<empty-dict>",))
            return
        for key in sorted(value, key=str):
            yield from _leaf_paths(value[key], prefix + (str(key),))
    elif isinstance(value, list):
        if not value:
            yield (prefix, ("<empty-list>",))
            return
        for index, item in enumerate(value):
            yield from _leaf_paths(item, prefix + (index,))
    else:
        yield (prefix, value)


def _is_consumed(path: tuple) -> bool:
    """True when `path` is read by some declared differ (a `CONSUMED_PATHS`
    pattern is a prefix of it, with `*` matching any segment)."""
    for pattern in CONSUMED_PATHS:
        if len(pattern) > len(path):
            continue
        if all(seg == _STAR or seg == path[i] for i, seg in enumerate(pattern)):
            return True
    return False


def _format_path(path: tuple) -> str:
    """A human-readable leaf path, e.g. `recovery_surface[1].kind`. Display
    only (labels may contain dots), never used for matching."""
    out = ""
    for seg in path:
        if isinstance(seg, int):
            out += f"[{seg}]"
        else:
            out += (f".{seg}" if out else str(seg))
    return out


def _completeness_guard(before_audit: dict, after_audit: dict) -> list[ChangelogLine]:
    """The path-granular, whole-document completeness guard (§4).

    Structurally diff `audit_report(before)` against `audit_report(after)` over
    the UNION of both sides' leaf paths (so a REMOVED optional surface present
    only in `before` - `parallel_plan`, `secrets` - is still seen), subtract the
    leaf paths a differ demonstrably reads (`CONSUMED_PATHS`), and emit one
    UNCLASSIFIED honesty line per residual top-level surface that moved. The
    line reports a TYPED/BOOLEAN delta (`changed: true` plus the moved leaf
    paths), never a bare count, so a same-length reshuffle is a change, not
    no-change.
    """
    before_leaves = dict(_leaf_paths(before_audit))
    after_leaves = dict(_leaf_paths(after_audit))
    all_paths = set(before_leaves) | set(after_leaves)

    # group residual moved paths by their top-level surface key, deterministically.
    moved_by_surface: dict[str, list[str]] = {}
    for path in all_paths:
        if not path:
            continue
        if _is_consumed(path):
            continue
        before_value = before_leaves.get(path, _MISSING)
        after_value = after_leaves.get(path, _MISSING)
        if before_value == after_value:
            continue
        surface = str(path[0])
        moved_by_surface.setdefault(surface, []).append(_format_path(path))

    lines: list[ChangelogLine] = []
    for surface in sorted(moved_by_surface):
        paths = tuple(sorted(moved_by_surface[surface]))
        lines.append(ChangelogLine(
            fact=f"audit-path:{surface}",
            category="unclassified",
            text=(f"{surface} changed - not yet differenced; review manually "
                  f"(slice 1 surfaces this honestly; slice 2 classifies it)"),
            changed=True,
            paths=paths,
        ))
    return lines


# --------------------------------------------------------------------------
# classification of the differenced facts (§2 table, §3 buckets).

def _classify_structural(delta: dict) -> list[ChangelogLine]:
    """The membership and wiring axes of `composition_diff.diff` (§2 table)."""
    lines: list[ChangelogLine] = []

    for name in delta["components"]["added"]:
        lines.append(ChangelogLine(
            fact=f"component.added:{name}", category="added",
            text=f"component {name} added"))
    for name in delta["components"]["removed"]:
        lines.append(ChangelogLine(
            fact=f"component.removed:{name}", category="breaking",
            text=f"component {name} removed"))

    for prov in delta["providers"]["changed"]:
        key = prov["key"]
        if prov["from_service"] != prov["to_service"]:
            lines.append(ChangelogLine(
                fact=f"provider.changed:{key}", category="breaking", lede=False,
                text=(f"provider of key {key} changed from {prov['from']} to "
                      f"{prov['to']} (service {prov['from_service']} -> "
                      f"{prov['to_service']})")))
        else:
            lines.append(ChangelogLine(
                fact=f"provider.swapped:{key}", category="internal",
                text=(f"provider of key {key} swapped from {prov['from']} to "
                      f"{prov['to']} (same service {prov['to_service']})")))
    for prov in delta["providers"]["added"]:
        svc = prov.get("service")
        via = f" by {svc}" if svc else ""
        lines.append(ChangelogLine(
            fact=f"provider.added:{prov['key']}", category="added",
            text=f"key {prov['key']} is now provided{via}"))
    for prov in delta["providers"]["removed"]:
        lines.append(ChangelogLine(
            fact=f"provider.removed:{prov['key']}", category="breaking",
            text=f"key {prov['key']} is no longer provided"))

    for edge in delta["requires"]["added"]:
        lines.append(ChangelogLine(
            fact=f"require.added:{edge['component']}:{edge['key']}",
            category="added",
            text=f"{edge['component']} now requires {edge['key']}"))
    for edge in delta["requires"]["removed"]:
        lines.append(ChangelogLine(
            fact=f"require.removed:{edge['component']}:{edge['key']}",
            category="internal",
            text=f"{edge['component']} no longer requires {edge['key']}"))
    for edge in delta["requires"]["broken"]:
        lines.append(ChangelogLine(
            fact=f"require.broken:{edge['component']}:{edge['key']}",
            category="breaking",
            text=(f"{edge['component']} requires {edge['key']} - no provider "
                  f"(broken dependency)")))
    return lines


def _split_crossing(token: str) -> tuple[str, str, str]:
    kind, comp, label = token.split(":", 2)
    return kind, comp, label


def _classify_crossings(delta: dict, reach: dict) -> list[ChangelogLine]:
    """The authority axis (§2 table). Every ADDED crossing is a widening
    (breaking, `lede=True`); a REMOVED crossing or a tightened reach is
    strictly-purer (added/relaxed). A weakened reach is a widening."""
    lines: list[ChangelogLine] = []

    for token in delta["crossings"]["added"]:
        kind, comp, label = _split_crossing(token)
        if kind == "emit":
            text = f"{comp} gained emission {label} (a new boundary reach)"
        elif kind == "host":
            text = f"{comp} now reaches host code {label}"
        elif kind == "taint":
            text = f"{comp} now routes untrusted origin {label} into an emission"
        elif kind == "declassify":
            text = f"{comp} now declassifies untrusted origin {label}"
        elif kind == "secret":
            text = f"secret {label} is now bound to capability {comp}"
        else:
            text = f"new boundary crossing {token}"
        lines.append(ChangelogLine(
            fact=f"crossing.added:{token}", category="breaking", lede=True,
            text=text))

    for token in reach.get("reach_weakened") or []:
        name = token.split(":", 1)[-1]
        lines.append(ChangelogLine(
            fact=f"reach.weakened:{name}", category="breaking", lede=True,
            text=f"extern {name} loosened its reach bound"))

    for token in delta["crossings"]["removed"]:
        kind, comp, label = _split_crossing(token)
        if kind == "emit":
            text = f"{comp} no longer emits {label}"
        elif kind == "host":
            text = f"{comp} no longer reaches host code {label}"
        elif kind == "taint":
            text = f"{comp} no longer routes untrusted origin {label}"
        elif kind == "declassify":
            text = f"{comp} no longer declassifies untrusted origin {label}"
        elif kind == "secret":
            text = f"secret {label} is no longer bound to capability {comp}"
        else:
            text = f"removed boundary crossing {token}"
        lines.append(ChangelogLine(
            fact=f"crossing.removed:{token}", category="added", text=text))

    for token in reach.get("reach_tightened") or []:
        name = token.split(":", 1)[-1]
        lines.append(ChangelogLine(
            fact=f"reach.tightened:{name}", category="added",
            text=f"extern {name} tightened its reach bound"))
    return lines


def _classify_audit_surfaces(before_audit: dict,
                             after_audit: dict) -> list[ChangelogLine]:
    """The Slice-2 authority surfaces (§7), each read by its own `audit_diff`
    differ. Every WIDENING is a breaking line with `lede=True` (an authority
    reach that grew - the operator's lede); every safe direction (a narrowed
    scope, a dropped backend, a gained recovery, a strengthened register, a
    tightened ceiling) is added/relaxed. These were Slice-1 honesty lines; now
    that their leaves are in `CONSUMED_PATHS`, the guard no longer surfaces them
    and they render classified here instead."""
    lines: list[ChangelogLine] = []

    # capability scope (boundary[*].capabilities): send.mail -> send.* on a
    # STABLE crossing is a widening the scope-free `emit:` token cannot carry.
    scopes = diff_capability_scopes(before_audit, after_audit)
    for token in scopes["scope_widened"]:
        _kind, comp, label = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"scope.widened:{comp}:{label}", category="breaking", lede=True,
            text=f"{comp} widened the capability scope of emission {label}"))
    for token in scopes["scope_tightened"]:
        _kind, comp, label = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"scope.tightened:{comp}:{label}", category="added",
            text=f"{comp} narrowed the capability scope of emission {label}"))

    # backends (externs[*].backends): a new host body is new reachable host code.
    backends = diff_backends(before_audit, after_audit)
    for token in backends["backends_added"]:
        _kind, name, backend = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"backend.added:{name}:{backend}", category="breaking",
            lede=True,
            text=(f"extern {name} gained a {backend} host body "
                  f"(new reachable host code)")))
    for token in backends["backends_removed"]:
        _kind, name, backend = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"backend.removed:{name}:{backend}", category="added",
            text=f"extern {name} dropped its {backend} host body"))

    # recovery_surface: a dropped inverse turns a reversible effect irreversible.
    recovery = diff_recovery(before_audit, after_audit)
    for token in recovery["recovery_dropped"]:
        _kind, name, rkind = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"recovery.dropped:{name}:{rkind}", category="breaking",
            lede=True,
            text=(f"extern {name} lost its {rkind} recovery "
                  f"(a reversible effect became irreversible)")))
    for token in recovery["recovery_weakened"]:
        _kind, name, rkind = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"recovery.weakened:{name}:{rkind}", category="breaking",
            lede=True,
            text=(f"extern {name} weakened the idempotency register of its "
                  f"{rkind} recovery")))
    for token in recovery["recovery_added"]:
        _kind, name, rkind = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"recovery.added:{name}:{rkind}", category="added",
            text=f"extern {name} gained a {rkind} recovery"))

    # capability_registers: a weakened idempotency floor lets a consumer that
    # relied on the stronger guarantee double-apply.
    registers = diff_registers(before_audit, after_audit)
    for token in registers["registers_weakened"]:
        cap = token.split(":", 1)[-1]
        lines.append(ChangelogLine(
            fact=f"register.weakened:{cap}", category="breaking", lede=True,
            text=f"idempotency register floor of {cap} weakened"))
    for token in registers["registers_strengthened"]:
        cap = token.split(":", 1)[-1]
        lines.append(ChangelogLine(
            fact=f"register.strengthened:{cap}", category="added",
            text=f"idempotency register floor of {cap} strengthened"))

    # cardinality: a raised emission ceiling admits more crossings per activation.
    cardinality = diff_cardinality(before_audit, after_audit)
    for token in cardinality["cardinality_widened"]:
        _kind, comp, cap = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"cardinality.widened:{comp}:{cap}", category="breaking",
            lede=True,
            text=f"{comp} raised its emission ceiling for {cap}"))
    for token in cardinality["cardinality_tightened"]:
        _kind, comp, cap = token.split(":", 2)
        lines.append(ChangelogLine(
            fact=f"cardinality.tightened:{comp}:{cap}", category="added",
            text=f"{comp} tightened its emission ceiling for {cap}"))
    return lines


def _classify_semver(version_result: dict | None) -> list[ChangelogLine]:
    """The interface axis (§2 table), read off `version.derive`'s `changes`.

    Classified by the change's own BUMP (the authoritative item-64 verdict): a
    major is breaking, a minor is added/relaxed. A change whose bump is neither
    (a future drift kind) is treated as unclassified-and-visible, never hidden.
    The rendered text is the change's own `reason`, never re-authored here.
    """
    if not version_result:
        return []
    lines: list[ChangelogLine] = []
    for change in version_result.get("changes") or []:
        where = change["service"]
        if change.get("method"):
            where = f"{change['service']}.{change['method']}"
        fact = f"semver:{where}:{change['kind']}"
        reason = _strip_ticks(change["reason"])
        if change["bump"] == MAJOR:
            lines.append(ChangelogLine(fact=fact, category="breaking",
                                       lede=False, text=reason))
        elif change["bump"] == MINOR:
            lines.append(ChangelogLine(fact=fact, category="added", text=reason))
        else:
            lines.append(ChangelogLine(fact=fact, category="unclassified",
                                       text=reason, changed=True))
    return lines


def _strip_ticks(text: str) -> str:
    return text.replace("`", "")


# --------------------------------------------------------------------------
# the headline (§3).

def _level_of_lines(lines: list[ChangelogLine]) -> str:
    """The bump floor implied by the classified (non-unclassified) lines: the
    join over each line's category level (breaking -> major, added -> minor,
    internal -> patch)."""
    level = PATCH
    for line in lines:
        implied = _CATEGORY_LEVEL.get(line.category)
        if implied and _RANK[implied] > _RANK[level]:
            level = implied
    return level


def _compute_headline(version_result: dict | None, classified: list[ChangelogLine],
                      unclassified: list[ChangelogLine], previous_version: str | None,
                      degraded: bool) -> dict:
    """The conservative headline (§3).

    `headline = max(version.bump, authority_bump, wiring_bump)`, computed as the
    join over the interface bump and every classified body line's implied level,
    so a changelog can NEVER headline PATCH over a body that carries a breaking
    line. A NON-EMPTY unclassified bucket forces `clean = false`: the headline
    may claim a definite level only when the bucket is empty, otherwise it
    refuses a clean level and carries the `LEVEL? (...)` incompleteness marker
    (§3, finding 2). In degraded mode (no interface table) the semver bump is
    withheld and stated, never faked.
    """
    body_level = _level_of_lines(classified)
    clean = not unclassified

    if degraded:
        headline: dict = {
            "clean": clean,
            "semver": False,
            "bump": "undetermined",
            "note": ("undetermined (inputs lack the interface table; run "
                     "against compiled composition docs for a semver headline)"),
            "floor": body_level,
        }
        if not clean:
            headline["marker"] = (
                "undetermined? (unclassified authority/recovery changes "
                "present; review body)")
        return headline

    version_bump = (version_result or {}).get("bump", PATCH)
    level = version_bump if _RANK[version_bump] >= _RANK[body_level] else body_level

    headline = {"clean": clean, "semver": True, "bump": level}
    if previous_version:
        headline["previousVersion"] = previous_version
        headline["nextVersion"] = (version_result or {}).get("nextVersion")
    if not clean:
        headline["marker"] = (
            f"{level.upper()}? (unclassified authority/recovery changes "
            f"present; review body)")
    return headline


# --------------------------------------------------------------------------
# assembly.

def _bucket(lines: list[ChangelogLine]) -> dict[str, list[ChangelogLine]]:
    """Partition lines into the four category buckets, lede-first within
    breaking (a stable partition that preserves upstream sorted order in each
    half). No set or map is iterated without a sort upstream, so bucket order is
    a pure function of the two inputs."""
    buckets: dict[str, list[ChangelogLine]] = {c: [] for c in _CATEGORY_ORDER}
    for line in lines:
        buckets[line.category].append(line)
    breaking = buckets["breaking"]
    buckets["breaking"] = ([ln for ln in breaking if ln.lede]
                           + [ln for ln in breaking if not ln.lede])
    return buckets


def _line_json(line: ChangelogLine) -> dict:
    out: dict = {"fact": line.fact, "text": line.text}
    if line.category == "breaking":
        out["lede"] = line.lede
    if line.category == "unclassified":
        out["changed"] = bool(line.changed)
        out["paths"] = list(line.paths)
    return out


def _build_changelog(delta: dict, version_result: dict | None,
                     before_audit: dict, after_audit: dict,
                     from_label: str, to_label: str,
                     previous_version: str | None, degraded: bool) -> dict:
    """The pure core: fold three already-computed differ results into the
    section-4 JSON document. Every input is a plain value, so this is the seam
    the tests inject precise audit reports through."""
    reach = diff_reach(before_audit, after_audit)

    lines: list[ChangelogLine] = []
    lines += _classify_crossings(delta, reach)
    lines += _classify_audit_surfaces(before_audit, after_audit)
    lines += _classify_semver(version_result)
    lines += _classify_structural(delta)

    guard_lines = _completeness_guard(before_audit, after_audit)
    lines += guard_lines

    buckets = _bucket(lines)
    classified = buckets["breaking"] + buckets["added"] + buckets["internal"]
    unclassified = buckets["unclassified"]

    headline = _compute_headline(version_result, classified, unclassified,
                                 previous_version, degraded)

    doc = {"headline": headline}
    for category in _CATEGORY_ORDER:
        doc[category] = [_line_json(ln) for ln in buckets[category]]
    doc["generatedFrom"] = {"fromLabel": from_label, "toLabel": to_label}
    return doc


def derive_changelog(before: dict, after: dict, previous_version: str | None = None,
                     no_semver: bool = False, from_label: str = "before",
                     to_label: str = "after") -> dict:
    """Compute the changelog document from two composition IRs.

    A pure function of the two IR documents: no wall-clock, no environment, no
    filesystem. Reuses `composition_diff.diff` for the structural delta,
    `version.derive` for the semver headline (unless degraded), and
    `audit_diff.diff_reach` plus the completeness guard for the authority axis.

    Degrades honestly: given a bare `audit --json` interchange doc (no interface
    table), or with `no_semver=True`, it emits the full structural + authority
    changelog and withholds the semver headline rather than faking one.
    """
    delta = composition_diff(before, after)
    before_audit = audit_report(before)
    after_audit = audit_report(after)

    version_result: dict | None = None
    degraded = no_semver
    if not no_semver:
        from .version import derive as version_derive  # noqa: PLC0415
        try:
            version_result = version_derive(before, after, previous_version)
        except ValueError:
            # an interchange doc carries the boundary surface, not the interface
            # table; structural honesty survives, only the semver headline is
            # withheld (§5).
            degraded = True

    return _build_changelog(delta, version_result, before_audit, after_audit,
                            from_label, to_label, previous_version, degraded)


# --------------------------------------------------------------------------
# rendering.

# The stable release-note skeleton (Slice 3, item 49 attach point). The section
# order and headings are a CONTRACT: release tooling splices under these anchors,
# so both the Markdown and the plain renderers drive off this single list, and
# every section is always emitted in this order - an empty one carries an
# explicit placeholder rather than vanishing, so a tool never has to guess
# whether a missing heading means "no changes" or "renderer changed".
SECTIONS = (
    ("breaking", "Breaking / authority widenings"),
    ("added", "Added / relaxed (compatible)"),
    ("internal", "Internal / wiring"),
    ("unclassified", "Unclassified changes (review manually)"),
)

# The output formats `revl changelog --format` accepts. `markdown` is the stable
# skeleton above; `json` is the section-4 document a registry or bot consumes;
# `plain` is the same skeleton with no Markdown markup, for a log line or a
# release tool that renders its own presentation.
FORMATS = ("markdown", "json", "plain")


def _render_headline(headline: dict) -> str:
    if headline.get("marker"):
        head = headline["marker"]
    elif not headline.get("semver", True):
        head = headline.get("note", "undetermined")
    else:
        head = headline["bump"].upper()
        if headline.get("previousVersion") and headline.get("nextVersion"):
            head += (f"  ({headline['previousVersion']} -> "
                     f"{headline['nextVersion']})")
    return head


def render_markdown(doc: dict, title: str | None = None) -> str:
    """Render the changelog document as a release note (Markdown, no timestamp).

    A STABLE skeleton (Slice 3): every `SECTIONS` heading is emitted in the fixed
    order, an empty section carrying an explicit `_None._` placeholder rather
    than disappearing, so release tooling can rely on the `##` anchors being
    present and ordered. A release date, if wanted, is passed via `title` by the
    operator and lives in the header the renderer treats as opaque, never in a
    derived line.
    """
    out: list[str] = []
    if title:
        out.append(f"# {title}")
        out.append("")
    out.append(f"**Release impact: {_render_headline(doc['headline'])}**")
    out.append("")

    for key, heading in SECTIONS:
        entries = doc.get(key) or []
        out.append(f"## {heading}")
        if not entries:
            out.append("_None._")
            out.append("")
            continue
        for entry in entries:
            out.append(f"- {entry['text']}")
            if key == "unclassified" and entry.get("paths"):
                out.append(f"  - moved: {', '.join(entry['paths'])}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_plain(doc: dict, title: str | None = None) -> str:
    """Render the changelog document as plain text - the same stable skeleton as
    `render_markdown`, with no Markdown markup (no `#`, `**`, or `-`).

    For a release tool that renders its own presentation, a commit-message body,
    or a log line, where Markdown decoration would be noise. Deterministic and
    timestamp-free, exactly like the Markdown form.
    """
    out: list[str] = []
    if title:
        out.append(title)
        out.append("=" * len(title))
        out.append("")
    out.append(f"Release impact: {_render_headline(doc['headline'])}")
    out.append("")

    for key, heading in SECTIONS:
        entries = doc.get(key) or []
        out.append(f"{heading}:")
        if not entries:
            out.append("  (none)")
            out.append("")
            continue
        for entry in entries:
            out.append(f"  * {entry['text']}")
            if key == "unclassified" and entry.get("paths"):
                out.append(f"      moved: {', '.join(entry['paths'])}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render(doc: dict, title: str | None = None, as_json: bool = False,
           fmt: str | None = None) -> str:
    """The CLI entry. `fmt` (one of `FORMATS`) selects the output; when it is
    None the legacy `as_json` flag chooses between Markdown and JSON, so callers
    predating `--format` keep working.
    """
    if fmt is None:
        fmt = "json" if as_json else "markdown"
    if fmt == "json":
        return json.dumps(doc, indent=2)
    if fmt == "plain":
        return render_plain(doc, title=title)
    if fmt == "markdown":
        return render_markdown(doc, title=title)
    raise ValueError(f"unknown changelog format: {fmt!r} (expected one of "
                     f"{', '.join(FORMATS)})")
