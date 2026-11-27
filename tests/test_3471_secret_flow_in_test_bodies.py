"""Issue #3471: the disclosure-sink (and G9 sink) check applies inside a
`test` body exactly as in a `fn` body.

Before this change, `check_taint` walked only top-level `fn` bodies and
component bodies, so a `Secret[T]` minted in a `test` and handed to an extern
with an ordinary `Str` parameter compiled and ran - the host body printed it,
and the pass showed in CI logs and gate receipts. The rule is now one
documented rule: the flow verdict covers every pure callable body, `test`
included (a plain comparison like `assert t == "tok-a"` is not a sink and
stays legal; a declared `Secret[T]` receiver stays legal; everything else is
refused with the same G-SECRET-FLOW / G9 codes as in a `fn`).

Checked here, on the py tier:

  * the issue's exact example (a test-only file) is refused with
    G-SECRET-FLOW, naming the extern and the test body's line;
  * the `fn` control is refused identically (no behaviour change there);
  * a declared `Secret[T]` receiver and a direct comparison of the secret in a
    `test` still compile and PASS under `revl test` semantics (run_py);
  * a secret reaching a capability-scoped disclosure sink (`emission[log]`)
    from a test body is refused, as is an untrusted web value reaching a
    shell sink (G9) from a test body;
  * a qualifier-free program is untouched: its `test` bodies compile and run
    exactly as before (the `model.active` gate keeps the pass a no-op).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.test import run_py  # noqa: E402

#: the issue's exact repro: a Secret minted in a test and handed to an extern
#: with an ordinary Str parameter (a disclosure sink).
LEAK_IN_TEST = (
    "extern pure fn mint(u: Str) -> Secret[Str] = @py { return \"tok-\" + u }\n"
    "extern pure fn leak(s: Str) -> Int = @py { print(\"LEAKED:\" + s); return 1 }\n"
    "\n"
    "test \"t\" { let t = mint(\"a\"); assert leak(t) == 1 }\n"
)

#: the same flow in a plain fn (the control: refused before and after).
LEAK_IN_FN = (
    "extern pure fn mint(u: Str) -> Secret[Str] = @py { return \"tok-\" + u }\n"
    "extern pure fn leak(s: Str) -> Int = @py { print(\"LEAKED:\" + s); return 1 }\n"
    "\n"
    "fn direct() -> Int { let t = mint(\"a\"); return leak(t) }\n"
)

#: the legitimate test shapes: a declared Secret[T] receiver, and a direct
#: comparison of the secret against an expected value (not a sink).
SECRET_OK_IN_TEST = (
    "extern pure fn mint(u: Str) -> Secret[Str] = @py { return \"tok-\" + u }\n"
    "extern pure fn verify(s: Secret[Str]) -> Int = @py { return 1 }\n"
    "\n"
    "test \"receiver\" { let t = mint(\"a\"); assert verify(t) == 1 }\n"
    "test \"compare\" { let t = mint(\"a\"); assert t == \"tok-a\" }\n"
)

#: a secret reaching a capability-scoped sink (log) from a test body.
SECRET_TO_LOG_SCOPE_IN_TEST = (
    "extern pure fn mint(u: Str) -> Secret[Str] = @py { return \"tok-\" + u }\n"
    "extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n"
    "\n"
    "test \"t\" { let t = mint(\"a\"); assert logit(t) == 0 }\n"
)

#: G9 in a test body: an untrusted web value reaching a shell sink.
G9_IN_TEST = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] "
    "= @py { return \"\" }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n"
    "\n"
    "test \"t\" { let w = fetch(\"x\"); assert run(w) == 0 }\n"
)

#: a qualifier-free program: the pass must stay a byte-identical no-op.
PLAIN_TEST = (
    "fn add(a: Int, b: Int) -> Int { return a + b }\n"
    "\n"
    "test \"t\" { assert add(1, 2) == 3 }\n"
)


def _code_of(src: str, filename: str = "i.rvl") -> str | None:
    try:
        compile_source(src, filename)
        return None
    except RevlError as e:
        return getattr(e, "code", None), e


def _refusal(src: str, filename: str = "i.rvl") -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, filename)
    return excinfo.value


# ------------------------------------------------------------- the refusals

def test_issue_repro_test_only_file_is_refused():
    err = _refusal(LEAK_IN_TEST)
    assert err.code == "G-SECRET-FLOW"
    assert "leak" in err.message
    assert "disclosure sink" in err.message


def test_refusal_names_the_test_body_line():
    err = _refusal(LEAK_IN_TEST)
    # the test is declared on line 4 of the source; the refusal must point there
    assert err.line == 4, f"expected line 4, got {err.line}: {err.message}"


def test_fn_control_is_refused_the_same_way():
    err = _refusal(LEAK_IN_FN)
    assert err.code == "G-SECRET-FLOW"
    assert "leak" in err.message


def test_secret_to_a_log_scoped_sink_in_a_test_is_refused():
    err = _refusal(SECRET_TO_LOG_SCOPE_IN_TEST)
    assert err.code == "G-SECRET-FLOW"
    assert "logit" in err.message


def test_g9_untrusted_to_shell_sink_in_a_test_is_refused():
    err = _refusal(G9_IN_TEST)
    assert err.code == "G9", f"expected G9, got {err.code}: {err.message}"
    assert "run" in err.message


# ------------------------------------------------- the legitimate test shapes

def test_declared_secret_receiver_in_a_test_compiles_and_passes():
    ir = compile_source(SECRET_OK_IN_TEST, "ok.rvl")
    verdict, detail = run_py(ir)
    assert verdict == "pass", detail


def test_plain_test_without_qualifiers_is_untouched():
    ir = compile_source(PLAIN_TEST, "plain.rvl")
    verdict, detail = run_py(ir)
    assert verdict == "pass", detail


def test_secret_flow_through_a_helper_fn_called_from_a_test_is_refused():
    src = (
        "extern pure fn mint(u: Str) -> Secret[Str] = @py { return \"tok-\" + u }\n"
        "extern pure fn leak(s: Str) -> Int = @py { return 1 }\n"
        "fn launder() -> Int { let t = mint(\"a\"); return leak(t) }\n"
        "\n"
        "test \"t\" { assert launder() == 1 }\n"
    )
    # the fn itself is refused at compile time, before the test is even reached
    err = _refusal(src)
    assert err.code == "G-SECRET-FLOW"
