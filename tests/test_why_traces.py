"""Why-traces: the derivation behind a search-based rejection (docs/why-traces.md).

Three rejections are the verdict of a search the compiler used to throw
away — G4's least fixed point over the call graph, G3's cycle detection,
G2's provider table. These tests pin the evidence each one now attaches:
the structured `why` on the error, its JSON projection, and the human
rendering. They also pin what must *not* move: the first line of every
message, and the set of programs that are rejected at all.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl.diagnostics import FIXES, GUARANTEES, explain, report  # noqa: E402
from revl.why import CHAIN, SET, TraceStep, WhyTrace, render  # noqa: E402

EXAMPLES = ROOT / "examples"


def _reject(source: str, filename: str = "why.rvl") -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, filename)
    return excinfo.value


# ---------------------------------------------------------------- G4 chain

# put -> write_through -> audit_log -> audit_write, the last an `emission`
# extern: three hops of the fixed point, none of them visible in the message
MULTI_HOP_G4 = """extern emission fn audit_write(msg: Str) -> Int = @py { return 1 }

fn audit_log(msg: Str) -> Int {
  return audit_write(msg)
}

fn write_through(key: Str) -> Int {
  return audit_log(key)
}

service Cache {
  fn put(key: Str, value: Str)
}

component LyingCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      let n = write_through(key)
    }
  }
}
"""


def test_g4_multi_hop_chain_is_the_whole_derivation():
    error = _reject(MULTI_HOP_G4)
    why = error.why
    assert why is not None
    assert why.kind == "emission-propagation"
    assert why.subject == "Cache.put"
    assert why.shape == CHAIN
    # the fixed point walked three call edges; every one is named, in order
    assert why.path() == ["put", "write_through", "audit_log", "audit_write"]


def test_g4_chain_steps_carry_kinds_and_locations():
    steps = _reject(MULTI_HOP_G4).why.steps
    assert [step.kind for step in steps] == [
        "provide-method", "call", "call", "emission"]
    assert [step.line for step in steps] == [18, 7, 3, 1]
    assert all(step.file == "why.rvl" for step in steps)
    # only the terminal step is classified — that is what makes it terminal
    assert steps[-1].detail == "emission"
    assert steps[1].detail is None


def test_g4_chain_renders_as_an_arrow_path():
    rendered = str(_reject(MULTI_HOP_G4))
    assert "why `Cache.put` is emission:" in rendered
    assert "put -> write_through -> audit_log -> audit_write   (emission)" in rendered
    # each hop is pinned to a source location the author can open
    assert "audit_write    why.rvl:1  emission" in rendered


def test_g4_first_line_is_unchanged():
    """The rejection suite matches on the message; the trace is additive."""
    error = _reject(MULTI_HOP_G4)
    assert error.message == (
        "`Cache.put` is declared plain, but this implementation "
        "reaches `write_through()`")
    assert str(error).splitlines()[0] == f"why.rvl:{error.line}: {error.message}"


def test_g4_single_hop_service_emission_names_the_service():
    """The checked-in fixture: one hop, straight to an `emission fn`."""
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(EXAMPLES / "rejections" / "g4_emission_not_declared.rvl")])
    why = excinfo.value.why
    assert why.path() == ["put", "db.execute"]
    assert why.steps[-1].kind == "emission"
    assert why.steps[-1].detail == "emission `Database.execute`"
    # located at the *service* declaration, not the call site: that is the
    # line the author has to edit to make the declaration honest
    assert why.steps[-1].line == 13


# ---------------------------------------------------------------- G3 cycle

THREE_COMPONENT_CYCLE = """service A { fn ping(tag: Str) -> Str }
service B { fn pong(tag: Str) -> Str }
service C { fn peng(tag: Str) -> Str }

component Alpha requires c: C provides a: A {
  provide a { fn ping(tag) = c.peng(tag) }
}

component Beta requires a: A provides b: B {
  provide b { fn pong(tag) = a.ping(tag) }
}

component Gamma requires b: B provides c: C {
  provide c { fn peng(tag) = b.pong(tag) }
}
"""


def test_g3_three_component_cycle_path():
    error = _reject(THREE_COMPONENT_CYCLE)
    assert error.message == "dependency cycle: Alpha -> Beta -> Gamma -> Alpha (G3)"
    why = error.why
    assert why.kind == "dependency-cycle"
    assert why.shape == CHAIN
    assert why.path() == ["Alpha", "Beta", "Gamma", "Alpha"]


def test_g3_cycle_steps_name_the_key_that_carries_each_edge():
    steps = _reject(THREE_COMPONENT_CYCLE).why.steps
    assert [step.detail for step in steps] == [
        "provides `a`", "provides `b`", "provides `c`", None]
    assert [step.line for step in steps] == [5, 9, 13, 5]
    assert all(step.kind == "component" for step in steps)


def test_g3_cycle_renders_the_path_and_the_locations():
    rendered = str(_reject(THREE_COMPONENT_CYCLE))
    assert "why `Alpha` is in a dependency cycle:" in rendered
    assert "Alpha -> Beta -> Gamma -> Alpha" in rendered
    assert "Beta   why.rvl:9  provides `b`" in rendered


def test_g3_self_provision_is_also_explained():
    error = _reject("""service S { fn f(a: Str) -> Str }

component Ouroboros requires s: S provides s: S {
  provide s { fn f(a) = a }
}
""")
    assert "requires a key it provides itself" in error.message
    assert error.why.path() == ["Ouroboros"]
    assert error.why.steps[0].detail == "provides and requires `s`"


# ------------------------------------------------------------ G2 conflict

TWO_PROVIDERS = """service Database { fn query(sql: Str) -> Str }

component PgDatabase provides db: Database {
  provide db { fn query(sql) = sql }
}

component SqliteDatabase provides db: Database {
  provide db { fn query(sql) = sql }
}
"""


def test_g2_conflict_names_both_providers_and_both_locations():
    error = _reject(TWO_PROVIDERS)
    why = error.why
    assert why.kind == "provision-conflict"
    assert why.subject == "db"
    # a conflict is a pair of exhibits, not a path: no arrow rendering
    assert why.shape == SET
    assert [step.name for step in why.steps] == ["PgDatabase", "SqliteDatabase"]
    assert [step.line for step in why.steps] == [3, 7]
    assert all(step.file == "why.rvl" for step in why.steps)
    assert all(step.detail == "provides `db`" for step in why.steps)


def test_g2_conflict_renders_both_sides():
    rendered = str(_reject(TWO_PROVIDERS))
    assert "why `db` has more than one provider:" in rendered
    assert "PgDatabase      why.rvl:3  provides `db`" in rendered
    assert "SqliteDatabase  why.rvl:7  provides `db`" in rendered
    assert "->" not in rendered.split("why `db`")[1]


def test_g2_realm_conflict_detail_names_the_realm():
    error = _reject("""service KV { fn get(k: Str) -> Str }

component StoreOne provides kv: KV {
  isolate kv in realm("tenant_a")
  provide kv { fn get(k) = k }
}

component StoreTwo provides kv: KV {
  isolate kv in realm("tenant_a")
  provide kv { fn get(k) = k }
}
""")
    assert all(step.detail == "provides `kv` in realm `tenant_a`"
               for step in error.why.steps)


# ------------------------------------------------------- structured output

def test_json_diagnostics_carry_the_trace():
    record = report(_reject(MULTI_HOP_G4))["diagnostics"][0]
    assert record["code"] == "G4"
    why = record["why"]
    assert why["kind"] == "emission-propagation"
    assert why["path"] == ["put", "write_through", "audit_log", "audit_write"]
    assert why["steps"][0] == {
        "name": "put", "kind": "provide-method", "file": "why.rvl",
        "line": 18, "detail": "provision `cache`",
    }
    # the whole diagnostic must survive a JSON round trip (MCP transport)
    assert json.loads(json.dumps(record)) == record


def test_a_set_shaped_trace_has_no_path_key():
    why = report(_reject(TWO_PROVIDERS))["diagnostics"][0]["why"]
    assert why["shape"] == "set"
    assert "path" not in why


def test_rejections_without_a_search_carry_no_trace():
    """A trace is evidence for a *derived* verdict; a direct one needs none."""
    error = _reject("""service S { fn f(a: Str) -> Str }

component C provides s: S {
  provide s { fn f(a) = missing(a) }
}
""")
    assert error.why is None
    assert "why" not in report(error)["diagnostics"][0]


def test_mcp_check_passes_the_trace_to_agents():
    from revl.mcp.server import _tool_check

    result = _tool_check({"source": MULTI_HOP_G4})
    assert result["ok"] is False
    assert result["diagnostics"][0]["why"]["path"][-1] == "audit_write"


# ------------------------------------------------------ trace value object

def test_render_of_an_unlocated_chain_keeps_the_arrow_line_only():
    trace = WhyTrace(kind="emission-propagation", subject="S.m",
                     steps=[TraceStep("m"), TraceStep("boom", detail="emission")])
    assert render(trace) == "  why `S.m` is emission:\n    m -> boom   (emission)"


def test_render_of_no_trace_is_empty():
    assert render(None) == ""
    assert render(WhyTrace(kind="dependency-cycle", subject="X", steps=[])) == ""


def test_unknown_trace_kind_still_renders():
    """`kind` is open so a later analysis can add one without edits here."""
    trace = WhyTrace(kind="capability-widening", subject="X",
                     steps=[TraceStep("X", file="a.rvl", line=2)])
    assert "why `X` was rejected:" in render(trace)


def test_a_step_can_carry_the_capabilities_it_reached():
    """G4's emission set is becoming a set of capabilities rather than a
    boolean. A step already has somewhere to put them, and both renderings
    already show them — so that analysis needs no change here."""
    step = TraceStep("audit_write", "emission", "a.rvl", 1, "emission",
                     ["net:write", "fs:append"])
    assert step.capabilities == ("net:write", "fs:append")
    assert step.label() == "emission [net:write, fs:append]"
    assert step.to_json()["capabilities"] == ["net:write", "fs:append"]

    trace = WhyTrace(kind="emission-propagation", subject="S.m",
                     steps=[TraceStep("m"), step])
    assert "m -> audit_write   (emission [net:write, fs:append])" in render(trace)


def test_capabilities_are_absent_while_the_analysis_is_a_boolean():
    """Today they must not appear — a boolean emission set knows no more."""
    step = _reject(MULTI_HOP_G4).why.steps[-1]
    assert step.capabilities == ()
    assert "capabilities" not in step.to_json()


def test_steps_are_immutable():
    trace = WhyTrace(kind="dependency-cycle", subject="X", steps=[TraceStep("X")])
    assert isinstance(trace.steps, tuple)
    with pytest.raises(Exception):
        trace.steps = ()


# ------------------------------------------------------------ revl explain

def test_explain_every_guarantee_has_a_fix():
    assert set(FIXES) <= set(GUARANTEES)
    for code in GUARANTEES:
        record = explain(code)
        assert record["ok"] and record["guarantee"]
        assert record["fix"], f"{code} has no fix line"


def test_explain_is_case_insensitive_and_lists_on_a_miss():
    assert explain("g4") == explain("G4")
    miss = explain("G99")
    assert miss["ok"] is False
    assert "G4" in miss["known"]


def test_explain_cli(capsys):
    from revl.__main__ import main

    assert main(["explain", "g3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("G3  acyclic dependencies")
    assert "fix:" in out

    assert main(["explain", "G4", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["code"] == "G4"

    assert main(["explain", "nope"]) == 1


# ----------------------------------------------- provenance across modules

def test_trace_locations_point_at_the_declaring_file(tmp_path):
    """A hop into an imported module names *that* module's file, not the
    root the compile started from."""
    (tmp_path / "audit.rvl").write_text(
        "pub extern emission fn audit_write(msg: Str) -> Int = @py { return 1 }\n")
    root = tmp_path / "main.rvl"
    root.write_text("""use "./audit.rvl" { audit_write }

service Cache { fn put(key: Str) }

component LyingCache provides cache: Cache {
  provide cache {
    fn put(key) {
      let n = audit_write(key)
    }
  }
}
""")
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(root)])
    steps = excinfo.value.why.steps
    assert steps[0].file.endswith("main.rvl")
    assert steps[-1].name == "audit_write"
    assert steps[-1].file.endswith("audit.rvl")
    assert steps[-1].line == 1
