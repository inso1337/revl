# Findings — dynamic plugins (finding #32)

Probe: the DSH-shaped dynamic-plugin loader in the lighthouse workload — a revl
component source arrives at runtime, is admitted against the running
composition (G2 gate), plugged into the live harness, used, and unloaded
(fiber.dispose reverts its effects). Verified: `tools/plugin_demo.py` PASS
(load CalcPlugin -> calc.add(2,3)=5; a colliding provider is REFUSED;
unload returns the registry to baseline), and the GUI routes work
(`/api/plugins` list/load/unload + page panel).

## Verified green

- load = `compile_source(source, manifest=SESSION.ir)` (admission) → emit →
  `rt.plug(ROOT, comp, {})` — the plugin's provided keys are live on the
  running harness;
- the gate refuses a plugin that collides with a running provider (G2):
  `refused: provision conflict: key 'tools' is provided by both Toolbox
  and ...`;
- unload = `await fiber.dispose()` — effects revert, `root.registry.size`
  back to baseline (no residue).

## Finding #32 — py erases async externs to blocking defs

The loader is host-owned because a component-side `unload` cannot express
`await fiber.dispose()`: `_emit_externs` emits every extern (async or not)
as a blocking `def`, and the py await-seed deliberately excludes externs
("an async extern erased to a blocking `def`", backends/python/emit.py
97-99). An async extern whose host body wants to await is therefore
inexpressible on py; ts awaits async externs. Ask: emit async externs as
`async def` and await them at their call sites on py (sync @py bodies keep
working). That would let a `PluginHost` component provide the DSH-shaped
`plugins` service (load/list/unload) the harness currently wires host-side.

## Also noted (harness-side, not a revl bug)

The harness's `ToolRegistry` providers return a fixed literal `list()`; a
plugin's services are callable by the host but its tools cannot reach the
agent loop without a dynamic tools registry the toolbox consults at
runtime. Client-side plugin UI (a plugin shipping its own panel) is a
follow-on.
