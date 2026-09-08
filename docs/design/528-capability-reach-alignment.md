# 528 — Cordis capability (runtime) vs. revl G1 reach (compile-time)

**Roadmap:** DESIGN.md G1 (declared access, Def. 25) and item 294 parameterized capabilities; relevant to the explicit-authorization requirement of 457 (#720) · **Source:** cordiverse/capability study, 2026-09-08 · **Status:** DESIGN DRAFT

## Purpose

revl's central guarantee G1 is compile-time: a component reads only what it declares, and undeclared access is a type error (DESIGN.md pillar 3, Def. 25). Cordis ships a runtime capability service. They are not competitors; they cover different axes. This note records what `@cordisjs/plugin-capability` actually does and states where each layer is authoritative, so revl binds to the runtime service for the data-dependent axis rather than trying to make G1 do a job it structurally cannot.

## What Cordis capability provides

The service is `ctx.capability` (`packages/core/src/index.ts:5`, `class Capability extends Service`, name `capability`). It is small and entirely runtime:

- **Declare:** `define(pattern, { check, inherits, depends, list })` (`index.ts:76`) and the shorthand `provide(pattern, check)` (`:89`). Patterns carry named placeholders, e.g. `resource(id)`, compiled to a matcher (`createMatch`, `:26`); this is the parameterized-capability shape.
- **Check:** `check(name, session)` (`:110`) returns a `Promise<boolean>`; it is satisfied if the session already lists the capability string (`session.capabilities`) or some registered `check(data, session)` returns true. `session` is `{ capabilities?: string[], userId?: number }` (`:46`).
- **Attenuate / compose:** `inherit` and `depend` links (`:93,:97`) form `inherits`/`depends` graphs walked by `subgraph` (`:125`); `test(names, session)` (`:142`) grants only when every dependency's inheritance closure passes a check. Grants are dynamic (an `ctx.effect` registration, `:83`) and per session.

Every input is a runtime string and every decision depends on the live session and user. Nothing here is known before the request arrives.

## Where each layer is authoritative

The two answer different questions and compose cleanly:

- **revl G1 answers "can this code reach this authority at all?"** It is structural and static: a component names its coeffect requirements, and the checker proves the body cannot reach a service or key it did not declare (Def. 25, G8 makes the boundary surface enumerable). This bounds *which capability keys a component could ever exercise* to a minimal, enumerable set, with no ambient escalation and no proxy-trap runtime cost. Cordis capability cannot state this: its store is a runtime list, so it cannot prove a plugin will never call a check it was not meant to.
- **Cordis capability answers "may this session, for this user, act on this resource instance right now?"** That is inherently data-dependent (the `(id)` placeholder, the `userId`, the granted-string set) and cannot be decided at compile time. revl should not try; it should require the component to hold the `Capability` coeffect and call `check`/`test` explicitly.

The alignment: revl statically fixes the *set* of capabilities a component can touch (reach, G1/G8), and delegates the *per-request grant decision over runtime values* to `ctx.capability`. Because G1 already bounds the surface, the runtime capability set a component registers is itself enumerable and auditable, which the standalone runtime library cannot guarantee. This is also the mechanism behind 457's rule that generated route metadata must never grant access: routing is derived, but the `ctx.capability.check` call stays an explicit, separately-reached step.

## Relationship to existing revl items

- Complements item 294 (parameterized capabilities): the `(group)` placeholder in Cordis patterns is the same parameterization revl models statically; the runtime service supplies the value-level check revl leaves open.
- Complements G9 / taint (docs/design/249-taint-provenance.md): revl prevents untrusted data from *creating* authority without a declassification; Cordis capability decides whether an already-declared authority is *granted* for this session. Different halves of the same boundary.

No new roadmap item proposed; this is a relationship note to prevent duplicating a runtime permission engine inside the language.
