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
    assert tool.LEDGER.read_text(encoding="utf-8") == tool.ledger_text(entries)
    doc = json.loads(tool.LEDGER.read_text(encoding="utf-8"))
    for entry in doc["classes"]:
        assert entry["sites"] == sorted(entry["sites"])
        assert entry["tokens"] == sorted(entry["tokens"])
        for site in entry["sites"]:
            assert "::" in site and ":" not in site.split("::")[1]


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
