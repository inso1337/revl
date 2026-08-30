"""Taint and provenance — static information-flow (roadmap item 249, Slice A).

The security property: untrusted input cannot DIRECTLY create authority. A value
that returns across an untrusted-origin boundary is `Untrusted[T]`; a sink that
grants authority declares its parameter `Trusted[T]`; the checker refuses an
`Untrusted[T]` reaching a `Trusted[T]` sink unless a declassifier intervenes.

The refusal is a compile error tagged G9 (docs/design/249-taint-provenance.md).
This is Slice A (the static half); the runtime tag is Slice B, queued behind 243
Slice 2. See the design doc for the full lattice, sinks and declassifiers.
"""

import pytest

from revl import RevlError
from revl.compiler import compile_source
from revl.diagnostics import classify, explain


# --- the core taint-flow rule: tainted value -> sink is refused ---------------

_TAINTED_REACHES_SINK = (
    "extern emission[web.fetch] fn fetch(url: Str) -> Untrusted[Str] = @py {\n"
    "    return \"\"\n"
    "}\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) = @py {\n"
    "    return\n"
    "}\n"
    "service Ops { emission fn go(url: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn go(url) {\n"
    "      let page = emit fetch(url)\n"
    "      emit run(page)\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def test_tainted_value_reaching_a_sink_is_refused_with_G9():
    with pytest.raises(RevlError) as excinfo:
        compile_source(_TAINTED_REACHES_SINK, "taint_sink.rvl")
    err = excinfo.value
    record = classify(err)
    assert record["code"] == "G9", f"expected G9, got {record['code']}: {err.message}"
    # the diagnostic names the origin and the sink
    assert "web" in err.message.lower() or "untrusted" in err.message.lower()


def test_G9_is_a_registered_guarantee_with_a_fix():
    record = explain("G9")
    assert record["ok"] and record["guarantee"]
    assert record["fix"]
