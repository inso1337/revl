"""Item 468 / issue #820: a policy diff over a real WAL.

The exit criterion is one sentence: a policy diff over a real WAL prints the
newly-allowed and newly-denied action sets and a bounded blast-radius summary.
Every test here attacks a way that could be faked.

* The sets must be COMPUTED from the recorded crossings, not from the rule text:
  `test_the_widening_the_run_never_took_is_not_in_the_diff` gives the new policy
  a capability the run never exercised and requires it to stay out.
* The blast radius must be BOUNDED BY THE RECORDED ACTION SET, so the bound has
  to move when that set moves and has to name the records the diff could not
  classify rather than swallowing them.
* The verdict must be the GATE's verdict. `test_the_diff_agrees_with_the_gate`
  runs `policy.evaluate` over the assembled boundary graph and requires the two
  to refuse exactly the same component/capability pairs, because a preview that
  can disagree with the admission it previews is worse than no preview.
* The undecided case must be reported, never resolved. A realm-scoped rule needs
  the component's realms, which only the compiled composition holds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import replay  # noqa: E402

from revl import policy_diff  # noqa: E402
from revl.policy import component_reach, evaluate, load_policy, parse_policy  # noqa: E402
from revl.wal import read_wal  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# builders: the py writer's own WAL, the same one `test_250_branch_slice2` uses
# ---------------------------------------------------------------------------


def _step(tl, kind, label, *, caps=None, scope=None, detail=None):
    step = replay.Step(len(tl.steps), kind, label, None, {"phase": "activation"},
                       detail=detail)
    step.scope = scope if scope is not None else ({"caps": list(caps)}
                                                  if caps else None)
    tl.steps.append(step)
    return step


def _wal(tmp_path, *components, name="run.wal"):
    """One WAL holding one timeline per component, written by the py in-process
    driver so `seq` is the step index. `components` is a list of
    `(name, [(kind, label, caps), ...])`."""
    path = str(tmp_path / name)
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    for component, steps in components:
        timeline = replay.Timeline(component)
        for kind, label, caps in steps:
            _step(timeline, getattr(replay, "KIND_" + kind.upper()), label,
                  caps=caps)
        wal.append_timeline(timeline)
    wal.commit_activation(components=[c for c, _ in components])
    wal.close()
    return path


def _policy(tmp_path, name, text):
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return load_policy(str(target))


def _write(tmp_path, name, text):
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return target


# `AgentSummarize` keeps to [llm, kv] and `AgentLeak` reaches past it, so the two
# policies below move exactly one recorded action of each kind.
WIDE = """component Agent* may reach llm, kv, net
"""
NARROW = """component Agent* may reach llm, kv
"""
DENY_NET = """component Agent* may reach llm, kv, net
component Agent* may not reach net
"""


# ===========================================================================
# the exit criterion
# ===========================================================================


def test_the_diff_prints_the_newly_allowed_and_newly_denied_action_sets(tmp_path):
    """A widening of the allow-list opens exactly the recorded action it names,
    and closing a deny rule does the same: both sets are computed over the
    recorded crossings, in both directions, over one real WAL."""
    wal = read_wal(_wal(tmp_path,
                        ("AgentSummarize", [("emission", "llm.ask", ["llm"]),
                                            ("emission", "kv.put", ["kv"])]),
                        ("AgentLeak", [("emission", "net.push", ["net"])])))

    widened = policy_diff.diff(_policy(tmp_path, "narrow.policy", NARROW),
                               _policy(tmp_path, "wide.policy", WIDE), wal)
    assert [m["token"] for m in widened["newlyAllowed"]] == ["net"]
    assert widened["newlyAllowed"][0]["component"] == "AgentLeak"
    assert widened["newlyAllowed"][0]["before"] == "deny"
    assert widened["newlyAllowed"][0]["after"] == "allow"
    assert widened["newlyDenied"] == []

    narrowed = policy_diff.diff(_policy(tmp_path, "wide2.policy", WIDE),
                                _policy(tmp_path, "denied.policy", DENY_NET), wal)
    assert [m["token"] for m in narrowed["newlyDenied"]] == ["net"]
    assert narrowed["newlyDenied"][0]["before"] == "allow"
    assert narrowed["newlyDenied"][0]["after"] == "deny"
    assert narrowed["newlyAllowed"] == []

    # the sets are printed, not merely computed
    text = policy_diff.render(widened)
    assert "NEWLY ALLOWED:" in text and "AgentLeak" in text and "net" in text
    assert "newly allowed" in text and "blast radius" in text


def test_the_widening_the_run_never_took_is_not_in_the_diff(tmp_path):
    """`revl audit --diff` answers whether the generation widened; this answers
    what that widening would have done to the actions the run took. A capability
    the new policy opens that the WAL never recorded is not an action of this
    run, so it cannot appear as newly allowed however widely the policy opens."""
    wal = read_wal(_wal(tmp_path, ("AgentSummarize",
                                   [("emission", "llm.ask", ["llm"])])))
    old = _policy(tmp_path, "a.policy", "component Agent* may reach llm\n")
    new = _policy(tmp_path, "b.policy",
                  "component Agent* may reach llm, net, db, mail\n")
    result = policy_diff.diff(old, new, wal)
    assert result["newlyAllowed"] == []
    assert result["newlyDenied"] == []
    assert result["blastRadius"]["recordedActions"] == 1
    assert "net" not in policy_diff.render(result)


# ===========================================================================
# the bound
# ===========================================================================


def test_the_blast_radius_is_bounded_by_the_recorded_action_set(tmp_path):
    """The bound is the WAL's own action set: it counts the distinct actions,
    the components, the effect records, and every recorded crossing the diff
    could not classify. It moves when the run moves, which is what makes it a
    measurement rather than a formula."""
    small = read_wal(_wal(tmp_path, ("C", [("emission", "llm.ask", ["llm"])]),
                          name="small.wal"))
    big = read_wal(_wal(tmp_path,
                        ("C", [("emission", "llm.ask", ["llm"]),
                               ("emission", "net.push", ["net"]),
                               ("emission", "kv.put", ["kv"])]),
                        ("D", [("emission", "net.push", ["net"])]),
                        name="big.wal"))
    old = _policy(tmp_path, "old.policy", "component * may reach llm\n")
    new = _policy(tmp_path, "new.policy", "component * may reach llm, net, kv\n")

    one = policy_diff.diff(old, new, small)
    two = policy_diff.diff(old, new, big)
    assert one["blastRadius"]["bound"] == 1
    assert one["blastRadius"]["recordedComponents"] == 1
    assert two["blastRadius"]["bound"] == 4
    assert two["blastRadius"]["recordedComponents"] == 2
    assert two["blastRadius"]["boundKind"] == "recorded actions in this WAL"
    assert two["blastRadius"]["gainedActions"] == 3
    # the witnesses are the recorded crossings the opened action was seen at
    assert sum(m["witnesses"][0]["seq"] is not None
               for m in two["newlyAllowed"]) == 3


def test_the_resource_scope_and_the_ceiling_come_from_the_recorded_token(tmp_path):
    """The diff decides the canonical spelling, which is the string the boundary
    graph carries and the string a `may reach` glob matches, so a token that
    binds a resource or a ceiling keeps its binding. The blast radius reads those
    bindings off the recorded token through the capability partial order
    (`split_ceilings`), and an action that binds nothing reports an empty set
    rather than a guess."""
    wal = read_wal(_wal(tmp_path,
                        ("C", [("emission", "kv.put", ["kv"]),
                               ("emission", "fs.write", ['fs(path="/var")']),
                               ("emission", "net.send", ["net(calls=3)"]),
                               ("emission", "mail.send", ["mail"])])))
    old = _policy(tmp_path, "old2.policy", "component * may reach kv\n")
    new = _policy(tmp_path, "new2.policy",
                  "component * may reach kv, fs*, net*, mail\n")
    result = policy_diff.diff(old, new, wal)
    assert [m["token"] for m in result["newlyAllowed"]] == [
        'fs(path="/var")', "mail", "net(calls=3)"]
    radius = result["blastRadius"]
    assert radius["gainedActions"] == 3
    assert radius["resourceScopes"] == {"C": ['fs(path="/var")']}
    assert radius["declaredCeilings"] == {"calls": [3]}
    # the binding is part of the token, so a bare glob does not open it: the
    # diff has to refuse what the gate refuses
    bare = policy_diff.diff(old, _policy(tmp_path, "bare.policy",
                                         "component * may reach kv, net\n"), wal)
    assert bare["newlyAllowed"] == []


def test_a_recorded_token_that_does_not_parse_keeps_its_spelling(tmp_path):
    """A token the partial order cannot re-read is kept as recorded and reported
    without parameters, because a diff over recorded history must not drop an
    action it merely failed to parse."""
    assert policy_diff._cap_token("net(calls=)") == ("net(calls=)", None, {})


def test_a_step_with_no_recorded_scope_is_not_an_action(tmp_path):
    """The WAL names a capability only where the scope declares one. A bare
    emission reads as no declared crossing and is never resolved to the label it
    recorded, which is the same reading the live classifier gives an absent
    scope, and a host-confined scope is not a crossing either. Both are counted
    on the blast radius, separately, because the WAL cannot tell them apart."""
    wal = read_wal(_wal(tmp_path,
                        ("C", [("emission", "llm.ask", ["llm"]),
                               ("emission", "mail.send", None),
                               ("effect", "fs.write", ["fs"])])))
    result = policy_diff.diff(
        _policy(tmp_path, "o.policy", "component * may reach llm\n"),
        _policy(tmp_path, "n.policy", "component * may reach llm, mail\n"), wal)
    assert result["newlyAllowed"] == []
    assert result["unscoped"] == [{"component": "C", "label": "mail.send",
                                   "seq": 1, "kind": "emission"}]
    radius = result["blastRadius"]
    assert radius["unscopedRecords"] == 1
    assert radius["confinedRecords"] == 1
    assert radius["recordedActions"] == 1
    assert "UNSCOPED" in policy_diff.render(result)


# ===========================================================================
# the case the WAL cannot answer
# ===========================================================================


def test_a_realm_rule_is_undecided_without_the_composition(tmp_path):
    """A realm-scoped rule decides by the realms a component joins and a WAL
    records none, so without the composition every action such a rule selects is
    undecided. Undecided is never allowed: the diff reports it and the CLI exits
    non-zero on it."""
    wal = read_wal(_wal(tmp_path, ("TenantAJob", [("emission", "bus.publish",
                                                   ["bus"])])))

    without = policy_diff.diff(
        _policy(tmp_path, "loose.policy", "realm tenantA may reach bus\n"),
        _policy(tmp_path, "tight.policy",
                "realm tenantA may not reach bus\n"), wal)
    assert without["newlyAllowed"] == [] and without["newlyDenied"] == []
    assert [m["token"] for m in without["undecided"]] == ["bus"]

    with_realms = policy_diff.diff(
        _policy(tmp_path, "loose2.policy", "realm tenantA may reach bus\n"),
        _policy(tmp_path, "tight2.policy", "realm tenantA may not reach bus\n"),
        wal, realms={"TenantAJob": ["tenantA"]})
    assert with_realms["undecided"] == []
    assert [m["token"] for m in with_realms["newlyDenied"]] == ["bus"]


def test_a_realm_rule_does_not_touch_a_component_it_does_not_select(tmp_path):
    """The undecided verdict is scoped to the component the realm rule selects.
    A component whose realms are known stays decidable under the same policy."""
    wal = read_wal(_wal(tmp_path, ("TenantAJob", [("emission", "bus.publish",
                                                    ["bus"])])))
    result = policy_diff.diff(
        _policy(tmp_path, "l3.policy", "realm tenantB may reach bus\n"),
        _policy(tmp_path, "t3.policy", "realm tenantB may not reach bus\n"),
        wal, realms={"TenantAJob": ["tenantA"]})
    assert result["undecided"] == []
    assert result["newlyAllowed"] == [] and result["newlyDenied"] == []


# ===========================================================================
# the one comparison site
# ===========================================================================


def test_the_diff_agrees_with_the_gate(tmp_path):
    """`policy.evaluate` is the admission decision and this diff is a preview of
    it, so over the same boundary graph the two must refuse exactly the same
    component/capability pairs. The WAL is built from the graph's own reach, so
    every pair the gate can see is an action the diff decides."""
    from revl.audit_diff import audit_report
    from revl.compiler import compile_files

    audit = audit_report(compile_files([str(FIXTURES / "policy_agents.rvl")]))
    pairs = []
    for name in audit["boundary"]:
        for reach in component_reach(audit, name):
            pairs.append((name, reach.token))
    assert pairs, "the fixture must reach something for this to prove anything"

    # one WAL whose recorded actions are exactly the graph's reach
    path = str(tmp_path / "graph.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    for name in audit["boundary"]:
        timeline = replay.Timeline(name)
        for reach in component_reach(audit, name):
            _step(timeline, replay.KIND_EMISSION, reach.token, caps=[reach.token])
        wal.append_timeline(timeline)
    wal.commit_activation(components=sorted(audit["boundary"]))
    wal.close()

    for text in ("component Agent* may reach llm, kv\n",
                 "component Agent* may reach llm, kv\ncomponent Agent* may not reach sendEmail\n",
                 "component AgentLeak may reach llm\n"):
        policy = parse_policy(text)
        version = policy_diff.diff(policy, policy, read_wal(path))
        refused = {(v.component, v.token) for v in evaluate(policy, audit)
                   if v.kind in ("capability", "deny")}
        decided = {(m["component"], m["token"]) for m in version["moves"]
                   if m["after"] == "deny"}
        assert decided == refused, text


# ===========================================================================
# the CLI surface
# ===========================================================================


def test_the_cli_exits_one_on_a_widening_and_zero_on_a_narrowing(tmp_path,
                                                                 capsys):
    from revl.__main__ import main

    history = _wal(tmp_path,
                   ("AgentSummarize", [("emission", "llm.ask", ["llm"]),
                                       ("emission", "net.push", ["net"])]))
    narrow = str(_write(tmp_path, "n.policy", NARROW))
    wide = str(_write(tmp_path, "w.policy", WIDE))

    assert main(["simulate", "policy-diff", narrow, wide,
                 "--history", history]) == 1
    out = capsys.readouterr().out
    assert "NEWLY ALLOWED:" in out and "net" in out and "blast radius" in out

    assert main(["simulate", "policy-diff", wide, narrow,
                 "--history", history]) == 0
    assert "NEWLY DENIED:" in capsys.readouterr().out

    assert main(["simulate", "policy-diff", narrow, wide, "--history", history,
                 "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "revl.policy-diff"
    assert [m["token"] for m in document["newlyAllowed"]] == ["net"]

    assert main(["simulate", "policy-diff", narrow, wide,
                 "--history", str(tmp_path / "absent.wal")]) == 2
    assert "cannot read WAL" in capsys.readouterr().err

    # the composition resolves the realms a realm-scoped rule decides by
    tenants = _wal(tmp_path, ("TenantAJob", [("emission", "bus.publish",
                                              ["bus"])]), name="tenants.wal")
    loose = str(_write(tmp_path, "loose.policy", "realm tenantA may reach bus\n"))
    tight = str(_write(tmp_path, "tight.policy",
                       "realm tenantA may not reach bus\n"))
    assert main(["simulate", "policy-diff", loose, tight,
                 "--history", tenants]) == 1
    assert "UNDECIDED" in capsys.readouterr().out
    assert main(["simulate", "policy-diff", loose, tight, "--history", tenants,
                 "--composition", str(FIXTURES / "policy_tenants.rvl")]) == 0
    assert "NEWLY DENIED:" in capsys.readouterr().out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))