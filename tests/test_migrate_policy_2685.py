"""`revl migrate --policy`: the policy-driven second slice of issue #2685.

The first slice classifies a version change from measured facts only. This one
reads the operator's boundary policy (docs/boundary-policy.md) over the
composition as it would stand AFTER the migration, by the same
`policy.evaluate` and `policy.approval_admission` the gate runs. These tests pin:

  * a violation the migration introduces refuses it, and the same change
    without a policy is only `needs-approval` (so the policy is what decides);
  * a violation the composition already carries before the change is reported
    as pre-existing and never blamed on the new version;
  * a violation the migration removes is reported as resolved;
  * `capability X requires approval` asks for an approval rather than forbids;
  * rules that read the candidate's published evidence are not decided from a
    compiled document and are reported as undecided, not failed;
  * the composed "after" document replaces the old version's components and
    keeps the rest, in dependency order;
  * a plan without `--policy` keeps the shape it had, and the command stays
    read-only.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.migrate import (  # noqa: E402
    NEEDS_APPROVAL,
    _classification,
    REFUSED,
    compose_after,
    plan,
)
from revl.policy import parse_policy  # noqa: E402


SERVICES = """
service Agent { emission[llm, mail] fn run() -> Str }
service LLM { emission[llm] fn ask(prompt: Str) -> Str }
service Mail { emission[mail] fn send(to: Str) }
"""

# v1 of the agent talks to the model only.
OLD = SERVICES + """
component AgentImpl requires llm: LLM provides agent: Agent {
  provide agent {
    fn run() {
      emit llm.ask("summarize")
      return "ok"
    }
  }
}
"""

# v2 also sends mail: a widening, and (under the policy) a refused one.
NEW = SERVICES + """
component AgentImpl requires llm: LLM, mail: Mail provides agent: Agent {
  provide agent {
    fn run() {
      emit llm.ask("summarize")
      emit mail.send("victim@example.com")
      return "ok"
    }
  }
}
"""

FRONT = "component Front requires agent: Agent { }\n"
AGAINST = OLD + FRONT

# an unrelated component that already breaks the allow-list below
WILD = """
component AgentWild requires mail: Mail {
  emit mail.send("already out of policy")
}
"""

ALLOW_LLM = "component Agent* may reach llm\n"


def _plan(old_src: str, new_src: str, against_src: str, policy_text=None):
    policy = parse_policy(policy_text, "test.policy") if policy_text else None
    return plan(compile_source(old_src), compile_source(new_src),
                compile_source(against_src), policy=policy)


# ------------------------------------------------- the policy decides

def test_a_violation_the_migration_introduces_refuses_it():
    result = _plan(OLD, NEW, AGAINST, ALLOW_LLM)
    assert result["classification"] == REFUSED
    new = result["policy"]["new"]
    assert [(v["kind"], v["component"], v["token"]) for v in new] == [
        ("capability", "AgentImpl", "mail")]
    assert "mail" in result["classificationReason"]
    assert result["policy"]["preexisting"] == []
    # nothing is applied on a refusal, so there is nothing to roll back
    assert "nothing is applied" in result["rollback"]["reason"]


def test_the_same_change_without_a_policy_only_needs_approval():
    # the differential: the policy, not the facts alone, made it a refusal.
    result = _plan(OLD, NEW, AGAINST)
    assert result["classification"] == NEEDS_APPROVAL
    assert "policy" not in result


def test_a_policy_that_admits_the_widening_does_not_hide_it():
    # the allow-list covers `mail`, so the policy is clean, but the new
    # authority is still a widening that needs approval.
    result = _plan(OLD, NEW, AGAINST, "component Agent* may reach llm, mail\n")
    assert result["policy"]["new"] == []
    assert result["classification"] == NEEDS_APPROVAL
    assert "widens authority" in result["classificationReason"]


def test_an_approval_required_capability_asks_for_approval():
    result = _plan(OLD, NEW, AGAINST, "capability mail requires approval\n")
    kinds = [v["kind"] for v in result["policy"]["new"]]
    assert kinds == ["approval"]
    assert result["classification"] == NEEDS_APPROVAL
    assert "requires an approval" in result["classificationReason"]
    assert "`mail`" in result["classificationReason"]


# ------------------------------------------- before and after, not blame

def test_a_violation_already_there_is_preexisting_not_new():
    against = AGAINST + WILD
    # the migration only adds a method: nothing new for the policy to refuse
    new = NEW.replace("emit mail.send(\"victim@example.com\")\n", "")
    new = new.replace("requires llm: LLM, mail: Mail", "requires llm: LLM")
    result = _plan(OLD, new, against, ALLOW_LLM)
    policy = result["policy"]
    assert policy["new"] == []
    assert [(v["component"], v["token"]) for v in policy["preexisting"]] == [
        ("AgentWild", "mail")]
    assert result["classification"] != REFUSED


def test_a_violation_the_migration_removes_is_resolved():
    # the composition runs the mail-sending v1, which the allow-list refuses;
    # v2 stops sending mail, so the migration resolves the violation.
    mailing = NEW
    quiet = OLD
    result = _plan(mailing, quiet, mailing + FRONT, ALLOW_LLM)
    policy = result["policy"]
    assert policy["new"] == []
    assert [(v["component"], v["token"]) for v in policy["resolved"]] == [
        ("AgentImpl", "mail")]
    assert result["classification"] != REFUSED


def test_a_second_new_violation_from_a_new_component_is_found():
    # the new version brings a second component that reaches mail
    new = NEW + """
component AgentMailer requires mail: Mail { emit mail.send("copy") }
"""
    result = _plan(OLD, new, AGAINST, ALLOW_LLM)
    found = {(v["component"], v["token"]) for v in result["policy"]["new"]}
    assert found == {("AgentImpl", "mail"), ("AgentMailer", "mail")}


# --------------------------------------------- what a document cannot decide

def test_evidence_rules_are_reported_undecided_not_failed():
    # an evidence floor reads the candidate's published bundle, which a compiled
    # document does not carry; failing it for want of evidence would refuse
    # every plan, so it is reported as not decided here.
    policy_text = (
        ALLOW_LLM + "component registry:* requires evidence "
                    "[attestation valid, fault-sweep full]\n")
    result = _plan(OLD, OLD, AGAINST, policy_text)
    assert result["policy"]["undecided"]["evidenceRules"] == 1
    assert result["policy"]["new"] == []
    assert result["classification"] != REFUSED


# -------------------------------------------------------- the composed view

def test_compose_after_replaces_the_old_components_and_keeps_the_rest():
    old, new, against = (compile_source(OLD), compile_source(NEW),
                         compile_source(AGAINST))
    after = compose_after(old, new, against)
    names = [c["name"] for c in after["components"]]
    assert sorted(names) == ["AgentImpl", "Front"]
    impl = next(c for c in after["components"] if c["name"] == "AgentImpl")
    assert sorted(impl["requires"]) == ["llm", "mail"]  # the NEW version's
    # the composed manifest is a view of the rows, not a link: no loadOrder
    assert "loadOrder" not in after["manifest"]
    # the replaced component keeps the position it held
    assert names == ["AgentImpl", "Front"]
    assert [r["name"] for r in after["manifest"]["components"]] == [
        "AgentImpl", "Front"]
    # the inputs are untouched: composing is a view, not an edit
    assert [c["name"] for c in against["components"]] == ["AgentImpl", "Front"]
    assert sorted(against["components"][0]["requires"]) == ["llm"]


def test_compose_after_takes_the_new_services_and_the_new_externs():
    old = compile_source("""
service Cache { fn get(key: Str) -> Str }
extern pure fn probe_a() -> Str = @py { return "a" }
component CacheImpl provides cache: Cache { provide cache { fn get(key) = key } }
""")
    new = compile_source("""
service Cache { fn get(key: Str) -> Str
  fn size() -> Int }
extern pure fn probe_b() -> Str = @py { return "b" }
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn size() = 0
  }
}
""")
    against = compile_source("""
service Cache { fn get(key: Str) -> Str }
extern pure fn probe_a() -> Str = @py { return "a" }
component CacheImpl provides cache: Cache { provide cache { fn get(key) = key } }
component App requires cache: Cache { }
""")
    after = compose_after(old, new, against)
    assert sorted(after["services"]["Cache"]["methods"]) == ["get", "size"]
    # the new version's declaration is added; the old version's `probe_a` is
    # withdrawn, since no component that stays reaches it (see the extern tests)
    assert [e["name"] for e in after["externs"]] == ["probe_b"]


def test_compose_after_keeps_a_realm_in_its_manifest_row():
    realm = SERVICES + """
component TenantAgent requires llm: LLM provides agent: Agent {
  isolate agent in realm("tenant_a")
  provide agent {
    fn run() {
      emit llm.ask("hi")
      return "ok"
    }
  }
}
"""
    old, new, against = (compile_source(OLD), compile_source(NEW),
                         compile_source(AGAINST + realm.split(
                             "service Mail { emission[mail] fn send(to: Str) }")[1]))
    after = compose_after(old, new, against)
    rows = {r["name"]: r for r in after["manifest"]["components"]}
    assert rows["TenantAgent"]["isolate"] == {"agent": "tenant_a"}


# ----------------------------------------------- the other rule families

BUS = """
service Bus { emission[bus] fn publish(message: Str) }
service Job { emission[bus] fn go() }
"""


def _tenant_a(isolated: bool) -> str:
    iso = 'isolate bus in realm("tenantA")\n  ' if isolated else ""
    return BUS + f"""
component TenantAJob requires bus: Bus provides job: Job {{
  {iso}provide job {{
    fn go() {{ emit bus.publish("a") }}
  }}
}}
"""


TENANT_B = """
component TenantBJob requires bus: Bus {
  isolate bus in realm("tenantB")
  emit bus.publish("b")
}
"""
JOB_FRONT = "component Front requires job: Job { }\n"
TENANTS = "tenants never reach each other\n"


def test_a_version_that_joins_a_tenant_realm_is_refused_by_the_tenant_rule():
    # v1 lives in the shared realm; v2 isolates into tenantA, which now shares
    # `bus` with tenantB: a violation only the migration introduces.
    result = _plan(_tenant_a(False), _tenant_a(True),
                   _tenant_a(False) + TENANT_B + JOB_FRONT, TENANTS)
    assert result["classification"] == REFUSED
    assert [(v["kind"], v["component"], v["token"])
            for v in result["policy"]["new"]] == [
        ("tenant", "TenantAJob", "bus")]
    assert result["policy"]["preexisting"] == []


def test_a_tenant_violation_already_there_does_not_flip_with_the_order():
    # the pair is named in component order. Replacing the migrating component
    # in place keeps the pair the same pair, so an unchanged violation is
    # preexisting, never one `new` plus one `resolved`.
    both = _tenant_a(True) + TENANT_B + JOB_FRONT
    result = _plan(_tenant_a(True), _tenant_a(True), both, TENANTS)
    policy = result["policy"]
    assert policy["new"] == []
    assert policy["resolved"] == []
    assert len(policy["preexisting"]) == 1
    assert result["classification"] != REFUSED


def _register_src(idempotent: str) -> str:
    return f"""
extern emission[db] {idempotent} fn push(k: Str) -> Unit = @py {{ pass }}
service Store {{ emission[db] fn ping(key: Str) }}
component Reader requires store: Store {{
  emit store.ping("c")
  emit push("a")
}}
"""


def test_a_register_floor_is_decided_from_the_composition():
    # register floors read the audit alone, so they are decided here: v2 weakens
    # the extern's register from keyed to declared, below the policy floor.
    keyed = _register_src("idempotent(key: k)")
    declared = _register_src("idempotent")
    floor = "capability db requires register keyed\n"
    result = _plan(keyed, declared, keyed, floor)
    assert [(v["kind"], v["token"]) for v in result["policy"]["new"]] == [
        ("register", "db")]
    assert result["classification"] == REFUSED
    # and the other direction clears it
    cleared = _plan(declared, keyed, declared, floor)
    assert cleared["policy"]["new"] == []
    assert [(v["kind"], v["token"]) for v in cleared["policy"]["resolved"]] == [
        ("register", "db")]


def test_a_pair_rule_refuses_a_version_that_holds_both_capabilities():
    result = _plan(OLD, NEW, AGAINST,
                   "component Agent* may not reach llm together with mail\n")
    assert [(v["kind"], v["component"]) for v in result["policy"]["new"]] == [
        ("pair", "AgentImpl")]
    assert result["classification"] == REFUSED


def test_a_deny_rule_refuses_the_new_reach():
    result = _plan(OLD, NEW, AGAINST, "component * may not reach mail\n")
    assert [(v["kind"], v["token"]) for v in result["policy"]["new"]] == [
        ("deny", "mail")]
    assert result["classification"] == REFUSED


def test_a_renamed_method_does_not_turn_a_breach_into_a_new_one():
    # the same breach reached through a renamed service method is the same
    # violation: the key is the rule, the component and the capability, not the
    # message, which embeds the emission label (`mail.send` vs `mail.deliver`).
    mailing = NEW
    renamed = NEW.replace("fn send(to: Str)", "fn deliver(to: Str)").replace(
        "mail.send", "mail.deliver")
    result = _plan(mailing, renamed, mailing + FRONT,
                   "component * may not reach mail\n")
    assert result["policy"]["new"] == []
    assert result["policy"]["resolved"] == []
    assert len(result["policy"]["preexisting"]) == 1


def test_both_approval_kinds_classify_as_needs_approval():
    for kind in ("approval", "declassify-approval"):
        policy = {"new": [{"kind": kind, "component": "C", "token": "t",
                           "message": "m"}], "preexisting": [], "resolved": []}
        verdict, reason = _classification(
            [], {"widened": [], "narrowed": []}, [], {"bump": "patch"}, policy)
        assert verdict == NEEDS_APPROVAL, kind
        assert "requires an approval" in reason


# ------------------------------------------------ withdrawn externs

def test_compose_after_withdraws_an_extern_only_the_old_version_carried():
    old = compile_source("""
extern emission[db] idempotent fn legacy_push(k: Str) -> Unit = @py { pass }
service Store { emission[db] fn ping(key: Str) }
component Reader requires store: Store { emit legacy_push("a") }
""")
    new = compile_source("""
extern emission[db] idempotent(key: k) fn fresh_push(k: Str) -> Unit = @py { pass }
service Store { emission[db] fn ping(key: Str) }
component Reader requires store: Store { emit fresh_push("a") }
""")
    against = compile_source("""
extern emission[db] idempotent fn legacy_push(k: Str) -> Unit = @py { pass }
service Store { emission[db] fn ping(key: Str) }
component Reader requires store: Store { emit legacy_push("a") }
component Other requires store: Store { emit store.ping("o") }
""")
    after = compose_after(old, new, against)
    assert [e["name"] for e in after["externs"]] == ["fresh_push"]


def test_compose_after_keeps_an_extern_a_retained_component_still_reaches():
    old = compile_source("""
extern emission[db] idempotent fn shared_push(k: Str) -> Unit = @py { pass }
service Store { emission[db] fn ping(key: Str) }
component Reader requires store: Store { emit shared_push("a") }
""")
    new = compile_source("""
service Store { emission[db] fn ping(key: Str) }
component Reader requires store: Store { emit store.ping("n") }
""")
    against = compile_source("""
extern emission[db] idempotent fn shared_push(k: Str) -> Unit = @py { pass }
service Store { emission[db] fn ping(key: Str) }
component Reader requires store: Store { emit shared_push("a") }
component Other requires store: Store { emit shared_push("o") }
""")
    after = compose_after(old, new, against)
    assert [e["name"] for e in after["externs"]] == ["shared_push"]


# ------------------------------------------------------------------- the CLI

def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_cli_policy_refusal_is_in_the_plan_and_nothing_is_written(
        tmp_path, capsys):
    old = _write(tmp_path, "old.rvl", OLD)
    new = _write(tmp_path, "new.rvl", NEW)
    against = _write(tmp_path, "against.rvl", AGAINST)
    policy = _write(tmp_path, "boundary.policy", ALLOW_LLM)

    def tree() -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(tmp_path.iterdir())}

    before = tree()
    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--policy", str(policy), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["classification"] == REFUSED
    assert doc["policy"]["source"] == str(policy)
    assert doc["policy"]["new"][0]["token"] == "mail"
    assert tree() == before

    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--policy", str(policy)]) == 0
    human = capsys.readouterr().out
    assert "boundary policy" in human
    assert "+ new: capability `AgentImpl` -> `mail`" in human
    assert "classification: refused" in human


def test_cli_without_a_policy_prints_no_policy_section(tmp_path, capsys):
    old = _write(tmp_path, "old.rvl", OLD)
    new = _write(tmp_path, "new.rvl", NEW)
    against = _write(tmp_path, "against.rvl", AGAINST)
    assert main(["migrate", str(old), str(new), "--against", str(against)]) == 0
    assert "boundary policy" not in capsys.readouterr().out


def test_cli_an_unreadable_policy_is_a_named_error(tmp_path, capsys):
    old = _write(tmp_path, "old.rvl", OLD)
    new = _write(tmp_path, "new.rvl", NEW)
    against = _write(tmp_path, "against.rvl", AGAINST)
    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--policy", str(tmp_path / "missing.policy")]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_a_malformed_policy_is_a_named_error(tmp_path, capsys):
    old = _write(tmp_path, "old.rvl", OLD)
    new = _write(tmp_path, "new.rvl", NEW)
    against = _write(tmp_path, "against.rvl", AGAINST)
    policy = _write(tmp_path, "bad.policy", "component * may reach llm kv\n")
    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--policy", str(policy)]) == 1
    assert "error:" in capsys.readouterr().err


def test_a_clean_policy_leaves_the_first_slice_verdict_alone():
    # identical versions: the policy finds nothing, so the plan is the first
    # slice's own (`safe`), not a refusal and not an approval
    result = _plan(OLD, OLD, AGAINST, ALLOW_LLM)
    assert result["policy"]["new"] == []
    assert result["classification"] == "safe"


def test_an_empty_policy_is_a_clean_verdict():
    result = _plan(OLD, NEW, AGAINST, "# no rules\n")
    assert result["policy"] == {
        "source": "test.policy", "new": [], "preexisting": [],
        "resolved": [],
        "undecided": {"evidenceRules": 0}}
    assert result["classification"] == NEEDS_APPROVAL
