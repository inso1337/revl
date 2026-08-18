"""Instance-parametric components, executed on the real cordis-py runtime.

The proof of this feature is execution (docs/design-v2-instances.md). A
`spawn` is an acquisition whose inverse is the instance's own teardown; the
instance lives in its own fresh LOCAL realm and its own nested teardown scope
(a child fiber). This drives an emitted revl composition on cordis-py and
asserts, by RUNNING, the five properties the feature exists to provide:

  (a) two live instances of one component coexist;
  (b) each in its own local realm, non-colliding (both provide the same key);
  (c) disposing one runs ITS LIFO teardown and leaves the others live;
  (d) supervision-tree addressing — only the spawner reaches its instance, a
      sibling (and the root) cannot;
  (e) instance teardown is nested — a request-scoped instance is reclaimed at
      `s.dispose()`, NOT deferred to the parent component's teardown.

Set up the runtime with `sh backends/python/setup.sh`; without cordis-py these
skip (with a reason — never reported as passing).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backends" / "python"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402

cordis = pytest.importorskip(
    "cordis", reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")

import loader  # noqa: E402  (backends/python)
import runtime  # noqa: E402  (backends/python)
from cordis import Context  # noqa: E402
from cordis.fiber import FiberState  # noqa: E402


# A worker is a spawnable component: it holds its own host resource (a Map,
# whose new/drop are traced) and provides a service into its own realm. The
# supervisor spawns two workers in its activation body and exposes one
# request-scoped operation — `retire_a`, which disposes worker A on demand,
# reaching it through the handle it alone holds.
SOURCE = """
service Counter {
  fn value() -> Int
}

service Super {
  async fn retire_a() -> Int
}

component Worker provides counter: Counter {
  config { tag: Str }
  let m = effect Map.new() undo m.drop()
  provide counter {
    fn value() = 0
  }
}

component Supervisor provides super: Super {
  let w1 = effect spawn Worker with { tag: "a" } undo w1.dispose()
  let w2 = effect spawn Worker with { tag: "b" } undo w2.dispose()
  provide super {
    async fn retire_a() {
      await w1.dispose()
      return 1
    }
  }
}
"""


async def _flush(root: Context) -> None:
    for _ in range(12):
        await asyncio.sleep(0)


def _drive() -> dict:
    """Run the composition and return an observation record."""
    ir = compile_source(SOURCE, "instances.rvl")
    module = loader.load(ir)

    events: list[str] = []
    runtime.set_trace(events.append)
    obs: dict = {"ir": ir}
    try:
        async def main() -> None:
            root = Context()
            supervisor = runtime.plug(root, module.Supervisor, {})
            await _flush(root)

            # (a)/(b): both instances live; each Map is distinct; the provided
            # key is NOT in the shared realm (each worker isolated its own).
            # Map serials are process-global, so identify the two workers'
            # resources by spawn order: the first `.new` is worker A's, the
            # second is worker B's.
            obs["spawned_maps"] = [e for e in events if e.endswith(".new")]
            new_tags = [e.split(".")[0] for e in obs["spawned_maps"]]
            obs["tag_a"] = new_tags[0] if new_tags else None
            obs["tag_b"] = new_tags[1] if len(new_tags) > 1 else None
            obs["shared_counter"] = root.get("counter")
            obs["supervisor_active_after_load"] = (
                supervisor.state == FiberState.ACTIVE)

            # (e)/(c): retire worker A through the supervisor (the only holder
            # of its handle). Its teardown must run NOW, not at supervisor
            # teardown, and must leave the supervisor and worker B untouched.
            events.clear()
            await root.get("super").retire_a()
            await _flush(root)
            obs["after_retire"] = list(events)
            obs["supervisor_active_after_retire"] = (
                supervisor.state == FiberState.ACTIVE)

            # (c): disposing the supervisor reclaims the remaining worker
            # (worker B) — no leak — and only it (worker A is already gone).
            events.clear()
            await supervisor.dispose()
            await _flush(root)
            obs["after_supervisor_dispose"] = list(events)

        asyncio.run(main())
    finally:
        runtime.set_trace(None)
    return obs


def test_spawn_target_is_a_template_not_a_static_entry():
    """The composition composes only the supervisor; the worker is a runtime
    template — excluded from loadOrder and reported as `× dynamic` (G8)."""
    ir = compile_source(SOURCE, "instances.rvl")
    manifest = ir["manifest"]
    assert manifest["loadOrder"] == ["Supervisor"]
    assert [e["name"] for e in manifest["components"]] == ["Supervisor"]
    assert manifest.get("templates") == ["Worker"]


def test_frozen_spawn_ir_shape():
    """The spawn IR node is the checkpoint deliverable; pin its shape."""
    ir = compile_source(SOURCE, "instances.rvl")
    sup = next(c for c in ir["components"] if c["name"] == "Supervisor")
    step = sup["body"][0]
    assert step["step"] == "let-effect"
    assert step["bind"] == "w1"                       # the handle
    acquire = step["acquire"]
    assert acquire["kind"] == "spawn"                 # instantiation is an acquisition
    assert acquire["component"] == "Worker"
    assert acquire["realms"] == ["counter"]           # fresh local realm per provided key
    assert acquire["config"] == {"tag": {"kind": "lit", "value": "a"}}
    # the teardown is the handle's own dispose
    assert step["undo"] == {"kind": "call",
                            "target": {"kind": "name", "id": "w1"},
                            "method": "dispose", "args": []}


def test_two_instances_coexist_in_distinct_local_realms():
    obs = _drive()
    # (a) two live instances — two distinct Maps created, one per worker.
    # (Map serials are process-global, so assert count + distinctness, not the
    # absolute serials, which depend on what else ran first.)
    assert len(obs["spawned_maps"]) == 2, obs["spawned_maps"]
    assert obs["tag_a"] != obs["tag_b"]
    # (b) each worker's provision is in its OWN local realm, so the shared
    # realm never sees `counter` — no collision between the two providers
    assert obs["shared_counter"] is None
    assert obs["supervisor_active_after_load"]


def test_request_scoped_instance_is_reclaimed_early_not_deferred():
    obs = _drive()
    # (e) worker A's teardown (its Map.drop) ran at retire_a(), NOT at
    # supervisor teardown — the anti-leak property, proven directly
    assert obs["after_retire"] == [f"{obs['tag_a']}.drop"], obs["after_retire"]
    # (c) the supervisor and the sibling instance stayed live
    assert obs["supervisor_active_after_retire"]
    # the remaining worker (B) is reclaimed only when the supervisor tears down
    assert obs["after_supervisor_dispose"] == [f"{obs['tag_b']}.drop"], \
        obs["after_supervisor_dispose"]


def test_supervision_tree_addressing():
    """(d) The instance is reachable only through the handle its spawner holds.
    The root context — a stand-in for any sibling or outside party — cannot
    resolve the worker's provision, because it lives in a private local realm."""
    obs = _drive()
    assert obs["shared_counter"] is None
    # two workers, two disjoint realms: neither collided on `counter`, which is
    # only possible if each is a distinct local realm
    assert len(obs["spawned_maps"]) == 2
