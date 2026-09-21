"""The issue-#1285 closed-vocabulary gate: it holds on this tree, it fires on
each way a mirror goes wrong, and it still names the three instances the issue
was filed for."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "check_vocabulary_mirrors", ROOT / "tools" / "check_vocabulary_mirrors.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _state():
    tool = _tool()
    sites = tool.scan()
    entries, err = tool.load_ledger(tool.LEDGER)
    return tool, sites, entries, err


def _claim_state():
    """The claim rule's inputs (issue #1336), beside the class rule's."""
    tool, sites, _, _ = _state()
    claims = tool.scan_claims(sites)
    entries, err = tool.load_claim_ledger(tool.LEDGER)
    return tool, sites, claims, entries, err


def test_the_tree_is_green_and_the_scan_is_not_vacuous():
    tool, sites, entries, err = _state()
    assert err is None, err
    # Vacuity first: a green verdict over an empty scan means nothing.
    assert len(sites) > 500, f"only {len(sites)} vocabulary sites; the walk is broken"
    assert len(tool.classes(sites)) > 10
    assert tool.check(sites, entries, err) == []


def test_every_recorded_class_carries_a_written_reason():
    tool, _, entries, _ = _state()
    for entry in entries:
        assert (entry.get("note") or "").strip(), entry["sites"]


def test_the_three_instances_issue_1285_was_filed_for_are_in_the_inventory():
    tool, sites, _, _ = _state()
    found = {site for cls in tool.classes(sites) for site in cls["sites"]}
    # 1: the type-to-schema mappings (issue #1272).
    assert "src/revl/mcp/schema.py::_JSON_TYPES" in found
    assert "src/revl/export_openapi.py::_SCALARS" in found
    # 2: the IR path normalizations (issue #1276). RESOLVED, and this is the
    # shape the resolution takes, so the assertion is the inverse one. Both
    # sites now delegate the rewriting to `attest.path_normalized_ir` and each
    # names fewer than MIN_TOKENS field names of its own, so neither is a
    # vocabulary site and the class is gone from the scan. The ledger entry
    # went with it (shrink-only, issue #1332). Asserting the delegation and not
    # just the absence is what keeps this a check: a copy re-grown here would
    # put the class back.
    assert not ({"src/revl/bundle.py::_canonical_ir",
                 "src/revl/registry.py::_normalize_ir_for_attest"} & found)
    for module in ("src/revl/bundle.py", "src/revl/registry.py"):
        assert "attest.path_normalized_ir" in ROOT.joinpath(module).read_text(
            encoding="utf-8"), (
            f"{module} no longer delegates the IR path normalization to "
            "attest.path_normalized_ir; issue #1276's duplication is back")
    # 3: the taint fold origins (issue #1195).
    assert {"src/revl/policy.py::TAINT_FOLD_ORIGINS",
            "src/revl/taint.py::_SOURCE_CLASS_SCOPES"} <= found


def test_the_taint_origin_mirror_is_recorded_as_one_class():
    """Instance 3 is the one an auto-approve decision is made against, so it is
    worth asserting by name rather than only by membership."""
    tool, sites, _, _ = _state()
    classes = {tuple(c["sites"]): c for c in tool.classes(sites)}
    key = ("src/revl/policy.py::TAINT_FOLD_ORIGINS",
           "src/revl/taint.py::_SOURCE_CLASS_SCOPES")
    assert key in classes
    # `screen` joined BOTH copies in one commit (item 521 Slice 2). That is the
    # vocabulary moving, not a drift, and the ledger records the six-token
    # spelling since issue #1332. Spelled out rather than read from the ledger:
    # a test that takes its expectation from the artifact under test asserts
    # nothing.
    assert classes[key]["tokens"] == ["fs", "input", "model", "net", "screen",
                                      "web"]


def test_a_one_sided_addition_to_a_recorded_class_reds():
    """Issue #1195's defect, run against the real ledger: a source class added
    to one copy and not the other."""
    tool, sites, entries, err = _state()
    drifted = [
        s if s.sid != "src/revl/taint.py::_SOURCE_CLASS_SCOPES"
        else tool.Site(s.module, s.name, s.kind,
                       s.tokens | {"clipboard"}, s.lineno)
        for s in sites]
    problems = tool.check(drifted, entries, err)
    assert len(problems) == 1, problems
    assert "UNNAMED DIVERGENCE" in problems[0]
    assert "clipboard" in problems[0]


def test_a_new_mirror_reds():
    """Issue #1285's third exit clause: a fourth instance cannot arrive
    silently."""
    tool, sites, entries, err = _state()
    origins = next(s for s in sites
                   if s.sid == "src/revl/policy.py::TAINT_FOLD_ORIGINS")
    fourth = tool.Site("src/revl/distill.py", "_ADMITTABLE_ORIGINS", "const",
                       origins.tokens, 1)
    problems = tool.check(sites + [fourth], entries, err)
    assert len(problems) == 1, problems
    assert "GREW A COPY" in problems[0]
    assert "src/revl/distill.py::_ADMITTABLE_ORIGINS" in problems[0]

    unrelated = tool.Site("src/revl/a.py", "V", "const",
                          frozenset({"alpha", "beta", "gamma", "delta"}), 1)
    twin = tool.Site("src/revl/b.py", "W", "const", unrelated.tokens, 1)
    problems = tool.check(sites + [unrelated, twin], entries, err)
    assert len(problems) == 1, problems
    assert "NEW MIRROR" in problems[0]


def test_a_resolved_mirror_left_in_the_ledger_reds():
    """Shrink-only: the fix for a mirror that is gone is to DELETE its entry,
    not to leave it behind where it can never fire again."""
    tool, sites, entries, err = _state()
    survivors = [s for s in sites
                 if s.sid != "src/revl/taint.py::_SOURCE_CLASS_SCOPES"]
    problems = tool.check(survivors, entries, err)
    assert len(problems) == 1, problems
    assert "NO LONGER OBSERVED" in problems[0]
    assert "DELETE" in problems[0]


def test_a_missing_ledger_reds_rather_than_reading_as_nothing_to_check():
    tool, sites, _, _ = _state()
    entries, err = tool.load_ledger(ROOT / "tests" / "fixtures" / "no-such.json")
    assert entries is None
    problems = tool.check(sites, entries, err)
    assert len(problems) == 1 and "RED" in problems[0]


def test_a_vacuous_scan_reds_rather_than_reading_as_a_clean_tree():
    tool, _, entries, err = _state()
    assert "VACUOUS" in tool.check([], entries, err)[0]


def test_the_ledger_on_disk_is_the_one_spelling_the_tool_writes():
    """The ledger is names and tokens only, both sorted, with no counts and no
    line numbers, so it is byte-identical under any interpreter (the reason PR
    #1214 and PR #1280 record names rather than numbers)."""
    tool, _, entries, _ = _state()
    claim_entries, _ = tool.load_claim_ledger(tool.LEDGER)
    assert tool.LEDGER.read_text(encoding="utf-8") == tool.ledger_text(
        entries, claim_entries)
    doc = json.loads(tool.LEDGER.read_text(encoding="utf-8"))
    for entry in doc["classes"]:
        assert entry["sites"] == sorted(entry["sites"])
        assert entry["tokens"] == sorted(entry["tokens"])
        for site in entry["sites"]:
            assert "::" in site and ":" not in site.split("::")[1]
    for entry in doc["claims"]:
        assert entry["only_here"] == sorted(entry["only_here"])
        assert entry["only_there"] == sorted(entry["only_there"])
        for site in (entry["site"], entry["mirrors"]):
            assert "::" in site and ":" not in site.split("::")[1]


# ------------------------------------------------------- issue #1336: claims

def test_the_claim_rule_is_green_and_its_scan_is_not_vacuous():
    tool, sites, claims, entries, err = _claim_state()
    assert err is None, err
    # Vacuity first, in both directions: a green verdict over an empty claim
    # scan, or over an empty prose walk, means nothing.
    assert len(claims) > 10, f"only {len(claims)} claims; the prose walk is broken"
    assert tool.check_claims(claims, tool.near_misses(sites, claims),
                             entries, err) == []


def test_every_recorded_near_miss_carries_a_written_reason():
    _, _, _, entries, _ = _claim_state()
    for entry in entries:
        assert (entry.get("note") or "").strip(), (entry["site"], entry["mirrors"])


def test_issue_1336s_own_copy_is_imported_rather_than_restated():
    """The third copy of item 536's verdict fields. It is fixed by INHERITING
    the four, so `as_dict` names one key of its own and is no longer a
    vocabulary site at all. Asserting the inheritance and not just the absence
    is what keeps this a check: a copy re-grown here puts the site back."""
    tool, sites, _, _, _ = _claim_state()
    found = {s.sid for s in sites}
    assert "tools/evolution_controller.py::Verdict.as_dict" not in found
    source = ROOT.joinpath("tools", "evolution_controller.py").read_text(
        encoding="utf-8")
    assert "from evolution_reward import Verdict as ComponentVerdict" in source
    assert "class Verdict(ComponentVerdict)" in source


def test_a_claim_that_drifts_by_one_token_reds_where_exact_equality_is_silent():
    """Issue #1336's shape, run against the real tree: the recorded near miss
    gains a token on the side that claims, and the class rule stays green.

    This is the whole argument for a second rule. `Session._live_fingerprint`
    says it produces the shape `apply.fingerprint` does; move the one token
    that differs and the claim is about a different difference than the one the
    ledger has a reason for."""
    tool, sites, claims, entries, err = _claim_state()
    site = "src/revl/mcp/session.py::Session._live_fingerprint"
    drifted = [
        c if c.site != site
        else tool.Claim(c.site, c.tokens | {"schemaVersion"}, c.cue, c.cited)
        for c in claims
    ]
    problems = tool.check_claims(drifted, tool.near_misses(sites, drifted),
                                 entries, err)
    assert len(problems) == 1, problems
    assert "NO LONGER OBSERVED" in problems[0]
    # The class rule saw nothing: the vocabularies were never equal.
    class_entries, class_err = tool.load_ledger(tool.LEDGER)
    assert tool.check(sites, class_entries, class_err) == []


def test_an_unrecorded_near_miss_reds():
    tool, sites, claims, entries, err = _claim_state()
    target = next(s for s in sites if s.sid == "src/revl/apply.py::fingerprint")
    invented = tool.Claim("src/revl/distill.py::_FINGERPRINT",
                          target.tokens | {"generation"}, "mirror",
                          ("src/revl/apply.py",))
    problems = tool.check_claims(claims + [invented],
                                 tool.near_misses(sites, claims + [invented]),
                                 entries, err)
    assert len(problems) == 1, problems
    assert "UNRECORDED NEAR MISS" in problems[0]
    assert "generation" in problems[0]


def test_a_claim_with_no_resolvable_citation_is_not_a_finding():
    """The rule's own containment. A cue is common prose; a cue plus a citation
    the scan can resolve is a checkable claim. Measured on this tree: 100 sites
    carry a cue and 28 resolve a module, and firing on the other 72 is PR
    #1295's blob by another route."""
    tool, sites, _, _, _ = _claim_state()
    modules = {s.module for s in sites}
    by_stem = {}
    for module in modules:
        by_stem.setdefault(module.rsplit("/", 1)[-1][:-3], []).append(module)
    by_stem = {k: tuple(sorted(v)) for k, v in by_stem.items()}
    text = "A hand-kept mirror of the shape the reducer downstream reads."
    assert tool._cue_in(text)
    assert tool._cited_modules(text, "src/revl/a.py", modules, by_stem) == ()
    # And an ambiguous basename does not resolve either: a guess about which of
    # two modules was meant is not a finding this gate may report.
    ambiguous = {"src/revl/a.py", "tools/a.py"}
    assert tool._cited_modules("mirrors `a.SOME_SET`", "src/revl/b.py",
                               ambiguous, {"a": ("src/revl/a.py", "tools/a.py")}) == ()


def test_a_missing_claims_list_reds_rather_than_reading_as_nothing_to_check():
    tool, sites, claims, _, _ = _claim_state()
    entries, err = tool.load_claim_ledger(
        ROOT / "tests" / "fixtures" / "no-such.json")
    assert entries is None
    problems = tool.check_claims(claims, tool.near_misses(sites, claims),
                                 entries, err)
    assert len(problems) == 1 and "RED" in problems[0]


def test_a_vacuous_claim_scan_reds_rather_than_reading_as_a_clean_tree():
    tool, sites, _, entries, err = _claim_state()
    assert "VACUOUS CLAIM SCAN" in tool.check_claims([], [], entries, err)[0]


def test_the_self_test_passes():
    assert _tool().self_test() == 0


def test_the_gate_is_stdlib_only_and_starts_no_subprocess():
    """CI's `lint` job installs no revl at all, and a tool that shells out can
    be handed a different checkout by the dev venv's editable meta-path finder,
    which outranks both `sys.path` and `PYTHONPATH`. This one parses source
    text and imports nothing outside the standard library, so neither applies."""
    import ast as _ast

    source = (ROOT / "tools" / "check_vocabulary_mirrors.py").read_text(encoding="utf-8")
    imported = set()
    for node in _ast.walk(_ast.parse(source)):
        if isinstance(node, _ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, _ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"argparse", "ast", "dataclasses", "json", "pathlib", "sys",
                        "__future__"}, imported
    assert "subprocess" not in source and "os.system" not in source
