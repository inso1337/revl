"""Issue #3182: `revl audit --diff` gates on every drift bucket, and `revl diff`
reports the drift a stable crossing can carry.

The gate used to read only added crossings, reach weakenings and inverse
weakenings, so a new host backend body or a scope widened on a stable crossing
printed "authority-drift: clean" and exited 0, and `revl diff` printed
"no structural change". The gated set now lives in
`audit_diff.widening_from_audits`, shared with the MCP update path.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import (  # noqa: E402
    GATED_BUCKETS, audit_report, diff_recovery, evaluate, widening_from_audits)
from revl.composition_diff import diff  # noqa: E402
from revl.mcp import update_tools  # noqa: E402

_ENGINE = """\
extern emission{reach} fn engine_run(argv_json: Str, cwd: Str, timeout_s: Int) -> Str = @py {{
    return ""
}}{ts}
service Ops {{ emission fn go(u: Str) -> Str }}
component Runner provides ops: Ops {{
  provide ops {{
    fn go(u) {{ return emit engine_run(u, ".", 5) }}
  }}
}}
"""
_TS = ' = @ts {\n    return "";\n}'
ENGINE_PY = _ENGINE.format(reach="(confined: cwd)", ts="")
ENGINE_PY_TS = _ENGINE.format(reach="(confined: cwd)", ts=_TS)
ENGINE_BARE = _ENGINE.format(reach="", ts="")

_CACHE = """\
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(`INSERT ${key} ${value}`) }
  }
}
component Front requires cache: Cache {
  emit cache.put("k", "v")
}
"""
CACHE_SCOPED = _CACHE
CACHE_WIDE = _CACHE.replace("emission[db] fn put", "emission fn put")


def _pair(tmp_path: Path, before: str, after: str) -> tuple[Path, Path, Path]:
    prev_src = tmp_path / "before.rvl"
    prev_src.write_text(before)
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(audit_report(compile_source(before))))
    after_src = tmp_path / "after.rvl"
    after_src.write_text(after)
    return prev_src, prev, after_src


# ------------------------------------------------------- revl audit --diff

def test_audit_diff_fails_on_a_new_backend_body(tmp_path, capsys):
    _, prev, after = _pair(tmp_path, ENGINE_PY, ENGINE_PY_TS)
    assert main(["audit", str(after), "--diff", str(prev)]) == 1
    out = capsys.readouterr().out
    assert "backend:engine_run:ts" in out
    assert "clean" not in out


def test_audit_diff_fails_on_a_widened_scope(tmp_path, capsys):
    _, prev, after = _pair(tmp_path, CACHE_SCOPED, CACHE_WIDE)
    assert main(["audit", str(after), "--diff", str(prev)]) == 1
    out = capsys.readouterr().out
    assert "scope:Front:cache.put" in out
    assert "clean" not in out


def test_a_new_backend_is_acknowledged_with_its_token(tmp_path, capsys):
    _, prev, after = _pair(tmp_path, ENGINE_PY, ENGINE_PY_TS)
    assert main(["audit", str(after), "--diff", str(prev)]) == 1
    assert main(["audit", str(after), "--diff", str(prev),
                 "--accept", "backend:engine_run:ts"]) == 0


def test_a_widened_scope_is_acknowledged_with_its_token(tmp_path, capsys):
    _, prev, after = _pair(tmp_path, CACHE_SCOPED, CACHE_WIDE)
    assert main(["audit", str(after), "--diff", str(prev)]) == 1
    assert main(["audit", str(after), "--diff", str(prev),
                 "--accept", "scope:Front:cache.put"]) == 0


def test_audit_diff_json_carries_the_new_buckets(tmp_path, capsys):
    _, prev, after = _pair(tmp_path, ENGINE_PY, ENGINE_PY_TS)
    assert main(["audit", str(after), "--diff", str(prev), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["backends_added"] == ["backend:engine_run:ts"]
    assert report["unacknowledged"] == ["backend:engine_run:ts"]
    assert report["widened"] is True


def test_a_dropped_backend_and_a_narrowed_scope_pass(tmp_path, capsys):
    _, prev, after = _pair(tmp_path, ENGINE_PY_TS, ENGINE_PY)
    assert main(["audit", str(after), "--diff", str(prev)]) == 0
    assert "backend:engine_run:ts" in capsys.readouterr().out
    _, prev, after = _pair(tmp_path, CACHE_WIDE, CACHE_SCOPED)
    assert main(["audit", str(after), "--diff", str(prev)]) == 0


# ------------------------------------------- one gated set, two consumers

def _synthetic(**surface) -> dict:
    return {"boundary": {}, "externs": [], **surface}


_WIDENINGS = {
    "scopesWidened": (
        _synthetic(boundary={"C": {"capabilities": {"n": ["send.mail"]}}}),
        _synthetic(boundary={"C": {"capabilities": {"n": ["send.*"]}}})),
    "backendsAdded": (
        _synthetic(externs=[{"name": "x", "backends": ["py"]}]),
        _synthetic(externs=[{"name": "x", "backends": ["py", "ts"]}])),
    "recoveryDropped": (
        _synthetic(recovery_surface=[{"name": "s", "kind": "effect",
                                      "register": "keyed"}]),
        _synthetic()),
    "registersWeakened": (
        _synthetic(capability_registers={"net": "keyed"}),
        _synthetic(capability_registers={"net": None})),
    "cardinalityWidened": (
        _synthetic(cardinality={"C": {"per_capability": {
            "send.mail": {"kind": "bounded", "bound": 2}}}}),
        _synthetic(cardinality={"C": {"per_capability": {
            "send.mail": {"kind": "bounded", "bound": 5}}}})),
}


def test_evaluate_gates_every_bucket_the_update_path_does():
    for key, (before, after) in _WIDENINGS.items():
        widening = widening_from_audits(before, after)
        assert widening[key], key
        result = evaluate(before, after)
        assert result["widened"] is True, key
        assert result["unacknowledged"] == [
            t for k, _ in GATED_BUCKETS for t in widening[k]], key


def test_the_update_path_reads_the_shared_gated_set():
    for before, after in _WIDENINGS.values():
        assert update_tools._widening_from_audits(before, after) == \
            widening_from_audits(before, after)


# ------------------------------------------------------------- revl diff

def test_revl_diff_reports_a_new_backend_body(tmp_path, capsys):
    before, _, after = _pair(tmp_path, ENGINE_PY, ENGINE_PY_TS)
    assert main(["diff", str(before), str(after)]) == 0
    out = capsys.readouterr().out
    assert "no structural change" not in out
    assert "`engine_run` gained a `ts` backend body" in out
    assert "~ Runner  (changed)" in out


def test_revl_diff_reports_a_widened_scope(tmp_path, capsys):
    before, _, after = _pair(tmp_path, CACHE_SCOPED, CACHE_WIDE)
    assert main(["diff", str(before), str(after)]) == 0
    out = capsys.readouterr().out
    assert "no structural change" not in out
    assert "component `Front` widened emission `cache.put` scope" in out


def test_revl_diff_reports_a_weakened_reach():
    delta = diff(compile_source(ENGINE_PY), compile_source(ENGINE_BARE))
    assert delta["changed"] is True
    assert delta["crossings"]["reach_weakened"] == ["reach-weakened:engine_run"]
    assert "host extern `engine_run` reach weakened (its bound was loosened " \
        "or moved)" in delta["guarantees"]
    assert delta["components"]["changed"] == ["Runner"]


def test_revl_diff_reports_the_safe_directions():
    delta = diff(compile_source(ENGINE_PY_TS), compile_source(ENGINE_PY))
    assert delta["crossings"]["backends_removed"] == ["backend:engine_run:ts"]
    delta = diff(compile_source(CACHE_WIDE), compile_source(CACHE_SCOPED))
    assert delta["crossings"]["scope_tightened"] == ["scope:Front:cache.put"]
    assert "component `Front` narrowed emission `cache.put` scope" in \
        delta["guarantees"]


# ------------------------------- a recovery kind change on a kept name

# The same crossing name with an exact inverse, then as a compensated emission:
# the recovery kind moves from `inverse` to `compensation` and the name stays.
_DECLARED = """\
type W = { path: Str }
type E = { msg: Str }
extern pure fn restore(w: W) -> Unit = @py { return None }
extern witnessed fn deposit(p: Str) -> Result[W, E] undo restore(result) = @py { return {"path": p} }
component C {
  effect deposit("t")
}
"""

_COMPENSATION = """\
extern pure fn rollback() -> Unit = @py { return None }
extern emission fn deposit(p: Str) -> Str compensate rollback() = @py { return p }
component C {
  emit deposit("t")
}
"""


def test_a_kind_change_on_a_kept_name_is_not_a_dropped_recovery():
    before = {"externs": [{"name": "a", "inverse": "compensation"}],
              "recovery_surface": [
                  {"name": "a", "kind": "compensation", "register": None}]}
    after = {"externs": [{"name": "a", "inverse": "declared"}],
             "recovery_surface": [
                 {"name": "a", "kind": "inverse", "register": None}]}
    assert diff_recovery(before, after)["recovery_dropped"] == []
    assert diff_recovery(after, before)["recovery_dropped"] == []


def test_a_recovery_removed_entirely_is_still_dropped():
    before = {"recovery_surface": [
        {"name": "a", "kind": "inverse", "register": None},
        {"name": "b", "kind": "compensation", "register": None}]}
    after = {"recovery_surface": [
        {"name": "b", "kind": "compensation", "register": None}]}
    assert diff_recovery(before, after)["recovery_dropped"] == [
        "recovery:a:inverse"]
    assert diff_recovery(before, {"recovery_surface": []})[
        "recovery_dropped"] == ["recovery:a:inverse", "recovery:b:compensation"]


def test_the_update_path_gives_the_gate_verdicts_on_a_kind_change():
    declared = audit_report(compile_source(_DECLARED, "corpus.rvl"))
    compensation = audit_report(compile_source(_COMPENSATION, "corpus.rvl"))
    weakened = update_tools._widening_from_audits(declared, compensation)
    assert weakened["widened"] is True
    assert weakened["inverseWeakened"] == ["inverse:deposit"]
    assert weakened["recoveryDropped"] == []
    assert evaluate(declared, compensation,
                    accepted={"inverse:deposit"})["widened"] is False
    strengthened = update_tools._widening_from_audits(compensation, declared)
    assert strengthened["widened"] is False
    assert evaluate(compensation, declared)["widened"] is False


def test_a_kind_change_against_a_baseline_without_inverse_is_still_gated():
    # A baseline captured before `inverse` existed: `diff_inverses` skips the
    # extern, so the recovery diff must still report the kind change.
    declared = audit_report(compile_source(_DECLARED, "corpus.rvl"))
    compensation = audit_report(compile_source(_COMPENSATION, "corpus.rvl"))
    for ext in declared["externs"]:
        ext.pop("inverse", None)
    result = evaluate(declared, compensation)
    assert result["inverse_weakened"] == []
    assert result["recovery_dropped"] == ["recovery:deposit:inverse"]
    assert result["widened"] is True
    assert update_tools._widening_from_audits(
        declared, compensation)["recoveryDropped"] == [
            "recovery:deposit:inverse"]


# An extern with both recovery kinds that loses one: the inverse class does not
# move (`declared` on both sides), so no `inverse:` token carries the change and
# the lost kind must stay a dropped recovery.
_BOTH = """\
type Sock = { fd: Int }
extern pure fn close_sock(s: Sock) -> Unit = @py { return None }
extern pure fn rollback() -> Unit = @py { return None }
extern acquire fn open_sock() -> Sock undo close_sock(result) compensate rollback() = @py { return {"fd": 1} }
component C {
  let s = effect open_sock() undo close_sock(s)
}
"""
_UNDO_ONLY = _BOTH.replace(" compensate rollback()", "", 1)


def test_losing_one_of_two_recovery_kinds_is_still_dropped():
    both = audit_report(compile_source(_BOTH, "corpus.rvl"))
    undo_only = audit_report(compile_source(_UNDO_ONLY, "corpus.rvl"))
    result = evaluate(both, undo_only)
    assert result["inverse_weakened"] == []
    assert result["recovery_dropped"] == ["recovery:open_sock:compensation"]
    assert result["widened"] is True
    assert result["unacknowledged"] == ["recovery:open_sock:compensation"]
    assert update_tools._widening_from_audits(
        both, undo_only)["recoveryDropped"] == [
            "recovery:open_sock:compensation"]


def _moved(kind: str, inverse: str, register: str | None) -> dict:
    ext = {"name": "a", "inverse": inverse}
    entry = {"name": "a", "kind": kind, "register": register}
    if register:
        ext["register"] = register
    return {"externs": [ext], "recovery_surface": [entry]}


def test_a_kind_change_that_loses_its_register_is_weakened():
    # compensation -> inverse strengthens the class, but the entry lost its
    # `keyed` register on the way: not a plain strengthening.
    before = _moved("compensation", "compensation", "keyed")
    after = _moved("inverse", "declared", None)
    recovery = diff_recovery(before, after)
    assert recovery["recovery_dropped"] == []
    assert recovery["recovery_weakened"] == ["recovery:a:inverse"]
    assert evaluate(before, after)["widened"] is True


def test_a_kind_change_that_keeps_or_gains_its_register_passes():
    for prev_reg, new_reg in (("keyed", "keyed"), (None, "keyed"),
                              ("declared", "keyed")):
        before = _moved("compensation", "compensation", prev_reg)
        after = _moved("inverse", "declared", new_reg)
        assert diff_recovery(before, after)["recovery_weakened"] == []
        assert evaluate(before, after)["widened"] is False


_PLAIN_EMISSION = _COMPENSATION.replace(" compensate rollback()", "", 1)


def test_a_moved_class_with_no_recovery_left_is_still_dropped():
    # compensation -> irreversible emits `inverse:deposit`, but the name has no
    # recovery entry left, so the lost kind is a drop, not a move. migrate and
    # the changelog read only the recovery diff.
    compensation = audit_report(compile_source(_COMPENSATION, "corpus.rvl"))
    plain = audit_report(compile_source(_PLAIN_EMISSION, "corpus.rvl"))
    result = evaluate(compensation, plain)
    assert result["inverse_weakened"] == ["inverse:deposit"]
    assert result["recovery_dropped"] == ["recovery:deposit:compensation"]


def test_a_weakened_move_is_not_also_a_gained_recovery():
    recovery = diff_recovery(_moved("compensation", "compensation", "keyed"),
                             _moved("inverse", "declared", None))
    assert recovery["recovery_weakened"] == ["recovery:a:inverse"]
    assert recovery["recovery_added"] == []


# migrate and the changelog read the recovery diff, not the gate: a kind move
# that weakens the inverse class must still reach both through `inverse:<name>`.

def test_migrate_reports_a_kind_move_that_weakens_the_inverse():
    from revl.migrate import authority  # noqa: PLC0415
    declared = compile_source(_DECLARED, "corpus.rvl")
    compensation = compile_source(_COMPENSATION, "corpus.rvl")
    assert authority(declared, compensation)["widened"] == ["inverse:deposit"]
    strengthened = authority(compensation, declared)
    assert strengthened["widened"] == []
    assert "inverse:deposit" in strengthened["narrowed"]


def test_the_changelog_breaks_on_a_kind_move_that_weakens_the_inverse():
    from revl.changelog import derive_changelog  # noqa: PLC0415
    declared = compile_source(_DECLARED, "corpus.rvl")
    compensation = compile_source(_COMPENSATION, "corpus.rvl")
    weakened = derive_changelog(declared, compensation, no_semver=True)
    ledes = [ln["fact"] for ln in weakened["breaking"] if ln["lede"]]
    assert "inverse.weakened:deposit" in ledes
    strengthened = derive_changelog(compensation, declared, no_semver=True)
    facts = [ln["fact"] for ln in strengthened["breaking"]]
    assert not [f for f in facts if f.startswith(("inverse.", "recovery."))]
    assert "inverse.strengthened:deposit" in [
        ln["fact"] for ln in strengthened["added"]]


def test_the_changelog_does_not_also_honesty_line_the_inverse_leaf():
    # `externs[*].inverse` has its own `inverse.*` line, so the completeness
    # guard must not list it again under `audit-path:externs`.
    from revl.changelog import derive_changelog  # noqa: PLC0415
    doc = derive_changelog(compile_source(_DECLARED, "corpus.rvl"),
                           compile_source(_COMPENSATION, "corpus.rvl"),
                           no_semver=True)
    externs = [ln["paths"] for ln in doc["unclassified"]
               if ln["fact"] == "audit-path:externs"]
    assert externs, "control: the extern class leaf is still honesty-lined"
    assert not [p for p in externs[0] if p.endswith(".inverse")]
