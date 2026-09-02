#!/usr/bin/env node
// A static file server for the browser demo, and nothing more.
//
// The demo page is plain ES modules plus `fetch`, so it needs an origin rather
// than a `file://` path. This is 40 lines of `node:http` so the example stays
// dependency-free apart from the transpiler itself. It serves this directory
// only, and it refuses any path that escapes it.
//
//   node serve.mjs            # then open http://127.0.0.1:8787/browser/
//   node serve.mjs 9000

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(fileURLToPath(new URL(".", import.meta.url)));
const PORT = Number(process.argv[2] ?? 8787);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".rvl": "text/plain; charset=utf-8",
  ".wasm": "application/wasm",
  ".wat": "text/plain; charset=utf-8",
};

createServer(async (req, res) => {
  let path = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  if (path.endsWith("/")) path += "index.html";
  const target = resolve(join(ROOT, normalize(path)));
  if (target !== ROOT && !target.startsWith(ROOT + sep)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  try {
    const body = await readFile(target);
    res.writeHead(200, {
      "content-type": TYPES[extname(target)] ?? "application/octet-stream",
    });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}).listen(PORT, "127.0.0.1", () => {
  process.stdout.write(
    `serving ${ROOT}\n  open http://127.0.0.1:${PORT}/browser/\n`,
  );
});
