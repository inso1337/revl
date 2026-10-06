"""Argument-bound capability destinations in an emission scope — issue #1985.

The parameterized capability surface (item 294) accepted a capability parameter
whose value had to be a compile-time CONSTANT:

    emission[network.call(host="api.example.test")] fn get(url: Str, host: Str)

so the declared destination was a literal nothing related to the method's own
`host` argument: the host body need not post there, and the auditing token was
the same no matter what the caller passed. `emission[network.call(host)]` — the
only spelling that makes the declared destination caller-supplied — was refused
outright (`expected '=' after a capability parameter name, found ')'`).

Surface: an emission scope may bind a capability parameter to one of the
DECLARATION'S OWN parameters (`emission[network.call(host=host)]`, or the
positional shorthand `emission[network.call(host)]`). Both spellings canonicalize
to the same token `network.call(host=host)`, which is what reaches the IR and
what `revl audit` PRINTS (`do_get [network.call(host=host)]`) — distinguished
from the constant form by the quotes. The literal and bare forms are unchanged,
byte-for-byte.

Semantics (checker-verified): the argument binding names the PARAMETER that
carries the destination, resolved against the signature in `lower` the way item
373's `confined: <param>` and item 309's `idempotent(key: <param>)` are — a name
that is not a parameter is a destination nothing enforces, so it is refused
(G4, `emission-scope`) rather than stored. The order stays fail-closed: an
argument binding is strictly narrower than the bare token and is NOT covered by a
constant, so a policy rule written as `network.call(host="api.example.test")`
selects the binding as an uncovered widening instead of silently matching it.
G4 is unchanged — an emission scope remains a static upper bound.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source, cap_order  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.parser import Parser  # noqa: E402


def caps_of(cap_scope: str) -> tuple:
    src = "service S { emission[" + cap_scope + "] fn f(x: Str, host: Str) -> Int }"
    return Parser(src, "<test>").parse().services[0].methods["f"].capabilities


def ir_of(src: str) -> dict:
    return compile_source(src, "1985.rvl")


# ------------------------------------------------------------------- grammar

def test_argument_bound_and_positional_spellings_canonicalize_alike():
    # the two spellings the issue asks for, and the one canonical token both
    # reduce to — the token that reaches the IR and the audit line.
    assert caps_of("network.call(host=host)") == ("network.call(host=host)",)
    assert caps_of("network.call(host)") == ("network.call(host=host)",)


def test_bare_and_literal_spellings_are_byte_identical():
    # additivity: neither the unparameterized nor the constant form changes.
    assert caps_of("network.call") == ("network.call",)
    assert caps_of('network.call(host="api.example.test")') == \
        ('network.call(host="api.example.test")',)
    assert caps_of("db, bus") == ("db", "bus")


def test_a_binding_naming_no_parameter_is_not_refused_at_parse():
    # the parameter LIST is below the scope, so parse cannot resolve the name;
    # the refusal is `lower`'s (mirrors item 373's `confined:` target).
    assert caps_of("network.call(host=nope)") == ("network.call(host=nope)",)


# ------------------------------------------------------------------------ IR

BOUND = """\
service Http {
  emission[network.call(host=host)] fn get(url: Str, host: Str) -> Str
}

extern emission[network.call(host=host)] fn do_get(url: Str, host: Str) -> Str
  = @py { return "ok" }

component Kit provides http: Http {
  provide http {
    fn get(url, host) = emit do_get(url, host)
  }
}
"""

LITERAL = """\
service Http {
  emission[network.call(host="api.example.test")] fn get(url: Str, host: Str) -> Str
}

extern emission[network.call(host="api.example.test")] fn do_get(url: Str, host: Str) -> Str
  = @py { return "ok" }

component Kit provides http: Http {
  provide http {
    fn get(url, host) = emit do_get(url, host)
  }
}
"""

BARE = """\
service Http {
  emission[network.call] fn get(url: Str, host: Str) -> Str
}

extern emission[network.call] fn do_get(url: Str) -> Str
  = @py { return "ok" }

component Kit provides http: Http {
  provide http {
    fn get(url, host) = emit do_get(url)
  }
}
"""


def test_binding_reaches_the_ir_on_both_surfaces():
    ir = ir_of(BOUND)
    extern = next(e for e in ir["externs"] if e["name"] == "do_get")
    assert extern["capabilities"] == ["network.call(host=host)"]
    assert ir["services"]["Http"]["methods"]["get"]["capabilities"] == \
        ["network.call(host=host)"]


def test_the_positional_shorthand_reaches_the_same_ir():
    assert ir_of(BOUND.replace("(host=host)", "(host)")) == ir_of(BOUND)


def test_the_constant_shape_lowers_exactly_as_before():
    ir = ir_of(LITERAL)
    extern = next(e for e in ir["externs"] if e["name"] == "do_get")
    assert extern["capabilities"] == ['network.call(host="api.example.test")']


# --------------------------------------------------------------------- audit

def _audit_line(tmp_path, capsys, src: str) -> str:
    path = tmp_path / "corpus.rvl"
    path.write_text(src)
    assert main(["audit", str(path)]) == 0
    out = capsys.readouterr().out
    marker = "host code: "
    start = out.index(marker)
    return out[start + len(marker):].split(";")[0]


def test_audit_names_the_caller_supplied_destination(tmp_path, capsys):
    # THE surface the reviewer reads: the binding is printed, and the missing
    # quotes are what distinguishes it from the constant form.
    assert _audit_line(tmp_path, capsys, BOUND) == \
        "do_get [network.call(host=host)] (emission, py)"


def test_audit_constant_and_bare_lines_are_unchanged(tmp_path, capsys):
    assert _audit_line(tmp_path, capsys, LITERAL) == \
        'do_get [network.call(host="api.example.test")] (emission, py)'
    assert _audit_line(tmp_path, capsys, BARE) == "do_get [network.call] (emission, py)"


def test_boundary_json_carries_the_canonical_token(tmp_path, capsys):
    path = tmp_path / "corpus.rvl"
    path.write_text(BOUND)
    assert main(["audit", str(path), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    externs = doc["boundary"]["Kit"]["externs"]
    assert externs == [{"name": "do_get", "class": "emission",
                        "capabilities": ["network.call(host=host)"],
                        "backends": ["py"]}]


# ----------------------------------------------------------------- refusals

@pytest.mark.parametrize("decl", [
    # the service-method surface
    'service Http { emission[network.call(host=nope)] fn get(url: Str, host: Str) -> Str }',
    # the extern surface
    ('extern emission[network.call(host=nope)] fn do_get(url: Str, host: Str) -> Str '
     '= @py {\n    return "ok"\n}\n'),
])
def test_a_binding_that_names_no_parameter_is_refused(decl):
    with pytest.raises(RevlError) as exc:
        compile_source(decl, "1985.rvl")
    msg = str(exc.value)
    assert "is not one of its parameters" in msg
    assert "`network.call(host=nope)`" in msg
    # the diagnostic names the argument AND the parameters it could have named.
    assert "`nope`" in msg
    assert "(host, url)" in msg


def test_the_refusal_is_g4_emission_scope():
    # an emission scope is a static upper bound (G4); the binding resolves
    # against the signature under the same code the other scope clauses use.
    with pytest.raises(RevlError) as exc:
        compile_source(
            "service Http { emission[network.call(host=nope)] "
            "fn get(url: Str, host: Str) -> Str }", "1985.rvl")
    assert getattr(exc.value, "code", None) == "G4"


def test_a_declaration_without_parameters_refuses_a_binding():
    with pytest.raises(RevlError) as exc:
        compile_source(
            'extern emission[network.call(host=host)] fn do_get(url: Str) -> Str '
            '= @py {\n    return "ok"\n}\n', "1985.rvl")
    assert "is not one of its parameters (url)" in str(exc.value)


# -------------------------------------------------------------- the order

def test_the_order_is_fail_closed_on_an_argument_binding():
    arg = cap_order.make_cap("network.call", [("host", cap_order.Symbol("host"))])
    lit = cap_order.make_cap("network.call", [("host", "api.example.test")])
    bare = cap_order.make_cap("network.call")
    other_arg = cap_order.make_cap("network.call", [("host", cap_order.Symbol("dest"))])

    # an argument binding is strictly narrower than the bare token...
    assert cap_order.covers(bare, arg) is True
    assert cap_order.covers(arg, bare) is False
    # ...equal only to the identical binding, and never to a constant. A policy
    # rule spelling the constant destination must NOT silently select it.
    assert cap_order.covers(arg, arg) is True
    assert cap_order.covers(lit, arg) is False
    assert cap_order.covers(arg, lit) is False
    assert cap_order.covers(other_arg, arg) is False
    assert cap_order.covers(arg, other_arg) is False


def test_covers_set_reports_the_uncovered_binding():
    arg = cap_order.make_cap("network.call", [("host", cap_order.Symbol("host"))])
    lit = cap_order.make_cap("network.call", [("host", "api.example.test")])
    bare = cap_order.make_cap("network.call")
    assert cap_order.covers_set([lit], [arg]) == [arg]
    assert cap_order.covers_set([bare], [arg]) == []
    assert cap_order.covers_set([arg], [arg]) == []


def test_a_spawn_with_block_does_not_resolve_an_argument_binding():
    # `substitute` resolves a `config.<field>` symbol only; a caller-supplied
    # destination has no spawn-site fix, so it must stay opaque.
    arg = cap_order.make_cap("network.call", [("host", cap_order.Symbol("host"))])
    assert cap_order.substitute(arg, {"host": "api.example.test"}) == arg


# ---------------------------------------------------------- attenuation

WIDENING = """\
service LitHost { emission[network.call(host="api.example.test")] fn get(url: Str, host: Str) -> Str }
service AnyHost { emission[network.call(host=host)] fn get(url: Str, host: Str) -> Str }
service Worker { emission fn run() -> Str }

component Kid requires fs: AnyHost provides worker: Worker {
  provide worker {
    fn run() {
      emit fs.get("u", "h")
      return "k"
    }
  }
}

component Router requires fs: LitHost {
  let w = effect spawn Kid with { } undo w.dispose()
}
"""


def test_a_caller_bound_destination_is_not_covered_by_a_constant_parent():
    # the parent holds the CONSTANT destination; the child declares a
    # caller-supplied one under the same token. The token is held, so this is
    # the valuation-widening path, and the message must name the CALLER (there
    # is no spawn-site `with { }` fix to offer, unlike a `config.` symbol).
    with pytest.raises(RevlError) as exc:
        compile_source(WIDENING, "1985.rvl")
    msg = str(exc.value)
    assert "supplied by the CALLER" in msg
    assert '`network.call(host="api.example.test")`' in msg
    assert "hold the same argument binding, or the bare `network.call`" in msg


def test_a_bare_parent_still_covers_a_caller_bound_destination():
    # the bare token is at-or-above every valuation, so this must still admit.
    src = WIDENING.replace(
        'emission[network.call(host="api.example.test")] fn get(url: Str, host: Str) -> Str',
        "emission[network.call] fn get(url: Str, host: Str) -> Str")
    assert compile_source(src, "1985.rvl") is not None
