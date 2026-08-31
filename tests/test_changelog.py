"""`revl changelog` - the derived release note (roadmap item 261, Slice 1).

The changelog is a MEASUREMENT, not a promise: every rendered line is a
projection of a fact one of the shipped differs already produces
(`composition_diff.diff`, `version.derive`, `audit_diff.diff_reach`), and a line
with no backing fact cannot be constructed. Slice 1 is honest-but-incomplete:
the audit surfaces no differ yet reads (`recovery_surface`,
`capability_registers`, `cardinality`, the per-component `boundary[*]`
capability scope map, `externs[*].backends`) are surfaced by the path-granular
completeness guard as unclassified honesty lines, never silently dropped, and a
non-empty unclassified bucket forces the headline non-clean.

These tests pin the definition of done. The guard regression suite is mandatory:
it is the mechanism that makes Slice 1 safe to ship before Slice 2 exists.
"""

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import audit_diff, composition_diff, version  # noqa: E402
from revl.audit_diff import audit_report, diff_reach  # noqa: E402
from revl.changelog import (  # noqa: E402
    CONSUMED_PATHS, ChangelogLine, _build_changelog, _completeness_guard,
    derive_changelog, render_markdown)


# an empty structural delta, for the pure-core headline tests that inject only
# audit reports (the shape `composition_diff.diff` returns for no change).
EMPTY_DELTA = {
    "components": {"added": [], "removed": [], "changed": []},
    "providers": {"added": [], "removed": [], "changed": []},
    "requires": {"added": [], "removed": [], "broken": []},
    "crossings": {"added": [], "removed": []},
}


# a stable two-component base (mirrors test_composition_diff's BASE).
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""


def _changelog(before_src: str, after_src: str, **kw) -> dict:
    return derive_changelog(compile_source(before_src),
                            compile_source(after_src), **kw)


# ----------------------------------------------------- the guard regression suite

def test_guard_capability_scope_widening_on_a_stable_crossing():
    """The new CRITICAL: an emission that widens its declared capability scope
    (`send.mail -> send.*`) while its crossing token stays `emit:C:notify` is
    invisible to `diff_crossings` (scope is not in the token). The path-granular
    guard must honesty-line it - it must NOT be dropped, and the release must
    NOT read as a clean PATCH."""
    before = {"boundary": {"C": {"emissions": ["notify"],
                                 "capabilities": {"notify": ["send.mail"]}}}}
    after = {"boundary": {"C": {"emissions": ["notify"],
                                "capabilities": {"notify": ["send.*"]}}}}

    lines = _completeness_guard(before, after)
    assert len(lines) == 1
    line = lines[0]
    assert line.category == "unclassified"
    assert line.fact == "audit-path:boundary"
    assert line.changed is True
    assert any("capabilities.notify" in p for p in line.paths)

    # and end-to-end it forces a non-clean headline, never a clean PATCH.
    doc = _build_changelog(EMPTY_DELTA, {"bump": "patch", "changes": []},
                           before, after, "v1", "v2", None, degraded=False)
    assert doc["headline"]["clean"] is False
    assert "PATCH?" in doc["headline"]["marker"]
    assert len(doc["unclassified"]) == 1


def test_guard_new_backend_host_body():
    """The second CRITICAL instance: an extern that GAINS a backend host body
    (`backends: ["rust"] -> ["py","rust"]`, new reachable host code) is
    invisible to `diff_reach`, which reads only `reach`. The guard honesty-lines
    it."""
    before = {"externs": [{"name": "x", "reach": {"kind": "net"},
                           "backends": ["rust"]}]}
    after = {"externs": [{"name": "x", "reach": {"kind": "net"},
                          "backends": ["py", "rust"]}]}
    lines = _completeness_guard(before, after)
    assert len(lines) == 1
    assert lines[0].fact == "audit-path:externs"
    assert any("backends" in p for p in lines[0].paths)


def test_guard_reach_change_alone_is_consumed_not_honesty_lined():
    """A pure `reach` change is read by `diff_reach`, so it is NOT a guard
    honesty line (it flows through the classified authority axis instead). This
    pins that `externs[*].reach` really is in CONSUMED_PATHS."""
    before = {"externs": [{"name": "x", "reach": {"kind": "net", "target": "a"},
                           "backends": ["rust"]}]}
    after = {"externs": [{"name": "x", "reach": {"kind": "net", "target": "b"},
                          "backends": ["rust"]}]}
    assert _completeness_guard(before, after) == []


def test_guard_removed_optional_surface_trips_over_before_union():
    """MEDIUM finding 3: `parallel_plan` is conditionally present. Removing the
    last parallel group leaves it in `before` and absent from `after`; a guard
    iterating `after` alone would never see it. The guard must drive over
    `before | after`."""
    before = {"parallel_plan": {"C": [{"group": [0, 1]}]}}
    after = {}
    lines = _completeness_guard(before, after)
    assert len(lines) == 1
    assert lines[0].fact == "audit-path:parallel_plan"
    assert lines[0].changed is True


def test_guard_same_length_reshuffle_reports_changed_not_a_count():
    """LOW finding 4: a same-length register swap inside `recovery_surface`
    (unhashable `list[dict]`) must report `changed: true` with the moved paths,
    never a bare count that a reshuffle could hide."""
    before = {"recovery_surface": [
        {"name": "a", "kind": "inverse", "register": "keyed"},
        {"name": "b", "kind": "inverse", "register": "declared"}]}
    after = {"recovery_surface": [
        {"name": "a", "kind": "inverse", "register": "declared"},
        {"name": "b", "kind": "inverse", "register": "keyed"}]}
    lines = _completeness_guard(before, after)
    assert len(lines) == 1
    assert lines[0].fact == "audit-path:recovery_surface"
    assert lines[0].changed is True
    assert any("register" in p for p in lines[0].paths)


def test_guard_dropped_recovery_inverse_honesty_lines_and_floors_headline():
    """The original CRITICAL: a witnessed/acquire extern's `undo` is deleted,
    turning a reversible effect irreversible. It shows up ONLY in
    `recovery_surface` (no crossing, no interface change). It must honesty-line
    AND force the headline non-clean - never a silent clean PATCH."""
    before = {"recovery_surface": [
        {"name": "acquire", "kind": "inverse", "register": "keyed"}]}
    after = {"recovery_surface": []}

    lines = _completeness_guard(before, after)
    assert len(lines) == 1
    assert lines[0].fact == "audit-path:recovery_surface"

    doc = _build_changelog(EMPTY_DELTA,
                           {"bump": "patch", "changes": [], "nextVersion": None},
                           before, after, "v1", "v2", None, degraded=False)
    assert doc["breaking"] == [] and doc["added"] == [] and doc["internal"] == []
    assert len(doc["unclassified"]) == 1
    assert doc["headline"]["clean"] is False
    assert doc["headline"]["bump"] == "patch"
    assert "PATCH?" in doc["headline"]["marker"]


# ---------------------------------------------------------- the headline invariant

def test_headline_clean_only_when_unclassified_empty():
    """When nothing honesty-lines and the only change is a consumed emission
    add, the headline may claim a definite level (clean=true, no marker)."""
    before = {"boundary": {"C": {"emissions": ["a"]}}}
    after = {"boundary": {"C": {"emissions": ["a", "b"]}}}
    delta = dict(EMPTY_DELTA)
    delta = json.loads(json.dumps(EMPTY_DELTA))
    delta["crossings"] = {"added": ["emit:C:b"], "removed": []}
    version_result = {"bump": "major", "changes": [
        {"service": "S", "method": "b", "kind": "added", "bump": "major",
         "reason": "b added"}], "nextVersion": None}
    doc = _build_changelog(delta, version_result, before, after, "v1", "v2",
                           None, degraded=False)
    assert doc["unclassified"] == []
    assert doc["headline"]["clean"] is True
    assert doc["headline"]["bump"] == "major"
    assert "marker" not in doc["headline"]


def test_headline_never_understates_a_breaking_body_line():
    """A body breaking line (an added crossing) floors the headline at major even
    when the interface diff reads PATCH - never headline PATCH over a widening."""
    before = {"boundary": {"C": {"emissions": ["a"]}}}
    after = {"boundary": {"C": {"emissions": ["a", "b"]}}}
    delta = json.loads(json.dumps(EMPTY_DELTA))
    delta["crossings"] = {"added": ["emit:C:b"], "removed": []}
    doc = _build_changelog(delta, {"bump": "patch", "changes": []},
                           before, after, "v1", "v2", None, degraded=False)
    assert doc["breaking"][0]["fact"] == "crossing.added:emit:C:b"
    assert doc["headline"]["bump"] == "major"
    assert doc["headline"]["clean"] is True


# ------------------------------------------------------------- CONSUMED_PATHS test

def test_every_consumed_path_corresponds_to_a_real_differ_read():
    """The allowlist cannot rot: every `CONSUMED_PATHS` leaf pattern must name a
    differ whose source demonstrably READS that leaf. If a future edit adds a
    path here without a backing read, this fails."""
    modules = {"audit_diff": audit_diff, "composition_diff": composition_diff}
    for pattern, (mod_name, func_name) in CONSUMED_PATHS.items():
        module = modules[mod_name]
        func = getattr(module, func_name)
        src = inspect.getsource(func)
        # the last concrete (non-wildcard) segment names the leaf the differ reads
        leaf = [seg for seg in pattern if seg != "*"][-1]
        assert leaf in src, (
            f"CONSUMED_PATHS pattern {pattern} claims {mod_name}.{func_name} "
            f"reads {leaf!r}, but its source does not reference it")


# ------------------------------------------------------------------ bijection test

def _resolvable_facts(before_ir: dict, after_ir: dict) -> set[str]:
    """Independently reconstruct the set of fact tokens the inputs can back,
    from the raw differ outputs (NOT from the renderer's line-building), so this
    is a real check that every rendered line traces to an upstream fact."""
    delta = composition_diff.diff(before_ir, after_ir)
    ba, aa = audit_report(before_ir), audit_report(after_ir)
    reach = diff_reach(ba, aa)
    facts: set[str] = set()
    for token in delta["crossings"]["added"]:
        facts.add(f"crossing.added:{token}")
    for token in delta["crossings"]["removed"]:
        facts.add(f"crossing.removed:{token}")
    for token in reach["reach_weakened"]:
        facts.add(f"reach.weakened:{token.split(':', 1)[-1]}")
    for token in reach["reach_tightened"]:
        facts.add(f"reach.tightened:{token.split(':', 1)[-1]}")
    for name in delta["components"]["added"]:
        facts.add(f"component.added:{name}")
    for name in delta["components"]["removed"]:
        facts.add(f"component.removed:{name}")
    for prov in delta["providers"]["changed"]:
        facts.add(f"provider.changed:{prov['key']}")
        facts.add(f"provider.swapped:{prov['key']}")
    for prov in delta["providers"]["added"]:
        facts.add(f"provider.added:{prov['key']}")
    for prov in delta["providers"]["removed"]:
        facts.add(f"provider.removed:{prov['key']}")
    for edge in delta["requires"]["added"]:
        facts.add(f"require.added:{edge['component']}:{edge['key']}")
    for edge in delta["requires"]["removed"]:
        facts.add(f"require.removed:{edge['component']}:{edge['key']}")
    for edge in delta["requires"]["broken"]:
        facts.add(f"require.broken:{edge['component']}:{edge['key']}")
    try:
        vr = version.derive(before_ir, after_ir, None)
        for change in vr["changes"]:
            where = (f"{change['service']}.{change['method']}"
                     if change["method"] else change["service"])
            facts.add(f"semver:{where}:{change['kind']}")
    except ValueError:
        pass
    for line in _completeness_guard(ba, aa):
        facts.add(line.fact)
    return facts


def test_every_rendered_line_has_a_backing_fact():
    """The bijection: every line in the document carries a `fact`, and every
    such `fact` resolves to a member of the union of the differ outputs."""
    before = compile_source(BASE)
    after = compile_source(BASE.replace(
        "component Front requires cache: Cache { }",
        'component Front requires cache: Cache { emit cache.put("h", "1") }')
        + '\ncomponent Metrics requires cache: Cache { }')
    doc = derive_changelog(before, after)
    allowed = _resolvable_facts(before, after)

    seen = 0
    for category in ("breaking", "added", "internal", "unclassified"):
        for entry in doc[category]:
            assert entry["fact"], "a rendered line carried no fact"
            assert entry["fact"] in allowed, (
                f"line fact {entry['fact']!r} does not resolve to an input fact")
            seen += 1
    assert seen > 0


def test_changelogline_refuses_an_empty_fact():
    """The structural guarantee behind 'no invented prose': a line cannot be
    built without a non-empty backing fact."""
    try:
        ChangelogLine(fact="", category="added", text="This release improves reliability")
    except ValueError:
        pass
    else:
        raise AssertionError("a ChangelogLine with no fact must be refused")


# --------------------------------------------------------------------- determinism

def test_determinism_byte_identical_and_serialization_invariant():
    """A pure function of the two IRs: rendered twice is byte-identical, and the
    result is invariant under re-serializing either input (JSON key order must
    not matter)."""
    before = compile_source(BASE)
    after = compile_source(BASE.replace(
        "component Front requires cache: Cache { }",
        'component Front requires cache: Cache { emit cache.put("h", "1") }'))

    first = render_markdown(derive_changelog(before, after))
    second = render_markdown(derive_changelog(before, after))
    assert first == second

    reser_before = json.loads(json.dumps(before))
    reser_after = json.loads(json.dumps(after))
    third = render_markdown(derive_changelog(reser_before, reser_after))
    assert first == third


# --------------------------------------------------------------- structural axes

def test_a_same_service_provider_swap_renders_internal_never_dropped():
    """CRITICAL-adjacent Attack A: a live provider swap (same service interface)
    is interface-compatible, so semver stays PATCH - but it must ALWAYS render
    (never folded into 'no change'), under Internal/wiring."""
    before = """
    service Database { emission fn execute(sql: Str) }
    component Pg provides db: Database {
      provide db { fn execute(sql) { } }
    }
    """
    after = """
    service Database { emission fn execute(sql: Str) }
    component Mysql provides db: Database {
      provide db { fn execute(sql) { } }
    }
    """
    doc = _changelog(before, after)
    swaps = [ln for ln in doc["internal"] if ln["fact"].startswith("provider.swapped")]
    assert swaps, "a same-service provider swap must render under internal"


def test_a_removed_component_is_breaking():
    after = """
    service Cache { emission[db] fn put(key: Str, value: Str) }
    service Database { emission fn execute(sql: Str) -> Int }
    component PgCache requires db: Database provides cache: Cache {
      provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
    }
    """  # Front removed
    doc = _changelog(BASE, after)
    facts = [ln["fact"] for ln in doc["breaking"]]
    assert "component.removed:Front" in facts
    assert doc["headline"]["bump"] == "major"


# ------------------------------------------------------------------ degraded input

def test_no_semver_withholds_the_headline_and_states_it():
    """`--no-semver` (and, transitively, a bare audit doc that lacks the
    interface table) emits the full structural changelog and WITHHOLDS the
    semver headline, stating its absence rather than faking a level."""
    before = compile_source(BASE)
    after = compile_source(BASE + """
    component Metrics requires cache: Cache { }
    """)
    doc = derive_changelog(before, after, no_semver=True)
    assert doc["headline"]["semver"] is False
    assert doc["headline"]["bump"] == "undetermined"
    assert "interface table" in doc["headline"]["note"]
    # the structural axis survives the degrade
    assert any(ln["fact"] == "component.added:Metrics" for ln in doc["added"])
