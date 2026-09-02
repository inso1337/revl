// The serverless half of the demo: an edge-worker `fetch` handler that
// pre-filters agent-authored revl before anything downstream sees it.
//
// This is the shape the item exists for. A worker has no Python, no `revl`
// binary and no room for a cold-start interpreter, but it does have a wasm
// engine, and the transpiled gate is one module import away. The whole gate
// loads once per isolate and every request after that is an in-process call.
//
// It is deliberately NOT an admission service. The response it writes has two
// outcomes and neither of them is "yes":
//
//   403 + {"decision":"REJECT"}    a refusal, authoritative, final
//   202 + {"decision":"ESCALATE"}  no refusal to make; the reference toolchain
//                                  decides, and this worker forwards rather
//                                  than answering
//
// A 202 is an ACCEPTED-FOR-PROCESSING, not an admission, and the body says so
// in the same words the module does. There is no 200 arm, because the gate has
// no arm that would justify one.
//
// Run it on node (`node worker.mjs`), or export `handler` to a runtime that
// speaks the Web `fetch` shape (workerd, Deno Deploy, Netlify, Vercel Edge,
// Fastly, Spin's JS SDK).

import { createServer } from "node:http";
import { pathToFileURL } from "node:url";

import { ESCALATE, REJECT, decide, loadGate } from "./gate.mjs";

// One gate per isolate, created lazily and reused: the cost this item exists
// to remove is a per-request cold start, so paying it per request would be the
// one mistake that makes the whole exercise pointless.
let gatePromise = null;
const gate = () => (gatePromise ??= loadGate());

export async function handler(request) {
  if (request.method !== "POST") {
    return new Response("POST revl source to this endpoint\n", { status: 405 });
  }
  const g = await gate();
  const source = await request.text();
  const verdict = g.admit(source);
  const decision = decide(verdict);

  const body = {
    decision,
    // Always false, on every arm. A downstream reader that branches on
    // `admitted` reads this endpoint as "never admits", which is the truth.
    admitted: verdict.admitted,
    kind: verdict.kind,
    code: verdict.code ?? null,
    message: verdict.message ?? null,
    // A verdict is only a fact together with the gate that produced it, so the
    // frontier travels with it and a reader can tell this tier's verdict from
    // the py reference-full tier's.
    gate: {
      api: g.version.api,
      language: g.version.language,
      frontier: g.version.frontier,
      tier: g.version.tier,
      layer: g.version.layer,
    },
    next: decision === REJECT
      ? "none: the reference compiler refuses this source too, with this code and this message"
      : "ask the reference toolchain (revl compile / revl.gate.admit); this gate issues no admissions",
  };
  return new Response(JSON.stringify(body) + "\n", {
    status: decision === REJECT ? 403 : 202,
    headers: { "content-type": "application/json" },
  });
}

async function main(port) {
  // Warm the isolate before the first request, the way a worker platform's
  // init hook would, so the measured per-request cost is the verdict alone.
  await gate();
  createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const response = await handler(new Request(`http://edge${req.url}`, {
      method: req.method,
      body: chunks.length ? Buffer.concat(chunks) : undefined,
    }));
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(await response.text());
  }).listen(port, "127.0.0.1", () => {
    process.stdout.write(
      `revl edge gate on http://127.0.0.1:${port}\n` +
      `  curl --data-binary @candidates/undeclared_tool.rvl ` +
      `http://127.0.0.1:${port}/admit\n`,
    );
  });
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main(Number(process.argv[2] ?? 8788));
}
