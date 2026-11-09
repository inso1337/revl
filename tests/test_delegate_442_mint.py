"""Typed delegation, the mint and call slice at the front end (roadmap item 442,
issue #2693; docs/design/442-typed-delegation.md §5.1, §4.3 C2).

Two pieces are checked here without a runtime:

  * the MINT: `effect lease` over a capability whose root names a `requires` key
    records that wiring key on the acquire node, which is what lets the runtime
    bind the handle to the provider the delegator holds. A lease over a bare
    capability (an extern's declared scope) records no key and stays an ordinary
    294 lease;
  * the DELEGATEE's call: a method called on a received `Delegate[S]` parameter
    is typed against S, is admitted exactly when S declares the operation and it
    is a sync operation, and lowers with `delegate: S` so the emitter routes it
    through the delegation seam.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

_FS = "service Fs { emission fn write(p: Str) }\n"


def _refuse(src: str) -> str:
    with pytest.raises(RevlError) as caught:
        compile_source(src, "t.rvl")
    return str(caught.value)


def _spec(component: dict, method: str) -> dict:
    step = next(s for s in component["body"] if s.get("step") == "provide")
    return next(m for m in step["methods"] if m["name"] == method)


def _call(node: dict) -> dict:
    return next(n for n in node.get("body") or []
                if n.get("step") == "emit")["expr"]


# ---------------------------------------------------------------------------
# the mint: a lease over a wiring key carries the key
# ---------------------------------------------------------------------------

def test_lease_over_a_wiring_key_records_the_key():
    ir = compile_source(
        'extern emission[fs.write(path="/tmp")] fn wr(p: Str) = @py {\n'
        "    return\n}\n"
        "service Fs { emission fn write(p: Str) }\n"
        "component P provides fs: Fs {\n"
        "  provide fs { fn write(p) { emit wr(p) } }\n"
        "}\n"
        "component A requires fs: Fs {\n"
        '  let l = effect lease fs.write(path="/tmp") ttl 1m uses 3 '
        "undo l.revoke()\n"
        "}\n", "t.rvl")
    step = next(s for s in ir["components"][1]["body"] if s.get("lease"))
    assert step["acquire"]["key"] == "fs"
    assert step["lease"]["key"] == "fs"


def test_lease_over_a_bare_capability_records_no_key():
    # The shipped 294 shape: the capability names an extern's declared scope and
    # the component requires nothing. No key means no delegation source, so the
    # handle stays the pre-442 opaque lease.
    ir = compile_source(
        'extern emission[fs.write(path="/tmp")] fn wr(p: Str) = @py {\n'
        "    return\n}\n"
        "service S { fn a() -> Int }\n"
        "component C provides x: S {\n"
        '  let l = effect lease fs.write(path="/tmp") ttl 1m uses 1 '
        "undo l.revoke()\n"
        "  provide x { fn a() = 0 }\n"
        "}\n", "t.rvl")
    step = next(s for s in ir["components"][0]["body"] if s.get("lease"))
    assert "key" not in step["acquire"]
    assert "key" not in step["lease"]


# ---------------------------------------------------------------------------
# the delegatee's call: typed against S and marked for the emitter
# ---------------------------------------------------------------------------

def test_call_on_a_delegated_parameter_is_marked_with_the_service():
    ir = compile_source(
        _FS
        + "service Worker { emission fn run(d: Delegate[Fs]) }\n"
        "component W provides worker: Worker {\n"
        '  provide worker { fn run(d) { emit d.write("x") } }\n'
        "}\n", "t.rvl")
    call = _call(_spec(ir["components"][0], "run"))
    assert call["delegate"] == "Fs"
    assert call["method"] == "write"


def test_call_to_a_method_the_delegated_service_does_not_declare_is_refused():
    # A value call, so the refusal under test is the delegated-surface check
    # rather than the earlier `emit` classification (which names the same gap
    # less precisely).
    msg = _refuse(
        _FS
        + "service Worker { fn run(d: Delegate[Fs]) -> Int }\n"
        "component W provides worker: Worker {\n"
        '  provide worker { fn run(d) { return d.read("x") } }\n'
        "}\n")
    assert "not an operation of service `Fs`" in msg
    assert "Delegate[S]" in msg


def test_async_delegated_call_is_refused():
    msg = _refuse(
        "service Fs { async fn write(p: Str) -> Int }\n"
        "service Worker { fn run(d: Delegate[Fs]) -> Int }\n"
        "component W provides worker: Worker {\n"
        '  provide worker { fn run(d) { return d.write("x") } }\n'
        "}\n")
    assert "async operation" in msg
    assert "delegated reference cannot cross to an async operation" in msg


def test_plain_fn_call_inside_the_delegatee_is_untouched():
    # A call to a module fn or a local is not a delegated crossing and is not
    # marked, so the emitter path for every existing program is unchanged.
    ir = compile_source(
        _FS
        + "fn helper(p: Str) -> Int { return 1 }\n"
        "service Worker { fn run(d: Delegate[Fs]) -> Int }\n"
        "component W provides worker: Worker {\n"
        '  provide worker { fn run(d) { return helper("x") } }\n'
        "}\n", "t.rvl")
    body = _spec(ir["components"][0], "run")["body"]
    assert all(n.get("expr", {}).get("delegate") is None for n in body)
