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

The second half pins the lexer: inside one tool call the read-only readers of
the sent text (path jail, authoring gate, completer, token gate) share one
parse of it, so a check lexes 5 times instead of 8, with the same response.
"""

import contextlib
import json
import os
import subprocess
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


# --- the lexer: one parse per text for the call's read-only readers ---------
#
# Before compiling, a check parsed the text it was sent three times (the path
# jail, the authoring gate and the completer), and the formatter's token gate
# lexed it once more. Those four readers only look, so inside one tool call
# they share one parse (`revl.shared_parse`). A compile still parses for
# itself, and the formatted text is a different text.

REFUSED = "component X { fn }"

# parses, then the authoring gate refuses it: the gate reads the shared program
GATE_REFUSED = ("extern pure fn rd(p: Str) -> Str\n"
                "= @py { return open(p).read() }\n"
                "service S { fn f(x: Str) -> Str }\n"
                "component C provides s: S { provide s { fn f(x) = rd(x) } }\n")


def _count_lexes(monkeypatch) -> list:
    """Every `lex` call, under whatever name a module bound it: the parser
    binds it at import, the formatter and `shared_parse` look it up per call."""
    import revl.lexer as lexer  # noqa: PLC0415

    calls = []
    real = lexer.lex

    def counting(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("source"))
        return real(*args, **kwargs)

    for name, module in list(sys.modules.items()):
        if (name == "revl" or name.startswith("revl.")) and \
                getattr(module, "lex", None) is real:
            monkeypatch.setattr(module, "lex", counting)
    return calls


def test_a_passing_check_lexes_the_sent_text_three_times_not_six(monkeypatch):
    formatted = canonical.canonicalise(TERSE).text
    compiles = _count_compiles(monkeypatch)
    lexes = _count_lexes(monkeypatch)
    result = _call("revl_check", {"source": TERSE})
    assert result["ok"] is True
    assert len(compiles) == 2, compiles
    # the shared read-only parse, the check's compile, the knowledge index
    assert lexes.count(TERSE) == 3, len(lexes)     # was 6
    # the token gate, the gate's compile
    assert lexes.count(formatted) == 2, len(lexes)
    assert len(lexes) == 5, len(lexes)             # was 8


def test_a_refused_check_lexes_the_sent_text_four_times_not_seven(monkeypatch):
    compiles = _count_compiles(monkeypatch)
    lexes = _count_lexes(monkeypatch)
    result = _call("revl_check", {"source": REFUSED})
    assert result["ok"] is False
    assert len(compiles) == 3, compiles
    assert lexes.count(REFUSED) == 4, len(lexes)   # was 7
    assert len(lexes) == 6, len(lexes)             # was 9


def test_nothing_is_shared_across_calls(monkeypatch):
    """The share ends with the call: the same text sent twice is lexed as
    often the second time as the first."""
    lexes = _count_lexes(monkeypatch)
    _call("revl_check", {"source": TERSE})
    first = len(lexes)
    _call("revl_check", {"source": TERSE})
    assert first == 5
    assert len(lexes) == 2 * first


def test_outside_a_tool_call_every_parse_is_fresh():
    from revl import shared_parse  # noqa: PLC0415

    assert shared_parse.program(TERSE, "x.rvl") is not \
        shared_parse.program(TERSE, "x.rvl")
    with shared_parse.scope():
        once = shared_parse.program(TERSE, "x.rvl")
        assert shared_parse.program(TERSE, "x.rvl") is once
        assert shared_parse.program(TERSE, "y.rvl") is not once  # same text only
        with pytest.raises(Exception) as first:
            shared_parse.program(REFUSED, "x.rvl")
        with pytest.raises(Exception) as again:
            shared_parse.program(REFUSED, "x.rvl")
        assert again.value is first.value         # the failure is shared too
    assert shared_parse.program(TERSE, "x.rvl") is not once


_RESPONSE = r'''
import contextlib, json, os, sys, tempfile
os.chdir(tempfile.mkdtemp())
from revl import shared_parse
if sys.argv[1] == "unshared":
    shared_parse.scope = contextlib.nullcontext
from revl.mcp import server
server.set_authoring_trust(roots=(os.getcwd(),))
response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "revl_check",
                                     "arguments": {"source": sys.argv[2]}}})
print(json.dumps(response, sort_keys=True))
'''


# does not LEX: the jail's parse records the lex error, and the formatter's
# token gate asks for the tokens of the same text and gets that error back
LEX_REFUSED = "service S { fn f() -> Str { `never closed } }\n"


@pytest.mark.parametrize("source", [TERSE, REFUSED, GATE_REFUSED, LEX_REFUSED],
                         ids=["passing", "refused", "gate-refused",
                              "lex-refused"])
def test_the_full_response_is_byte_identical_without_the_share(source):
    """With the share switched off every reader parses for itself, which is the
    code path before it existed. Each side runs in a fresh process, so no
    session state from one reaches the other."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    out = {}
    for mode in ("unshared", "shared"):
        done = subprocess.run([sys.executable, "-c", _RESPONSE, mode, source],
                              capture_output=True, text=True, env=env,
                              check=True)
        out[mode] = done.stdout
    content = json.loads(out["shared"])["result"]["structuredContent"]
    assert content["ok"] is (source == TERSE)
    if source == GATE_REFUSED:
        assert content["authoringTrust"] == "untrusted"
    assert out["shared"] == out["unshared"]


# --- the readers really read the shared object, and only read it ------------

def _entries() -> dict:
    from revl import shared_parse  # noqa: PLC0415

    return shared_parse._MEMO.get()


def _dump(entry) -> bytes:
    import pickle  # noqa: PLC0415

    return pickle.dumps((entry.program, entry.tokens))


def test_a_lex_failure_is_shared_with_the_token_gate(monkeypatch):
    """`tokens()` re-raises the lex error that an earlier `program()` recorded
    for the same text, without lexing again; the formatter's gate reads that as
    "does not lex". The formatter's own scanner refuses such text before the
    gate in a real check, so the gate's half is driven directly."""
    from revl import formatter, shared_parse  # noqa: PLC0415

    lexes = _count_lexes(monkeypatch)
    with shared_parse.scope():
        with pytest.raises(Exception) as jail:
            shared_parse.program(LEX_REFUSED, "<candidate>.rvl")
        assert lexes.count(LEX_REFUSED) == 1, len(lexes)
        with pytest.raises(Exception) as gate:
            shared_parse.tokens(LEX_REFUSED, "<candidate>.rvl")
        assert gate.value is jail.value
        assert formatter._token_signature(LEX_REFUSED, "<candidate>.rvl") is None
        assert lexes.count(LEX_REFUSED) == 1, len(lexes)  # never lexed again


@pytest.mark.parametrize("corrupt", ["program", "tokens"])
def test_corrupting_the_shared_parse_changes_the_outcome(monkeypatch, corrupt):
    """Positive control for the identity tests: if the readers did not read the
    shared object, damaging it would change nothing. The first reader's result
    is replaced in the memo; a later reader must see the damage."""
    from revl import shared_parse  # noqa: PLC0415
    from revl.parser import Parser  # noqa: PLC0415

    source = GATE_REFUSED if corrupt == "program" else TERSE
    clean = _call("revl_check", {"source": source})
    benign = Parser("service Z { fn g() -> Int }\n", "z.rvl")
    benign_program = benign.parse()
    done = []
    real = shared_parse.program

    def corrupting(text, filename):
        result = real(text, filename)
        if not done:
            done.append(True)
            entry = _entries()[(text, filename)]
            if corrupt == "program":
                entry.program = benign_program
            else:
                entry.tokens = list(benign.toks)
        return result

    monkeypatch.setattr(shared_parse, "program", corrupting)
    damaged = _call("revl_check", {"source": source})
    assert done
    assert damaged != clean, corrupt


@pytest.mark.parametrize("source", [TERSE, REFUSED, GATE_REFUSED, LEX_REFUSED],
                         ids=["passing", "refused", "gate-refused",
                              "lex-refused"])
def test_the_readers_leave_the_shared_parse_untouched(monkeypatch, source):
    """"Read-only reader" is a convention of `shared_parse`; this enforces it.
    A deep snapshot of each shared program and token list is taken when it is
    first handed out and compared after every later read and at the end of the
    call, so a reader that mutates what it was given fails here."""
    from revl import shared_parse  # noqa: PLC0415

    first: dict = {}
    reads = []
    real_program, real_tokens = shared_parse.program, shared_parse.tokens
    real_scope = shared_parse.scope

    def check(key):
        entry = _entries().get(key)
        if entry is None or not (entry.parsed or entry.tokens is not None):
            return
        dump = _dump(entry)
        assert first.setdefault(key, dump) == dump, f"mutated: {key[1]}"
        reads.append(key)

    def program(text, filename):
        try:
            return real_program(text, filename)
        finally:
            check((text, filename))

    def tokens(text, filename):
        try:
            return real_tokens(text, filename)
        finally:
            check((text, filename))

    @contextlib.contextmanager
    def scope():
        with real_scope():
            yield
            for key in list(_entries()):
                check(key)

    monkeypatch.setattr(shared_parse, "program", program)
    monkeypatch.setattr(shared_parse, "tokens", tokens)
    monkeypatch.setattr(shared_parse, "scope", scope)
    _call("revl_check", {"source": source})
    # the sent text was read more than once, so the comparison compared something
    assert reads.count((source, "<candidate>.rvl")) >= 2, reads
