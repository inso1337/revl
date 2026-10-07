"""Issue #1938: a capability's own DECLARED resource dimensions.

The capability resource-parameter registry (`revl.cap_order._REGISTRY`) names
the dimensions every capability shares — `path`/`host`/`table` plus the
`calls`/`size`/`time` ceilings. A product capability can need a dimension the
core vocabulary does not name: the ACCOUNT an `emission[mail.send]` sends as.
Without a way to say so, that fact ends up carried OUTSIDE the capability
spelling, in a gate's own bookkeeping, where `revl audit` never sees it — the
audited artifact omits a fact the audit is supposed to be about.

The resolution (design 294, "Declared resource dimensions"):

```
capability <token>(<name>: <kind>, ...)
```

- the declaration adds a NAME the capability may bind, never a new ORDER:
  `<kind>` is one of the three orders the registry already spells
  (`path`/`discrete`/`ceiling`), so the no-widening argument is unchanged;
- the registry stays closed against UNDECLARED names, so a typo is still a
  parse-time refusal;
- a declared name is legal on the capability that declares it and nowhere
  else.

This file pins the three properties that make the feature safe rather than
merely present: the refusal still fires, an existing token's bytes do not move,
and the ONE place a declared dimension could silently stop being recorded — the
stored/artifact read — is a distinct read from the human-input one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, cap_order as co  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.distill import _resource_join  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402
from revl.parser import Parser  # noqa: E402

MAIL = 'capability mail.send(account: discrete)\n'


def program(src: str):
    return Parser(src, "<test>").parse()


def caps_of(cap_scope: str, decl: str = "") -> tuple:
    src = (decl + "service S { emission[" + cap_scope + "] fn f(x: Str) -> Int }")
    return program(src).services[0].methods["f"].capabilities


# ------------------------------------------------- the declaration surface


def test_a_declaration_records_the_dimensions_of_one_capability():
    prog = program(MAIL + "service S { fn f(x: Str) -> Int }")
    assert [d.token for d in prog.capability_decls] == ["mail.send"]
    assert [(n, k) for n, k, _line in prog.capability_decls[0].parameters] == \
        [("account", "discrete")]


def test_a_declared_dimension_is_spellable_on_its_capability():
    assert caps_of('mail.send(account="ops")', MAIL) == \
        ('mail.send(account="ops")',)


def test_a_declared_path_dimension_canonicalizes_like_a_core_path():
    # the KIND is not part of the bytes: a declared `path` renders exactly as
    # the core `path` row does, so nothing downstream has to know the name.
    assert caps_of('mail.send(folder="/var/spool/")',
                   'capability mail.send(folder: path)\n') == \
        ('mail.send(folder="/var/spool")',)


def test_a_use_may_precede_its_declaration():
    # the declaration is collected in a pre-pass, so an author can put it at the
    # bottom of a file (and a scoped extern can name a capability declared later).
    src = ('service S { emission[mail.send(account="ops")] fn f(x: Str) -> Int }\n'
           + MAIL)
    assert program(src).services[0].methods["f"].capabilities == \
        ('mail.send(account="ops")',)


def test_a_declared_dimension_is_not_spellable_on_another_capability():
    # the declaration is per capability: `account` is `mail.send`'s dimension,
    # not a global name, so it does not leak into an unrelated capability.
    with pytest.raises(RevlError) as exc:
        caps_of('mail.send(account="ops")',
                'capability other.thing(account: discrete)\n')
    assert "unknown capability parameter `account`" in str(exc.value)


def test_an_undeclared_name_is_still_refused():
    # THE load-bearing guard: opening the registry to declared names must not
    # open it to typos. A misspelling on a capability that declares nothing is
    # refused exactly as before, at parse.
    with pytest.raises(RevlError) as exc:
        caps_of('fs.write(pth="/x")')
    assert "unknown capability parameter `pth`" in str(exc.value)


@pytest.mark.parametrize("decl,needle", [
    ("capability m.s(a: blob)\n", "unknown capability parameter kind `blob`"),
    ("capability m.s(a: path, a: discrete)\n",
     "duplicate capability parameter `a`"),
    # a declared dimension may not re-spell a core one: one name would then
    # denote two orders depending on which declaration was in scope.
    ("capability m.s(path: discrete)\n",
     "already a core capability parameter"),
    (MAIL + MAIL.replace("account: discrete", "account: path"),
     "declared twice with different resource parameters"),
])
def test_a_malformed_declaration_is_refused(decl, needle):
    with pytest.raises(RevlError) as exc:
        program(decl + "service S { fn f(x: Str) -> Int }")
    assert needle in str(exc.value)


# ------------------------------------------------------ the two reads


DECL = {"account": "discrete", "folder": "path"}

# One composition that exercises a declared dimension end to end: the
# declaration, the scoped extern, and a provide-method that forwards the
# caller's argument to the call site the binder traces.
_BINDING_SOURCE = (
    MAIL
    + 'extern emission[mail.send] fn mail_send(account: Str, body: Str)'
      ' = @py { return }\n'
    + "service Mail { emission fn send(account: Str, body: Str) }\n"
    + "component Agent provides mail: Mail {\n"
      "  provide mail { fn send(account, body)"
      " { emit mail_send(account, body) } }\n"
      "}\n")


def test_the_input_read_refuses_a_declared_name_without_its_declaration():
    # the FAIL-CLOSED backstop, and the reason the registry is still "closed":
    # a declared spelling read where its declaration is not in scope refuses
    # rather than being admitted at an order guessed from its bytes.
    with pytest.raises(co.CapError) as exc:
        co.parse_cap('mail.send(account="ops")')
    assert "unknown capability parameter `account`" in str(exc.value)


def test_the_input_read_admits_a_declared_name_with_its_declaration():
    assert co.parse_cap('mail.send(account="ops")', DECL).to_str() == \
        'mail.send(account="ops")'


def test_the_stored_read_admits_a_declared_name():
    assert co.parse_stored_cap('mail.send(account="ops")').to_str() == \
        'mail.send(account="ops")'


def test_the_stored_read_renders_byte_identically_to_the_declared_read():
    # the property that makes the stored read safe to use on an artifact whose
    # declaration is gone: same bytes, so an audit line, a WAL record and a
    # ticket field cannot disagree about the spelling.
    text = 'mail.send(account="ops",folder="/var/spool")'
    assert co.parse_stored_cap(text).to_str() == co.parse_cap(text, DECL).to_str()


def test_the_stored_read_sentinel_is_not_a_mapping():
    # `_ADMIT_UNKNOWN` is deliberately not a Mapping, so `declared_order` and
    # `registered_names` answer "nothing declared" for it rather than treating
    # it as an empty-or-populated declaration.
    assert not isinstance(co._ADMIT_UNKNOWN, dict)
    assert co.declared_order("account", co._ADMIT_UNKNOWN) is None
    assert co.registered_names(co._ADMIT_UNKNOWN) == co.registered_names()
    assert co.is_registered("account", co._ADMIT_UNKNOWN) is False


def test_a_declared_path_degrades_to_equality_on_a_stored_read():
    # THE stated cost. A declared `path` canonicalizes to a component tuple;
    # re-read without its declaration it falls to the type-based order and
    # becomes a `discrete` string of the same characters, rendering to the same
    # bytes. Containment therefore degrades to EQUALITY — strictly narrower, so
    # a read can never widen a cone, only fail to find one.
    parent = co.parse_cap('mail.send(folder="/var/spool")', DECL)
    child = co.parse_cap('mail.send(folder="/var/spool/job")', DECL)
    assert co.covers(parent, child)                      # containment, declared
    stored_parent = co.parse_stored_cap('mail.send(folder="/var/spool")')
    stored_child = co.parse_stored_cap('mail.send(folder="/var/spool/job")')
    assert not co.covers(stored_parent, stored_child)    # equality, stored
    assert co.covers(stored_parent, stored_parent)       # reflexive still holds


def test_the_stored_read_never_widens_a_cone():
    # the no-widening argument, one dimension over: the stored parent covers
    # exactly the values the declared parent covers OR FEWER, never more.
    for value in ('/var/spool', '/var/spool/job', '/var/spool/job/deep'):
        text = f'mail.send(folder="{value}")'
        assert (co.covers(co.parse_stored_cap('mail.send(folder="/var/spool")'),
                          co.parse_stored_cap(text))
                <= co.covers(co.parse_cap('mail.send(folder="/var/spool")', DECL),
                             co.parse_cap(text, DECL)))


# ------------------------------------------- the order over a declared name


def test_a_declared_discrete_joins_by_equality():
    a = co.parse_cap('mail.send(account="ops")', DECL)
    b = co.parse_cap('mail.send(account="ops")', DECL)
    c = co.parse_cap('mail.send(account="other")', DECL)
    assert co.covers(a, b) and not co.covers(a, c)


def test_a_declared_ceiling_is_a_ceiling():
    decl = {"spend": "ceiling"}
    assert co.is_ceiling("spend", decl)
    # narrower child, wider parent: same direction as `calls`/`size`/`time`.
    assert co.covers(co.parse_cap("mail.send(spend=5)", decl),
                     co.parse_cap("mail.send(spend=3)", decl))


def test_registered_names_include_a_declaration_and_not_without_it():
    assert "account" in co.registered_names(DECL)
    assert co.is_registered("account", DECL)
    assert "account" not in co.registered_names()
    assert not co.is_registered("account")


def test_a_declared_path_cone_joins_only_with_its_declaration():
    # a join is a WIDENING, so it may only happen where the order is known. The
    # stored read has degraded the declared path to a discrete, so the cones
    # join by equality (and these two disagree) rather than to a common ancestor.
    with_decl = _resource_join([
        co.parse_cap('mail.send(folder="/var/spool/a")', DECL),
        co.parse_cap('mail.send(folder="/var/spool/b")', DECL)])
    assert with_decl is not None and with_decl.to_str() == \
        'mail.send(folder="/var/spool")'
    assert _resource_join([
        co.parse_stored_cap('mail.send(folder="/var/spool/a")'),
        co.parse_stored_cap('mail.send(folder="/var/spool/b")')]) is None


def test_a_core_path_cone_still_joins_under_the_stored_read():
    # the control: a CORE `path` is a registry row, so the stored read resolves
    # it from the registry and it joins exactly as before. The declared layer
    # adds a name; it does not change a core one.
    joined = _resource_join([
        co.parse_stored_cap('fs.write(path="/var/spool/a")'),
        co.parse_stored_cap('fs.write(path="/var/spool/b")')])
    assert joined is not None and joined.to_str() == 'fs.write(path="/var/spool")'


# ------------------------------------------------------ the recorded surfaces


def test_the_ir_carries_the_declaration():
    ir = compile_source(MAIL + 'extern emission[mail.send] fn m(a: Str) = @py { return }\n'
                        "service S { fn f(x: Str) -> Int }", "t.rvl")
    assert ir["capability_declarations"] == {"mail.send": {"account": "discrete"}}


def test_the_ir_member_is_absent_without_a_declaration():
    # additivity: a program that declares nothing produces an IR byte-identical
    # to before, so the two tests that pin the IR key set keep passing.
    ir = compile_source("service S { fn f(x: Str) -> Int }", "t.rvl")
    assert "capability_declarations" not in ir


def test_a_declaration_rides_the_import_closure():
    # a `capability` declaration in an IMPORTED module reaches the merged IR:
    # the consumers that read it off the IR (the approval classifier, the
    # distiller) fold a parameterized spelling with no source in hand, so
    # dropping it at the merge would make them read a declared name as a typo.
    provider = (
        MAIL
        + 'extern emission[mail.send] fn mail_send(account: Str, body: Str)'
          ' = @py { return }\n'
        + 'service Mail { emission[mail.send(account="ops")]'
          ' fn send(account: Str, body: Str) }\n'
        + 'pub fn helper() -> Str { return "h" }\n')
    ir = compile_source('use "prov.rvl" { helper }\n', "<root>.rvl",
                        modules={"prov.rvl": provider})
    assert ir["capability_declarations"] == {"mail.send": {"account": "discrete"}}


def test_a_declaration_binds_into_the_approval_spelling():
    # THE end-to-end pin, and the one that would have caught the feature being a
    # silent no-op: the approval classifier is what puts the dimension into the
    # ticket's spelling, the ledger record and the audit line. If the
    # declaration does not reach `make_cap` here, the binder refuses and returns
    # `(None, None, False)` — which reads as "this capability has no resource
    # dimension", so the token keys BARE and the fact the audit is about is
    # dropped without a word.
    ir = compile_source(_BINDING_SOURCE, "t.rvl")
    spelling, refusal, from_caller = ClassMap(ir).bind_resource_scope(
        "mail.send", ["ops", "payload"], "Agent:mail.send")
    assert refusal is None
    assert spelling == 'mail.send(account="ops")'
    assert from_caller is True          # the value is the CALLER's, not a literal


def test_a_core_dimension_still_binds_identically():
    # the control: the declared layer does not disturb a core `host` binding.
    ir = compile_source(_BINDING_SOURCE.replace(
        MAIL, "").replace("account", "host"), "t.rvl")
    spelling, refusal, from_caller = ClassMap(ir).bind_resource_scope(
        "mail.send", ["api.example", "payload"], "Agent:mail.send")
    assert refusal is None
    assert spelling == 'mail.send(host="api.example")' and from_caller is True


def test_a_declared_spelling_is_redacted_from_the_withheld_record():
    # The item-251 N1 leak, one dimension over. `_distillation_ledger_fields`
    # recovers a bare token from a caller-valued spelling; with the
    # INPUT-VALIDATION read a declared spelling raises, and because the recovery
    # sits in a generator inside a `sorted(...)` with no handler the redaction
    # is SKIPPED and the caller's resource value is written to the cross-session
    # WAL. Both halves are asserted: the read that would have raised, and the
    # redaction that now completes.
    from revl.mcp.session import Session

    with pytest.raises(co.CapError):
        co.parse_cap('mail.send(account="ops")')        # the hazard, live

    session = Session.__new__(Session)
    session.approval_record_values = "withheld"
    session._session_id = "s1"
    session._operator_token = lambda: "op"
    fields = session._distillation_ledger_fields({
        "classCCapabilities": ['mail.send(account="ops")', "fs.write"],
        "resourceScopes": {"mail.send": 'mail.send(account="ops")'},
        "resourceScopesFromCallerArgs": ["mail.send"],
        "realm": "",
    })
    # the caller's value is gone from BOTH channels, and the crossing is still
    # named (the bare token), so the record stays legible.
    assert fields["classCCapabilities"] == ["fs.write", "mail.send"]
    assert fields["resourceScopes"] == {"mail.send": None}
    assert "ops" not in repr(fields)


def test_an_author_written_literal_is_not_redacted():
    # the false-positive control: a literal is in the source already and
    # discloses nothing about the caller, so it records verbatim under both
    # modes — declared dimension or core.
    from revl.mcp.session import Session

    session = Session.__new__(Session)
    session.approval_record_values = "withheld"
    session._session_id = "s1"
    session._operator_token = lambda: "op"
    fields = session._distillation_ledger_fields({
        "classCCapabilities": ['mail.send(account="ops")'],
        "resourceScopes": {"mail.send": 'mail.send(account="ops")'},
        "realm": "",
    })
    assert fields["resourceScopes"] == {"mail.send": 'mail.send(account="ops")'}
