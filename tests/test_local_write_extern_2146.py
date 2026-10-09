"""`local` externs — roadmap item 2146: a durable write to local state.

The gap this closes: G4's classification had no word for "this mutates local
state" as distinct from "this leaves the system". Before this item the only
shape that compiled for a local durable write was an `acquire` extern with a
no-argument `pure` teardown, spelled at the site as

    effect w(path, text) undo settled()

which (a) misreads a single open-write-close as an acquisition of a handle held
across calls, and (b) is RELAYED rather than classified — it raised no approval
request, so nothing pended, and it left no record of the write either, because
`acquire` is not a boundary crossing. An audit of crossings therefore did not
see it at all.

`local` is the fifth class: it is RECORDED (named by `revl audit`, listed in
the erase report) without being a CROSSING (it never asks an owner, never
pends, and never enters `crossings()`). Four things must hold together, and
each has its own section below:

  1. the class parses, checks and lowers, and a plain provide method may reach
     it (the only site it is reachable from),
  2. it is NOT a crossing — no approval class, no relay, no `crossings()` token,
  3. it IS recorded — `revl audit` names the class and the erase report gives it
     its own bucket outside the compensated/bare counts,
  4. the four shapes that must stay refused are still refused, and the two
     positions where a `local` call is legitimate stay accepted.

All sources are inline: a new `tests/fixtures/*.rvl` under a `CORPUS_DIRS`
prefix would be a moved input for the census artifact
(tools/census_artifact.py `moved_inputs`), and nothing here needs a fixture on
disk.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl import erase_report  # noqa: E402
from revl import query  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402
from revl.__main__ import main  # noqa: E402

SCOPE = "W:j.append"

_WRITE = 'extern local fn record(p: Str) -> Unit = @py { return None }\n'

# The write is reached from a provide-method body — the ONE position a `local`
# call is legitimate. An activation body cannot hold a bare plain call at all
# ("expected a statement (`let`, `effect`, `emit`, `fail`, `if`, `provide`)").
_CORPUS = (
    'service Journal { fn append(n: Str) -> Unit }\n'
    'component W provides j: Journal {\n'
    '  isolate j in realm("r")\n'
    '  provide j {\n'
    '    fn append(n) = record("r")\n'
    '  }\n'
    '}\n'
)


def _ir(tmp_path, body: str = _CORPUS, extern: str = _WRITE) -> dict:
    return compile_source(extern + body, str(tmp_path / "local_2146.rvl"))


def _record(ir: dict) -> dict:
    return next(e for e in ir["externs"] if e["name"] == "record")


def _refusal(source: str, tmp_path) -> RevlError:
    with pytest.raises(RevlError) as exc:
        compile_source(source, str(tmp_path / "refused.rvl"))
    return exc.value


# -- 1. the class parses, checks and lowers ---------------------------------

def test_parser_accepts_local_classification():
    prog = Parser(_WRITE, "t.rvl").parse()
    rec = next(e for e in prog.externs if e.name == "record")
    assert rec.classification == "local"


def test_local_is_a_contextual_keyword_not_reserved():
    # `local` is recognised only in the classification slot, so it stays a legal
    # ordinary identifier everywhere else and needs no self-host KEYWORDS sync.
    ir = compile_source("fn f(local: Int) -> Int { return local }", "t.rvl")
    assert ir["functions"][0]["name"] == "f"


def test_local_extern_lowers_with_no_teardown_and_no_scope():
    rec = _record(_ir(Path("/tmp")))
    assert rec["class"] == "local"
    assert rec.get("emission") is None
    assert rec.get("capabilities") is None
    assert rec.get("deferred") is None
    assert rec.get("undo") is None
    assert rec.get("compensate") is None


def test_a_plain_provide_method_may_reach_a_local_extern(tmp_path):
    ir = _ir(tmp_path)
    scope = query.Composition(ir).scopes[SCOPE]
    assert [f["name"] for f in scope["facts"]["externs"]] == ["record"]


def test_a_local_call_in_a_fn_body_is_accepted():
    # Deliberate contrast with `acquire`/`witnessed`, which the effect-position
    # refusal rejects in a `fn` body. A `local` write declares no teardown, so
    # there is no bracket to be out of position.
    ir = compile_source(
        _WRITE + "fn save(p: Str) { record(p) }\n", "t.rvl")
    assert ir["functions"][0]["name"] == "save"


def test_a_local_call_in_a_test_body_is_accepted():
    ir = compile_source(
        _WRITE + 'test "t" { record("x") }\n', "t.rvl")
    assert ir["tests"][0]["name"] == "t"


# -- 2. it is NOT a crossing ------------------------------------------------

def test_the_class_map_gives_no_action_class_and_no_capability(tmp_path):
    # The crux of item 2146: the write is reached and recorded, but the per-call
    # decision sees no crossing, so nothing pends for owner approval.
    reach = ClassMap(_ir(tmp_path)).classify_call("j", "append")
    assert reach is not None
    assert reach["class"] is None
    assert reach["crossings"] == []
    assert reach["capabilities"] == set()
    assert reach["classC"] == set()


def test_the_reach_facts_do_not_call_it_an_emission(tmp_path):
    scope = query.Composition(_ir(tmp_path)).scopes[SCOPE]
    fact = next(f for f in scope["facts"]["externs"] if f["name"] == "record")
    assert fact["class"] == "local"
    assert not fact.get("emission")
    assert fact["deferred"] is False


def test_it_is_not_relayed(tmp_path):
    # An `acquire` write spelled `effect w(..) undo settled()` was RELAYED: the
    # map folded it to its target's class and nothing pended, so it was
    # invisible. A `local` extern is classified, not relayed.
    assert ClassMap(_ir(tmp_path)).relayed_emissions() == {}


def test_it_adds_no_crossing_token(tmp_path):
    # `crossings()` must stay byte-identical: it walks emit:/host:/taint:/
    # declassify:/secret:/env: tokens, and a `local` reach contributes only the
    # same `host:<c>:<name>` token a `pure` reach contributes.
    from revl.audit_diff import crossings
    ir = _ir(tmp_path)
    before = crossings(compile_source(
        'service Journal { fn append(n: Str) -> Unit }\n'
        'extern pure fn record(p: Str) -> Unit = @py { return None }\n'
        'component W provides j: Journal {\n'
        '  isolate j in realm("r")\n'
        '  provide j { fn append(n) = record("r") }\n'
        '}\n', str(tmp_path / "pure.rvl")))
    after = crossings(ir)
    assert [t for t in after if "record" in t] \
        == [t for t in before if "record" in t]
    assert not [t for t in after if t.startswith("emit:")]


def test_emission_analysis_does_not_see_it(tmp_path):
    from revl.lower import _emitting_capabilities
    ir = _ir(tmp_path)
    assert "record" not in _emitting_capabilities(
        ir.get("functions") or [], ir["externs"])


# -- 3. it IS recorded ------------------------------------------------------

def test_audit_json_names_the_local_class(tmp_path, capsys):
    src = tmp_path / "corpus.rvl"
    src.write_text(_WRITE + _CORPUS)
    assert main(["audit", str(src), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    externs = doc["boundary"]["W"]["externs"]
    assert {"name": "record", "class": "local", "backends": ["py"]} in externs


def test_audit_human_line_names_the_local_class(tmp_path, capsys):
    src = tmp_path / "corpus.rvl"
    src.write_text(_WRITE + _CORPUS)
    assert main(["audit", str(src)]) == 0
    out = capsys.readouterr().out
    assert "record (local, py)" in out


def test_erase_report_gives_it_its_own_bucket_outside_the_counts(tmp_path):
    report = erase_report.build_report(_ir(tmp_path), "r", prove_residue=False)
    cross = report["boundaryCrossings"]
    assert cross["local"] == [{
        "component": "W", "scope": "provide-method", "name": "record",
        "class": "local", "crossing": False, "token": "local:W:record"}]
    # not a crossing, so it is in none of the crossing buckets
    assert cross["total"] == 0
    assert cross["bareCount"] == 0
    assert cross["compensatedCount"] == 0
    assert not cross.get("bareTokens")
    assert not cross.get("compensableTokens")
    assert cross["externs"] == []
    # no (a)/(b)/(c) action class: those describe crossings
    assert "actionClass" not in cross["local"][0]


def test_erase_report_human_render_lists_the_local_write(tmp_path):
    report = erase_report.build_report(_ir(tmp_path), "r", prove_residue=False)
    text = erase_report.render(report)
    assert "[LOCAL]" in text
    assert "host record()" in text


def test_a_realm_with_no_local_write_omits_the_key(tmp_path):
    # Additive only: the key is present exactly when there is a local write, so
    # every pre-existing realm's report is byte-identical. This realm reaches a
    # `pure` host extern through the same provide-method shape.
    clean = compile_source(
        'extern pure fn record(p: Str) -> Unit = @py { return None }\n'
        'service Journal { fn append(n: Str) -> Unit }\n'
        'component W provides j: Journal {\n'
        '  isolate j in realm("r")\n'
        '  provide j { fn append(n) = record("r") }\n'
        '}\n', str(tmp_path / "clean.rvl"))
    cross = erase_report.build_report(
        clean, "r", prove_residue=False)["boundaryCrossings"]
    assert "local" not in cross


# -- 4. the refused shapes and the accepted positions -----------------------

def test_local_cannot_declare_undo(tmp_path):
    err = _refusal(
        'extern pure fn settled() -> Unit = @py { return None }\n'
        'extern local fn record(p: Str) -> Unit undo settled()'
        ' = @py { return None }\n', tmp_path)
    assert "cannot declare `undo` or `compensate`" in err.message
    assert err.code == "G4"


def test_local_cannot_declare_compensate(tmp_path):
    err = _refusal(
        'extern pure fn settled() -> Unit = @py { return None }\n'
        'extern local fn record(p: Str) -> Unit compensate settled()'
        ' = @py { return None }\n', tmp_path)
    assert "cannot declare `undo` or `compensate`" in err.message
    assert err.code == "G4"


def test_local_cannot_be_deferred(tmp_path):
    err = _refusal(
        'extern local deferred fn record(p: Str) -> Unit'
        ' = @py { return None }\n', tmp_path)
    assert "is only valid on an `emission` extern" in err.message
    assert "`record` is `local`" in err.message


def test_local_takes_no_capability_scope(tmp_path):
    err = _refusal(
        'extern local[fs] fn record(p: Str) -> Unit = @py { return None }\n',
        tmp_path)
    assert "takes no capability scope" in err.message


def test_the_acquire_workaround_is_now_refused_with_a_pointed_message(tmp_path):
    # Shape 4 from the issue: the only shape that used to compile for a local
    # durable write. It is refused now, and the refusal names the class.
    err = _refusal(
        _WRITE + _CORPUS.replace('fn append(n) = record("r")',
                                 'fn append(n) { effect record("r") }'),
        tmp_path)
    assert "is a `local` extern" in err.message
    assert "plain call" in err.hint


def test_an_unclassified_extern_hint_names_local(tmp_path):
    """The MESSAGE enumeration is gate-mirrored (selfhost/lower.rvl) and so is
    still the gate's four; the HINT is the reference's own field and names
    `local`, which is what tells the author the class exists."""
    err = _refusal("extern fn w(p: Str) -> Unit = @py { return None }\n",
                   tmp_path)
    assert "unclassified extern" in err.message
    assert "`local`" in err.hint
    assert "`local`" not in err.message


# -- 5. not read-only, not pure ----------------------------------------------
#
# `local` is not a crossing, but it is a durable write that nothing reverts.
# Two surfaces used to read "not a crossing" as "no effect": the MCP tool hints
# and the `cache pure` admission.

def test_an_mcp_tool_reaching_a_local_write_is_not_read_only(tmp_path):
    from revl.mcp.schema import tools_from_ir
    tool = tools_from_ir(_ir(tmp_path))[0]
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["x-revl"]["effects"]["reachesLocalWrite"] == ["record"]
    assert "Local durable write" in tool["description"]
    assert "refused any unreverted mutation" not in tool["description"]


def test_an_mcp_tool_reaching_only_a_pure_extern_keeps_its_hints(tmp_path):
    # the control: same shape, `pure` extern, unchanged annotations and bytes
    from revl.mcp.schema import tools_from_ir
    tool = tools_from_ir(_ir(tmp_path, extern=_WRITE.replace("local", "pure")))[0]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert "reachesLocalWrite" not in tool["x-revl"]["effects"]
    assert "Read-only" in tool["description"]


def test_cache_pure_on_a_fn_reaching_a_local_write_is_refused(tmp_path):
    err = _refusal(
        _WRITE + 'fn sq(x: Str) -> Unit cache pure { return record(x) }\n',
        tmp_path)
    assert err.code == "G4"
    assert "writes through the `local` extern `record`" in err.message
    assert "durable write is not pure" in err.message


def test_cache_pure_on_a_seam_method_reaching_a_local_write_is_refused(tmp_path):
    from revl.mcp.approval import cache_applicability_refusal
    from revl.mcp.session import Session
    ir = _ir(tmp_path, body=(
        'service S { fn get(p: Str) -> Unit cache pure }\n'
        'component C provides s: S { provide s { fn get(p) = record(p) } }\n'))
    index = Session._build_cache_index(Session.__new__(Session), ir)
    problem = cache_applicability_refusal(ClassMap(ir), index)
    assert problem is not None
    assert "reaches the `local` extern `record`" in problem


# The routes a `local` write takes past the direct call. A service declaration
# bounds its providers' EMISSIONS, not their `local` writes, and a callable
# handed on as a value reaches whatever it names.

_APPLY = 'fn apply(f: (Str) -> Unit, x: Str) -> Unit { return f(x) }\n'


def _tool(ir: dict, name: str) -> dict:
    from revl.mcp.schema import tools_from_ir
    return next(t for t in tools_from_ir(ir) if t["name"] == name)


def test_an_mcp_tool_calling_a_required_op_that_writes_locally_is_not_read_only(
        tmp_path):
    ir = _ir(tmp_path, body=(
        'service Store { fn save(n: Str) -> Unit }\n'
        'service Journal { fn append(n: Str) -> Unit }\n'
        'component S provides st: Store { provide st { fn save(n) = record(n) } }\n'
        'component W requires st: Store provides j: Journal {\n'
        '  provide j { fn append(n) = st.save(n) }\n'
        '}\n'))
    tool = _tool(ir, "revl.j.append")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["x-revl"]["effects"]["reachesLocalWrite"] == ["record"]
    assert "Local durable write" in tool["description"]


def test_an_mcp_tool_handing_a_local_extern_on_as_a_value_is_not_read_only(
        tmp_path):
    ir = _ir(tmp_path, body=_APPLY + _CORPUS.replace(
        'fn append(n) = record("r")', 'fn append(n) = apply(record, n)'))
    tool = _tool(ir, "revl.j.append")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["x-revl"]["effects"]["reachesLocalWrite"] == ["record"]


def test_cache_pure_on_a_seam_method_handing_a_local_extern_on_is_refused(
        tmp_path):
    from revl.mcp.approval import cache_applicability_refusal
    from revl.mcp.session import Session
    ir = _ir(tmp_path, body=_APPLY + (
        'service S { fn get(p: Str) -> Unit cache pure }\n'
        'component C provides s: S { provide s { fn get(p) = apply(record, p) } }\n'))
    index = Session._build_cache_index(Session.__new__(Session), ir)
    problem = cache_applicability_refusal(ClassMap(ir), index)
    assert problem is not None
    assert "reaches the `local` extern `record`" in problem


def test_cache_pure_on_a_fn_handing_a_local_extern_on_names_it(tmp_path):
    err = _refusal(
        _WRITE + _APPLY
        + 'fn sq(x: Str) -> Unit cache pure { return apply(record, x) }\n',
        tmp_path)
    assert "writes through the `local` extern `record`" in err.message
    assert "`*`" not in err.message
