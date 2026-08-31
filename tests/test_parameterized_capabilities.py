"""Parameterized, resource-bounded capabilities: the STATIC layer (item 294,
Slice 1).

Covers the source grammar (an optional literal parameter list on the existing
dotted token, validated against a CLOSED registry at parse), and the key-to-token
attenuation bridge: `_check_spawn_attenuation` now resolves an emit step's
wiring key to the declared `emission[...]` valuation on BOTH the child's reach
and the parent's held, so a parameterized token is actually compared via
`cap_order.covers` instead of degrading to a bare wiring key.

The lease runtime, cone-aware grant lookups, and 411 enforcement are later
slices and are not exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.parser import Parser  # noqa: E402


def caps_of(cap_scope: str) -> tuple:
    src = "service S { emission[" + cap_scope + "] fn f(x: Str) -> Int }"
    return Parser(src, "<test>").parse().services[0].methods["f"].capabilities


# ---------------------------------------------------------------- grammar


def test_parameterized_token_parses_structured():
    assert caps_of('fs.write(path="/data/incoming")') == \
        ('fs.write(path="/data/incoming")',)


def test_numeric_ceiling_parses():
    assert caps_of("model.complete(calls=3)") == ("model.complete(calls=3)",)


def test_trailing_slash_canonicalized_at_parse():
    assert caps_of('fs.write(path="/tmp/")') == ('fs.write(path="/tmp")',)


def test_bare_token_is_byte_identical():
    # additivity: no parameter, no change to the stored spelling.
    assert caps_of("fs.write") == ("fs.write",)
    assert caps_of("db, bus") == ("db", "bus")


@pytest.mark.parametrize("bad,needle", [
    ('fs.write(pth="/x")', "unknown capability parameter `pth`"),
    ('fs.write(path="/")', "narrows nothing"),
    ('fs.write(path="relative")', "is not absolute"),
    ('fs.write(path="/a/./b")', "`.` component"),
    ('fs.write(path="/a/../b")', "`..` component"),
    ('fs.write(path="/tmp",path="/etc")', "duplicate capability parameter"),
    ('fs.write(path=notliteral)', "must be a string or integer literal"),
])
def test_parse_refusals(bad, needle):
    with pytest.raises(RevlError) as exc:
        caps_of(bad)
    assert needle in str(exc.value)


# ---------------------------------------------------------------- the bridge
#
# Parent holds `fs.write(path="/tmp")` (it requires a store bounded to /tmp) and
# spawns a child requiring a store bounded to a different path - the ONLY
# difference living in the valuation, both wired under the same key `fs`. If the
# fold compared bare wiring keys (the pre-294 behavior), every case below would
# admit. They must instead be decided by the path cone.

FLAGSHIP = """
service TmpStore {{ emission[fs.write(path="/tmp")] fn ingest(row: Str) -> Int }}
service KidStore {{ emission[{kid}] fn ingest(row: Str) -> Int }}
service Worker {{ emission fn run() -> Str }}
component Kid requires fs: KidStore provides worker: Worker {{
  provide worker {{ fn run() {{ emit fs.ingest("row") return "k" }} }}
}}
component Router requires fs: TmpStore {{
  let w = effect spawn Kid with {{ }} undo w.dispose()
}}
"""


def _atten(kid_token: str):
    ir = compile_source(FLAGSHIP.format(kid=kid_token), "t.rvl")
    return {e["child"]: e for e in ir["manifest"]["instances"]}


def test_bridge_narrower_path_admits():
    by = _atten('fs.write(path="/tmp/job-42")')
    assert by["Kid"]["granted"] == ['fs(path="/tmp/job-42")']
    assert by["Kid"]["holds"] == ['fs(path="/tmp")']
    # the dropped breadth is shown on the chain (the parent's wider cone).
    assert by["Kid"]["attenuated"] == ['fs(path="/tmp")']


def test_bridge_wider_path_is_refused():
    # reach-completeness: the ONLY violation lives in the valuation (/etc vs
    # /tmp), so this proves the valuation crossed the key-to-token bridge into
    # the fold. With the bridge stubbed out (bare-key compare) it would admit.
    with pytest.raises(RevlError) as exc:
        _atten('fs.write(path="/etc")')
    msg = str(exc.value)
    assert 'fs(path="/etc")' in msg
    assert "never widen them" in msg


def test_bridge_dropped_parameter_is_refused():
    # the flagship: a child declaring BARE fs.write under a parent holding
    # fs.write(path="/tmp") drops the parameter, which widens.
    with pytest.raises(RevlError) as exc:
        _atten("fs.write")
    msg = str(exc.value)
    assert "granting it `fs`" in msg
    assert "a capability parameter only narrows" in msg


def test_bridge_sibling_path_is_refused():
    # /tmp/other is not under /tmp/job (component-wise), even though both are
    # under /tmp - the parent here is bounded to /tmp/job.
    src = FLAGSHIP.replace('path="/tmp"', 'path="/tmp/job"')
    with pytest.raises(RevlError):
        compile_source(src.format(kid='fs.write(path="/tmp/other")'), "t.rvl")


# ---------------------------------------------------------------- additivity


TWO_TENANTS = """
service StoreA { emission[kv_a] fn write_a(row: Str) -> Int }
service StoreB { emission[kv_b] fn write_b(row: Str) -> Int }
service Worker { emission fn tenant() -> Str }
component TenantAWorker requires kv_a: StoreA provides worker: Worker {
  provide worker { fn tenant() { emit kv_a.write_a("a") return "a" } }
}
component TenantBWorker requires kv_b: StoreB provides worker: Worker {
  provide worker { fn tenant() { emit kv_b.write_b("b") return "b" } }
}
component Router requires kv_a: StoreA requires kv_b: StoreB {
  let a = effect spawn TenantAWorker with { } undo a.dispose()
  let b = effect spawn TenantBWorker with { } undo b.dispose()
}
"""


def test_parameter_free_attenuation_unchanged():
    # a parameter-free program's chain is spelled with bare wiring keys, exactly
    # as before the bridge - empty valuation behaves identically to the old
    # string.
    ir = compile_source(TWO_TENANTS, "t.rvl")
    by = {e["child"]: e for e in ir["manifest"]["instances"]}
    assert by["TenantAWorker"]["holds"] == ["kv_a", "kv_b"]
    assert by["TenantAWorker"]["granted"] == ["kv_a"]
    assert by["TenantAWorker"]["attenuated"] == ["kv_b"]
    assert by["TenantBWorker"]["granted"] == ["kv_b"]
    assert by["TenantBWorker"]["attenuated"] == ["kv_a"]
