"""Applyable quick fixes (roadmap item 287): the shared fix engine, its two
front doors, and the honesty gate that keeps a wrong edit from ever shipping.

Each fixable code is proven end to end: a diagnostic, the generated edit, and a
re-check of the *patched* source showing the rejection is gone. The engine's
promise is that it never returns an edit that would not resolve the diagnostic,
so the tests apply every edit and assert the recompiled source is clean (or at
least no longer carries that rejection). Non-mechanical diagnostics — and
candidates that fail to verify — yield no action.
"""

from __future__ import annotations

import pytest

from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.errors import RevlError
from revl.lsp import (
    LspServer,
    compute_code_actions,
    compute_diagnostics,
    fix_code,
    generate_fix,
)
from revl.lsp.fixgen import apply_edits


# ------------------------------------------------------------ fixtures

# T2 — `null` in an optional context: `null` has no type, but the surrounding
# `Opt[Int]` makes `None` the unambiguous rewrite.
T2_OPT = "fn f() -> Opt[Int] {\n  return null\n}\n"

# T2 — `null` where the context is NOT optional. `None` types as `Opt[Any]`,
# so the naive rewrite would only trade T2 for a type mismatch on the same
# line; the engine must withhold the fix.
T2_NON_OPT = "fn f() -> Int {\n  let x: Int = null\n  return x\n}\n"

# A9 — a provide block keyed `cache` while the component declares exactly one
# provision, `db`. The only key the block can have meant is `db`.
A9_SOLE = (
    "service Store {\n"
    "  fn get(k: Str) -> Str\n"
    "}\n"
    "component Cache provides db: Store {\n"
    "  provide cache {\n"
    "    fn get(k: Str) -> Str { return k }\n"
    "  }\n"
    "}\n"
)

# A9 — same shape but two declared provisions: the intended key is ambiguous,
# so no mechanical rename is safe.
A9_AMBIGUOUS = (
    "service Store {\n"
    "  fn get(k: Str) -> Str\n"
    "}\n"
    "component Cache provides db: Store, mem: Store {\n"
    "  provide cache {\n"
    "    fn get(k: Str) -> Str { return k }\n"
    "  }\n"
    "}\n"
)

# G1 — an undeclared variable in a function body. The repair (a typo? a missing
# binding? a requirement?) is not mechanical, so the engine offers nothing.
G1_UNDECLARED = "fn add(a: Int, b: Int) -> Int {\n  return a + c\n}\n"


def _only_diag(text: str) -> dict:
    diags = compute_diagnostics(text)
    assert len(diags) == 1, diags
    return diags[0]


def _rejection(text: str, filename: str = "<lsp>.rvl") -> dict:
    """The structured rejection an agent would hold — `diagnostics.classify` of
    the first RevlError, exactly as the agent payload receives it."""
    try:
        compile_source(text, filename)
    except RevlError as error:
        return classify(error)
    raise AssertionError("expected a rejection")


# ------------------------------------------------------------ T2: null -> None

def test_t2_generates_null_to_none_and_it_resolves():
    diag = _only_diag(T2_OPT)
    assert diag["code"] == "T2"
    fix = generate_fix(T2_OPT, diag)
    assert fix is not None
    assert fix.code == "T2"
    # the edit replaces exactly the `null` token
    edit = fix.edits[0]
    assert edit["newText"] == "None"
    patched = apply_edits(T2_OPT, fix.edits)
    assert "null" not in patched and "None" in patched
    # PROOF the fix resolves the diagnostic: the patched source re-checks clean
    assert compute_diagnostics(patched) == []


def test_t2_is_withheld_when_none_would_not_typecheck():
    diag = _only_diag(T2_NON_OPT)
    assert diag["code"] == "T2"
    # a non-optional context: `None` only moves the problem, so no edit is safe
    assert generate_fix(T2_NON_OPT, diag) is None


# ------------------------------------------------------------ A9: rename block

def test_a9_renames_provide_block_to_the_sole_declared_key():
    diag = _only_diag(A9_SOLE)
    assert diag["code"] == "A9"
    fix = generate_fix(A9_SOLE, diag)
    assert fix is not None
    assert fix.code == "A9"
    assert "`db`" in fix.title
    patched = apply_edits(A9_SOLE, fix.edits)
    assert "provide db" in patched and "provide cache" not in patched
    # PROOF: the renamed block satisfies A9 (and A6) — the patch re-checks clean
    assert compute_diagnostics(patched) == []


def test_a9_is_withheld_when_the_target_key_is_ambiguous():
    diag = _only_diag(A9_AMBIGUOUS)
    assert diag["code"] == "A9"
    # two declared keys: nothing unambiguous to rename to
    assert generate_fix(A9_AMBIGUOUS, diag) is None


# ------------------------------------------------------------ no-action codes

def test_a_non_mechanical_diagnostic_yields_no_code_action():
    diag = _only_diag(G1_UNDECLARED)
    assert diag["code"] == "G1"
    assert generate_fix(G1_UNDECLARED, diag) is None


def test_a_clean_program_offers_no_fixes():
    clean = "fn f() -> Int {\n  return 1\n}\n"
    assert compute_diagnostics(clean) == []
    whole = {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 0}}
    assert compute_code_actions(clean, "file:///c.rvl", whole) == []


# ------------------------------------------------------------ LSP code actions

def test_code_actions_return_a_quickfix_for_an_overlapping_diagnostic():
    diag = _only_diag(A9_SOLE)
    actions = compute_code_actions(A9_SOLE, "file:///c.rvl", diag["range"])
    assert len(actions) == 1
    action = actions[0]
    assert action["kind"] == "quickfix"
    assert action["diagnostics"][0]["code"] == "A9"
    # the workspace edit targets this document with the verified edit
    edits = action["edit"]["changes"]["file:///c.rvl"]
    assert edits == generate_fix(A9_SOLE, diag).edits


def test_code_actions_ignore_a_range_that_misses_the_diagnostic():
    diag = _only_diag(A9_SOLE)
    # a cursor two lines above the rejection does not overlap it
    far = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    assert compute_code_actions(A9_SOLE, "file:///c.rvl", far) == []
    # but a zero-width cursor on the diagnostic's own start does
    at = {"start": diag["range"]["start"], "end": diag["range"]["start"]}
    assert len(compute_code_actions(A9_SOLE, "file:///c.rvl", at)) == 1


def test_server_dispatches_textdocument_codeaction():
    server = LspServer()
    server.handle({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///a.rvl", "text": T2_OPT}},
    })
    diag = _only_diag(T2_OPT)
    out = server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "textDocument/codeAction",
        "params": {"textDocument": {"uri": "file:///a.rvl"}, "range": diag["range"],
                   "context": {"diagnostics": [diag]}},
    })
    assert out[0]["id"] == 5
    actions = out[0]["result"]
    assert actions[0]["title"].startswith("Replace `null`")
    assert actions[0]["edit"]["changes"]["file:///a.rvl"][0]["newText"] == "None"


def test_initialize_advertises_the_code_action_capability():
    server = LspServer()
    out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    caps = out[0]["result"]["capabilities"]
    assert caps["codeActionProvider"]["codeActionKinds"] == ["quickfix"]


# ------------------------------------------------------------ agent payload

def test_agent_fix_code_returns_the_applyable_edit():
    rejection = _rejection(T2_OPT)
    assert rejection["code"] == "T2"
    payload = fix_code(rejection, T2_OPT)
    assert payload["ok"] is True
    assert payload["code"] == "T2"
    # applying the agent's edit resolves the rejection, same as the LSP path
    patched = apply_edits(T2_OPT, payload["edits"])
    assert compute_diagnostics(patched) == []


def test_agent_and_lsp_share_one_engine_and_one_edit():
    rejection = _rejection(A9_SOLE)
    payload = fix_code(rejection, A9_SOLE)
    lsp_actions = compute_code_actions(A9_SOLE, "file:///c.rvl", _only_diag(A9_SOLE)["range"])
    # both front doors carry byte-identical edits from the shared engine
    assert payload["edits"] == lsp_actions[0]["edit"]["changes"]["file:///c.rvl"]


def test_agent_fix_code_reports_no_fix_for_a_non_mechanical_rejection():
    rejection = _rejection(G1_UNDECLARED)
    payload = fix_code(rejection, G1_UNDECLARED)
    assert payload["ok"] is False
    assert payload["code"] == "G1"
    assert "reason" in payload


def test_agent_fix_code_when_source_does_not_reproduce_the_rejection():
    # a rejection carried against a source that now checks clean: no diagnostic
    # to fix, reported honestly rather than guessed
    rejection = _rejection(T2_OPT)
    clean = "fn f() -> Opt[Int] {\n  return None\n}\n"
    payload = fix_code(rejection, clean)
    assert payload["ok"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
