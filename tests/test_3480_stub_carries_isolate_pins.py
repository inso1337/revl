"""Issue #3480: a refused component's header stub keeps its `isolate` realm
pins, so its provisions keep their realm placement.

When a component's body lowering aborts outright (Stage 1), the compile keeps
a header-only `poisoned` stub for the topology. The stub carried `routes`
(#3360) but not `isolate`, so a stubbed provider's provisions sat in the
SHARED realm: a router routing to one of those keys then drew a fabricated
"no component provides `<key>` in realm `<realm>`" (item 162) on top of the
body's real refusal, and the shared-realm placement could draw a spurious G2
beside them.

The stub now carries the body's `isolate <key> in realm(...)` pins for keys
the header declares (provides or requires), exactly as it carries routes. A
refused body produces only its own refusal.

Checked here:
  * the repro: a provider whose body is refused (A9: declared provision never
    installed) and which isolates `led` in realm `deep`, with a router routing
    `led` across `realms("deep")` — the only diagnostic is the real A9; no
    fabricated "no component provides ... in realm" and no spurious shared-
    realm G2;
  * the stub unit: `_component_header_stub` carries isolate pins for declared
    keys and drops pins for keys the header does not declare;
  * a clean (non-refused) component's IR is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.lower import _component_header_stub  # noqa: E402
from revl.parser import parse_text  # noqa: E402

#: the repro: P's body is refused (A9) so it becomes a Stage-1 header stub; P
#: isolates its provision into realm `deep`; R routes the key across that realm.
REPRO = """\
service Ledger { fn record(k: Str, v: Str) }

component P provides led: Ledger {
  isolate led in realm("deep")
}
component R requires led: Ledger provides led: Ledger {
  isolate led in realms("deep")
}
"""


def _messages(error: RevlError) -> list[str]:
    diags = getattr(error, "diagnostics", None)
    if diags:
        return [d["message"] for d in diags]
    return [error.message]


def test_refused_body_produces_only_its_own_refusal():
    with pytest.raises(RevlError) as excinfo:
        compile_source(REPRO, "repro.rvl")
    messages = _messages(excinfo.value)
    # the real refusal is there...
    assert any("no `provide led" in m and "A9" in m for m in messages), messages
    # ...and nothing fabricated rides on it
    assert not any("in realm `deep`" in m and "no component provides" in m
                   for m in messages), messages
    assert not any("provision conflict" in m for m in messages), messages


def test_header_stub_carries_isolate_pins_for_declared_keys():
    program = parse_text(REPRO, "repro.rvl")
    comp = next(c for c in program.components if c.name == "P")
    stub = _component_header_stub(comp, "repro.rvl")
    assert stub["poisoned"] is True
    assert stub["isolate"] == {"led": "deep"}
    assert stub["provides"] == {"led"}


def test_header_stub_drops_isolate_pins_for_undeclared_keys():
    src = """\
service Ledger { fn record(k: Str, v: Str) }
component P provides led: Ledger {
  isolate other in realm("deep")
  provide led { fn record(k, v) { } }
}
"""
    program = parse_text(src, "i.rvl")
    stub = _component_header_stub(program.components[0], "i.rvl")
    assert "isolate" not in stub


def test_clean_component_ir_unchanged():
    src = """\
service Ledger { fn record(k: Str, v: Str) }
component P provides led: Ledger {
  isolate led in realm("deep")
  provide led { fn record(k, v) { } }
}
"""
    ir = compile_source(src, "ok.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "P")
    assert comp.get("isolate") == {"led": "deep"}
    assert "poisoned" not in comp
