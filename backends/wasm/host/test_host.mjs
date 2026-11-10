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
// returns a constant or calls a function index.

function wasmModule({ imports = [], funcs = [] }) {
  const text = (s) => [s.length, ...new TextEncoder().encode(s)];
  const section = (id, items) => {
    const body = [items.length, ...items.flat()];
    return [id, body.length, ...body];
  };
  const code = funcs.map((f) => {
    const ops = "call" in f ? [0x10, f.call] : [0x41, f.value];
    const body = [0x00, ...ops, 0x0b];
    return [body.length, ...body];
  });
  return new Uint8Array([
    0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00,
    ...section(1, [[0x60, 0x00, 0x01, 0x7f]]),
    ...section(2, imports.map(([mod, name]) => [...text(mod), ...text(name), 0x00, 0x00])),
    ...section(3, funcs.map(() => [0x00])),
    ...section(7, funcs.map((f, i) => [...text(f.export), 0x00, imports.length + i])),
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

console.log("browser host edge cases: ok");
