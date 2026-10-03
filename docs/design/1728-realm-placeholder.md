# Realm placeholders: an author writes `realm(?name)`, the operator binds it

Issue #1728. Status: implemented.

## The problem

A realm is an authority address. The approval policy scopes standing approvals
and auto-approve rules by `(component glob, realm)`, and an untrusted author
writes its own component names, so the realm is the only half of that scope
that binds it. The untrusted-author profile therefore refuses naming a realm
(G9, `admit_profile.check_no_realm_placement`, item 334 slice 2).

The rule is right, and it left an agent no way to author realm-scoped code at
all. Measured on main `5c3d21bf0`:

| source | trusted compile | untrusted-author profile |
|---|---|---|
| `isolate kv in realm("tenant_a")` | admits | refused G9: "this profile forbids naming a realm" |
| `isolate kv in realm(?tenant)` | parse refusal: "dynamic realm labels are not supported" | same parse refusal |

So `examples/tenants.rvl`, the canonical multi-tenant composition, cannot be
iterated on by an agent at all.

## The design

A **realm placeholder** separates the two facts the literal fused: that a
provision is isolated (the author's to say, it is the shape of the code) and
which realm it lands in (the operator's to say, it is the authority).

Compiled with no binding, this is refused by name (code G2); compiled with
`--bind-realm a=tenant_a`, it is `examples/tenants.rvl`'s `TenantAStore`
placed in `tenant_a`:

```revl reject G2
service Kv { fn get(k: Str) -> Opt[Str] }

component TenantAStore provides kv: Kv {
  isolate kv in realm(?a)
  provide kv { fn get(k) = None }
}
```

- **Syntax.** `isolate <key> in realm(?<name>)`. `?` followed by an
  identifier, in the singular form only. The plural routing form
  `realms(...)` still takes literals; a placeholder there stays a parse
  refusal.
- **Binding.** The operator binds `<name>` to a realm. The binding comes from
  the operator's side only:
  - `revl compile --bind-realm NAME=REALM` (repeatable);
  - `revl mcp serve --bind-realm NAME=REALM`, which binds every source the
    server admits;
  - `AdmissionProfile.realm_bindings`, for code that builds the profile;
  - `Gate.propose(source, ..., realm_bindings={NAME: REALM})`, for an embedder.
    The caller of `propose` is the operator's harness, not the agent, so this
    is the "admission call" the issue asks for.

  No MCP verb argument binds a realm. The arguments of `revl_check`,
  `revl_admit`, `revl_swap` and the rest come from the agent, and letting
  them carry a binding would hand the agent the authority the profile exists
  to withhold (`test_the_agent_cannot_bind_through_the_call`).
- **When.** `realm_placeholders.bind` runs after the profile's structural
  checks and before lowering, in both compile paths (`compile_source` and
  `compile_files`, roots with their own profile, imported modules with the
  compile's). The placeholder's `IsolateStmt.realm` becomes the bound realm,
  so everything downstream sees an ordinary realm the operator chose:
  - the G2 link check over `(key, realm)`;
  - the manifest's `isolate` map;
  - the approval policy's realm scoping.

  A bound placeholder compiles to the same manifest as the literal source
  (`test_a_bound_placeholder_compiles_and_admits_for_an_untrusted_author`).

## The rules, and why each is sound

1. **G9 is unchanged for a literal realm.** `check_no_realm_placement` skips
   only an `isolate` that names a placeholder. `realm("tenant_a")` under the
   untrusted profile is refused exactly as before.
2. **An unbound placeholder is refused by name** (code G2). It has no realm,
   so there is nothing to check provision disjointness against, and sending
   it to the shared realm would decide for the operator. The refusal names
   the placeholder and the key, and says the operator binds it.
3. **G2 is checked after binding.** Two placeholders the operator binds to
   one realm, over the same key, are the same `(key, realm)` address and are
   refused as a provision conflict
   (`test_two_placeholders_bound_to_one_realm_are_checked_for_g2`). Two names
   are not two realms; the binding decides.
4. **The bound value is validated** with the realm-label grammar a literal
   obeys. A malformed binding is a flag error, not a compile.

What the author can still do with a placeholder is choose WHICH of its own
provisions share a placeholder. That is the code's structure, not an
authority: the operator decides what each placeholder means, and can bind two
of them apart or together.

## Self-host and gate parity

The gate (`selfhost/lower.rvl`'s `admit_src`, `admit_all` and
`admit_ambient`, embedded in `crates/revl-gate`) reads realm clauses at the
token level. It takes no bindings, so for it every placeholder is unbound, and
refusing it is the fail-closed answer. `realm_placeholder_scan` refuses the
first placeholder with the reference's sentence and tag (`G2`), after the
parse-stage refusals and before any composition reasoning. That is where the
reference raises it too: the binding runs before lowering, so a later lowering
refusal does not outrank it.

`tests/test_selfhost_lower.py` holds both cases to agreement, and its
classifier names the sentence `G2`. The census reads them as `agree-refuse/G2`.

A placeholder the operator HAS bound is a program the reference admits and
the gate refuses. That is the direction the gate is allowed to err in (a
`false-reject`, never a bypass): an embedder that binds realms admits through
the reference compiler. The self-host frontend's own lowering (`compile_to`)
has no binding input and is not changed; a corpus document carrying a
placeholder would need one.

## Not in this change

- **Placeholders in `realms(...)`.** The multi-realm route names the realms a
  router spreads across. A placeholder list is a straightforward extension of
  the same binding, and was left out to keep the surface this issue asked for.
- **Placeholders outside `isolate`.** A composition's `remote` and `host`
  rows also take `in realm("...")`. Those are written by the operator in a
  composition document, not by an agent, so the case does not arise.
