"""`revl plan` — the dry run for admission (docs/plan.md).

The claim under test is that a plan tells you the *consequences* of a swap
without producing any of them: the same gate runs, the same manifests are
diffed, and nothing — not the running IR, not the disk — is touched.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.plan import plan, render  # noqa: E402

EXAMPLES = ROOT / "examples"


# A three-deep chain, so a withdrawal at the bottom has somewhere to cascade:
#   Db --db--> Store --cache--> Front
CHAIN = """
service Database { fn query(sql: Str) -> List[Row]
                   emission fn execute(sql: Str) -> Int }
service Cache { fn get(key: Str) -> Opt[Str] }
service Api { fn fetch(id: Str) -> Opt[Str] }

component Db provides db: Database {
  let pool = effect Pool.open("u", 1) undo pool.close()
  provide db { fn query(sql) = pool.query(sql)
               fn execute(sql) = pool.execute(sql) }
}
component Store requires db: Database provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache { fn get(key) = m.get(key) }
}
component Front requires cache: Cache provides api: Api {
  provide api { fn fetch(id) = cache.get(id) }
}
"""


@pytest.fixture
def running():
    return compile_source(CHAIN)


def _names(records):
    return [record["name"] for record in records]


def _keys(records):
    return {record["key"] for record in records}


# ---------------------------------------------------------------- addition

ADDITION = """
service Ping { fn go() -> Int }
component Pinger provides ping: Ping {
  provide ping { fn go() = 1 }
}
"""


def test_pure_addition_gains_a_provision_and_disturbs_nothing(running):
    result = plan(source=ADDITION, manifest=running)

    assert result["admissible"] is True
    assert result["basis"] == "admitted"
    assert result["diagnostics"] == []
    assert result["components"]["added"] == ["Pinger"]
    assert result["components"]["replaced"] == []
    assert result["components"]["withdrawn"] == []
    assert _keys(result["provisions"]["gained"]) == {"ping"}
    assert result["provisions"]["withdrawn"] == []
    assert result["provisions"]["rebound"] == []
    # nothing running is disturbed, so nothing tears down
    assert result["cascade"]["diverted"] == []
    assert result["cascade"]["rebound"] == []
    assert result["cascade"]["unaffected"] == ["Db", "Front", "Store"]
    assert result["teardownOrder"] == []


def test_a_gained_provision_names_its_service(running):
    gained = plan(source=ADDITION, manifest=running)["provisions"]["gained"]
    assert gained == [{"key": "ping", "service": "Ping", "provider": "Pinger"}]


def test_the_resulting_load_order_comes_from_the_linker(running):
    result = plan(source=ADDITION, manifest=running)
    assert result["running"]["loadOrder"] == ["Db", "Store", "Front"]
    assert set(result["resulting"]["loadOrder"]) == {"Db", "Store", "Front", "Pinger"}
    order = result["resulting"]["loadOrder"]
    assert order.index("Db") < order.index("Store") < order.index("Front")


def test_a_cold_start_is_all_gain():
    """No running manifest: every provision in the candidate is a gain."""
    result = plan(source=CHAIN)
    assert result["admissible"] is True
    assert result["running"]["components"] == []
    assert _keys(result["provisions"]["gained"]) == {"db", "cache", "api"}
    assert result["provisions"]["withdrawn"] == []
    assert result["teardownOrder"] == []


# ---------------------------------------------------------------- replacement

REPLACE_STORE = """
component Store requires db: Database provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  emit db.execute("warm") compensate db.execute("cool")
  provide cache { fn get(key) = m.get(key) }
}
"""


def test_a_same_name_component_is_reported_as_a_replacement(running):
    result = plan(source=REPLACE_STORE, manifest=running)

    assert result["admissible"] is True
    assert result["components"]["added"] == []
    assert _names(result["components"]["replaced"]) == ["Store"]
    replaced = result["components"]["replaced"][0]
    assert replaced["provides"]["before"] == replaced["provides"]["after"] == ["cache"]
    assert replaced["provides"]["added"] == replaced["provides"]["removed"] == []
    assert replaced["requires"]["after"] == ["db"]


def test_a_replacement_rebinds_the_provision_rather_than_gaining_it(running):
    result = plan(source=REPLACE_STORE, manifest=running)
    assert result["provisions"]["gained"] == []
    assert result["provisions"]["withdrawn"] == []
    rebound = result["provisions"]["rebound"]
    assert rebound == [{"key": "cache", "service": "Cache",
                        "from": "Store", "to": "Store"}]


def test_a_replacements_consumers_deactivate_and_reactivate(running):
    """R2: the consumer is not lost, but it does cycle through the swap."""
    cascade = plan(source=REPLACE_STORE, manifest=running)["cascade"]
    assert _names(cascade["rebound"]) == ["Front"]
    assert cascade["rebound"][0]["keys"] == ["cache"]
    assert cascade["diverted"] == []
    assert cascade["unaffected"] == ["Db"]


def test_teardown_runs_consumers_before_providers(running):
    """G7/R3: LIFO over the running load order, restricted to what is
    actually disturbed — Db is untouched and never tears down."""
    result = plan(source=REPLACE_STORE, manifest=running)
    assert result["teardownOrder"] == ["Front", "Store"]


def test_a_replacement_that_changes_the_interface_is_reported(running):
    """Same name, different provision: `cache` goes away, `hits` appears."""
    result = plan(source="""
service Hits { fn n() -> Int }
component Store requires db: Database provides hits: Hits {
  let m = effect Map.new() undo m.drop()
  provide hits { fn n() = 1 }
}
""", manifest=running)
    assert result["admissible"] is True
    replaced = result["components"]["replaced"][0]
    assert replaced["provides"]["removed"] == ["cache"]
    assert replaced["provides"]["added"] == ["hits"]
    assert _keys(result["provisions"]["withdrawn"]) == {"cache"}
    assert _keys(result["provisions"]["gained"]) == {"hits"}
    # Front required `cache` and nothing provides it any more
    assert _names(result["cascade"]["diverted"]) == ["Front"]


# ---------------------------------------------------------------- withdrawal

def test_withdrawing_a_provision_diverts_its_consumers_transitively(running):
    """Db leaves; Store loses `db` directly and Front loses `cache` because
    its provider is itself diverted."""
    result = plan(source=ADDITION, manifest=running, replacing=("Db",))

    assert result["admissible"] is True
    assert result["components"]["withdrawn"] == ["Db"]
    assert _keys(result["provisions"]["withdrawn"]) == {"db"}
    assert result["provisions"]["withdrawn"][0]["service"] == "Database"

    diverted = {entry["name"]: entry for entry in result["cascade"]["diverted"]}
    assert set(diverted) == {"Store", "Front"}
    assert diverted["Store"]["withdrawnKeys"] == ["db"]
    assert diverted["Store"]["upstreamKeys"] == []
    # Front is a second-order casualty: `cache` still has a provider entry,
    # but that provider cannot activate
    assert diverted["Front"]["withdrawnKeys"] == []
    assert diverted["Front"]["upstreamKeys"] == ["cache"]
    assert "cascade" in diverted["Front"]["reason"]


def test_the_whole_disturbed_set_tears_down_in_lifo_order(running):
    result = plan(source=ADDITION, manifest=running, replacing=("Db",))
    assert result["running"]["loadOrder"] == ["Db", "Store", "Front"]
    assert result["teardownOrder"] == ["Front", "Store", "Db"]


def test_a_diverted_component_is_flagged_in_the_notes(running):
    result = plan(source=ADDITION, manifest=running, replacing=("Db",))
    assert any("PENDING" in note for note in result["notes"])


def test_replacing_a_component_that_is_not_running_is_noted(running):
    result = plan(source=ADDITION, manifest=running, replacing=("Nope",))
    assert result["admissible"] is True
    assert any("`Nope`" in note and "ignored" in note for note in result["notes"])
    assert result["cascade"]["diverted"] == []


def test_a_newly_satisfied_requirement_is_reported_as_an_activation():
    """A component that was PENDING for want of a provider can activate."""
    running = compile_source("""
service Kv { fn get(k: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component Reader requires kv: Kv provides log: Log {
  provide log { fn note(m) = 1 }
}
""")
    assert running["manifest"]["components"][0]["inject"] == ["kv"]
    result = plan(source="""
component KvStore provides kv: Kv {
  let m = effect Map.new() undo m.drop()
  provide kv { fn get(k) = m.get(k) }
}
""", manifest=running)
    assert _names(result["cascade"]["activated"]) == ["Reader"]
    assert result["cascade"]["activated"][0]["keys"] == ["kv"]
    assert result["teardownOrder"] == []


# ---------------------------------------------------------------- realms

def test_realms_scope_the_cascade():
    """Replacing tenant A's store must not disturb tenant B (G2 is
    per-(key, realm), and so is the plan)."""
    running = compile_source(Path(EXAMPLES / "tenants.rvl").read_text())
    result = plan(source="""
component TenantAStore provides kv: Kv {
  isolate kv in realm("tenant_a")
  let store = effect Map.new() undo store.drop()
  provide kv { fn get(k) = store.get(k)
               fn set(k, v) { effect store.insert(k, v) undo store.remove(k) } }
}
""", manifest=running)

    assert result["admissible"] is True
    assert result["provisions"]["rebound"][0]["realm"] == "tenant_a"
    assert _names(result["cascade"]["rebound"]) == ["TenantAApp"]
    assert set(result["cascade"]["unaffected"]) == {"TenantBApp", "TenantBStore"}
    assert result["teardownOrder"] == ["TenantAApp", "TenantAStore"]


# ---------------------------------------------------- emission surface (G8)

def test_a_replacement_that_adds_an_emit_grows_the_surface(running):
    surface = plan(source=REPLACE_STORE, manifest=running)["emissionSurface"]
    assert surface["basis"] == "computed"
    assert surface["gained"]["emissions"] == ["Store.db.execute"]
    assert surface["withdrawn"]["emissions"] == []
    assert surface["before"]["emissionSites"] == 0
    assert surface["after"]["emissionSites"] == 1
    assert surface["after"]["compensated"] == 1
    assert "Store" in surface["byComponent"]


def test_withdrawing_the_emitter_shrinks_the_surface():
    running = compile_source(CHAIN.replace(
        "  let m = effect Map.new() undo m.drop()\n  provide cache",
        "  let m = effect Map.new() undo m.drop()\n"
        "  emit db.execute(\"warm\") compensate db.execute(\"cool\")\n  provide cache"))
    assert plan(source=ADDITION, manifest=running)["emissionSurface"]["before"][
        "emissionSites"] == 1

    result = plan(source=ADDITION, manifest=running, replacing=("Store",))
    surface = result["emissionSurface"]
    assert surface["withdrawn"]["emissions"] == ["Store.db.execute"]
    assert surface["gained"]["emissions"] == []
    assert surface["after"]["emissionSites"] == 0


def test_the_surface_reuses_the_audit_walk(running):
    """Not a reimplementation: the same `_boundary` `revl audit` prints."""
    from revl.__main__ import _boundary

    candidate = compile_source(REPLACE_STORE, manifest=running)
    surface = plan(source=REPLACE_STORE, manifest=running)["emissionSurface"]
    assert surface["byComponent"]["Store"]["after"] == _boundary(candidate)["Store"]


def test_iteration_boundaries_are_counted():
    # the same `Kv` examples/pulse.rvl declares, so nothing drifts
    running = compile_source("""
service Kv { fn get(k: Int) -> Int
             fn set(k: Int, v: Int) }
component Idle provides kv: Kv {
  let m = effect Map.new() undo m.drop()
  provide kv {
    fn get(k) = 0
    fn set(k, v) { effect m.insert(k, v) undo m.remove(k) }
  }
}""")
    result = plan(files=[str(EXAMPLES / "pulse.rvl")], manifest=running)
    assert result["admissible"] is True
    assert result["emissionSurface"]["after"]["iterationBoundaries"] == 1
    assert result["emissionSurface"]["before"]["iterationBoundaries"] == 0


def test_a_manifest_without_bodies_says_the_before_surface_is_unknown(running):
    """A caller may pass `{manifest, services}` rather than a full IR; the
    plan must not report the whole candidate as a *gain* in that case."""
    trimmed = {"manifest": running["manifest"], "services": running["services"]}
    surface = plan(source=REPLACE_STORE, manifest=trimmed)["emissionSurface"]
    assert surface["basis"] == "unavailable"
    assert "unknown" in surface["note"]


# ---------------------------------------------------------------- rejection

DRIFT = """
service Database { fn query(sql: Str) -> List[Row] }
component Probe requires db: Database {
  let m = effect Map.new() undo m.drop()
}
"""


def test_a_rejected_admission_is_explained_not_raised(running):
    result = plan(source=DRIFT, manifest=running)

    assert result["ok"] is True          # a plan was produced
    assert result["admissible"] is False  # but the gate would refuse
    assert result["basis"] == "standalone"
    assert result["diagnostics"][0]["code"] == "G2"
    assert result["diagnostics"][0]["from"] == "admission"
    assert "differs from the running manifest" in result["diagnostics"][0]["message"]
    assert any("rejected this candidate" in note for note in result["notes"])


def test_a_rejected_admission_still_reports_the_delta(running):
    result = plan(source=DRIFT, manifest=running)
    assert result["components"]["added"] == ["Probe"]
    assert result["resulting"]["components"] == ["Db", "Front", "Probe", "Store"]
    # no resulting load order: the linker never got to build one
    assert result["resulting"]["loadOrder"] is None


def test_interface_drift_reports_what_the_gate_would_refuse(running):
    drift = plan(source=DRIFT, manifest=running)["interfaceDrift"]
    assert [entry["service"] for entry in drift] == ["Database"]
    assert drift[0]["method"] == "execute"
    assert drift[0]["kind"] == "removed"
    assert "a running consumer may still call it" in drift[0]["reason"]
    # the running components the change strands, read off the manifest
    assert "Store" in drift[0]["consumers"]
    assert "Db" in drift[0]["providers"]


def test_an_admitted_plan_reports_no_drift(running):
    assert plan(source=ADDITION, manifest=running)["interfaceDrift"] == []


# ------------------------------------------- compatible evolution (§5/§6.6)

# The structural relation (docs/service-compat.md) admits *compatible* change
# and refuses incompatible change, but only against the running components
# that actually touch the interface. Each candidate below replaces its
# provider (`Db`/`N1` is re-declared, so no running provider is retained —
# G2 forbids two providers of one key), while a running *consumer* survives
# (`Store` injects `db`, `Watch` injects `nick`): the candidate is NOT allowed
# to redeclare that consumer, so its call sites pin the shared methods. The
# old preview flagged every dict inequality as drift; the structural relation
# must report none of these.

EVOLVE_ADD = """
service Database { fn query(sql: Str) -> List[Row]
                   emission fn execute(sql: Str) -> Int
                   fn health() -> Bool }
component Db provides db: Database {
  let pool = effect Pool.open("u", 1) undo pool.close()
  provide db { fn query(sql) = pool.query(sql)
               fn execute(sql) = pool.execute(sql)
               fn health() = true }
}
"""

NICK_CHAIN = """
service Nick { fn name(id: Str) -> Opt[Str]
               fn area(w: Int) -> Int }
component N1 provides nick: Nick {
  let m = effect Map.new() undo m.drop()
  provide nick { fn name(id) = m.get(id)
                 fn area(w) = w }
}
component Watch requires nick: Nick {
  let m = effect Map.new() undo m.drop()
}
"""

EVOLVE_WIDEN_PARAM = """
service Nick { fn name(id: Str) -> Opt[Str]
               fn area(w: Float) -> Int }
component N1 provides nick: Nick {
  let m = effect Map.new() undo m.drop()
  provide nick { fn name(id) = m.get(id)
                 fn area(w) = 0 }
}
"""

EVOLVE_NARROW_RETURN = """
service Nick { fn name(id: Str) -> Str
               fn area(w: Int) -> Int }
component N1 provides nick: Nick {
  let m = effect Map.new() undo m.drop()
  provide nick { fn name(id) = "ok"
                 fn area(w) = w }
}
"""

EVOLVE_DROP_EMISSION = """
service Database { fn query(sql: Str) -> List[Row]
                   fn execute(sql: Str) -> Int }
component Db provides db: Database {
  let pool = effect Pool.open("u", 1) undo pool.close()
  provide db { fn query(sql) = pool.query(sql)
               fn execute(sql) = 0 }
}
"""


@pytest.mark.parametrize("source", [EVOLVE_ADD, EVOLVE_WIDEN_PARAM,
                                    EVOLVE_NARROW_RETURN,
                                    EVOLVE_DROP_EMISSION],
                         ids=["add-method", "widen-param", "narrow-return",
                              "drop-emission"])
def test_compatible_evolution_is_not_drift(running, source):
    """Compatible evolution under a *retained consumer* is admitted and is not
    drift: `Store`/`Watch` keeps its call sites against the old interface, and
    the relation allows what keeps them valid (a method may be added, a
    parameter widened, a return narrowed, an emission dropped)."""
    manifest = running if source in (EVOLVE_ADD, EVOLVE_DROP_EMISSION) \
        else compile_source(NICK_CHAIN)
    result = plan(source=source, manifest=manifest)
    assert result["admissible"] is True
    assert result["interfaceDrift"] == []


# Genuine breaks, each under the same retained consumer. The candidate
# deliberately does NOT redeclare `Watch`: a running consumer whose call sites
# are never recompiled is what pins the interface (docs/service-compat.md).
# Redeclaring the whole program would drop `Watch` from the gate's ambient and
# the admission would (correctly) admit any shape change — that mistake is
# exactly what the earlier draft of these tests made.

BREAK_EMISSION = """
service Nick { fn name(id: Str) -> Opt[Str]
               emission fn area(w: Int) -> Int }
component N1 provides nick: Nick {
  let m = effect Map.new() undo m.drop()
  provide nick { fn name(id) = m.get(id)
                 fn area(w) = m.drop() }
}
"""

BREAK_NARROW_PARAM = """
service Nick { fn name(id: Str) -> Opt[Str]
               fn area(w: Str) -> Int }
component N1 provides nick: Nick {
  let m = effect Map.new() undo m.drop()
  provide nick { fn name(id) = m.get(id)
                 fn area(w) = 0 }
}
"""

BREAK_REMOVE_METHOD = """
service Nick { fn name(id: Str) -> Opt[Str] }
component N1 provides nick: Nick {
  let m = effect Map.new() undo m.drop()
  provide nick { fn name(id) = m.get(id) }
}
"""


@pytest.mark.parametrize("source,kind,method,needle", [
    (BREAK_EMISSION, "emission", "area", "becomes an `emission`"),
    (BREAK_NARROW_PARAM, "signature", "area", "narrows"),
    (BREAK_REMOVE_METHOD, "removed", "area", "removed"),
], ids=["emission-appears", "narrow-param", "remove-method"])
def test_genuine_breaks_still_appear_as_drift(source, kind, method, needle):
    """Genuine breaks are refused and the plan names the same method, kind and
    reason the gate's own G2 rejection does, plus the running consumer the
    change strands."""
    running = compile_source(NICK_CHAIN)
    result = plan(source=source, manifest=running)
    assert result["admissible"] is False
    entries = [(d["kind"], d["method"]) for d in result["interfaceDrift"]]
    assert (kind, method) in entries
    entry = next(d for d in result["interfaceDrift"]
                 if (d["kind"], d["method"]) == (kind, method))
    assert needle in entry["reason"]
    assert "Watch" in entry["consumers"]


def test_a_g2_conflict_is_reported_with_the_delta_it_would_have_caused(running):
    """A second provider for `db` — rejected, but the plan still shows what
    the author was reaching for."""
    result = plan(source="""
component Db2 provides db: Database {
  let pool = effect Pool.open("v", 1) undo pool.close()
  provide db { fn query(sql) = pool.query(sql)
               fn execute(sql) = pool.execute(sql) }
}
""", manifest=running)
    assert result["admissible"] is False
    assert result["diagnostics"][0]["code"] == "G2"
    assert "provision conflict" in result["diagnostics"][0]["message"]
    assert result["components"]["added"] == ["Db2"]


def test_a_candidate_that_does_not_compile_falls_back_to_its_headers(running):
    """A type error inside the body still leaves the component's shape
    readable, so the delta is reported from the AST."""
    result = plan(source="""
component Extra requires db: Database {
  let m = effect Map.new() undo m.drop()
  effect db.query(42) undo m.drop()
}
""", manifest=running)
    assert result["ok"] is True
    assert result["admissible"] is False
    assert result["basis"] == "parsed"
    assert result["diagnostics"][0]["code"] == "T1"
    assert result["components"]["added"] == ["Extra"]
    assert any("recovered from the AST" in note for note in result["notes"])


def test_a_standalone_only_diagnostic_is_labelled_as_such(running):
    """Re-compiling the candidate alone loses the ambient services, so its
    complaints must not read as findings about the candidate."""
    result = plan(source="""
component Extra requires db: Database {
  let m = effect Map.new() undo m.drop()
  effect db.query(42) undo m.drop()
}
""", manifest=running)
    extra = [d for d in result["diagnostics"] if d["from"] == "standalone"]
    assert extra, "expected the standalone compile's complaint to be surfaced"
    assert all("may not be a real defect" in d["note"] for d in extra)


def test_an_unparseable_candidate_reports_one_diagnostic_not_two(running):
    result = plan(source="component Broken provides", manifest=running)
    assert result["ok"] is False
    assert result["admissible"] is False
    assert result["basis"] == "none"
    assert len(result["diagnostics"]) == 1
    assert result["diagnostics"][0]["code"] == "SYNTAX"


def test_an_unparseable_candidate_still_describes_what_is_running(running):
    result = plan(source="component Broken provides", manifest=running)
    assert result["running"]["components"] == ["Db", "Front", "Store"]
    assert result["running"]["loadOrder"] == ["Db", "Store", "Front"]
    assert render(result).startswith("plan: REJECTED")


def test_a_missing_file_is_one_diagnostic_not_a_traceback(tmp_path, running):
    result = plan(files=[str(tmp_path / "absent.rvl")], manifest=running)
    assert result["ok"] is False
    assert result["basis"] == "none"
    assert len(result["diagnostics"]) == 1
    assert "file not found" in result["diagnostics"][0]["message"]
    assert result["running"]["components"] == ["Db", "Front", "Store"]


# ---------------------------------------------------------------- purity

def test_a_plan_mutates_nothing(running):
    before = copy.deepcopy(running)
    plan(source=REPLACE_STORE, manifest=running, replacing=("Db",))
    plan(source=DRIFT, manifest=running)
    plan(source="component Broken provides", manifest=running)
    assert running == before


def test_a_plan_writes_no_files(tmp_path, running):
    plan(source=REPLACE_STORE, manifest=running)
    assert list(tmp_path.iterdir()) == []


def test_the_payload_separates_guaranteed_from_predicted(running):
    result = plan(source=ADDITION, manifest=running)
    assert any("gate was actually run" in claim for claim in result["guaranteed"])
    assert any("not an observation" in claim for claim in result["predicted"])


def test_render_marks_the_cascade_as_predicted(running):
    text = render(plan(source=REPLACE_STORE, manifest=running))
    assert "reactive cascade (predicted" in text
    assert "teardown order (predicted" in text
    assert "nothing was compiled to disk, admitted or swapped" in text


def test_render_handles_every_basis(running):
    for source in (ADDITION, DRIFT, "component Broken provides"):
        assert isinstance(render(plan(source=source, manifest=running)), str)


# ---------------------------------------------------------------- CLI

def test_cli_plan_without_a_manifest_is_a_cold_start(capsys):
    assert main(["plan", str(EXAMPLES / "user_cache.rvl")]) == 0
    out = capsys.readouterr().out
    assert "plan: ADMISSIBLE" in out
    assert "db: Database" in out


def test_cli_plan_against_a_running_manifest(tmp_path, capsys, running):
    path = tmp_path / "running.json"
    path.write_text(json.dumps(running))
    assert main(["plan", str(EXAMPLES / "registry.rvl"), "--manifest", str(path)]) == 0
    out = capsys.readouterr().out
    assert "added:     RegistryStore" in out
    assert "reg: Registry" in out


def test_cli_exit_status_follows_the_gate(tmp_path, capsys, running):
    path = tmp_path / "running.json"
    path.write_text(json.dumps(running))
    candidate = tmp_path / "drift.rvl"
    candidate.write_text(DRIFT)
    assert main(["plan", str(candidate), "--manifest", str(path)]) == 1
    out = capsys.readouterr().out
    assert "plan: REJECTED" in out
    assert "interface drift (would be refused)" in out


def test_cli_replacing_flag_drives_the_cascade(tmp_path, capsys, running):
    path = tmp_path / "running.json"
    path.write_text(json.dumps(running))
    candidate = tmp_path / "add.rvl"
    candidate.write_text(ADDITION)
    assert main(["plan", str(candidate), "--manifest", str(path),
                 "--replacing", "Db"]) == 0
    out = capsys.readouterr().out
    assert "DIVERTED   Store" in out
    assert "Front -> Store -> Db" in out


def test_cli_json_is_the_structured_plan(tmp_path, capsys, running):
    path = tmp_path / "running.json"
    path.write_text(json.dumps(running))
    assert main(["plan", str(EXAMPLES / "registry.rvl"), "--manifest", str(path),
                 "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["admissible"] is True
    assert payload["components"]["added"] == ["RegistryStore"]


def test_cli_reports_an_unreadable_manifest(tmp_path, capsys):
    assert main(["plan", str(EXAMPLES / "registry.rvl"),
                 "--manifest", str(tmp_path / "absent.json")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_cli_rejects_a_manifest_that_is_not_a_document(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("[1, 2]")
    assert main(["plan", str(EXAMPLES / "registry.rvl"), "--manifest", str(path)]) == 1
    assert "expected a compiled IR document" in capsys.readouterr().err


# ---------------------------------------------------------------- MCP

def _call(name: str, arguments: dict) -> dict:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    return handle(message)["result"]["structuredContent"]


def test_mcp_advertises_revl_plan_as_read_only():
    tools = {t["name"]: t
             for t in handle({"jsonrpc": "2.0", "id": 1,
                              "method": "tools/list"})["result"]["tools"]}
    assert "revl_plan" in tools
    assert tools["revl_plan"]["annotations"]["readOnlyHint"] is True
    assert tools["revl_plan"]["annotations"]["destructiveHint"] is False
    # `manifest` is optional here (unlike revl_admit) — it defaults to the
    # loaded session
    assert "required" not in tools["revl_plan"]["inputSchema"]


def test_mcp_plan_takes_in_memory_sources(running):
    payload = _call("revl_plan", {"manifest": running, "source": REPLACE_STORE})
    assert payload["ok"] is True
    assert payload["admissible"] is True
    assert payload["against"] == "manifest"
    assert _names(payload["cascade"]["rebound"]) == ["Front"]
    assert payload["teardownOrder"] == ["Front", "Store"]
    assert "nothing was admitted" in payload["note"]


def test_mcp_plan_takes_in_memory_modules(tmp_path):
    """A multi-module candidate that has never existed as a file."""
    payload = _call("revl_plan", {
        "files": [str(tmp_path / "app.rvl")],
        "modules": {
            str(tmp_path / "app.rvl"): 'use "lib.rvl" { Kv }\n'
                                       "component App requires kv: Kv {\n"
                                       '  effect kv.set("a", "b") undo kv.set("a", "")\n}',
            str(tmp_path / "lib.rvl"): "service Kv { fn set(k: Str, v: Str) }",
        },
    })
    assert payload["ok"] is True and payload["admissible"] is True
    assert payload["components"]["added"] == ["App"]
    assert not list(tmp_path.iterdir())


def test_mcp_plan_without_a_manifest_is_a_cold_start():
    payload = _call("revl_plan", {"source": CHAIN})
    assert payload["ok"] is True
    assert payload["against"].startswith("nothing")
    assert _keys(payload["provisions"]["gained"]) == {"db", "cache", "api"}


def test_mcp_plan_explains_a_rejection_without_erroring_the_transport(running):
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "revl_plan",
                          "arguments": {"manifest": running, "source": DRIFT}}}
    result = handle(message)["result"]
    payload = result["structuredContent"]
    assert payload["admissible"] is False
    assert payload["interfaceDrift"][0]["service"] == "Database"
    # `ok` is true — a plan *was* produced — so this is not a tool error
    assert result["isError"] is False


def test_mcp_plan_leaves_the_running_manifest_untouched(running):
    before = copy.deepcopy(running)
    _call("revl_plan", {"manifest": running, "source": REPLACE_STORE,
                        "replacing": ["Db"]})
    assert running == before
