# 529 — aligning revl authentication with Cordis SSO

**Roadmap:** item 457 (issue #720), whose exit test requires an explicit authorization step; part of the 462 arc · **Source:** cordiverse/sso study, 2026-09-08 · **Status:** DESIGN DRAFT

## Purpose

457's exit test says removing an endpoint's explicit auth step must deny the request, and generated route metadata alone must grant nothing. That requires a real authentication service to be the auth step. Cordis ships one (`@cordisjs/plugin-sso`). This note records its shape so the exemplary app authenticates against a bound Cordis service rather than a hand-rolled token check, and so the "explicit auth" step in 462 has something concrete to call.

## What Cordis SSO provides

The service is `ctx.sso` (`packages/core/src/index.ts:9`, `class Sso extends Service`, name `sso`, injects `database`).

- **Identity model** lives in the database (it binds Cordis Database, see 527). The `Sso` constructor declares three tables (`index.ts:348-378`): `sso.user` `{ id, name?, display?, createdAt, updatedAt }`, `sso.identity` `{ id, userId, provider, createdAt }`, `sso.session` `{ token, userId, identityId, createdAt, expiresAt }`. A user has many identities (one per provider); a session is one bearer token bound to a user and identity.
- **Sessions are bearer tokens.** `createSession(userId, identityId)` mints a UUID and stores it with an expiry (`:481`); `validateSession(token)` returns the `User` or null, evicting expired tokens (`:495`); `destroySession` / `destroyUserSessions` revoke (`:505,:509`). `sessionMaxAge` is config (default 7 days, `:339`).
- **Providers are pluggable** behind `abstract class SsoProvider` (`:59`), which self-registers via `ctx.sso.register(this)` in `[Service.init]`. Three built-in categories cover the common flows: `CredentialsProvider` (password-style, `:83`), `ChallengeProvider` (TOTP / WebAuthn issue+verify, `:134`), and `RedirectProvider` (OAuth authorization-redirect, `getAuthUrl`, `:248`). A separate `@cordisjs/plugin-server-oauth` makes the app an OAuth *provider* (`oauth_client`/`oauth_code`/`oauth_token` tables). Intent kinds are `login | register | bind | stepup` (`:282`), so account linking and step-up MFA are first class.
- **How a plugin consumes an authenticated identity.** The HTTP surface (`@cordisjs/plugin-sso-server`, `packages/server/src/index.ts`) is registered on `ctx.server` (the Cordis HTTP server, `ctx.server.get/post/delete`). A protected route pulls the bearer token from the request (`extractToken`, `:112`) and calls `ctx.sso.validateSession(token)` to get the `User` before doing anything (`requireSession`, `:104`; used at `:49,:56,:63,:73`). That `validateSession -> User` call is exactly the explicit auth step 457 wants.

## What revl reuses vs. adds

Reuse:

- Bind revl's auth `service` to `ctx.sso`. The identity/session/user shapes and the `SsoProvider` categories are the surface; the exemplary app should not define its own user table or token scheme. Auth over HTTP is `extractToken` + `ctx.sso.validateSession`, layered on the same `ctx.server` runtime that 456/457 target.

Where revl's static gate adds value:

- **Explicit, non-derivable auth (457's core requirement).** revl models `validateSession` as a coeffect the handler must reach and call; because routing is *derived* metadata and the auth step is a *declared reach*, generated routing can never stand in for it. Deleting the auth call is a visible removal of a declared step, which is what makes "remove the auth step and the request is denied" a checkable property rather than a convention.
- **Identity as typed, taint-tracked authority.** The `User` from `validateSession` is the authority the rest of the handler acts under. revl's G9/taint layer (docs/design/249-taint-provenance.md) can treat the authenticated `userId` as the declassified root of authority, so an unauthenticated request cannot manufacture a `userId` to reach user-scoped data. This pairs with the runtime capability check (528): SSO says *who* the session is, `ctx.capability` says *what* that session may do.

## Exit alignment (feeds 457 / #720)

A protected endpoint in 462 authenticates by reaching `ctx.sso.validateSession` as an explicit, declared step; removing that reach denies the request; and the authenticated `User` is the typed authority handed to the capability check, with no grant derivable from routing metadata alone.

## Proposed roadmap note

457 is currently framed around route/wire-type derivation with auth staying explicit. Recommend either extending 457's owner scope to name `ctx.sso` as the concrete auth service the "explicit auth step" binds to, or adding a small sibling item for auth-service binding adjacent to 457. Exact proposed text is in the PR report.
