"""cordis-wasm realm-conformance runner (driven by
tests/test_realm_conformance.py). Runs under the cordis-wasm venv (it has
wasmtime). Same contract as the other tiers.

The wasm tier has no runtime realm registry: `isolate kv in realm("shared")`
is lowered by compile-time name mangling into the module's export namespace
(`provide:shared/kv.set`). That still realizes the documented contract at
runtime: two providers naming the SAME realm string mangle to the SAME key and
the runtime refuses the second as a provision conflict (H); DIFFERENT realm
strings mangle to different keys and coexist (S).

argv: <workdir> <CORDIS_WASM runtime dir>
The workdir holds mods.json: {component_name: wat_source}.
"""

import json
import os
import pathlib
import sys


def main() -> None:
    work = pathlib.Path(sys.argv[1])
    sys.path.insert(0, sys.argv[2])  # cordis-wasm runtime

    from runtime import Runtime, State  # cordis-wasm

    mods = json.loads((work / "mods.json").read_text(encoding="utf-8"))

    # (H) two providers of kv in the SAME realm string.
    rt = Runtime()
    a = rt.plug("SharedStoreA", mods["SharedStoreA"])
    h = {"a_state": str(a.state)}
    try:
        b = rt.plug("SharedStoreB", mods["SharedStoreB"])
        h["b_state"] = str(b.state)
        h["verdict"] = "REFUSED" if b.state is not State.ACTIVE else "BOTH_ACTIVE"
    except Exception as exc:  # provision conflict is raised at plug time
        h["verdict"] = "REFUSED"
        h["detail"] = f"{type(exc).__name__}: {exc}"[:200]

    # (S) two providers in DIFFERENT realm strings -> distinct, independent.
    rt2 = Runtime()
    a2 = rt2.plug("SharedStoreA", mods["SharedStoreA"])
    o2 = rt2.plug("SharedStoreOther", mods["SharedStoreOther"])
    both_active = a2.state is State.ACTIVE and o2.state is State.ACTIVE
    rt2.unplug(a2)
    other_survived = o2.state is State.ACTIVE
    s = {
        "a_state": str(a2.state),
        "other_state_after_dispose": str(o2.state),
        "verdict": "SEPARATE" if (both_active and other_survived) else "FAIL",
    }

    print("RC_JSON " + json.dumps({"tier": "cordis-wasm", "H": h, "S": s}))


if __name__ == "__main__":
    main()
