// Node test for the browser host: load the compiled demo, prove it is gated
// until its `clock` coeffect arrives, then call its Float provision.
//
//     node backends/wasm/host/test_host.mjs
//
// The same CordisHost module is what index.html imports, so this exercises the
// browser path (fetch excepted) on a real WebAssembly engine.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { CordisHost } from "./cordis-host.mjs";

const bytes = await readFile(new URL("./demo.wasm", import.meta.url));
const host = new CordisHost({ log: (line) => console.log(`host: ${line}`) });

const record = await host.load("Cart", bytes);
assert.equal(record.state, "gated", "a component with an unsatisfied coeffect must not instantiate");
assert.deepEqual(host.missing("Cart"), ["clock.frame_dt"]);
assert.equal(host.state("Cart"), "gated");
assert.throws(() => host.call("Cart", "provide:motion.step", 0), /gated/);

let dt = 1 / 60;
host.provideHost("clock", { frame_dt: () => dt });
assert.equal(host.state("Cart"), "active", "providing the coeffect activates the gated component");

let x = 100;
x = host.call("Cart", "provide:motion.step", x);
assert.equal(x, 102, "the Float arithmetic crosses the boundary: 100 + (1/60)*120 = 102");

dt = 0.5;
x = host.call("Cart", "provide:motion.step", x);
assert.equal(x, 162, "the host coeffect is read on every call, not snapshotted");

host.provideHost("clock", { frame_dt: () => 0 });
assert.equal(host.call("Cart", "provide:motion.step", 7), 7);

assert.equal(host.unload("Cart"), true);
assert.equal(host.state("Cart"), "absent");
assert.throws(() => host.call("Cart", "provide:motion.step", 0), /not loaded/);

console.log("browser host: ok");

// ---------------------------------------------------------------------------
// Gating and provision edge cases, over tiny modules built byte by byte here
// (node has no WAT parser). Every function has type () -> i32 and either
// returns a constant, calls a function index, or runs `raw` opcodes
// (`[0x00]` is `unreachable`, a trap).

function wasmModule({ imports = [], funcs = [], start = null }) {
  const text = (s) => [s.length, ...new TextEncoder().encode(s)];
  const section = (id, items) => {
    const body = [items.length, ...items.flat()];
    return [id, body.length, ...body];
  };
  const code = [...funcs, ...(start ? [start] : [])].map((f) => {
    const ops = f.raw ?? ("call" in f ? [0x10, f.call] : [0x41, f.value]);
    const body = [0x00, ...ops, 0x0b];
    return [body.length, ...body];
  });
  // type 0 is () -> i32 for every listed function; type 1 is () -> () for
  // the optional start function, which runs while the instance is created
  return new Uint8Array([
    0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00,
    ...section(1, [[0x60, 0x00, 0x01, 0x7f], [0x60, 0x00, 0x00]]),
    ...section(2, imports.map(([mod, name]) => [...text(mod), ...text(name), 0x00, 0x00])),
    ...section(3, [...funcs.map(() => [0x00]), ...(start ? [[0x01]] : [])]),
    ...section(7, funcs.map((f, i) => [...text(f.export), 0x00, imports.length + i])),
    ...(start ? [0x08, 0x01, imports.length + funcs.length] : []),
    ...section(10, code),
  ]);
}

// An inherited property of the ops object is not a provided op: `constructor`
// and `toString` are functions on every object, and must stay refused.
{
  const edge = new CordisHost();
  edge.provideHost("clock", { frame_dt: () => 0 });
  for (const op of ["constructor", "toString"]) {
    const bytes = wasmModule({
      imports: [["coeffect:clock", op]],
      funcs: [{ export: "probe", call: 0 }],
    });
    await edge.load(`Inherit_${op}`, bytes);
    assert.equal(edge.state(`Inherit_${op}`), "gated",
      `an inherited ${op} must not satisfy the coeffect:clock import`);
    assert.deepEqual(edge.missing(`Inherit_${op}`), [`clock.${op}`]);
  }
}

// A component that fails on a provision conflict publishes NONE of its
// provisions, so nothing can activate against it or call into it.
{
  const edge = new CordisHost();
  await edge.load("A", wasmModule({ funcs: [{ export: "provide:m.b", value: 1 }] }));
  assert.equal(edge.state("A"), "active");
  await assert.rejects(
    edge.load("B", wasmModule({ funcs: [
      { export: "provide:m.c", value: 2 },
      { export: "provide:m.b", value: 3 },
    ] })),
    /provision conflict on m\.b/);
  assert.equal(edge.state("B"), "failed");
  await edge.load("U", wasmModule({
    imports: [["coeffect:m", "c"]],
    funcs: [{ export: "probe", call: 0 }],
  }));
  assert.equal(edge.state("U"), "gated",
    "U must not activate against a provision of the failed component B");
  assert.deepEqual(edge.missing("U"), ["m.c"]);
  assert.equal(edge.unload("B"), true);
  assert.equal(edge.state("U"), "gated");
}

// A trap in activate_step fails THAT component: it is marked failed, its
// deactivate runs, nothing it provides is published, and later loads of
// unrelated components are not rejected by its error.
{
  const edge = new CordisHost();
  let undone = 0;
  edge.provideHost("log", { hit: () => { undone++; return 0; } });
  await assert.rejects(
    edge.load("T", wasmModule({
      imports: [["coeffect:log", "hit"]],
      funcs: [
        { export: "activate_step", raw: [0x00] },
        { export: "deactivate", call: 0 },
        { export: "provide:m.t", value: 1 },
      ],
    })),
    /unreachable/);
  assert.equal(edge.state("T"), "failed", "a trapping activation fails the component");
  assert.equal(undone, 1, "a failed activation runs deactivate once");
  await edge.load("Z", wasmModule({ funcs: [{ export: "p", value: 0 }] }));
  assert.equal(edge.state("Z"), "active");
  assert.equal(edge.state("T"), "failed", "a failed component is not retried");
  await edge.load("U", wasmModule({
    imports: [["coeffect:m", "t"]],
    funcs: [{ export: "p", call: 0 }],
  }));
  assert.equal(edge.state("U"), "gated", "a failed activation publishes no provision");
}

// An activation that never finishes is failed at the step limit, and its
// deactivate runs as well.
{
  const edge = new CordisHost();
  let undone = 0;
  edge.provideHost("log", { hit: () => { undone++; return 0; } });
  await assert.rejects(
    edge.load("L", wasmModule({
      imports: [["coeffect:log", "hit"]],
      funcs: [
        { export: "activate_step", value: 1 },
        { export: "deactivate", call: 0 },
      ],
    })),
    /activation did not finish/);
  assert.equal(edge.state("L"), "failed");
  assert.equal(undone, 1, "the step-limit failure runs deactivate");
}

// A conflict found while pumping fails the conflicting component only: the
// load that triggered the pump resolves, and the pump goes on to activate
// every other component it can.
{
  const edge = new CordisHost();
  await edge.load("A", wasmModule({ funcs: [{ export: "provide:m.b", value: 1 }] }));
  await edge.load("G", wasmModule({
    imports: [["coeffect:x", "y"]],
    funcs: [{ export: "p", call: 0 }, { export: "provide:m.b", value: 2 }],
  }));
  await edge.load("H", wasmModule({
    imports: [["coeffect:x", "y"]],
    funcs: [{ export: "q", call: 0 }],
  }));
  await edge.load("P", wasmModule({ funcs: [{ export: "provide:x.y", value: 3 }] }));
  assert.equal(edge.state("P"), "active");
  assert.equal(edge.state("G"), "failed");
  assert.match(edge.error("G"), /provision conflict on m\.b/);
  assert.equal(edge.state("H"), "active", "the pump keeps going past G");
  assert.equal(edge.call("H", "q"), 3);
}

// A provision conflict is found from the module's export list, BEFORE it is
// instantiated: the conflicting component never runs activation, so its
// coeffects are never called and a deactivate that would trap never runs to
// replace the conflict error.
{
  const edge = new CordisHost();
  let calls = 0;
  edge.provideHost("log", { hit: () => { calls++; return 0; } });
  await edge.load("A", wasmModule({ funcs: [{ export: "provide:m.b", value: 1 }] }));
  await assert.rejects(
    edge.load("B", wasmModule({
      imports: [["coeffect:log", "hit"]],
      funcs: [
        { export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x41, 0x00] },
        { export: "deactivate", raw: [0x00] },
        { export: "provide:m.b", value: 2 },
      ],
    })),
    /provision conflict on m\.b/);
  assert.equal(edge.state("B"), "failed");
  assert.equal(calls, 0, "a conflicting component must not run its activation");
}

// Ops are the OWN function properties of the object given to provideHost: a
// class instance's prototype method is not one. Wrap it in a plain object.
{
  class Clock { frame_dt() { return 7; } }
  const clock = new Clock();
  const edge = new CordisHost();
  edge.provideHost("clock", clock);
  const probe = () => wasmModule({
    imports: [["coeffect:clock", "frame_dt"]],
    funcs: [{ export: "p", call: 0 }],
  });
  await edge.load("C", probe());
  assert.equal(edge.state("C"), "gated", "a prototype method is not a provided op");
  edge.provideHost("clock", { frame_dt: () => clock.frame_dt() });
  assert.equal(edge.state("C"), "active");
  assert.equal(edge.call("C", "p"), 7);
}

// A host op that calls provideHost while X is activating must not re-enter
// X: the nested pump activates Y (which provides m.b too), so X then fails
// on the conflict instead of going active with no exports over Y's provider.
{
  const edge = new CordisHost();
  let once = false;
  await edge.load("Y", wasmModule({
    imports: [["coeffect:z", "w"]],
    funcs: [{ export: "p", call: 0 }, { export: "provide:m.b", value: 2 }],
  }));
  edge.provideHost("k", { go: () => {
    if (!once) { once = true; edge.provideHost("z", { w: () => 0 }); }
    return 0;
  } });
  await assert.rejects(
    edge.load("X", wasmModule({
      imports: [["coeffect:k", "go"]],
      funcs: [
        { export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x41, 0x00] },
        { export: "provide:m.b", value: 1 },
      ],
    })),
    /provision conflict on m\.b: Y already provides it/);
  assert.equal(edge.state("Y"), "active");
  assert.equal(edge.state("X"), "failed");
  await edge.load("U", wasmModule({
    imports: [["coeffect:m", "b"]],
    funcs: [{ export: "p", call: 0 }],
  }));
  assert.equal(edge.call("U", "p"), 2, "m.b is still Y's provision");
}

// A deactivate whose host op throws a non-Error (null) while undoing a failed
// activation: the original trap is what the load rejects with, and the
// component is failed rather than left gated with a live instance.
{
  const edge = new CordisHost();
  edge.provideHost("k", { go: () => { throw null; } });
  await assert.rejects(
    edge.load("T", wasmModule({
      imports: [["coeffect:k", "go"]],
      funcs: [
        { export: "activate_step", raw: [0x00] },
        { export: "deactivate", call: 0 },
      ],
    })),
    /unreachable/);
  assert.equal(edge.state("T"), "failed");
  assert.match(edge.error("T"), /unreachable/);
}

// An error that does not come from the component itself (here an ops object
// whose own getter throws) still fails that component, with its reason
// recorded, instead of leaving it gated with nothing said.
{
  const edge = new CordisHost();
  await edge.load("G", wasmModule({
    imports: [["coeffect:k", "go"]],
    funcs: [{ export: "p", call: 0 }],
  }));
  edge.provideHost("k", { get go() { throw new Error("getter boom"); } });
  assert.equal(edge.state("G"), "failed");
  assert.match(edge.error("G"), /getter boom/);
}

// Two concurrent loads of one name: the name is reserved before the module
// is compiled, so the second rejects instead of replacing the first.
{
  const edge = new CordisHost();
  let undone = 0;
  edge.provideHost("k", { go: () => { undone++; return 0; } });
  const module = () => wasmModule({
    imports: [["coeffect:k", "go"]],
    funcs: [{ export: "provide:m.b", value: 1 }, { export: "deactivate", call: 0 }],
  });
  const [first, second] = await Promise.allSettled(
    [edge.load("A", module()), edge.load("A", module())]);
  assert.equal(first.status, "fulfilled");
  assert.equal(second.status, "rejected");
  assert.match(second.reason.message, /already loaded/);
  assert.equal(edge.state("A"), "active");
  assert.equal(edge.unload("A"), true);
  assert.equal(undone, 1, "unload deactivates the one instance that exists");
}

// A trap in the module's start function, which runs while the instance is
// being created, takes the same failure path: failed, reason recorded, and
// an unrelated later load is not rejected by it.
{
  const edge = new CordisHost();
  await assert.rejects(
    edge.load("S", wasmModule({
      funcs: [{ export: "provide:m.s", value: 1 }],
      start: { raw: [0x00] },
    })),
    /unreachable/);
  assert.equal(edge.state("S"), "failed");
  assert.match(edge.error("S"), /unreachable/);
  await edge.load("Z", wasmModule({ funcs: [{ export: "p", value: 0 }] }));
  assert.equal(edge.state("Z"), "active");
  await edge.load("U", wasmModule({
    imports: [["coeffect:m", "s"]],
    funcs: [{ export: "p", call: 0 }],
  }));
  assert.equal(edge.state("U"), "gated", "a failed instance publishes no provision");
}

console.log("browser host edge cases: ok");
