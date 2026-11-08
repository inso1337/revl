"""AgentDojo port: the tool-to-service mapping and the admitted-run crossing.

These tests do not need AgentDojo installed: the mapping is exercised on
stand-in tool objects with the same pydantic-shaped interface. The execution
test is skipped where cordis is absent, like the other live-runtime tests.

The property that matters is not that the generated source is pretty. It is
that every suite tool appears in the base composition as a service, that the
grant the driver passes is exactly that set, and that a turn admitted against
it runs through the host bridge and calls the same tool name and arguments the
baseline pipeline would have called.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "agentdojo"
sys.path.insert(0, str(ROOT / "src"))

pkg_spec = importlib.util.spec_from_file_location(
    "bench_agentdojo", BENCH / "__init__.py", submodule_search_locations=[str(BENCH)]
)
pkg = importlib.util.module_from_spec(pkg_spec)
sys.modules.setdefault("bench_agentdojo", pkg)
pkg_spec.loader.exec_module(pkg)

from bench_agentdojo import bridge, services  # noqa: E402


class FakeField:
    def __init__(self, annotation):
        self.annotation = annotation


class FakeTool:
    def __init__(self, name, **fields):
        self.name = name
        self.parameters = types.SimpleNamespace(
            model_fields={k: FakeField(v) for k, v in fields.items()}
        )


SEND_MONEY = FakeTool(
    "send_money",
    recipient=str,
    amount=float,
    subject=str,
    date=str,
)


def test_revl_type_maps_the_suite_annotations_the_port_uses():
    assert services.revl_type(str) == "Str"
    assert services.revl_type(int) == "Int"
    assert services.revl_type(float) == "Float"
    assert services.revl_type(bool) == "Bool"
    assert services.revl_type(list[str]) == "List[Str]"
    assert services.revl_type(str | None) == "Opt[Str]"
    assert services.revl_type(list[str] | None) == "Opt[List[Str]]"


def test_declarations_name_every_tool_and_grant_its_service():
    declared = services.declarations([SEND_MONEY])
    assert declared["granted"] == ["Tool_send_money"]
    assert "service Tool_send_money" in declared["source"]
    assert "provides tool_send_money: Tool_send_money" in declared["source"]
    assert 'send_money"' in declared["source"]
    assert declared["signatures"]["send_money"] == (
        "call(recipient: Str, amount: Float, subject: Str, date: Str) -> Str"
    )


def test_generated_base_compiles_and_provides_each_granted_key():
    from revl import compile_files

    declared = services.declarations([SEND_MONEY])
    path = str(BENCH.resolve() / "<test-base>.rvl")
    ir = compile_files([path], sources={path: declared["source"]})
    provided = {
        key
        for component in ir["components"]
        for key in (component.get("provides") or {})
    }
    assert "tool_send_money" in provided


needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the admitted-run crossing needs a live cordis composition",
)


@needs_cordis
def test_admitted_turn_reaches_the_bridge_and_calls_the_suite_tool():
    from revl import compile_files
    from revl.mcp.session import Session

    declared = services.declarations([SEND_MONEY])
    path = str(BENCH.resolve() / "<test-base>.rvl")
    ir = compile_files([path], sources={path: declared["source"]})
    session = Session()
    session.load(ir, record=False)
    turn = (
        "service Turn { fn run() -> Str }\n"
        "component StepTurn requires tool_send_money: Tool_send_money provides turn: Turn {\n"
        '  provide turn { fn run() = tool_send_money.call("bob", 10.0, "rent", "2024-01-02") }\n'
        "}\n"
    )
    verdict = session.admit(turn, granted=declared["granted"])
    assert verdict.admitted, verdict.message

    calls = []

    class Runtime:
        def run_function(self, env, name, kwargs):
            calls.append((name, kwargs))
            return {"paid": kwargs["amount"]}, None

    sys.modules["revl_agentdojo_bridge"] = bridge
    bridge.install(Runtime(), {})
    out = verdict.handle.call("turn", "run", [])
    assert "paid" in out["result"]
    assert calls == [
        (
            "send_money",
            {"recipient": "bob", "amount": 10.0, "subject": "rent", "date": "2024-01-02"},
        )
    ]
    assert bridge.taken()[0]["function"] == "send_money"
