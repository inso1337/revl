"""The served idiom for each construct (issue #1701).

Exit tests from the issue:

1. every construct a fillSpec can name has an idiom entry;
2. every idiom entry compiles and admits;
3. a fillSpec carries the idiom for its hole.

The idioms are `.rvl` files under src/revl/idioms/, so the round trip below is
what keeps the table and the fillSpec from drifting apart: with an idiom's
internal `fill` marker replaced by a typed hole, the fillSpec of that hole
names that very construct and serves that very idiom.

The served name for that marker is `exampleExpression`, not `fill` (issue
#2115): it is the expression position inside the example, so it carries the
example component's own names and is refused if submitted as a fill. Section 6
pins the name on all three doors.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source, idioms  # noqa: E402
from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.mcp import fillspec  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

ENTRIES = idioms.table()


def _compile(source, name, profile=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compile_source(source, f"{name}.rvl", profile=profile)


def _call(tool, arguments):
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]


# ---- 1. every construct has an idiom ----------------------------------------

def test_every_construct_a_fill_spec_names_has_an_idiom():
    assert set(fillspec.CONSTRUCTS) <= set(ENTRIES)
    for construct in fillspec.CONSTRUCTS:
        assert "type" in ENTRIES[construct], construct


def test_the_idiom_files_are_well_formed():
    files = sorted(p.stem for p in idioms.IDIOM_DIR.glob("*.rvl"))
    assert files == idioms.names()
    assert len(files) >= len(fillspec.CONSTRUCTS) + 4
    for entry in ENTRIES.values():
        assert 1 <= len(entry["rules"]) <= 2, entry["name"]
        assert entry["fill"] in entry["example"]


# ---- 2. every idiom compiles and admits ---------------------------------------

@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_every_idiom_compiles_with_no_hole(name):
    ir = _compile(ENTRIES[name]["example"], name)
    assert not ir.get("holes")


@pytest.mark.parametrize("name", sorted(ENTRIES))
def test_every_idiom_admits_for_an_untrusted_author(name):
    """The profile the MCP server compiles agent text under, granted exactly
    the services the idiom declares. No idiom needs host code."""
    ir = _compile(ENTRIES[name]["example"], name)
    granted = set(ir.get("services") or {})
    _compile(ENTRIES[name]["example"], name,
             profile=AdmissionProfile.untrusted_author(granted))


# ---- 3. a fillSpec carries the idiom for its hole ------------------------------

@pytest.mark.parametrize("construct", fillspec.CONSTRUCTS)
def test_a_hole_at_the_construct_serves_its_idiom(construct):
    entry = ENTRIES[construct]
    ir = _compile(idioms.with_hole(entry), construct)
    obligations = fillspec.enrich(ir)
    assert len(obligations) == 1, obligations
    spec = obligations[0]["fillSpec"]
    assert spec["construct"] == construct
    assert spec["idiom"]["name"] == construct
    assert spec["idiom"]["example"] == entry["example"]
    assert spec["idiom"]["rules"] == entry["rules"]
    assert spec["expected"] == entry["type"]


def test_revl_check_delivers_the_idiom_with_each_hole():
    source = idioms.with_hole(ENTRIES["emission-method"])
    payload = _call("revl_check", {"source": source})["structuredContent"]
    assert [h["fillSpec"]["idiom"]["name"] for h in payload["holes"]] == ["emission-method"]


def test_an_emission_and_a_plain_method_get_different_idioms():
    source = (
        "service Db { emission fn put(k: Str, v: Str) -> Str\n"
        "  fn get(k: Str) -> Str }\n"
        "service Cache { emission[db] fn set(key: Str) -> Str\n"
        "  fn read(key: Str) -> Str }\n"
        "component C requires db: Db provides c: Cache {\n"
        "  let store = effect hole[Int] \"a\" undo hole[Unit] \"u\"\n"
        "  provide c {\n"
        "    fn set(key) = hole[Str] \"s\"\n"
        "    fn read(key) = hole[Str] \"r\"\n"
        "  }\n"
        "}\n")
    by_message = {o["message"]: o["fillSpec"]["construct"]
                  for o in fillspec.enrich(_compile(source, "mixed"))}
    assert by_message == {"a": "effect-acquire", "u": "effect-undo",
                          "s": "emission-method", "r": "provide-method"}


# ---- queryable by name ------------------------------------------------------

def test_the_mcp_tool_lists_and_serves_by_name():
    listed = _call("revl_idiom", {})["structuredContent"]
    assert [i["name"] for i in listed["idioms"]] == idioms.names()
    one = _call("revl_idiom", {"name": "spawn"})["structuredContent"]
    assert one["idiom"]["example"] == ENTRIES["spawn"]["example"]


def test_the_mcp_tool_refuses_an_unknown_name():
    result = _call("revl_idiom", {"name": "nope"})
    assert result["isError"] is True
    assert "no idiom named 'nope'" in result["structuredContent"]["diagnostics"][0]["message"]


def test_the_cli_prints_an_idiom():
    run = subprocess.run([sys.executable, "-m", "revl", "idiom", "effect-undo", "--json"],
                         capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["exampleExpression"] == "store.drop()"
    missing = subprocess.run([sys.executable, "-m", "revl", "idiom", "nope"],
                             capture_output=True, text=True, cwd=ROOT,
                             env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert missing.returncode == 2 and "no idiom named" in missing.stderr


# ---- 6. the served block is an example, not a fill (issue #2115) --------------

def test_the_served_expression_is_named_for_where_it_stands():
    """`served` never calls the marker a fill: the name says it is the
    expression position inside the example, and the internal marker is intact."""
    for entry in ENTRIES.values():
        block = idioms.served(entry)
        assert idioms.SERVED_EXPRESSION_KEY == "exampleExpression"
        assert block[idioms.SERVED_EXPRESSION_KEY] == entry["fill"]
        assert "fill" not in block, entry["name"]
        assert set(block) == {"name", "summary", "rules", "exampleExpression", "example"}


def test_every_door_serves_the_same_expression_name():
    """The three doors cannot disagree: fillSpec, `revl_idiom`, `revl idiom --json`."""
    hole = idioms.with_hole(ENTRIES["effect-undo"])
    payload = _call("revl_check", {"source": hole})["structuredContent"]
    idiom = payload["holes"][0]["fillSpec"]["idiom"]
    assert idiom["exampleExpression"] == ENTRIES["effect-undo"]["fill"]
    assert "fill" not in idiom

    served = _call("revl_idiom", {"name": "effect-undo"})["structuredContent"]["idiom"]
    assert served == idiom

    run = subprocess.run([sys.executable, "-m", "revl", "idiom", "effect-undo", "--json"],
                         capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == idiom


def test_the_served_expression_is_not_a_submittable_fill():
    """The substance of #2115, measured on the scaffold the issue used.

    The served expression names the *example* component's binding (`store`,
    `db`, `key`), so it is refused as a fill; the author-facing fills are the
    hole's `fillable.producers[].write`, which for the `Str` hole names the
    component's own parameter `msg`.
    """
    scaffold = _call("revl_scaffold", {
        "service": "Audit", "component": "AuditLog", "provides": "audit",
        "methods": ["record(msg: Str) -> Str"], "resource": "Map[Str, Str]",
    })["structuredContent"]
    by_construct = {o["fillSpec"]["construct"]: o["fillSpec"]
                    for o in scaffold["obligations"]}
    assert set(by_construct) == {"effect-acquire", "effect-undo", "provide-method"}

    # every hole serves the expression under the honest name, never `fill`
    for spec in by_construct.values():
        assert "fill" not in spec["idiom"]
        assert spec["idiom"]["exampleExpression"] == ENTRIES[spec["construct"]]["fill"]

    # the example's names are the ones the issue measured, not the author's
    assert by_construct["effect-undo"]["idiom"]["exampleExpression"] == "store.drop()"
    assert by_construct["provide-method"]["idiom"]["exampleExpression"] == "db.get(key)"

    # the fills offered to the author carry no example binding
    writes = [p["write"] for p in
              by_construct["provide-method"]["fillable"]["producers"]]
    assert "msg" in writes
    assert not [w for w in writes if "db" in w or "key" in w], writes
