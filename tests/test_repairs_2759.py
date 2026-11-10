"""Issue #2759: an admission refusal carries verified candidate repairs.

A capability or admission refusal used to carry one prose `fix` and the author
had to work out the rewrite. A covered record now carries a `repairs` list next
to `fix`: each entry names a KIND, an edit as LSP `TextEdit`s, and whether the
candidate widens the source's declared authority. The fixgen engine applies
every candidate to the refused source and re-checks it, so the list is the
verified ways out, not a menu of guesses.

Three refusal kinds are covered, each proven end to end here by applying every
returned candidate and compiling the patch clean:

  * G1 `requirement` - a body reaches a key its component does not declare;
  * G2 `linking` - two components install the same provision key;
  * R2 `admission` - a `requires` binds a service outside the granted set.

The widening rule is the safety half: a candidate that enlarges the declared
authority is absent from the default list, present only when the caller opts
in, and never ordered first. A candidate that would trade the refusal for a
different one is dropped rather than returned.
"""

from __future__ import annotations

import pytest

from revl import AdmissionProfile, compile_source
from revl.diagnostics import classify, explain, report
from revl.errors import RevlError
from revl.lsp import fixgen
from revl.lsp.fixgen import apply_edits, generate_repairs

_SERVICES = "service Clock { fn now() -> Int }\nservice UserSvc { fn go() -> Int }\n"

# G1: the program declares the provider of `clock`. Declaring the dependency
# still adds a `requires` row, so the candidate widens the declared authority
# even though the reach is internal.
G1_INTERNAL = _SERVICES + (
    "component Provider provides clock: Clock {\n"
    "  provide clock { fn now() = 1 }\n"
    "}\n"
    "component User provides user: UserSvc {\n"
    "  provide user { fn go() = clock.now() }\n"
    "}\n"
)

# G1: nothing in the program provides `clock`, so the same declaration reaches
# an ambient service and is the widening candidate.
G1_AMBIENT = _SERVICES + (
    "component User provides user: UserSvc {\n"
    "  provide user { fn go() = clock.now() }\n"
    "}\n"
)

# G1: a `let` statement whose call can be removed, but the same program does
# not declare `now` on the service, so naming the requirement would introduce
# a new refusal and must be dropped.
G1_WHOLE_STATEMENT = (
    "service Clock { fn now() -> Int }\n"
    "service UserSvc { fn go() -> Int }\n"
    "component User provides user: UserSvc {\n"
    "  provide user {\n"
    "    fn go() -> Int {\n"
    "      let t = clock.now()\n"
    "      return 1\n"
    "    }\n"
    "  }\n"
    "}\n"
)
G1_WRONG_INTERFACE = (
    "service Clock { fn tick() -> Int }\n"
    "service UserSvc { fn go() -> Int }\n"
    "component User provides user: UserSvc {\n"
    "  provide user {\n"
    "    fn go() -> Int {\n"
    "      let t = clock.now()\n"
    "      return 1\n"
    "    }\n"
    "  }\n"
    "}\n"
)
# G1: the refused binding IS used afterwards, so removing its line leaves an
# undeclared name and declaring the requirement names a service without the
# method; no candidate survives verification.
G1_BINDING_USED = (
    "service Clock { fn tick() -> Int }\n"
    "service UserSvc { fn go() -> Int }\n"
    "component User provides user: UserSvc {\n"
    "  provide user {\n"
    "    fn go() -> Int {\n"
    "      let t = clock.now()\n"
    "      return t\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# G1: the component already declares a `requires` clause, so the candidate has
# to extend it rather than introduce one.
G1_WITH_REQUIRES = (
    "service Clock { fn now() -> Int }\n"
    "service A { fn a() -> Int }\n"
    "service B { fn b() -> Int }\n"
    "service UserSvc { fn go() -> Int }\n"
    "component User requires x: A, y: B provides user: UserSvc {\n"
    "  provide user { fn go() = clock.now() }\n"
    "}\n"
)

# G1 `binding`: a fn body reads an undeclared name. The code is covered but
# this failure mode has no candidate, so the record must keep no list at all.
G1_BINDING = "pub fn f() -> Int { return counter }\n"

# G2: two providers of the same key in the same realm.
G2_CONFLICT = _SERVICES + (
    "service Store { fn get(k: Str) -> Str }\n"
    "component A provides s: Store {\n"
    '  provide s { fn get(k) = "a" }\n'
    "}\n"
    "component B provides s: Store {\n"
    '  provide s { fn get(k) = "b" }\n'
    "}\n"
)

# G2: the same key provided twice inside one named realm, so the rename has to
# carry the provision's `isolate` line with it.
G2_REALM_CONFLICT = _SERVICES + (
    "service Store { fn get(k: Str) -> Str }\n"
    "component A provides s: Store {\n"
    '  isolate s in realm("r")\n'
    '  provide s { fn get(k) = "a" }\n'
    "}\n"
    "component B provides s: Store {\n"
    '  isolate s in realm("r")\n'
    '  provide s { fn get(k) = "b" }\n'
    "}\n"
)

# R2: the running composition provides both `kv` and `fs`; the turn is
# granted only `kv` and reaches `fs`.
_R2_BASE = (
    "service Kv { fn get(k: Str) -> Str }\n"
    "service FsSvc { fn get(p: Str) -> Str }\n"
    "component KvProvider provides kv: Kv { provide kv { fn get(k) = \"v\" } }\n"
    "component FsProvider provides fs: FsSvc "
    "{ provide fs { fn get(p) = \"data\" } }\n"
)
_R2_TURN = (
    "service Turn { fn run() -> Str }\n"
    "component TurnComp requires fs: FsSvc provides turn: Turn {\n"
    '  provide turn { fn run() = fs.get("x") }\n'
    "}\n"
)


def _lsp(record: dict) -> dict:
    line = max(record["line"] - 1, 0)
    return {"code": record["code"],
            "range": {"start": {"line": line, "character": 0},
                      "end": {"line": line, "character": 0}}}


def _verifier_for(source: str, **compile_kwargs):
    """The report contract's verifier: re-check the patch through the same
    compiler door the refusal came from, with the same manifest and profile."""
    def verify(patched: str, error: RevlError):
        try:
            compile_source(patched, error.filename, **compile_kwargs)
        except RevlError as refusal:
            errors = getattr(refusal, "errors", None) or [refusal]
            return [_lsp(classify(one)) for one in errors]
        return []
    return verify


def _record(source: str, *, widens_authority: bool = False,
            **compile_kwargs) -> dict:
    try:
        compile_source(source, "<candidate>.rvl", **compile_kwargs)
    except RevlError as error:
        payload = report(error, sources={"<candidate>.rvl": source},
                         verifier=_verifier_for(source, **compile_kwargs),
                         widens_authority=widens_authority)
        return payload["diagnostics"][0]
    raise AssertionError("expected a refusal")


def _assert_clears(source: str, repair: dict, **compile_kwargs) -> None:
    patched = apply_edits(source, repair["fix_code"]["edits"])
    compile_source(patched, "<patched>.rvl", **compile_kwargs)


# --------------------------------------------------------------- one per kind

def test_g1_declares_the_missing_requirement_and_the_patch_clears_it():
    record = _record(G1_INTERNAL, widens_authority=True)
    kinds = [repair["kind"] for repair in record["repairs"]]
    assert "add-requires" in kinds
    add = next(r for r in record["repairs"] if r["kind"] == "add-requires")
    assert add["widens_authority"] is True      # a new `requires` row
    assert add["fix_code"]["edits"][0]["newText"] == " requires clock: Clock"
    _assert_clears(G1_INTERNAL, add)


def test_g2_renames_the_conflicting_provision_and_the_patch_clears_it():
    record = _record(G2_CONFLICT)
    assert [repair["kind"] for repair in record["repairs"]] == ["rename-key"]
    rename = record["repairs"][0]
    assert rename["widens_authority"] is False
    assert any(edit["newText"] == "s_v2: Store"
                  for edit in rename["fix_code"]["edits"])
    _assert_clears(G2_CONFLICT, rename)


def test_r2_routes_the_ungranted_key_to_a_granted_service_and_the_patch_clears_it():
    base_ir = compile_source(_R2_BASE, "base.rvl")
    profile = AdmissionProfile.untrusted_author({"Kv"})
    record = _record(_R2_TURN, manifest=base_ir, profile=profile)
    assert [repair["kind"] for repair in record["repairs"]] == [
        "route-via-granted-service"]
    route = record["repairs"][0]
    assert route["widens_authority"] is False
    assert route["fix_code"]["edits"][0]["newText"] == "fs: Kv"
    _assert_clears(_R2_TURN, route, manifest=base_ir, profile=profile)


def test_add_requires_extends_an_existing_requires_clause():
    record = _record(G1_WITH_REQUIRES, widens_authority=True)
    add = next(r for r in record["repairs"] if r["kind"] == "add-requires")
    assert add["fix_code"]["edits"][0]["newText"] == ", clock: Clock"
    _assert_clears(G1_WITH_REQUIRES, add)


def test_rename_key_carries_the_provision_isolation_line():
    record = _record(G2_REALM_CONFLICT)
    assert [repair["kind"] for repair in record["repairs"]] == ["rename-key"]
    rewritten = [edit["newText"] for edit in record["repairs"][0]["fix_code"]["edits"]]
    assert "s_v2: Store" in rewritten
    assert rewritten.count("s_v2") == 2      # the provide block and the isolate
    _assert_clears(G2_REALM_CONFLICT, record["repairs"][0])


def test_a_covered_code_in_an_uncovered_mode_keeps_no_repairs_list():
    # G1's `binding` mode is not the undeclared-requirement shape, so the engine
    # never claims it checked: the record is exactly the one it always was.
    record = _record(G1_BINDING)
    assert record["code"] == "G1"
    assert "repairs" not in record


# ----------------------------------------------------------------- the rules

def test_a_widening_candidate_is_absent_by_default_and_present_on_opt_in():
    default = _record(G1_AMBIENT)
    assert [r["kind"] for r in default["repairs"]] == []
    opted = _record(G1_AMBIENT, widens_authority=True)
    assert [r["kind"] for r in opted["repairs"]] == ["add-requires"]
    assert opted["repairs"][0]["widens_authority"] is True
    _assert_clears(G1_AMBIENT, opted["repairs"][0])


def test_add_requires_widens_even_when_a_sibling_provides_the_key():
    # a sibling component provides `clock`, but the new `requires` row still
    # enlarges what User declares: the default list must not carry it
    default = _record(G1_INTERNAL)
    assert [r["kind"] for r in default["repairs"]] == []
    seeds = fixgen._g1_requirement_repairs(
        G1_INTERNAL, {"code": "G1", "message":
                      "`clock` is not a declared requirement of User",
                      "range": {"start": {"line": 6, "character": 0},
                                "end": {"line": 6, "character": 0}}},
        "<candidate>.rvl")
    adds = [seed for seed in seeds if seed.kind == "add-requires"]
    assert adds and all(seed.widens_authority for seed in adds)


def test_a_narrowing_candidate_is_ordered_ahead_of_a_widening_one():
    opted = _record(G1_WHOLE_STATEMENT, widens_authority=True)
    kinds = [repair["kind"] for repair in opted["repairs"]]
    assert kinds == ["remove-call", "add-requires"]
    assert opted["repairs"][0]["widens_authority"] is False
    for repair in opted["repairs"]:
        _assert_clears(G1_WHOLE_STATEMENT, repair)


def test_a_candidate_that_would_introduce_a_new_refusal_is_dropped():
    # The generator can name the requirement, but the service does not declare
    # the called method: applying that seed trades G1 for an A6 interface
    # refusal on the same line, so the verified list must not carry it.
    diagnostic = {
        "code": "G1",
        "message": "`clock` is not a declared requirement of User",
        "range": {"start": {"line": 5, "character": 0},
                  "end": {"line": 5, "character": 5}},
    }
    seeds = fixgen._g1_requirement_repairs(
        G1_WRONG_INTERFACE, diagnostic, "<candidate>.rvl")
    add = next(seed for seed in seeds if seed.kind == "add-requires")
    with pytest.raises(RevlError) as refused:
        compile_source(apply_edits(G1_WRONG_INTERFACE, add.edits),
                       "<patched>.rvl")
    assert classify(refused.value)["code"] == "A6"

    verified = generate_repairs(G1_WRONG_INTERFACE, diagnostic,
                                "<candidate>.rvl")
    assert [repair.kind for repair in verified] == ["remove-call"]


def test_a_candidate_that_leaves_the_refusal_is_dropped():
    # Deleting the `let` line would leave `t` undeclared where it is returned,
    # and declaring `clock: Clock` names the wrong interface, so no candidate
    # survives verification and the record carries no repairs at all.
    record = _record(G1_BINDING_USED, widens_authority=True)
    assert record["repairs"] == []


def test_a_repair_entry_carries_the_kind_action_and_verified_edit():
    record = _record(G1_INTERNAL, widens_authority=True)
    entry = record["repairs"][0]
    assert set(entry) == {"kind", "action", "widens_authority", "fix_code"}
    assert entry["action"] == entry["fix_code"]["title"]
    assert entry["fix_code"]["edits"]


def test_revl_check_carries_the_verified_repairs_to_the_agent_door():
    from revl.mcp.server import handle

    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_check",
                                  "arguments": {"source": G1_WHOLE_STATEMENT}}})
    record = response["result"]["structuredContent"]["diagnostics"][0]
    assert record["code"] == "G1"
    # the agent door asks for no widening, so `add-requires` is not offered
    assert [repair["kind"] for repair in record["repairs"]] == ["remove-call"]
    _assert_clears(G1_WHOLE_STATEMENT, record["repairs"][0])


def _g1_components(count: int) -> str:
    """`count` components, each reaching the undeclared `clock` from a
    whole-statement `let`, so each yields one G1 record with two candidates."""
    source = "service Clock { fn now() -> Int }\n"
    for i in range(count):
        source += (
            f"service S{i} {{ fn go() -> Int }}\n"
            f"component C{i} provides s{i}: S{i} {{\n"
            f"  provide s{i} {{\n"
            "    fn go() -> Int {\n"
            "      let t = clock.now()\n"
            "      return 1\n"
            "    }\n"
            "  }\n"
            "}\n")
    return source


def _report_counting(source: str, **kwargs) -> tuple[dict, list[str]]:
    compiles: list[str] = []
    check = _verifier_for(source)

    def verify(patched: str, error: RevlError):
        compiles.append(patched)
        return check(patched, error)

    try:
        compile_source(source, "<candidate>.rvl")
    except RevlError as error:
        return (report(error, sources={"<candidate>.rvl": source},
                       verifier=verify, **kwargs), compiles)
    raise AssertionError("expected a refusal")


def test_one_report_spends_one_compile_budget_across_its_records():
    source = _g1_components(12)
    payload, compiles = _report_counting(source, widens_authority=True)
    records = payload["diagnostics"]
    assert [record["code"] for record in records] == ["G1"] * 12
    # two candidates per record would be 24 compiles without a shared budget
    assert len(compiles) == 8
    from revl.diagnostics import REPAIR_COMPILE_BUDGET
    assert REPAIR_COMPILE_BUDGET == 8
    unchecked = [record for record in records if "repairs" not in record]
    assert len(unchecked) == 12 - REPAIR_COMPILE_BUDGET // 2


def test_a_line_removal_is_kept_when_later_refusals_shift_up():
    # removing C0's `let` line moves C1's and C2's G1 records up one line; they
    # are the same pre-existing refusals, not new ones
    source = _g1_components(3)
    payload, _compiles = _report_counting(source)
    for record in payload["diagnostics"]:
        assert [r["kind"] for r in record["repairs"]] == ["remove-call"]
        patched = apply_edits(source, record["repairs"][0]["fix_code"]["edits"])
        with pytest.raises(RevlError) as refused:
            compile_source(patched, "<patched>.rvl")
        assert len(refused.value.errors) == 2


def _component(i: int, body: str) -> str:
    return (f"service S{i} {{ fn go() -> Int }}\n"
            f"component C{i} provides s{i}: S{i} {{\n"
            f"  provide s{i} {{\n    fn go() -> Int {{\n{body}    }}\n  }}\n}}\n")


def test_a_record_is_checked_whole_or_not_at_all():
    # C0 has one candidate, C1 to C5 two each: 1 + 2 + 2 + 2 = 7 fit the
    # budget of 8, C4 needs 2 with 1 left, so C4 and C5 are unchecked rather
    # than C4 getting a list cut short to its first candidate
    source = "service Clock { fn now() -> Int }\n" + _component(
        0, "      return clock.now()\n")
    for i in range(1, 6):
        source += _component(i, "      let t = clock.now()\n      return 1\n")
    payload, compiles = _report_counting(source, widens_authority=True)
    records = payload["diagnostics"]
    assert len(records) == 6
    lists = [[(r["kind"], r["widens_authority"]) for r in record["repairs"]]
             if "repairs" in record else None for record in records]
    both = [("remove-call", False), ("add-requires", True)]
    assert lists == [[("add-requires", True)], both, both, both, None, None]
    assert len(compiles) == 7
    # the narrowing candidate is checked before the widening one
    first = apply_edits(source, records[1]["repairs"][0]["fix_code"]["edits"])
    assert compiles[1] == first


def test_the_g4_site_undo_check_draws_from_the_same_budget():
    # the compiler reports one G4 per compile, but `report` takes any
    # collected set; each G4 record costs one compile, inside the budget
    from revl.errors import RevlErrors

    source = (
        "\nservice S { fn go(u: Str) -> Int }\n\n"
        "component Leaky provides s: S {\n"
        "  config { url: Str }\n"
        '  let pool = effect Pool.open("u", 1)\n'
        "  provide s { fn go(u) = 1 }\n"
        "}\n")
    try:
        compile_source(source, "<candidate>.rvl")
    except RevlError as refusal:
        one = (getattr(refusal, "errors", None) or [refusal])[0]
    else:                                    # pragma: no cover
        raise AssertionError("expected a refusal")
    compiles: list[str] = []
    check = _verifier_for(source)

    def verify(patched: str, error: RevlError):
        compiles.append(patched)
        return check(patched, error)

    payload = report(RevlErrors([one] * 10),
                     sources={"<candidate>.rvl": source}, verifier=verify)
    records = payload["diagnostics"]
    assert [record["code"] for record in records] == ["G4"] * 10
    assert len(compiles) == 8
    verified = ["fix_code" in record["repairs"][0] for record in records]
    assert verified == [True] * 8 + [False] * 2
    # an unchecked G4 record keeps its prose repairs, as #2684 pins
    assert all(record["repairs"][0]["kind"] == "site-undo" for record in records)


def test_a_known_record_on_a_deleted_line_does_not_excuse_a_new_one():
    # removing line 5 moves line 6 up onto 5: a C record there now is new,
    # not the known C record that sat on the deleted line
    def diag(code: str, line: int) -> dict:
        return {"code": code, "range": {"start": {"line": line, "character": 0}}}

    remove = {"range": {"start": {"line": 5, "character": 0},
                        "end": {"line": 6, "character": 0}}, "newText": ""}
    target = ("G1", 5)
    assert fixgen._repair_clears(target, {target, ("C", 5)},
                                 [diag("C", 5)], [remove]) is False
    assert fixgen._repair_clears(target, {target, ("C", 7)},
                                 [diag("C", 6)], [remove]) is True


# ------------------------------------------------------------ explain, records

def test_explain_shows_the_candidate_kinds_for_the_covered_codes():
    assert explain("G1")["repairs"] == ["add-requires", "remove-call"]
    assert explain("G2")["repairs"] == ["rename-key"]
    # R2 is in the evolution curriculum's reserved set: explain keeps refusing
    # it on purpose, and its record carries the same repairs list.
    assert explain("R2")["ok"] is False


def test_a_refusal_with_no_line_does_not_crash_the_engine():
    # an ambient manifest entry has no declaration line, so a conflict it
    # participates in can name a record whose `line` is None: the engine must
    # not subtract from it
    from revl.errors import RevlError as Refusal

    error = Refusal(
        "<candidate>.rvl", None,
        "provision conflict: key `s` is provided by both A and B (G2)")
    payload = report(error, sources={"<candidate>.rvl": G2_CONFLICT},
                     verifier=lambda patched, one: [])
    record = payload["diagnostics"][0]
    assert record["line"] is None
    assert [repair["kind"] for repair in record["repairs"]] == ["rename-key"]


def test_a_record_without_source_or_verifier_keeps_the_prose_fix_only():
    # the caller that holds neither the compiled text nor a checker gets no
    # candidate list rather than unverified edits
    try:
        compile_source(G1_INTERNAL, "<candidate>.rvl")
    except RevlError as error:
        record = report(error)["diagnostics"][0]
    else:                                    # pragma: no cover
        raise AssertionError("expected a refusal")
    assert "repairs" not in record
    assert record["fix"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
