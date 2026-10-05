"""Issue #1937: taint survives the IR.

`extract_and_normalize` strips `Untrusted[T]`/`Trusted[T]` off a service
signature before lowering, so the IR recorded `fetch(...) -> Untrusted[Str]`
as `-> Str`. A unit compiled against that IR as a manifest (a per-turn source,
`revl_load`/`revl_swap` against a running composition, an admitted turn)
reaches the operation only through it, so it read fetched mail as clean and G9
stopped refusing it at a sink.

The IR now keeps what the strip removed, beside the bare types:
- `trusted`/`untrusted` on a parameter;
- `returns_qualifier` (`Untrusted` or `Secret`) on the operation.

`taint.fold_ambient_composition` reads them back for every unit compiled
against the manifest, whatever that unit's own declaration of the service
says.
"""

import copy
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402

BASE = (
    'extern emission[mail.fetch] fn imap_fetch(account: Str, query: Str) -> '
    'Untrusted[Str] = @py { return "" }\n'
    'extern emission[shell] fn host_exec(cmd: Trusted[Str]) = @py { return }\n'
    'service Mail { emission[mail.fetch, imap_fetch] fn fetch(account: Str, '
    'query: Str) -> Untrusted[Str] }\n'
    'service Sh { emission[shell, host_exec] fn exec(cmd: Trusted[Str]) }\n'
    'component Inbox provides mail: Mail, sh: Sh {\n'
    '  provide mail { fn fetch(account, query) = emit imap_fetch(account, query) }\n'
    '  provide sh { fn exec(cmd) { emit host_exec(cmd) } }\n'
    '}\n'
)

LOCAL_SINK = ('extern emission[shell] fn run_sh(cmd: Trusted[Str]) = '
              '@py { return }\n')
TURN_SERVICE = 'service Turn { emission fn run(a: Str) }\n'


def _manifest() -> dict:
    path = os.path.abspath("base_mail.rvl")
    return compile_files([path], sources={path: BASE})


def _turn(requires: str, body: str, prelude: str = "") -> str:
    return (prelude + TURN_SERVICE
            + f"component T requires {requires} provides turn: Turn {{\n"
            + f"  provide turn {{ fn run(a) {{ {body} }} }}\n}}\n")


def _refused(turn: str) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_source(turn, "turn.rvl", manifest=copy.deepcopy(_manifest()))
    return caught.value


# ------------------------------------------------ the IR keeps the qualifiers


def test_the_ir_records_the_qualifiers_beside_the_bare_types():
    services = _manifest()["services"]
    fetch = services["Mail"]["methods"]["fetch"]
    assert fetch["returns"] == "Str"
    assert fetch["returns_qualifier"] == "Untrusted"
    (cmd,) = services["Sh"]["methods"]["exec"]["params"]
    assert cmd == {"name": "cmd", "type": "Str", "trusted": True}


def test_a_signature_with_no_qualifier_is_byte_identical():
    plain = ("service S { fn f(a: Str) -> Str }\n"
             "component C provides s: S { provide s { fn f(a) = a } }\n")
    method = compile_source(plain)["services"]["S"]["methods"]["f"]
    assert method == {"params": [{"name": "a", "type": "Str"}],
                      "returns": "Str", "emission": False}


# ------------------------------------------------ across the manifest boundary


def test_fetched_text_into_a_local_shell_sink_through_the_ir_is_refused():
    """The issue's exit test: the turn requires `mail` from the running
    composition and sends what it fetched into a shell sink."""
    error = _refused(_turn("mail: Mail",
                           'let m = emit mail.fetch(a, "x")  emit run_sh(m)',
                           LOCAL_SINK))
    assert classify(error)["code"] == "G9"
    assert "mail.fetch" in error.message and "shell command" in error.message


def test_a_trusted_parameter_of_an_ambient_service_is_still_a_sink():
    error = _refused(_turn("mail: Mail, sh: Sh",
                           'let m = emit mail.fetch(a, "x")  emit sh.exec(m)'))
    assert classify(error)["code"] == "G9"
    assert "trusted sink `exec`" in error.message


def test_a_turn_cannot_clean_the_data_by_redeclaring_the_service_plain():
    """The consumer's own declaration says `-> Str`. The running composition's
    declaration still stands."""
    plain = ("service Mail { emission[mail.fetch, imap_fetch] fn fetch("
             "account: Str, query: Str) -> Str }\n")
    error = _refused(_turn("mail: Mail",
                           'let m = emit mail.fetch(a, "x")  emit run_sh(m)',
                           plain + LOCAL_SINK))
    assert classify(error)["code"] == "G9"


def test_fetched_text_that_reaches_no_sink_is_admitted():
    turn = _turn("mail: Mail", 'let m = emit mail.fetch(a, "x")')
    compile_source(turn, "turn.rvl", manifest=copy.deepcopy(_manifest()))

