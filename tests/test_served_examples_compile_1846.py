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
    # issue #1857: a `Unit` method's fill is the crossing it makes
    "unit emission": {"service": "Audit", "requires": ["sink"],
                      "capabilities": ["sink"],
                      "emits": ["record(line: Str) -> Unit"]},
    "unit split emission": {"service": "Relay", "requires": ["db", "net"],
                            "capabilities": ["db", "net"],
                            "emits": ["push(item: Str) -> Unit"]},
}

# What the scaffold's `// TODO: declare the operations X must offer.` asks the
# agent to write first: the operation a crossing goes through. Only a `Unit`
# method needs it, since a `Str` crossing hole has a literal fill.
STUB_OPERATIONS = {
    "unit emission": {"Sink": "emission fn write(line: Str)"},
    "unit split emission": {"Db": "emission fn put(item: Str)",
                            "Net": "emission fn send(item: Str) -> Unit"},
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


@pytest.mark.parametrize("name", list(SPECS))
def test_a_served_scaffold_compiles_with_its_holes_minimally_filled(name):
    served = _call("revl_scaffold", SPECS[name])
    assert served["ok"] is True, served
    source = served["source"]
    for service, operation in STUB_OPERATIONS.get(name, {}).items():
        stub = f"service {service} {{ }}"
        assert stub in source, stub
        source = source.replace(stub, f"service {service} {{\n  {operation}\n}}")
    filled = _fill_minimally(source)
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


def test_a_pure_unit_method_is_refused_with_the_reason():
    """A pure operation that returns `Unit` has no fill (revl has no unit
    value) and computes nothing a caller can see, so the scaffold says so
    instead of writing a hole nothing can fill (#1857)."""
    refused = _call("revl_scaffold", {"service": "Log",
                                      "methods": ["note(a: Str) -> Unit"]})
    assert refused["ok"] is False
    message = refused["diagnostics"][0]["message"]
    assert "`note` returns `Unit` and is pure" in message
    assert "--emits" in message


# ---------------------------------- a write on the component's own resource

# issue #1948: the third case of the `Unit` bullet — a `Unit` method that
# writes the resource its own component acquired, the shape of
# `fn seed(k) = data.insert(k, 1)` in examples/verified_effect.rvl. `Map` is
# the one host family whose operations return nothing, so it is the resource
# such a method writes.
STORE = {"service": "Audit", "provides": "audit", "resource": "Map[Str, Str]",
         "methods": ["record(msg: Str) -> Unit"]}


def test_a_unit_method_that_writes_its_own_resource_is_scaffolded():
    """The method is not pure: a later read sees what it wrote, and its fill is
    a call on the resource the component acquired itself, which is in scope for
    its own method (the way `lower.py` admits the owner's own handle)."""
    served = _call("revl_scaffold", STORE)
    assert served["ok"] is True, served
    obligations = enrich(compile_source(served["source"], "audit.rvl"))
    record = [ob for ob in obligations
              if ob["fillSpec"]["construct"] == "provide-method"]
    assert len(record) == 1  # one obligation on `record`, and it is not refused
    spec = record[0]["fillSpec"]
    # the resource is in scope for the method that writes it
    assert {"name": "resource", "type": "Map[Str, Str]"} in spec["bindings"]
    # and the resource's operations that return nothing are what it offers
    assert [p["write"] for p in spec["fillable"]["producers"]] == [
        "resource.insert(<str_0: Str>, <str_1: Str>)",
        "resource.remove(<str_0: Str>)"]
    # the boundary, unchanged: a `Unit` method whose spec acquires no resource
    # has nothing in scope that returns `Unit`, so the #1857 refusal stands
    refused = _call("revl_scaffold", {**STORE, "effect": False})
    assert refused["ok"] is False
    assert "`record` returns `Unit` and is pure" in \
        refused["diagnostics"][0]["message"]
    # the issue's own reproduction line, served: a declared resource whose type
    # names no host family lifts the refusal too, and no producer is invented
    # for it — nothing it offers writes it and returns nothing
    plain = _call("revl_scaffold", {**STORE, "resource": "AuditResource"})
    assert plain["ok"] is True, plain
    method = next(ob["fillSpec"] for ob in
                  enrich(compile_source(plain["source"], "plain.rvl"))
                  if ob["fillSpec"]["construct"] == "provide-method")
    assert {"name": "resource", "type": "AuditResource"} in method["bindings"]
    assert method["fillable"]["producers"] == []
    assert "resource this component acquired (`resource`)" in \
        method["fillable"]["reason"]


def test_the_unit_method_fills_with_a_write_on_the_resource_and_admits():
    """Filling that obligation with the resource write it offers compiles with
    no holes, and the filled component is admissible."""
    served = _call("revl_scaffold", STORE)
    assert served["ok"] is True, served
    source = served["source"]
    record = next(ob["fillSpec"] for ob in enrich(compile_source(source))
                  if ob["fillSpec"]["construct"] == "provide-method")
    fill = _concrete(record["fillable"]["producers"][0]["write"],
                     record["bindings"])
    assert fill == "resource.insert(msg, msg)"
    filled = re.sub(r'hole\[Unit\] "produce record\'s Unit result[^"]*"',
                    fill, source, count=1)
    assert fill in filled  # the substitution landed on the method's hole
    # the acquisition and its inverse, as `Map.new`/`Map.drop` declare them
    filled = filled.replace(
        'effect hole[Map[Str, Str]] "acquire the resource AuditProvider manages"',
        "effect Map.new()", 1).replace(
        'undo hole[Unit] "release `resource` fully (no residue): the inverse '
        'its acquisition declares"', "undo resource.drop()", 1)
    assert not compile_source(filled, "filled.rvl").get("holes")
    # and the write it filled is admitted, not merely parsed: the runtime gate
    # refuses a write on a resource the component did not acquire, and this
    # one is the component's own
    compile_source(filled, "candidate.rvl", manifest=compile_source(""))
