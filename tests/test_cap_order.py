"""The capability partial order for parameterized tokens (item 294, Slice 1).

`cap_order` is the ONE place a parameterized capability is parsed, the ONE
registry of parameter kinds, and the ONE definition of `covers`. These tests pin
the order's soundness (reflexive, antisymmetric, transitive), the three value
orders (component-prefix path containment, integer ceiling, discrete equality),
the `*` top, and the closed registry's parse-time refusals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import cap_order as co  # noqa: E402


def cap(s: str) -> co.Cap:
    return co.parse_cap(s)


# ---------------------------------------------------------------- path order


def test_path_component_prefix_covers():
    # `/tmp/job-42 <= /tmp`: the parent cone contains the child path.
    assert co.covers(cap('fs.write(path="/tmp")'),
                     cap('fs.write(path="/tmp/job-42")'))


def test_path_is_component_wise_not_string_prefix():
    # the classic prefix bug: `/tmp/jobber` is NOT under `/tmp/job`.
    assert not co.covers(cap('fs.write(path="/tmp/job")'),
                         cap('fs.write(path="/tmp/jobber")'))


def test_trailing_slash_canonicalizes():
    # `/tmp/` and `/tmp` are one cone: one grant covers crossings spelled either.
    assert cap('fs.write(path="/tmp/")') == cap('fs.write(path="/tmp")')


# ---------------------------------------------------------------- bare / cone


def test_bare_token_tops_its_cone():
    assert co.covers(cap("fs.write"), cap('fs.write(path="/tmp")'))
    assert co.covers(cap("fs.write"), cap("fs.write"))


def test_dropping_a_parameter_widens_and_is_refused():
    # a bare child under a parameterized parent widens: not covered.
    assert not co.covers(cap('fs.write(path="/tmp")'), cap("fs.write"))


# ---------------------------------------------------------------- ceiling


def test_ceiling_smaller_is_narrower():
    assert co.covers(cap("m.c(calls=100)"), cap("m.c(calls=10)"))
    assert not co.covers(cap("m.c(calls=10)"), cap("m.c(calls=100)"))


# ---------------------------------------------------------------- discrete


def test_discrete_equality_only():
    assert co.covers(cap('db.read(table="orders")'), cap('db.read(table="orders")'))
    assert not co.covers(cap('db.read(table="users")'), cap('db.read(table="orders")'))


def test_host_is_discrete():
    assert co.covers(cap('api.call(host="example.com")'),
                     cap('api.call(host="example.com")'))
    assert not co.covers(cap('api.call(host="a.com")'), cap('api.call(host="b.com")'))


# ---------------------------------------------------------------- tokens / star


def test_distinct_tokens_are_incomparable():
    assert not co.covers(cap("fs.write"), cap("fs.read"))
    assert not co.covers(cap("fs.read"), cap("fs.write"))


def test_star_is_strictly_top():
    assert co.covers(cap("*"), cap("*"))
    assert not co.covers(cap("*"), cap("fs.write"))       # * covers nothing else
    assert not co.covers(cap("fs.write"), cap("*"))       # nothing covers *
    assert not co.covers(cap('fs.write(path="/tmp")'), cap("*"))


# ---------------------------------------------------------------- order laws


PATHS = ["/a", "/a/b", "/a/b/c", "/a/x", "/tmp", "/tmp/job", "/tmp/jobber"]


def test_reflexive():
    for p in PATHS:
        c = cap(f'fs.write(path="{p}")')
        assert co.covers(c, c)


def test_antisymmetric():
    for p in PATHS:
        for q in PATHS:
            a, b = cap(f'fs.w(path="{p}")'), cap(f'fs.w(path="{q}")')
            if co.covers(a, b) and co.covers(b, a):
                assert a == b


def test_transitive():
    caps = [cap(f'fs.w(path="{p}")') for p in PATHS] + [cap("fs.w"),
                                                        cap('fs.w(path="/a/b/c/d")')]
    for a in caps:
        for b in caps:
            for c in caps:
                if co.covers(a, b) and co.covers(b, c):
                    assert co.covers(a, c), (a, b, c)


# ---------------------------------------------------------------- closed registry


@pytest.mark.parametrize("bad,needle", [
    ('fs.write(pth="/x")', "unknown capability parameter `pth`"),
    ('fs.write(path="/")', 'narrows nothing'),
    ('fs.write(path="relative")', "is not absolute"),
    ('fs.write(path="/a/./b")', "`.` component"),
    ('fs.write(path="/a/../b")', "`..` component"),
    ('fs.write(path="/a//b")', "empty component"),
    ('*(path="/x")', "`*` takes no capability parameters"),
    ('fs.write(path="/a",path="/b")', "duplicate capability parameter"),
])
def test_parse_refusals(bad, needle):
    with pytest.raises(co.CapError) as exc:
        cap(bad)
    assert needle in str(exc.value)


def test_to_str_round_trips():
    for s in ["fs.write", 'fs.write(path="/tmp/job")', "m.c(calls=3)",
              'db.r(host="h",table="t")', "*"]:
        assert co.parse_cap(co.parse_cap(s).to_str()) == co.parse_cap(s)


def test_empty_valuation_is_bare_string_equivalent():
    # additivity: a bare token is Cap(T, ()) and renders back to the plain string.
    c = cap("gateway.send")
    assert c.is_bare()
    assert c.to_str() == "gateway.send"
    assert co.covers(c, c)


# ---------------------------------------------------------------- covers_set


def test_covers_set_reports_uncovered():
    held = {cap('fs.write(path="/tmp")')}
    reach = {cap('fs.write(path="/tmp/job")'), cap('fs.write(path="/etc")')}
    uncovered = co.covers_set(held, reach)
    assert [c.to_str() for c in uncovered] == ['fs.write(path="/etc")']


# ------------------------------------------- per-instance symbols (Slice 2)


def test_symbol_round_trips_unquoted():
    # a `config.` value re-reads from its canonical (unquoted) spelling as a
    # Symbol, never as a discrete string.
    c = cap("fs.write(path=config.job_root)")
    val = c.param_map()["path"]
    assert isinstance(val, co.Symbol)
    assert val.field == "job_root"
    assert c.to_str() == "fs.write(path=config.job_root)"


def test_symbol_incomparable_to_literal_fail_closed():
    # an UNRESOLVED symbol is incomparable to any literal, on either side:
    # covered by neither, covering neither (fail closed).
    sym = cap("fs.write(path=config.job_root)")
    lit = cap('fs.write(path="/tmp")')
    assert not co.covers(lit, sym)
    assert not co.covers(sym, lit)


def test_identical_symbol_covers_itself():
    # a symbol can be passed down and is covered ONLY by the identical symbol.
    a = cap("fs.write(path=config.job_root)")
    b = cap("fs.write(path=config.job_root)")
    other = cap("fs.write(path=config.other)")
    assert co.covers(a, b)
    assert not co.covers(a, other)


def test_bare_token_covers_a_symbol_child():
    # the bare token tops its cone: a parent holding bare `fs.write` covers a
    # child reaching a per-instance `fs.write(path=config.x)`.
    assert co.covers(cap("fs.write"), cap("fs.write(path=config.x)"))


def test_substitute_resolves_into_the_cone():
    sym = cap("fs.write(path=config.job_root)")
    resolved = co.substitute(sym, {"job_root": "/tmp/job-42"})
    assert resolved.to_str() == 'fs.write(path="/tmp/job-42")'
    assert co.covers(cap('fs.write(path="/tmp")'), resolved)
    # a resolution OUTSIDE the cone is refused, same order as any literal.
    outside = co.substitute(sym, {"job_root": "/etc"})
    assert not co.covers(cap('fs.write(path="/tmp")'), outside)


def test_substitute_leaves_unbound_symbol_and_is_inert_on_literals():
    sym = cap("fs.write(path=config.job_root)")
    # a field the bindings do not carry stays symbolic (fail closed).
    assert co.substitute(sym, {"other": "/x"}) == sym
    # a literal-only or bare cap is returned unchanged (byte-identical).
    lit = cap('fs.write(path="/tmp")')
    assert co.substitute(lit, {"job_root": "/tmp"}) is lit
    bare = cap("fs.write")
    assert co.substitute(bare, {"job_root": "/tmp"}) is bare


def test_ceiling_symbol_refused():
    # a ceiling is a STATIC bound; a per-instance `config.` value is not one.
    with pytest.raises(co.CapError) as exc:
        cap("model.complete(calls=config.n)")
    assert "ceiling" in str(exc.value)
