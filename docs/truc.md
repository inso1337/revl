# truc — the revl component manager

**Name (decided 2026-08-24): `truc`, always lowercase.**

## Identity

The identity is the act of assembly: taking little bits of stuff and putting
them together into something bigger. That is the composition model itself —
components are the *petits bouts*, the composition is the assembly — and it is
how agents are meant to build with revl. French, playful, deliberately
unpretentious; the anti-"install".

**Tone rule:** the inspiration (the Stupeflip DIY-collage ethos) is referenced
in prose only — never quote the lyric in docs, taglines, release notes, or
marketing. People who know, know.

## Vocabulary

- dependencies are **trucs** (the plural is the gift)
- `truc add <name>` / `truc rm <name>` — pull a component into the composition
- `truc assemble` — resolve → admit (G2) → compose. The flagship verb: truc
  *assembles*, it does not "install"
- `truc ship` — publish (the verb the ecosystem already uses)
- manifest `truc.toml`, lockfile `truc.lock`, vendored components in `trucs/`

## Entry points

Dual: standalone `truc …` for the brand and the registry surface, and
`revl add` / `revl assemble` aliasing into it so the language keeps one front
door for newcomers.

## What makes it not-npm

Every fetched component passes the admission gate before it joins the
assembly, and the registry index already carries each component's
provides/requires surface, capability count, and manifest/source hashes
(`registry/index.json`). truc's promise is not "packages, faster" — it is
*assembly you can admit*: the bits declare what they cross before they're in.

See roadmap item 136.
