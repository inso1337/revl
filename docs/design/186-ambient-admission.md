# 186 — ambient / running-manifest composition admission

Issue #86, roadmap item 186. This note records the shape of the ambient
(running-manifest) admission entry point and lands its FIRST concrete slice:
provision disjointness against an already-running manifest.

## The gap

`admit_src(src)` (`selfhost/lower.rvl`) is the single-source gate. It answers
one question: *does this one program text admit?* It has everything it needs in
the text — every component, every provision, every requirement is in front of
it, so its G2 provision-disjointness scan (`_link` / `collect_g2`) compares each
component's provision keys against the other components in the SAME text.

The reference `check_and_lower` carries a second input `admit_src` has no
analogue for: `ambient`. That is a DIFFERENT admission entry point — not "does
this program admit" but "may this component join a composition that is ALREADY
RUNNING". The running composition is not in the incoming text; there is nothing
in one program to compare it against. So this needs a NEW entry point that takes
the running manifest as a second argument:

```
admit_ambient(src: Str, manifest: Str) -> Str
```

The full surface behind `ambient` is a wave, not a slice (roadmap item 186 says
so, folded in from 419f): hot-swap / service replacement, `handoff` STATE
replacement, and a differential oracle that constructs a running manifest on
BOTH the reference and the selfhost side. This note carries the whole shape so
it is not lost, and lands the one piece that is self-contained TODAY.

## Slice 1 — provision disjointness against the manifest

The manifest is the linker's already-live provision table: which component
provides which key, in which realm. A component being admitted against it
conflicts exactly when it provides a `(key, realm)` the manifest already
provides — the cross-manifest analogue of the in-text G2 conflict `admit_src`
already computes among the components of one text.

### Manifest wire

The manifest is serialised as rows `"<component>/<key>/<realm>"` joined by `;`,
realm `""` meaning the shared realm:

```
Store/db/ ; KvA/kv/tenant_a
```

`parse_manifest` reads it into the same `List[Prov3]` (`{key, rlm, comp}`) the
in-text linker builds. An empty manifest is the empty table, so
`admit_ambient(src, "")` is `admit_src(src)` exactly — the base invariant.

### Algorithm

1. Run `admit_src(src)`. An ambient composition never launders an internally
   refused component, so a non-empty single-source verdict is forwarded
   unchanged. (This is the seam a later wave relaxes for handoff /
   state-migration, where a replacement legitimately overrides what the manifest
   already holds — see "What remains".)
2. Seed `collect_g2` with the manifest's provisions instead of an empty table
   and scan the incoming components' provision keys against it. A key the
   manifest already provides in the same realm is a conflict, reported against
   the incoming component (the SECOND provider), byte-identical to the wording
   `_link` uses for an in-text G2:

   ```
   G2|provision conflict: key `db` is provided by both Store and NewComp (G2)
   ```

   Per-realm separation carries over unchanged: the same key in a DIFFERENT
   realm composes (multi-tenancy), the same key in the SAME realm conflicts.

### Why this is a real differential, not a self-check

The reference exposes no `admit_ambient` to drive directly, but slice 1 has an
exact single-source equivalent: admitting component `X` against a manifest that
holds component `M`'s provisions is, for the provision-disjointness guarantee,
the SAME verdict as single-source-admitting the text `M ++ X`. So the oracle is

```
admit_ambient(X, manifest_of(M))  ==  admit_src(M ++ X)  ==  reference(M ++ X)
```

`tests/test_selfhost_lower.py` pins all three legs. This equivalence is precisely
what a later wave BREAKS — a hot-swap replaces `M`'s provision rather than
conflicting with it, and there is no single-source text whose G2 scan models
replacement — which is why replacement is a wave and disjointness is a slice.

## Slice 2 — multi-realm routing validation against ambient realms

Item 162's `isolate <key> in realms("a","b")` route distributes a *required* key
across N named realms; the link-time half checks every routed realm has a
provider (`route_realm_scan` / `collect_routes`). For the single-source path
that provider must be in the same text. Against a running manifest, a route may
legitimately target a realm provided ONLY by the manifest — the store already
running that a newly admitted router binds to.

Slice 1 seeded only `collect_g2`. Slice 2 makes `admit_ambient` recompute the
WHOLE G2/ROUTE/G3 link seeded with the manifest (`link_refusals(pg, seed)`),
because a route refusal is *composition-dependent*, not internal: forwarding the
self-verdict wholesale (slice 1's `if admit_src(src) != "" return it`) wrongly
refused a route the manifest satisfies. So `admit_ambient` now splits
`admit_src` into its internal refusals (`collect_nonlink` — per-component,
module-fn, spawn, boot, shared verbatim with the single-source path) plus the
manifest-seeded link, ordered together by `(line, seq)`.

The oracle is the same M ++ X equivalence, since a `realms(...)` route reads the
per-`(key, realm)` provider table `find_provider` computes and the manifest wire
fully populates it:

```
admit_ambient(Router, manifest_of(StoreA, StoreB)) == admit_src(StoreA ++ StoreB ++ Router)
```

A realm provided by neither the text nor the manifest still refuses ROUTE,
byte-identical to the composed single source. `tests/test_selfhost_lower.py`
pins both directions (admit and the surviving-dangling-leg refusal) plus the
diagnostic-ordering case where an earlier-line ambient G2 outranks a later
internal refusal — the merge slice 2 enables.

## What remains for full closure of #86 / item 186

- **`handoff` STATE replacement / service replacement / hot-swap.** The
  replacement semantics that break the single-source equivalence above. Needs a
  reference-side ambient oracle (construct a running manifest on both sides), the
  wave the roadmap describes. This is the seam `collect_nonlink` deliberately
  keeps closed: an internally refused component is still forwarded, never
  laundered by the manifest.
- **G3 acyclicity across the manifest boundary.** Slices 1-2 seed the G2/ROUTE
  provider table, which the wire (rows of PROVISIONS) fully carries. The
  manifest's provider→consumer edges — which require the manifest to also carry
  each running component's REQUIREMENTS — are not folded into the acyclicity
  scan, so a cycle that closes THROUGH the running manifest is not yet caught.
  Extending the wire is part of the replacement wave.
- **Residual coloring approximation (b).** Callee collection descends into a
  nested COERCED arrow instead of stopping (`stop_async_arrows`); reached by no
  fixture, rule-2 masks it. Unchanged by this slice; tracked in item 186.
- **419c line-ordered collecting sink** already landed (`collect_refusals` /
  `admit_all`); its interaction with ambient refusals (an ambient entry has no
  declaration line and defaults to 1) is noted but only exercised for the
  single-conflict case here.

None of this is required for the native single-source `revl_compile`; item 186
stays lower priority.
