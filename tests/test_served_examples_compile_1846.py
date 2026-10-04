"""Every example the MCP serves compiles (issue #1846).

An agent copies what the server hands it. `revl_grammar` served
`let r = effect acquire() undo r.release()`, which does not parse, and
`revl_scaffold` wrote `undo resource.release()` under a typed hole. That
skeleton checked as a draft only because the hole hid the call: once the hole
was filled the compile failed, and the only way back to green was deleting a
line the scaffold had written. So this file compiles what each door serves, as
it is served:

* the code of the `revl_grammar` summary, as one program;
* `revl_scaffold` output for a spread of specs, with every hole filled by the
  minimal fill its own fill spec offers, one hole at a time, the way an agent
  fills them. A hole whose type only host code builds gets the extern its spec
  asks for, declared by a trusted author;
* every `revl_idiom` example (the same text every fill spec carries);
* the scaffold example in docs/scaffold.md, which must be what the server
  writes for that spec.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.fillspec import enrich  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------ the grammar summary


def _summary_code(served: str) -> str:
    """The program in the summary: everything between the title paragraph and
    the closing prose paragraph."""
    body = served.split("\n\n", 1)[1]
    return body.split("\nRules that reject code:", 1)[0]


def test_the_grammar_summary_compiles_as_served():
    served = _call("revl_grammar", {})["grammar"]
    code = _summary_code(served)
    assert "effect" in code and "undo" in code
    compile_source(code, "revl_grammar.rvl")


# ------------------------------------------------ scaffolds, minimally filled

SPECS = {
    "default": {"service": "Cache"},
    "wired emission": {"service": "Analysis", "provides": "analysis",
                       "requires": ["filesystem"],
                       "capabilities": ["filesystem.read"],
                       "emits": ["run(input: Str) -> Str"]},
    "split emission": {"service": "Sync", "requires": ["db", "net"],
                       "capabilities": ["db", "net"],
                       "emits": ["push(item: Str) -> Str"]},
    "config and methods": {"service": "Rates",
                           "methods": ["rate(code: Str) -> Int",
                                       "known(code: Str) -> Bool"],
                           "config": ["base: Int"]},
    "named resource": {"service": "Pool", "resource": "Socket"},
    "no effect": {"service": "Echo", "effect": False},
}

_PLACEHOLDER = re.compile(r"<(\w+): ([^>]+)>")
_LITERAL = {"Str": '"..."', "Int": "0", "Bool": "false"}


def _concrete(write: str, bindings: list[dict]) -> str | None:
    """A producer's `write` with its `<name: Type>` placeholders bound, or
    None when it is prose rather than code or a placeholder cannot be bound."""
    if "`" in write or "->" in write or ": ..." in write or " | " in write:
        return None

    def bind(match):
        wanted = match.group(2)
        named = [b["name"] for b in bindings if b.get("type") == wanted]
        if named:
            return named[0]
        if wanted in _LITERAL:
            return _LITERAL[wanted]
        raise LookupError(wanted)

    try:
        return _PLACEHOLDER.sub(bind, write)
    except LookupError:
        return None


def _host_code(handle_type: str) -> str:
    """What a trusted author declares for a handle only host code builds: an
    `acquire` extern returning it, and the inverse it names over `result`."""
    stem = handle_type.lower()
    return (f"extern pure fn close_{stem}(r: {handle_type}) = @py {{ return None }}\n"
            f"extern acquire fn open_{stem}() -> {handle_type} "
            f"undo close_{stem}(result) = @py {{ return 1 }}\n")


def _fill_minimally(source: str) -> str:
    prelude = ""
    for _ in range(32):
        obligations = enrich(compile_source(prelude + source, "filled.rvl"))
        if not obligations:
            return prelude + source
        ob = obligations[0]
        spec = ob["fillSpec"]
        fills = [_concrete(p["write"], spec["bindings"])
                 for p in spec["fillable"]["producers"]]
        fill = next((f for f in fills if f is not None), None)
        if fill is None and spec["fillable"]["needsHostCode"]:
            assert spec["externs"]["mayDeclare"], spec["externs"]
            prelude += _host_code(spec["expected"])
            continue
        assert fill is not None, (
            f"line {ob['line']}: the fill spec offers no minimal fill for "
            f"hole[{spec['expected']}] ({spec['fillable']})")
        hole = f'hole[{spec["expected"]}] "{ob["message"]}"'
        assert hole in source, hole
        source = source.replace(hole, fill, 1)
    raise AssertionError("the holes did not run out")


@pytest.mark.parametrize("arguments", list(SPECS.values()), ids=list(SPECS))
def test_a_served_scaffold_compiles_with_its_holes_minimally_filled(arguments):
    served = _call("revl_scaffold", arguments)
    assert served["ok"] is True, served
    filled = _fill_minimally(served["source"])
    ir = compile_source(filled, "filled.rvl")
    assert not ir.get("holes")


def test_the_effect_undo_names_the_acquired_value():
    """The inverse's fill spec has the acquired binding in scope, and offers
    the inverse the acquisition's extern declares, applied to it."""
    source = _host_code("Socket") + _call(
        "revl_scaffold", SPECS["named resource"])["source"]
    source = source.replace('hole[Socket] "acquire the resource PoolProvider manages"',
                            "open_socket()", 1)
    undo = next(ob for ob in enrich(compile_source(source))
                if ob["fillSpec"]["construct"] == "effect-undo")
    spec = undo["fillSpec"]
    assert {"name": "resource", "type": "Socket"} in spec["bindings"]
    writes = [p["write"] for p in spec["fillable"]["producers"]]
    assert writes == ["close_socket(<r: Socket>)"]


def test_an_acquire_extern_is_offered_as_the_bare_call_in_the_acquisition_slot():
    source = _host_code("Socket") + _call(
        "revl_scaffold", SPECS["named resource"])["source"]
    acquire = next(ob for ob in enrich(compile_source(source))
                   if ob["fillSpec"]["construct"] == "effect-acquire")
    writes = [p["write"] for p in acquire["fillSpec"]["fillable"]["producers"]]
    assert writes == ["open_socket()"]


# ------------------------------------------------ idioms


def _idiom_names() -> list[str]:
    return [entry["name"] for entry in _call("revl_idiom", {})["idioms"]]


@pytest.mark.parametrize("name", _idiom_names())
def test_every_served_idiom_compiles(name):
    served = _call("revl_idiom", {"name": name})["idiom"]
    compile_source(served["example"], f"{name}.rvl")


# ------------------------------------------------ the docs' scaffold example


def test_the_scaffold_doc_shows_what_the_server_writes():
    doc = (ROOT / "docs" / "scaffold.md").read_text(encoding="utf-8")
    shown = doc.split("writes `csv_analyzer.rvl`:\n\n```revl\n", 1)[1].split("```", 1)[0]
    served = _call("revl_scaffold", {
        "service": "Analysis", "provides": "analysis", "requires": ["filesystem"],
        "capabilities": ["filesystem.read"]})["source"]
    assert shown == served
