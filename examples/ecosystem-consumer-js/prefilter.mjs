#!/usr/bin/env node
// agent-prefilter-js — a THIRD-PARTY JavaScript consumer of the revl wasm gate
// (roadmap item 335 slice 4, item 338's polyglot exit).
//
// This file is not part of revl. It is what an external project looks like once
// it depends on revl's admission gate as a WASM COMPONENT transpiled to JS,
// instead of shelling out to the `revl` CLI or shipping a Python runtime into a
// browser tab. Its only revl import is `./gate.mjs`, which wraps the transpiled
// component and nothing else: no Python, no native toolchain, no server.
//
// It is the JS sibling of `../ecosystem-consumer-rs/` (rust) and
// `../ecosystem-consumer/` (py), and deliberately not the same program, because
// the three tiers do not give the same guarantee. The contract this one embeds
// against is stated at the top of `gate.mjs`; the short version is that this
// tier has NO ADMISSION ARM, so this program's only decisions are REJECT (on a
// refusal) and ESCALATE (on everything else). The words REGISTER and ADMIT
// appear nowhere in its output, and the test holds that.
//
// Usage:
//   node prefilter.mjs candidates/            # human log
//   node prefilter.mjs candidates/ --json     # machine-readable summary

import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { ESCALATE, REJECT, decide, loadGate } from "./gate.mjs";

/**
 * The full gate version as a cache key. All FOUR identifying fields the wasm
 * tier carries (api, language, frontier, tier), never `language` alone: two
 * gates at the same language and a different frontier disagree on the same
 * source by construction (see `double_tool.rvl`, which py ADMITS and this gate
 * merely has nothing to refuse), so a cached verdict is valid only for the
 * exact gate that produced it.
 */
function cacheKey(version) {
  return [version.api, version.language, version.frontier, version.tier].join(" ");
}

/** An in-memory stand-in for what a real CI system or registry would persist. */
class VerdictCache {
  #store = new Map();

  get(version, name) {
    return this.#store.get(cacheKey(version))?.get(name);
  }

  put(version, record) {
    const key = cacheKey(version);
    if (!this.#store.has(key)) this.#store.set(key, new Map());
    this.#store.get(key).set(record.name, record);
  }
}

async function prefilterCandidate(gate, dir, name, cache) {
  const hit = cache.get(gate.version, name);
  if (hit) return { ...hit, cached: true };

  const source = await readFile(join(dir, name), "utf8");
  const verdict = gate.admit(source);
  const record = {
    name,
    // Always false, on every arm, exactly as the WIT record types it: a
    // downstream reader of this program's output that branches on `admitted`
    // reads this tier as "never admits" rather than mistaking a no-objection
    // for an admission.
    admitted: verdict.admitted,
    decision: decide(verdict),
    // The WIT arm name, kebab-case on this boundary.
    kind: verdict.kind,
    code: verdict.code ?? null,
    // `message` is logged for a human to read (it is the repair signal), never
    // parsed: it is the compiler's diagnostic verbatim and is NOT versioned.
    message: verdict.message ?? null,
    // Recorded on EVERY record, because a verdict is only a fact together with
    // the frontier that produced it.
    frontier: gate.version.frontier,
    cached: false,
  };
  cache.put(gate.version, record);
  return record;
}

export async function runPrefilter(dir, { gateSpecifier, log = () => {} } = {}) {
  const gate = await loadGate(gateSpecifier);
  const version = gate.version;
  log(`gate_version: api=${version.api} language=${version.language} ` +
      `frontier=${version.frontier} tier=${version.tier}`);
  // `layer` is the field a consumer of a native gate must read before trusting
  // any non-refusal: it says, in prose, what this gate does and does not decide.
  log(`gate_layer: ${version.layer}`);

  const names = (await readdir(dir)).filter((n) => n.endsWith(".rvl")).sort();
  const cache = new VerdictCache();
  const results = [];
  for (const name of names) {
    const record = await prefilterCandidate(gate, dir, name, cache);
    if (record.decision === REJECT) {
      // Clause 1: a refusal is authoritative. Nothing about this candidate is
      // fetched, compiled, instantiated or run, now or later, in this process.
      log(`REJECT   ${record.name}  code=${record.code ?? "?"} ${record.message ?? ""}`);
    } else {
      // NOT an acceptance. The reference toolchain decides; this program has
      // only established that the wasm gate had no refusal to make.
      log(`${ESCALATE} ${record.name}  (${record.kind}: ask the reference gate, ` +
          `this tier issues no admissions)`);
    }
    results.push(record);
  }
  return { gate_version: version, results };
}

async function main(argv) {
  let dir = null;
  let asJson = false;
  for (const arg of argv) {
    if (arg === "--json") asJson = true;
    else if (dir === null) dir = arg;
    else {
      process.stderr.write("usage: prefilter.mjs <candidates-dir> [--json]\n");
      process.exit(2);
    }
  }
  if (dir === null) {
    process.stderr.write("usage: prefilter.mjs <candidates-dir> [--json]\n");
    process.exit(2);
  }
  const log = asJson ? () => {} : (line) => process.stdout.write(line + "\n");
  const summary = await runPrefilter(dir, { log });
  if (asJson) process.stdout.write(JSON.stringify(summary) + "\n");
  // Exit status is 0 on a completed run: a batch containing a refusal is the
  // expected shape of a pass over mixed agent proposals, and the refusal was
  // reported rather than swallowed.
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`agent-prefilter-js: ${error?.stack ?? error}\n`);
    process.exit(1);
  });
}
