# The composition bootstrap: the one place host code is unavoidable

A revl composition normally comes from a fixed list of files: `revl run
a.rvl b.rvl c.rvl`, or `revl_check`/`revl_admit` with sources handed straight
to the compiler. Some hosts want more than that: the file list itself depends
on runtime configuration (which interface is booting, which environment tier
is selected), and they would rather compute it in revl, next to the
components it describes, than duplicate that logic in the host language.
That is a **composition manifest**: a revl document whose exported function
returns the file list for the composition it describes.

revl-harness does exactly this. `src/manifest.rvl` declares the harness's
shared core plus every interface, and exports `mf_files(mode: Str) ->
List[Str]`, resolved by boot mode (`voice`, `code`, `cli`). Its own header
states the intent plainly: the host's only role left is to bootstrap-compile
this one file, call `mf_files`/`mf_config` with the mode flag, and append the
environment tier chosen by CLI flags. Everything the harness is lives in
revl from there.

## The two-stage bootstrap

Resolving a manifest-driven composition takes the host two compiles, not one.

1. **Compile the manifest alone.** `revl-harness`'s `_manifest_module()`
   (`tools/server/app.py`) runs `compile_files(["src/manifest.rvl"])`, emits
   it to Python, and execs the result as a module, so `mf_files` and
   `mf_config` become callable Python functions. This step cannot be revl
   code calling revl code: nothing has compiled the manifest yet, so nothing
   revl can call it.
2. **Ask it for the mode's files, then compile the rest.** `_composition()`
   calls `man.mf_files(mode)` to get the real file list, appends whatever the
   environment tier adds (in the harness's case, the model provider and the
   persistence layer; see the comment above `_composition` for the exact
   fields), and compiles that list as the actual, running composition.

Only step 1 is irreducible host code. Step 2's compile is an ordinary
composition compile, indistinguishable from any other `revl run`; it just
happens to be handed a file list that revl itself computed.

## Why step 1 cannot be revl

A revl document cannot exist as a running thing before a revl runtime
compiles it. The manifest is revl source; before it is compiled, it is text,
not a callable. So the very first compile of any session, the one that turns
the manifest from text into something that can answer "what files am I?", is
necessarily driven by a runtime that already exists outside revl: host code,
by construction.

This is not specific to revl-harness's design, and it is not a gap this
project can close by shipping more revl. **It is true of every composition
framework that lets the composition describe its own shape.** The host
runtime that will eventually run the composition necessarily precedes the
composition, because something has to compile the first document before
there is anything to hand control to. A framework in any language hits the
same floor the moment it lets its own configuration be written in the
language it configures. Document this as the honest floor, not as a defect
to file against: the goal is a minimal, well-understood host stage, not a
zero one, because a zero one is not reachable.

## What stays true above the floor

The manifest keeps being the single source of truth for the file list once
step 1 has run. In revl-harness's case that already holds: `mf_files`
decides core plus interface, and the Python host only appends the
environment tier (model provider, durable store), which is runtime-selected
(CLI flags), not part of the composition's own shape. The host does not
maintain a second, parallel file list; when the manifest fails to compile
mid-refactor, `_composition()` falls back to a known-good hardcoded list
instead of guessing, and that fallback is itself the only place host code
substitutes for the manifest's authority. Keep it that way: if a host ever
needs to grow its own append beyond an environment tier, that is a sign the
manifest should grow a new mode instead, not that the host should start
deciding composition shape.

## The environment tier is the other half

Step 2's "append whatever the environment tier adds" is the second thing the
host knows before the composition exists: the port, the auth token, the data
dir, which model provider to boot against. That half has its own declaration
now — a `boot` component, whose `config {}` block is the typed, bounded,
audited contract for exactly those values, with `revl run --env` as its one
door. The floor described above is unchanged (something still has to compile the
first document); what moves is that the environment values crossing it are no
longer an undeclared host-authored map. See
[environment-binding.md](environment-binding.md).

## See also

- `src/manifest.rvl` and `_composition()` / `_manifest_module()` in
  `tools/server/app.py`, both in the revl-harness repository, the worked
  instance this doc generalizes from
- [v2.0-roadmap.md](v2.0-roadmap.md) item 348, where this finding was filed
- [environment-binding.md](environment-binding.md), the environment contract
  the host injects across this same boundary (roadmap item 350)
- [interchange-format.md](interchange-format.md), a different sense of
  "manifest" (the linker's provides/requires/load-order manifest emitted by
  `revl audit --json`), not to be confused with a composition manifest
  document like `manifest.rvl`
