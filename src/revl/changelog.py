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
  * `audit_diff.diff_reach` (item 373): the reach drift a crossing-set diff
    cannot see.

Every rendered line carries a mandatory provenance `fact` token drawn from one
of those differs. A line with no backing fact is a defect, not a feature, and
`ChangelogLine` refuses to construct one.

Slice 1 is honest-but-incomplete. The `audit_diff` foundation differences only
crossings and reach today; the other authority surfaces `audit_report` carries
(`recovery_surface`, `capability_registers`, `cardinality`, the per-component
`boundary[*].capabilities` scope map, and `externs[*].{backends,class,...}`) have
no differ yet. So the completeness guard (`_completeness_guard`) structurally
diffs the WHOLE `audit_report(before)` against `audit_report(after)`, subtracts
the exact leaf paths a differ demonstrably reads (`CONSUMED_PATHS`), and emits
one UNCLASSIFIED honesty line per residual differing path. A non-empty
unclassified bucket also forces the headline non-clean (it may claim a definite
bump level only when the bucket is empty). Slice 2 will add the missing differs
to `audit_diff.py` and move each honesty line to its real class.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .audit_diff import audit_report, diff_reach
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

    A release date, if wanted, is passed via `title` by the operator and lives
    in the header the renderer treats as opaque, never in a derived line.
    """
    out: list[str] = []
    if title:
        out.append(f"# {title}")
        out.append("")
    out.append(f"**Release impact: {_render_headline(doc['headline'])}**")
    out.append("")

    sections = [
        ("breaking", "Breaking / authority widenings"),
        ("added", "Added / relaxed (compatible)"),
        ("internal", "Internal / wiring"),
        ("unclassified", "Unclassified changes (review manually)"),
    ]
    any_body = False
    for key, heading in sections:
        entries = doc.get(key) or []
        if not entries:
            continue
        any_body = True
        out.append(f"## {heading}")
        for entry in entries:
            out.append(f"- {entry['text']}")
            if key == "unclassified" and entry.get("paths"):
                out.append(f"  - moved: {', '.join(entry['paths'])}")
        out.append("")

    if not any_body:
        out.append("No structural, authority, or interface change detected.")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render(doc: dict, title: str | None = None, as_json: bool = False) -> str:
    """The CLI entry: Markdown by default, the structured document with
    `--json`."""
    if as_json:
        return json.dumps(doc, indent=2)
    return render_markdown(doc, title=title)
