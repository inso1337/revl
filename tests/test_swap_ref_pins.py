"""`revl swap` carries the host-module pins onto the successor.

Item 396 option B / 410 give a placement process a *deploy contract*: the spec
carries `refs` (a `{extern, path, sha256}` pin per `@ts ref` host module) plus
the two roots those paths resolve against, `refRoot` and `stdlibRefRoot`, and
`backends/typescript/placement_runner.ts` hash-checks every pin BEFORE it
imports the emitted module — so a host module that changed since compile
refuses the boot instead of running.

`do_swap` built its successor spec as a narrower dict literal that omitted all
three. A successor therefore booted with an empty pin list and an empty
`refRoot`, so the check walked nothing and passed vacuously: the contract held
at boot and stopped holding at the first `revl swap`, silently, while the swap
reported success.

Two tests here, and the second is the one that matters more:

1. the successor carries the pins, scoped to the component it hosts;
2. a structural guard over the two spec literals in `placement.py`, so the NEXT
   key added to the per-process spec cannot go missing from the successor the
   way `refs`, `refRoot`, `stdlibRefRoot` and `depends` all did.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement as _placement  # noqa: E402

from test_swap import _wire_conductor, _write  # noqa: E402

# `shout` carries a `@py` body AND a `@ts ref`, the shape stdlib/fs.rvl uses:
# the running composition is py-tiered, and the ref pin travels in every process
# spec so a node-tiered process can hash-check it before importing host code.
# `Consumer` never reaches it, which is what makes the per-slice scoping of the
# pin list observable.
_REF_APP = """
extern pure fn shout(x: Str) -> Str = @py {
    return x.upper()
} = @ts ref shout from "host/shout.ts"

service Cache { async fn get(k: Str) -> Str }
service App { async fn run() -> Str }

component MemCache provides cache: Cache {
  provide cache { async fn get(k) = shout(k) }
}
component Consumer requires cache: Cache provides app: App {
  provide app { async fn run() = cache.get("k") }
}
"""

_REF_PLACEMENT = """
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
"""


def test_swap_successor_carries_the_host_module_pins(tmp_path, monkeypatch):
    """The successor of a swapped ref-reaching component gets the same three
    pin keys its predecessor booted with, scoped to its own slice.

    Without the fix `refs`, `refRoot` and `stdlibRefRoot` are all absent from
    the successor spec, `spec.refs || []` in the node runner is empty, and the
    deploy-contract hash check verifies nothing.
    """
    (tmp_path / "host").mkdir(parents=True, exist_ok=True)
    _write(tmp_path / "host", "shout.ts",
           "export function shout(x: string): string { return x.toUpperCase() }\n")
    app = _write(tmp_path, "app.rvl", _REF_APP)
    plc = _write(tmp_path, "app.toml", _REF_PLACEMENT)
    procs = _wire_conductor(tmp_path, monkeypatch, ["swap MemCache --to py", ":q"])

    assert _placement.run_placement([app], plc, once=False) == 0

    boot = procs["provider"].spec
    succ = procs[next(n for n in procs if n.startswith("MemCache__t"))].spec

    # the predecessor booted under the contract: one pin, for the extern its
    # slice reaches, against the user compile root.
    assert [r["extern"] for r in boot["refs"]] == ["shout"]

    # ... and so does the successor, with the SAME pin: same host module, same
    # compile-time hash, same roots. A successor hash-checking a digest the
    # running composition was never compiled against would be a different bug
    # of the same shape, so this is equality, not merely non-emptiness.
    assert succ.get("refs") == boot["refs"], (
        "the successor carries no host-module pins, so the node runner's "
        "deploy-contract hash check has nothing to verify")
    assert succ.get("refRoot") == boot["refRoot"]
    assert succ.get("stdlibRefRoot") == boot["stdlibRefRoot"]

    # scoped to the successor's own slice, not copied wholesale from the
    # predecessor: `Consumer` reaches no ref, so its process carries no pin.
    assert procs["consumer"].spec["refs"] == []

    # §46 edges are computed for the successor's component set, not omitted.
    assert succ.get("depends") == {"MemCache": []}


# ---------------------------------------------------------------------------
# The drift guard.
#
# `refs`/`refRoot`/`stdlibRefRoot` were dropped and `depends` was dropped for
# one reason: the per-process spec and the successor spec are two independent
# dict literals in `placement.py` and nothing compared them. (The correlation
# guard was dropped the same way and caught late, in 421 F8.) This reads both
# literals out of the source and compares their key sets, so a key added to one
# and forgotten in the other fails here rather than in production, on a tier
# nobody ran locally, some months later.
# ---------------------------------------------------------------------------

#: Keys the successor spec is allowed to differ on, each with the reason it is
#: not drift. An entry here is a claim that the key carries no property a swap
#: can silently drop — write the reason down, do not just add the name.
_SUCCESSOR_KEY_EXCEPTIONS: dict[str, str] = {
    # Empty today: the successor spec is key-for-key the boot spec. A
    # legitimate future entry would be a key meaningless for a synthesized
    # process, whose ABSENCE every runner treats exactly as it treats the value
    # the boot path would have computed.
}


def _own_nodes(node):
    """Every node under `node`, NOT descending into a nested function or lambda
    (so the boot spec's scan does not pick up `adapt_spec`'s shaping, which is
    applied to both specs alike)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _spec_keys(source: str, target: str) -> set[str]:
    """Every key the spec bound to `target` ends up carrying: the keys of its
    dict literal, plus the conditional `target["k"] = ...` assignments made in
    the same function (`spec["serve"]`, say)."""
    tree = ast.parse(source)
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        literal = next(
            (n.value for n in _own_nodes(scope)
             if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
             and target in {t.id for t in n.targets if isinstance(t, ast.Name)}),
            None)
        if literal is None:
            continue
        keys = set()
        for key in literal.keys:
            if key is None:
                continue  # a `**` spread, expanded below
            assert isinstance(key, ast.Constant), ast.dump(key)
            keys.add(key.value)
        # `**host_ref_pins(...)` contributes the three pin keys to both
        # literals. Expanding it keeps the guard honest about a spread instead
        # of silently ignoring the keys it carries.
        for value in literal.values:
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "host_ref_pins":
                keys |= set(_placement.host_ref_pins({}, [], []))
        # conditional top-level assignments in the same scope
        for node in _own_nodes(scope):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for slot in targets:
                if (isinstance(slot, ast.Subscript)
                        and isinstance(slot.value, ast.Name) and slot.value.id == target
                        and isinstance(slot.slice, ast.Constant)):
                    keys.add(slot.slice.value)
        return keys
    raise AssertionError(f"no dict literal assigned to {target!r} found")


def test_successor_spec_carries_every_boot_spec_key():
    """Every key the per-process boot spec sets is set on the swap successor
    too (or is listed, with a reason, in `_SUCCESSOR_KEY_EXCEPTIONS`).

    The invariant this encodes: **a spec key that carries a security property
    must either be carried across a swap or the swap must refuse.** A swap is
    ordinary use, so a property that holds only until the first swap is not a
    property. Key-set parity is the cheap, mechanical half of that, and it is
    what would have caught `refs`, `refRoot`, `stdlibRefRoot` and `depends` in
    one go.
    """
    source = (ROOT / "src" / "revl" / "placement.py").read_text(encoding="utf-8")
    boot = _spec_keys(source, "spec")
    succ = _spec_keys(source, "succ_spec")
    assert "refs" in boot and "serve" in succ, (boot, succ)  # found the right two

    missing = boot - succ - set(_SUCCESSOR_KEY_EXCEPTIONS)
    assert not missing, (
        f"the swap successor spec drops per-process spec key(s) {sorted(missing)}. "
        f"A key that carries a security property must be carried across a swap, "
        f"or the swap must refuse — see `do_swap` in src/revl/placement.py. "
        f"Carry it, or add it to _SUCCESSOR_KEY_EXCEPTIONS with the reason it "
        f"is safe to omit.")
    # and the other direction: a successor-only key means the boot path is the
    # one missing something.
    assert not (succ - boot), (
        f"the swap successor spec sets key(s) {sorted(succ - boot)} the "
        f"per-process boot spec never sets")
