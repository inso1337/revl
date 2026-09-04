# Security Policy

revl's entire pitch is a safety claim: a component that would corrupt a running
system *cannot compile*. A language that makes that claim owes you a clear way
to report where it fails to keep it. This document is that way.

## What "a security issue in revl" means here

revl is a compiler, not a running service — so the threat model is unusual, and
worth stating plainly. A report is in scope if it shows one of these:

- **A soundness escape.** A `.rvl` program that the checker *accepts* but that
  violates a guarantee it promises to enforce: G1–G9, the `Secret` families
  (`G-SECRET`, `G-SECRET-FLOW`), the typing rules T1–T3, or the lifecycle rules
  A1, A2, A3, A5, A6, A8 and A9 — listed rather than given as a range, because
  A4 and A7 are not guarantee codes. The set is generated from
  `revl.diagnostics.GUARANTEES` into [DESIGN.md](DESIGN.md) §4 and
  [docs/rejections.md](docs/rejections.md), so neither can drift from the
  compiler. Undeclared access that compiles, a
  mutation with no inverse and no `emit` that compiles, a dependency cycle or
  provision conflict that links, teardown that leaves residue the checker
  claimed it could not — each is a security issue, because downstream systems
  admit generated components on the strength of "it compiled." This is the
  highest-severity class for this project.
- **An audit-surface escape.** A boundary crossing — an `extern`, an
  `emission`, a host-object seam — that does **not** appear on the G8 audit
  surface (`revl audit`). If code can reach outside its declared capabilities
  without `revl audit` naming it, the review surface that agents and CI rely on
  is compromised.
- **Emitted-code injection or unsafety.** A `.rvl` source that causes an
  emitter to produce host code (Python, TypeScript, Rust, Java, Go, or wasm)
  that executes something the source did not describe — injection through a
  template string, an identifier that escapes host-keyword sanitization (A3),
  an emitted construct that is memory-unsafe or breaks its runtime's sandbox.
- **MCP / admission-gate bypass.** `revl mcp serve` is an admission gate for
  agent-authored components. A candidate that `revl_admit` accepts but that
  should have been refused, a path that reaches the filesystem or host through
  the MCP tools when it should not, or a rejection that fails to carry its
  structured diagnostic in a way that lets a bad component through — all in
  scope.
- **Toolchain / supply-chain issues** in what this repo ships: the packaged
  `revl` distribution, the release workflow, or the pinned toolchain versions
  in CI.

### Out of scope

- **Bugs in the runtime targets themselves.** revl emits for community Cordis
  ports (cordis-py, cordis, cordis-rs, cordis4j, cordis-wasm, stc-go). A defect
  in one of *those* runtimes belongs to that project — report it there. A
  divergence revl already documents in
  [docs/contract-errata.md](docs/contract-errata.md) is a known limitation, not
  a new vulnerability; check the errata first.
- **A `.rvl` program that the checker correctly *rejects*.** That is the
  feature working. If you think a rejection is wrong, open a normal issue.
- **Denial of service from a pathological source** (a deeply nested program
  that makes the checker slow) unless it is trivially triggerable and clearly
  disproportionate — file it, but expect it triaged as a normal bug.
- **Findings that require a modified compiler, a modified runtime, or a
  privileged position on the developer's machine.**

If you are unsure whether something is in scope, report it privately anyway and
let us make the call. A false alarm costs a few minutes; a missed soundness
hole costs the project's one promise.

For why capability confinement makes revl structurally resistant to prompt
injection — and where that resistance stops — see
[docs/prompt-injection-resistance.md](docs/prompt-injection-resistance.md).

## How to report

**Please do not open a public issue for a suspected vulnerability**, and please
do not describe it in a pull request, a discussion, or any public channel until
it has been addressed.

Use **GitHub's private vulnerability reporting**:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** (this opens a private GitHub Security
   Advisory visible only to you and the maintainers).
3. Fill in the advisory. A private fork for developing and testing a fix can be
   created from the advisory itself.

Direct link: **https://github.com/inso1337/revl/security/advisories/new**

<!--
MAINTAINER TODO: if you want a fallback contact for reporters who cannot use
GitHub Security Advisories, add a real address here (a monitored inbox, ideally
one you can encrypt to). Do not ship a placeholder address — an unanswered
security inbox is worse than none. Until one exists, GitHub private advisories
above are the only supported channel.
-->

If GitHub Security Advisories are unavailable to you, say so in a minimal public
issue that contains **no exploit details** — just "I have a private security
report, how should I send it?" — and a maintainer will arrange a channel.

### What to include

A soundness report is only actionable with a reproducer. Please include, as
much as applies:

- the **`.rvl` source** that triggers it (minimal is ideal — the rejection
  suite in `examples/rejections/` shows the size we work with);
- the **target tier** (`py` / `ts` / `rust` / `java` / `wasm` / `go`, or "all")
  and the exact command (`python -m revl compile …`, `revl audit …`,
  `revl mcp …`);
- **what the compiler did** (accepted, emitted, admitted) and **what it should
  have done** (which guarantee or lifecycle rule you believe was violated, by
  its code from `src/revl/diagnostics.py`);
- for an emitted-code issue, the **emitted output** and why it is unsafe;
- the revl version or commit SHA, and the toolchain versions if a runtime is
  involved.

## What to expect

This is a small research project, not a vendor with an SLA, so these are honest
intentions rather than contractual guarantees:

- **Acknowledgement** within about **5 business days**.
- An initial **assessment** — in scope or not, and a rough severity — within
  about **10 business days**.
- For an accepted report, we will work with you on a fix, keep you updated, and
  agree with you on timing before any public disclosure. **Coordinated
  disclosure** is the default: we ask that you give us a reasonable window
  (typically up to **90 days**, shorter for an actively exploited issue) before
  publishing.
- **Credit.** With your consent, we will credit you in the advisory and the
  release notes. Tell us the name/handle to use, or ask to stay anonymous.

## When a fix lands

revl's soundness surface is *self-proving by design*: every guarantee has a
rejection fixture the checker must refuse, and the fixture set is exhaustive by
test (`tests/test_frontend.py::test_every_rejection_file_is_covered`). A fixed
soundness hole therefore does not just get patched — **it joins the executable
spec**: a new fixture in `examples/rejections/`, a new row in the `REJECTIONS`
table, so the escape can never silently reopen. A security fix that cannot be
expressed as a rejection (an emitter or runtime-boundary issue) lands with the
regression test that pins it. See [CONTRIBUTING.md](CONTRIBUTING.md).

Fixes ship in a new tagged release published to PyPI; see
[docs/stability.md](docs/stability.md) for what a version number promises.
