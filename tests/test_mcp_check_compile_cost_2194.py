"""Issue #2194 (PERF-04): each `revl_check` compiled the candidate three times.

The check compiles what it was sent, and then the canonicaliser's
IR-equivalence gate compiled that same text again to compare it with the
formatted one. The check now hands its document to the gate, so a plain inline
source is compiled twice: once as sent, once formatted. These tests pin the
count and the reports; the timing is a PR-body measurement (37.3 -> 31.6 ms
CPU per check on `examples/model_store.rvl`).

The reuse is only sound where the check's compile is the compile the gate
would make for itself: inline `source`, no `modules` (the gate compiles the
main source without them) and no operator providers or realm bindings. The
other shapes keep the plain path, and the report must be identical to it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import revl.compiler as compiler  # noqa: E402
from revl.mcp import canonical  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

TERSE = ("service Clock { fn now() -> Int\n fn later(n: Int) -> Int }\n"
         "component FixedClock provides clock: Clock {\n"
         "  provide clock { fn now()=7 fn later(n)=n+1 }\n"
         "}\n")

MODULE_SOURCE = ('use "m.rvl" { S }\n'
                 'component C provides s: S { provide s { fn f(x) = x } }\n')
MODULES = {"m.rvl": "service S { fn f(x: Str) -> Str }\n"}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


def _count_compiles(monkeypatch) -> list:
    """Every `compile_source` call made by the check or the formatter's gate.

    `server` binds the function at import and the formatter imports it lazily,
    so both names are patched with the one counting wrapper."""
    calls = []
    real = compiler.compile_source

    def counting(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("source"))
        return real(*args, **kwargs)

    monkeypatch.setattr(compiler, "compile_source", counting)
    monkeypatch.setattr(server_mod, "compile_source", counting)
    return calls


def test_a_check_compiles_a_plain_inline_source_twice(monkeypatch):
    formatted = canonical.canonicalise(TERSE).text
    assert formatted != TERSE
    calls = _count_compiles(monkeypatch)
    result = _call("revl_check", {"source": TERSE})
    assert result["ok"] is True
    assert len(calls) == 2, calls
    assert calls[0] == TERSE                       # as sent
    assert calls[1] == formatted                   # formatted, once


def test_the_reused_document_leaves_the_canonical_report_unchanged(monkeypatch):
    """The gate compares the check's own compile of the sent text with the
    formatted one; the report a caller sees is the one the plain gate made."""
    result = _call("revl_check", {"source": TERSE})
    assert result["canonicalSource"] == canonical.canonicalise(TERSE).report()


def test_modules_keep_the_plain_gate_and_the_same_report():
    """The gate compiles the main source WITHOUT `modules`, so the check's
    compile (which has them) cannot stand in for it."""
    arguments = {"source": MODULE_SOURCE, "modules": MODULES}
    assert server_mod._gate_can_reuse(arguments) is False
    result = _call("revl_check", arguments)
    assert result["ok"] is True
    assert result["canonicalSource"] == canonical.prepare(arguments)[2]


def test_a_refused_candidate_still_reports_canonical_source(monkeypatch):
    """Nothing is stored on a refusal, but the report still describes the
    text, so the gate runs its own compile as before."""
    calls = _count_compiles(monkeypatch)
    result = _call("revl_check", {"source": "component X { fn }"})
    assert result["ok"] is False
    assert result["canonicalSource"]
    assert len(calls) == 3, calls


def test_a_hole_bearing_candidate_still_canonicalises():
    """`_check_as_sent` enriches the compiled document with fill specs before
    handing it to the gate (issue #2194). Enrichment must not mutate the
    document: the formatted candidate is compared against the compile, so a
    mutation would make every draft with a hole uncanonicalisable."""
    source = ("service S { fn f(x: Str) -> Str }\n"
              "component C provides s: S {\n"
              '  provide s { fn f(x) = hole[Str] "todo" }\n'
              "}\n")
    result = _call("revl_check", {"source": source})
    assert result["ok"] is True
    assert result["holes"], result
    assert result["canonicalSource"] == canonical.canonicalise(source).report()


def test_the_profile_does_not_change_the_compiled_document():
    """The check compiles under `AUTHORING.profile()` and the gate's compile
    has no profile. Reusing the check's document is sound only while the
    profile fields refuse or are IR-neutral, so this pins that: an extern-free
    program that compiles under both must produce the same IR."""
    from revl.admit_profile import AdmissionProfile  # noqa: PLC0415
    from revl.compiler import compile_source  # noqa: PLC0415
    from revl.formatter import _canonical_ir  # noqa: PLC0415

    source = ("service Ledger { emission fn bump() -> Int }\n"
              "component C provides ledger: Ledger {\n"
              "  provide ledger { fn bump() = 1 }\n"
              "}\n")
    plain = compile_source(source, "<candidate>.rvl")
    profiled = compile_source(
        source, "<candidate>.rvl",
        profile=AdmissionProfile.untrusted_author((), ()))
    assert _canonical_ir(plain) == _canonical_ir(profiled)
