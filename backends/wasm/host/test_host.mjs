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
