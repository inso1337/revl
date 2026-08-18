"""cordis-py realm-conformance runner (driven by tests/test_realm_conformance.py).

Loads three separately-emitted revl components on the REAL cordis-py runtime
and prints one `RC_JSON` line with the runtime verdict for both directions:

  H (sharing/conflict): SharedStoreA and SharedStoreB both `isolate kv in
     realm("shared")`. Equal strings = same realm, so the second provider of
     `kv` in that realm must be REFUSED (its fiber lands FAILED with a G2
     provision conflict) — the direction no existing runtime test asserted.
  S (separation):       SharedStoreA (realm "shared") and SharedStoreOther
     (realm "other") are distinct realms -> both active; disposing one leaves
     the other untouched.

argv: <workdir> <backends/python dir>
The workdir holds a.py / b.py / other.py, the emitted component modules.
"""

import asyncio
import json
import pathlib
import sys
import types


def _load(path: pathlib.Path, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def main() -> None:
    work = pathlib.Path(sys.argv[1])
    sys.path.insert(0, sys.argv[2])  # so the emitted `from runtime import ...` resolves

    import runtime as rt  # cordis-py backend adapter
    from cordis import Context
    from cordis.fiber import FiberState

    a = _load(work / "a.py", "rc_a").SharedStoreA
    b = _load(work / "b.py", "rc_b").SharedStoreB
    other = _load(work / "other.py", "rc_other").SharedStoreOther

    async def flush() -> None:
        for _ in range(30):
            await asyncio.sleep(0)

    async def run() -> None:
        # (H) two providers of kv in the SAME realm string.
        root = Context()
        fa = rt.plug(root, a)
        fb = rt.plug(root, b)
        await flush()
        refused = fb.state is not FiberState.ACTIVE
        detail = ""
        err = getattr(fb, "_error", None)
        if err is not None:
            detail = str(err)
        h = {
            "a_state": fa.state.name,
            "b_state": fb.state.name,
            "verdict": "REFUSED" if refused else "BOTH_ACTIVE",
            "detail": detail,
        }

        # (S) two providers in DIFFERENT realm strings -> distinct, independent.
        root2 = Context()
        fa2 = rt.plug(root2, a)
        fo2 = rt.plug(root2, other)
        await flush()
        both_active = fa2.state is FiberState.ACTIVE and fo2.state is FiberState.ACTIVE
        fa2.dispose()
        await flush()
        other_survived = fo2.state is FiberState.ACTIVE
        s = {
            "a_state": fa2.state.name,
            "other_state_before_dispose": "ACTIVE" if both_active else fo2.state.name,
            "other_state_after_dispose": fo2.state.name,
            "verdict": "SEPARATE" if (both_active and other_survived) else "FAIL",
        }

        print("RC_JSON " + json.dumps({"tier": "cordis-py", "H": h, "S": s}))

    asyncio.run(run())


if __name__ == "__main__":
    main()
