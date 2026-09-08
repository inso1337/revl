# 530 — the webui-entry surface: enabling slice and the decision the full close needs

**Roadmap:** item 459 (issue #722), part of the 462 web-platform arc · **Builds on:** docs/design/526-webui-asset-alignment.md · **Status:** DECISION A RESOLVED (ambient coeffect, shipped) · B FOLDED INTO 457 · FULL CLOSE 462-GATED

## What shipped (the enabling slice)

459 wants the frontend off the "JS carried in revl strings" anti-pattern: external asset files, a real TS frontend, and source maps to the originals, behind a clean typed boundary next to Cordis WebUI rather than a parallel console framework (526).

`examples/webui-entry/` is the canonical shape for that boundary, built with only reviewed language surface:

- `console.rvl` contributes a frontend by naming external asset files and handing them to Cordis WebUI's `addEntry`.
- The binding is one real TypeScript module, `webui_host.ts`, reached through the item-396/410 host-ref door (`= @ts ref webuiAddEntry from "./webui_host.ts"`), the same door `stdlib/fs.rvl` uses to reach `backends/typescript/revl_fs_ts.ts`.
- The frontend entry `frontend/entry.client.ts` is a normal Cordis WebUI client module (Vite/Vue), referenced by path.

`tests/test_webui_entry_asset_ref_459.py` pins the properties the string console cannot state about itself: the emitted TS reaches the binding by importing an external module whose bytes are content-hash-pinned in the IR (the integrity and source-map anchor to the original file), passes the entry and Vite manifest through as external paths, and contains zero inline HTML/CSS/JS blob.

This moves the frontend boundary off JS-in-strings today. It does not fully close 459.

## Why this is a slice, not the close

459's exit test is "462's UI is built from external assets and a real TS frontend behind a typed boundary, with no JS extracted from revl strings and source maps pointing at the original files." Full closure needs the exemplary app (462), which does not exist yet. Two pieces are also underspecified at the language surface and want an architect decision before they are built:

### Decision A — how a revl component reaches the ambient `ctx.webui` service — RESOLVED: ambient coeffect

**Resolved: option 1, the ambient-service coeffect, shipped (builds on the #761 slice).** `examples/webui-entry/` now declares `service WebUI { emission fn add_entry(...) }` and `component ConsoleUI requires webui: WebUI { emit webui.add_entry(...) }`, on reviewed language surface only (`service` + `requires`, no new syntax). The emitted artifact resolves `webui` through Cordis' own `inject` (`inject: ["webui"]`, the body reads `ctx.webui`), so the host-provided ambient service satisfies the coeffect at wiring time. `examples/webui-entry/webui_host.ts` is now embedder setup that `provide`s the `webui` key (adapting the revl contract to Cordis `addEntry`); the revl artifact no longer imports it, and `globalThis.__revlWebui` is gone. `revl audit` reports the crossing as a first-class boundary: the `webui` requirement on `ConsoleUI` (G1) and the `webui.add_entry` emission (the trusted host boundary, G8). `tests/test_webui_entry_asset_ref_459.py` pins those properties and the absence of the global bridge. The remaining checker refinement — teaching a full composition GATE to admit an ambient/host-provided key without a revl `provides` (today an unprovided `requires` is a composition error at that gate, though `audit` already treats it as an outward reach) — is item-186/462 work, wanted once the exemplary app composes a real host: the reference and its audit surface do not need it, and inventing an ambient-provider syntax ahead of 462 was declined.

The two candidate surfaces that were weighed:

1. **Ambient-service coeffect.** A component `requires webui: WebUI` where `WebUI` resolves to the host's `ctx.webui` rather than a revl `provides`. This reuses the existing `requires` surface and the runtime's `ctx[key]` resolution (backends/typescript/runtime.ts:116), but needs a decision on how an ambient/host-provided key is distinguished from a revl-provided one at the checker (today an unprovided `requires` is a composition error). Relates to item 186 (ambient composition) and the 528 capability-reach note.
2. **A blessed stdlib module.** Promote the reference `webui_host.ts` binding into a `stdlib/webui.rvl` typed boundary. This is a smaller step but implies a blessed stdlib API on a ts-only tier, which is a stdlib-hygiene call (every current stdlib extern is multi-tier).

Recommendation: option 1, because it makes the entry's published surface an enumerable declared coeffect (the property 526 argues revl adds over the untyped `data: T`), and because 457's "one definition" work will want the same ambient-service treatment for `ctx.server` and `ctx.sso`.

### Decision B — the typed reactive-state / RPC contract

Cordis `addEntry(files, data)` takes an untyped `data: T` that becomes both the reactive state broadcast to the browser and, for each function on it, an RPC method the browser can call (526). 459's typed-boundary requirement is that this surface be a typed contract shared between the revl component and the TS client, so the browser's `useRpc<T>()` type and the server's published fields are one declaration. That is the same "one definition" idea as 457 applied to the console channel, and should be designed alongside 457 rather than invented here. The slice publishes an empty `data` surface deliberately.

## Requested of the architect

1. ~~Pick Decision A's surface (ambient-service coeffect vs. blessed stdlib module) so the ambient `ctx.webui` binding stops being a `globalThis` bridge.~~ RESOLVED: the ambient-service coeffect (option 1), shipped — see Decision A above.
2. Confirm Decision B is folded into 457's "one definition" scope (typed reactive-state/RPC as a shared declaration) rather than a separate surface.
3. With A and B decided, 459 closes as part of 462: the exemplary frontend is a Vite/Vue project registered through the chosen boundary, and this reference is the shape it copies.

No roadmap edit is made here (the roadmap is architect-owned); this note records the shipped slice and the exact decisions the close waits on.
