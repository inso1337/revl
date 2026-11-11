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
// X: the provideHost is deferred to the end of X's load, so X goes active
// first with m.b published, and the drained pump then fails Y (which
// provides m.b too) on the conflict instead of publishing over X.
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
  await edge.load("X", wasmModule({
    imports: [["coeffect:k", "go"]],
    funcs: [
      { export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x41, 0x00] },
      { export: "provide:m.b", value: 1 },
    ],
  }));
  assert.equal(edge.state("X"), "active");
  assert.equal(edge.state("Y"), "failed");
  assert.match(edge.error("Y"), /provision conflict on m\.b: X already provides it/);
  edge.__checkInvariant();
  await edge.load("U", wasmModule({
    imports: [["coeffect:m", "b"]],
    funcs: [{ export: "p", call: 0 }],
  }));
  assert.equal(edge.call("U", "p"), 1, "m.b is X's provision");
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

// An unload of X from inside its own activation, after which the step traps
// anyway: the unload wins. The name is released, deactivate ran once, and
// the load rejects with the unload, not the trap.
{
  let undo = 0;
  const edge = new CordisHost();
  edge.provideHost("k", {
    go: () => { assert.equal(edge.unload("X"), true); return 0; },
    undo: () => { undo++; return 0; },
  });
  await assert.rejects(edge.load("X", wasmModule({
    imports: [["coeffect:k", "go"], ["coeffect:k", "undo"]],
    funcs: [
      { export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x00] },
      { export: "deactivate", call: 1 },
      { export: "provide:m.b", value: 1 },
    ],
  })), /X: was unloaded while it was activating/);
  assert.equal(edge.state("X"), "absent");
  assert.equal(undo, 1);
  edge.__checkInvariant();
  await edge.load("X", wasmModule({ funcs: [{ export: "p", value: 4 }] }));
  assert.equal(edge.call("X", "p"), 4, "the name is free to load again");
}

// A host op reached through call() runs while the component's code is on
// the stack, so the unload it asks for is applied after the export returns:
// deactivate runs after the op the export calls next, not between the two.
{
  const order = [];
  const edge = new CordisHost();
  edge.provideHost("k", {
    go: () => { order.push("go"); edge.unload("X"); return 0; },
    after: () => { order.push("after"); return 5; },
    undo: () => { order.push("undo"); return 0; },
  });
  await edge.load("X", wasmModule({
    imports: [["coeffect:k", "go"], ["coeffect:k", "after"], ["coeffect:k", "undo"]],
    funcs: [
      { export: "p", raw: [0x10, 0x00, 0x1a, 0x10, 0x01] },
      { export: "deactivate", call: 2 },
    ],
  }));
  assert.equal(edge.call("X", "p"), 5);
  assert.deepEqual(order, ["go", "after", "undo"]);
  assert.equal(edge.state("X"), "absent");
  edge.__checkInvariant();
}

// A load then an unload of the same name, both made from inside an
// activation, are applied in that order: Z is cancelled, as it is when the
// two calls are made directly.
{
  const edge = new CordisHost();
  let pending;
  let unloaded;
  edge.provideHost("k", { go: () => {
    pending = edge.load("Z", wasmModule({ funcs: [{ export: "p", value: 0 }] }));
    unloaded = edge.unload("Z");
    return 0;
  } });
  await edge.load("X", wasmModule({
    imports: [["coeffect:k", "go"]],
    funcs: [{ export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x41, 0x00] }],
  }));
  await assert.rejects(pending, /"Z" was unloaded while it was loading/);
  assert.equal(unloaded, true, "Z is loaded by the time the unload runs");
  assert.equal(edge.state("Z"), "absent");
  edge.__checkInvariant();
}

// An unload while the module is still compiling releases the name without
// throwing (there is no module to inspect yet), and its load rejects.
{
  const edge = new CordisHost();
  const pending = edge.load("L", wasmModule({ funcs: [{ export: "p", value: 1 }] }));
  assert.equal(edge.state("L"), "loading");
  assert.equal(edge.unload("L"), true);
  await assert.rejects(pending, /"L" was unloaded while it was loading/);
  edge.__checkInvariant();
}

// A host op that throws a value String() cannot convert (a null-prototype
// object, an Error whose `message` getter throws) still fails the component
// completely: error() says something, deactivate runs once, the load
// rejects with the thrown value, and the invariant holds.
for (const [label, make] of [
  ["a null-prototype object", () => Object.create(null)],
  ["an Error with a throwing message getter", () => {
    const error = new Error("unused");
    Object.defineProperty(error, "message", { get() { throw new Error("no message"); } });
    return error;
  }],
]) {
  let undo = 0;
  let thrown;
  const edge = new CordisHost();
  edge.provideHost("k", {
    go: () => { thrown = make(); throw thrown; },
    undo: () => { undo++; return 0; },
  });
  await edge.load("Q", wasmModule({
    imports: [["coeffect:k", "go"], ["coeffect:k", "undo"]],
    funcs: [
      { export: "activate_step", raw: [0x10, 0x00] },
      { export: "deactivate", call: 1 },
    ],
  })).then(() => assert.fail(`${label}: the load must reject`), (error) => {
    assert.equal(error, thrown, `${label}: the load rejects with the thrown value`);
  });
  assert.equal(edge.state("Q"), "failed", label);
  assert.equal(typeof edge.error("Q"), "string", label);
  assert.equal(undo, 1, `${label}: deactivate runs once`);
  edge.__checkInvariant();
}

// The same unconvertible value thrown by a deactivate during a deferred
// unload is logged, and the mutations queued after that unload still run.
{
  const lines = [];
  const edge = new CordisHost({ log: (line) => lines.push(line) });
  edge.provideHost("k", { go: () => 0, undo: () => { throw Object.create(null); } });
  await edge.load("Y", wasmModule({
    imports: [["coeffect:k", "go"], ["coeffect:k", "undo"]],
    funcs: [{ export: "deactivate", call: 1 }, { export: "provide:y.y", value: 1 }],
  }));
  edge.provideHost("j", { go: () => {
    edge.unload("Y");
    edge.provideHost("late", { a: () => 0 });
    return 0;
  } });
  await edge.load("W", wasmModule({
    imports: [["coeffect:j", "go"]],
    funcs: [{ export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x41, 0x00] }],
  }));
  assert.equal(edge.state("Y"), "absent");
  assert.ok(lines.some((line) => line.startsWith("a deferred host call threw:")));
  await edge.load("L", wasmModule({ imports: [["coeffect:late", "a"]], funcs: [{ export: "p", call: 0 }] }));
  assert.equal(edge.state("L"), "active", "the provideHost queued after the unload ran");
  edge.__checkInvariant();
}

// Queued unloads of one name report as the direct calls would: the first
// finds it loaded, the second finds it already gone, also behind a queued
// load of that name.
{
  const edge = new CordisHost();
  const results = [];
  let pending;
  await edge.load("Y", wasmModule({ funcs: [{ export: "p", value: 0 }] }));
  edge.provideHost("j", { go: () => {
    results.push(edge.unload("Y"), edge.unload("Y"));
    pending = edge.load("Z", wasmModule({ funcs: [{ export: "p", value: 0 }] }));
    results.push(edge.unload("Z"), edge.unload("Z"));
    return 0;
  } });
  await edge.load("W", wasmModule({
    imports: [["coeffect:j", "go"]],
    funcs: [{ export: "activate_step", raw: [0x10, 0x00, 0x1a, 0x41, 0x00] }],
  }));
  await assert.rejects(pending, /"Z" was unloaded while it was loading/);
  assert.deepEqual(results, [true, false, true, false]);
  assert.equal(edge.state("Y"), "absent");
  assert.equal(edge.state("Z"), "absent");
}

// load resolves to a frozen snapshot, not the host's own record: writing to
// it changes nothing, and no export is reachable except through call().
{
  const edge = new CordisHost();
  const loaded = await edge.load("S", wasmModule({
    imports: [["coeffect:z", "w"]],
    funcs: [{ export: "p", call: 0 }, { export: "deactivate", value: 0 }],
  }));
  assert.deepEqual(Object.keys(loaded).sort(), ["missing", "name", "state"]);
  assert.equal(loaded.state, "gated");
  assert.ok(Object.isFrozen(loaded) && Object.isFrozen(loaded.missing));
  assert.throws(() => { "use strict"; loaded.state = "active"; }, TypeError);
  assert.equal(edge.state("S"), "gated");
  edge.provideHost("z", { w: () => 6 });
  assert.equal(loaded.state, "gated", "a snapshot, not a live view");
  assert.equal(edge.call("S", "p"), 6);
}

console.log("browser host edge cases: ok");

// RANDOMIZED SEQUENCES. Fixed seeds and a mulberry32 generator, so every run
// is the same sequences. Each sequence is up to 12 steps of load / unload /
// provideHost (also with an empty ops object) / call / state reads, over
// components that trap in activate_step (also right after a host op), run
// past the step limit, call a host op that itself loads, unloads, provides
// or calls, depend on m.b and call a host op while activating, have a
// deactivate that traps or throws null, or provide the same op as another.
// The log callback throws on some lines. After every step (and once the loads it started have settled) the
// host's invariant must hold; `__checkInvariant` is a test-only method.
// Deactivations are also counted from outside: load number n gets its own
// host op u.g<n>, which its deactivate calls, so a second deactivate of one
// activation is seen whatever the host's own bookkeeping says.
{
  const SEEDS = 1000;
  const MAX_STEPS = 12;
  const GENERATIONS = 64;
  const NAMES = ["A", "B", "C", "D"];
  // mulberry32: every output bit mixes the whole state, so small ranges do
  // not cycle the way an LCG's low bits do
  const rng = (seed) => {
    let a = seed >>> 0;
    return (n) => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return (((t ^ (t >>> 14)) >>> 0) / 4294967296 * n) | 0;
    };
  };
  const callDrop = (i) => [0x10, i, 0x1a];
  const ret0 = [0x41, 0x00];
  // name -> module bytes for that kind; import 0 is always u.g<gen>, and
  // `go` is k.go_<name>, the re-entrant host op, so the op knows its caller
  const component = (kind, gen, name) => {
    const go = ["coeffect:k", `go_${name}`];
    const counter = ["coeffect:u", `g${gen}`];
    const countThen = (tail) => [...callDrop(0), ...tail];
    switch (kind) {
      case 0: // plain provider of m.b
        return wasmModule({ imports: [counter], funcs: [
          { export: "provide:m.b", value: 1 },
          { export: "deactivate", raw: countThen(ret0) }] });
      case 1: // gated on z.w, provides m.b too (a same-name conflict)
        return wasmModule({ imports: [counter, ["coeffect:z", "w"]], funcs: [
          { export: "provide:m.b", value: 2 }, { export: "p", call: 1 },
          { export: "deactivate", raw: countThen(ret0) }] });
      case 2: // a dependent of m.b that calls it, then k.go, while activating
        return wasmModule({ imports: [counter, ["coeffect:m", "b"], go], funcs: [
          { export: "activate_step", raw: [...callDrop(1), ...callDrop(2), ...callDrop(1), ...ret0] },
          { export: "p", call: 1 }, { export: "deactivate", raw: countThen(ret0) }] });
      case 3: // activation and deactivation call k.go, a re-entrant host op
        return wasmModule({ imports: [counter, go], funcs: [
          { export: "activate_step", raw: [...callDrop(1), ...ret0] },
          { export: "provide:n.c", value: 3 },
          { export: "deactivate", raw: countThen([...callDrop(1), ...ret0]) }] });
      case 4: // activate_step traps
        return wasmModule({ imports: [counter], funcs: [
          { export: "activate_step", raw: [0x00] }, { export: "provide:m.b", value: 4 },
          { export: "deactivate", raw: countThen(ret0) }] });
      case 5: // activate_step never finishes: the step limit
        return wasmModule({ imports: [counter], funcs: [
          { export: "activate_step", value: 1 }, { export: "provide:n.c", value: 5 },
          { export: "deactivate", raw: countThen(ret0) }] });
      case 6: // deactivate traps
        return wasmModule({ imports: [counter], funcs: [
          { export: "provide:n.c", value: 6 },
          { export: "deactivate", raw: countThen([0x00]) }] });
      case 8: // a host op that throws a value String() cannot convert, from
        // activate_step when the seed says so, and always from deactivate
        return wasmModule({ imports: [counter, ["coeffect:k", "weird"]], funcs: [
          { export: "activate_step", raw: [...callDrop(1), ...ret0] },
          { export: "provide:m.b", value: 9 },
          { export: "deactivate", raw: countThen([...callDrop(1), ...ret0]) }] });
      case 7: // activate_step calls k.go, then traps
        return wasmModule({ imports: [counter, go], funcs: [
          { export: "activate_step", raw: [...callDrop(1), 0x00] },
          { export: "provide:n.c", value: 8 }, { export: "p", call: 1 },
          { export: "deactivate", raw: countThen(ret0) }] });
      default: // deactivate's host op throws null; activation calls k.go once
        return wasmModule({ imports: [counter, ["coeffect:k", "nul"], go],
          funcs: [
            { export: "activate_step", raw: [...callDrop(2), ...ret0] },
            { export: "provide:m.b", value: 7 },
            { export: "deactivate", raw: countThen([...callDrop(1), ...ret0]) }] });
    }
  };
  const KINDS = 10;
  const failures = [];
  for (let seed = 1; seed <= SEEDS; seed++) {
    const pick = rng(seed);
    const nested = rng(seed + 100000);
    const throwOn = pick(3) === 0 ? null : `${["active", "failed"][pick(2)]} ${NAMES[pick(4)]}`;
    const host = new CordisHost({ log: (line) => {
      if (throwOn && line.startsWith(throwOn)) throw new Error(`log refused: ${line}`);
    } });
    const deactivations = new Array(GENERATIONS).fill(0);
    const counters = {};
    for (let g = 0; g < GENERATIONS; g++) counters[`g${g}`] = () => { deactivations[g]++; return 0; };
    let gen = 0;
    let budget = 4; // nested mutations per sequence
    let weirdNow = pick(2) === 0;
    const pending = [];
    const trace = [];
    const loadOne = (name, kind) => {
      if (gen >= GENERATIONS) return;
      const g = gen++;
      trace.push(`load ${name} kind ${kind}`);
      pending.push(host.load(name, component(kind, g, name)).catch(() => {}));
    };
    // `self` is the component whose code made the call, when there is one:
    // half the nested mutations aim at it (an unload of itself while it
    // activates, a call into itself)
    const mutate = (choose, self = null) => {
      const what = choose(6);
      const name = self && choose(2) === 0 ? self : NAMES[choose(4)];
      try {
        if (what === 0) loadOne(name, choose(KINDS));
        else if (what === 1) { trace.push(`unload ${name}`); host.unload(name); }
        else if (what === 2) { trace.push("provide z"); host.provideHost("z", { w: () => 0 }); }
        else if (what === 3) { trace.push("provide z empty"); host.provideHost("z", {}); }
        else if (what === 4) {
          const exportName = ["p", "provide:m.b", "provide:n.c"][choose(3)];
          trace.push(`call ${name}.${exportName}`);
          host.call(name, exportName);
        } else {
          trace.push(`read ${name}`);
          host.state(name);
          host.error(name);
          host.missing(name);
        }
      } catch {
        // an unload whose deactivate traps, or a call on a component that is
        // not active, throws to its caller; expected
      }
    };
    host.provideHost("u", counters);
    host.provideHost("k", {
      ...Object.fromEntries(NAMES.map((self) => [`go_${self}`, () => {
        if (budget > 0) { budget--; trace.push(`  nested from ${self}:`); mutate(nested, self); }
        return 0;
      }])),
      nul: () => { throw null; },
      weird: () => {
        if (weirdNow) throw Object.create(null);
        weirdNow = true; // the first call (an activation) passes, later ones throw
        return 0;
      },
    });
    const steps = 1 + pick(MAX_STEPS);
    try {
      for (let s = 0; s < steps; s++) {
        mutate(pick);
        while (pending.length) await pending.shift();
        host.__checkInvariant();
        const twice = deactivations.findIndex((n) => n > 1);
        if (twice >= 0) {
          throw new Error(`load #${twice} ran deactivate ${deactivations[twice]} times`);
        }
      }
    } catch (error) {
      failures.push(`seed ${seed}: ${String(error?.message ?? error).split("\n").join(" | ")}` +
        `\n    after: ${trace.join("; ")}`);
    }
  }
  assert.equal(failures.length, 0,
    `${failures.length} of ${SEEDS} seeds broke the invariant:\n${failures.slice(0, 5).join("\n")}`);
  console.log(`browser host randomized sequences: ok (${SEEDS} seeds)`);
}
