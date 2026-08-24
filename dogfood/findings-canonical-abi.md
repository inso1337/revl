# Findings — canonical ABI in the harness (item 41 slice-3, finding #26)

Probe: the revl-harness verification of item 41 slice-3 (`wasm/canonical.rvl`
+ `tools/canonical_demo.py` + `docs/item-41-slice3-verification.md`, milestone
31 in `~/Projects/revl-harness`). One finding: the slice's honest scope is
"pure functions whose whole signature is Str" — and on the wasm tier that
subset is smaller than the harness's own wire protocol needs.

## Verified green (pinned by the harness)

`backends/wasm/canonical.py::emit_component` turns the harness's real pipe
line format into a standard WASI-P2 component:

```revl
fn log_line(id: Str, role: Str, content: Str) -> Str {
  return id + "|" + role + "|" + content
}
```

- boundary = exactly the Str-only pure fns (`log_line`, `echo`, `tag`);
- `cabi_realloc` + interface-qualified export + bare->internal lift present;
- wasm-tools build + validate clean;
- wasmtime component model runs it — wave-encoded invoke
  `revl:exported/ledger.log-line("s1","user","hello")` → `"s1|user|hello"`
  (multi-arg canonical ABI works), empty-string edge, exact echo/tag round
  trips.

Driver detail worth recording: wasmtime's component `--invoke` takes a
**wave-encoded** call — dotted path `pkg/iface.func(...)`, comma-separated
quoted args in one expression. Bare `func("a","b")` fails: `_` is an invalid
token in the function name (`log_line` → parse error at 3..4), and multi-arg
only parses in the dotted form.

## The finding: split/indexOf fence the harness's reader artifacts

The boundary carries what concat builds. The harness's *reader* artifacts are
refused by the wasm emitter:

| Harness artifact | Needs | Wasm tier today |
| --- | --- | --- |
| durable pipe writer `log_line` | concat | ✅ canonical component |
| durable pipe **reader** (`durable_sessions.rvl` parse) | `split` | ❌ `unsupported builtin method 'split'` |
| agent `add` tool (`mtier/toolbox.rvl` `add_tool`) | `split` | ❌ same |
| web router (`web_page.rvl` `route_request`) | `indexOf` | ❌ `indexOf is not lowerable on this tier yet` |

So slice-3's claim holds — a Str boundary works — but the harness's full
agent wire protocol (toolbox parser, durable reader, web router) cannot yet
be a standard component. Ask (roadmap item 97): lower `split`/`join` and
`indexOf` on the wasm tier (linear-memory forms exist in the reference
backends; the tier's restriction table already names them as hard
EmitErrors). Repro: `tools/canonical_demo.py` (PASS) vs `add_tool` from
`mtier/toolbox.rvl` (REFUSED: `split`).
