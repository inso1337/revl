# syntax-2.0 acceptance benchmark — run summary

runner: `local` · model: `hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M`
max iterations: 3
scored by: `src/revl`

## revl variants — compile-gated (residue refused at compile)

| variant | specs | first-pass compile | green ≤ max iters | mean iters-to-green | mean tokens-to-green | cost |
|---|---|---|---|---|---|---|
| v2 | 5 | 2/5 (40%) | 5/5 | 2.00 | 11629 | $0.00 |

## raw-ts — probe-scored (does revl earn its keep?)

### raw-ts — lifecycle correctness (residue probe, 5 cycles/plugin)

**1/5 (20%) of raw-TS attempts carry residue that revl would have refused at compile time.**

- clean (no residue — what revl compiles): 4/5 (80%)
- leaked (≥1 category — revl would refuse): 0/5
- failed to mount (bad plugin — also refused): 1/5

Residue-carrying cells:

- `03-user-cache` — did not mount: [runner error] local runner: openai-compatible adapter for role `bench`: timed out after 900.0s waiting for http://127.0.0.1:11434/v1/chat/completions

> The revl variants are compile-gated: a residue-carrying component never reaches this corpus — the compiler refuses it. The raw-ts column is what that gate would have caught.


## mcp: probe-scored (the third host)

### mcp: residue after unload (5 install/unload cycles per pack)

- clean: 3/5
- leaked (at least one category): 0/5
- could not be probed: 2/5

Unload is `remove()` on every registration handle, then the teardown `install` returned, if any. The return convention is this benchmark's, not the SDK's.

- `03-user-cache`: error: mcp-probe: Expression expected
- `04-migrator`: error: mcp-probe: Expected ';', '}' or <eof>

