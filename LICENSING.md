# Licensing

Copyright (c) 2026 Thomas Moussajee. All copyright in this repository is held
by Thomas Moussajee unless a file says otherwise (see "Third-party material").

revl is licensed under two licenses, split by what a file is for. The rule is
simple: the compiler and everything that decides admission is AGPL-3.0; anything
that ends up inside YOUR program is MIT, and the program the compiler produces
from your source is yours.

## The compiler, the gate, the tooling: AGPL-3.0-only

Licensed under the GNU Affero General Public License, version 3 only, as
published by the Free Software Foundation. The full text is in [LICENSE](LICENSE).

- `src/revl/` (the reference compiler, checker, MCP admission gate, CLI)
- `selfhost/` (the compiler written in revl)
- `crates/` (`revl-gate`, `revl-gate-wasm`, `revl-lsp`)
- `backends/*/emit.py` and `backends/typescript/emit_temporal.py` (the emitters
  are compiler source; the rest of `backends/` is MIT, below)
- `tools/`, `tests/`, `tck/`, `formal/`, `schema/`, `registry/`, `bench/`,
  `dogfood/`, `ci/`, `site/`, `playground/`, `docs/`, and every other path not
  named in the MIT list below

The AGPL's network clause (section 13) applies. If you run a modified revl,
including a modified admission gate, as a service that others interact with over
a network, you must offer them the corresponding source of your modified
version under this license.

## What ships inside your program: MIT

Licensed under the MIT License. The full text is in [LICENSES/MIT.txt](LICENSES/MIT.txt).

- `stdlib/` (the revl standard library, compiled into your components)
- `backends/`, except the emitters listed above: the runtime shims, bridges,
  placement runners, host stubs and harnesses that emitted code copies, links or
  imports (`backends/python/runtime.py`, `backends/typescript/runtime.ts`,
  `backends/rust/placement_runner`, `backends/go/placement_runner`,
  `backends/java/placement`, and their siblings), together with the goldens and
  scenarios that document them
- `examples/` and `demo/`
- `tree-sitter-revl/` (the editor grammar)

These are MIT so that a component compiled with revl carries no AGPL obligation.
A closed-source program may be written in revl, compiled with revl, and shipped
with the MIT runtime shims inside it.

## Output of the compiler

The IR, the emitted host code, bundles, attestations, plans and every other
artifact `revl` produces from source you wrote are yours. They are not a covered
work under the AGPL; section 0 of the license covers the compiler, not its
output. The only revl-authored material that can appear in your output is the
MIT-licensed runtime and stdlib code above.

## Third-party material

Files under `forks/` are modified copies of upstream runtimes and keep their
upstream license (`forks/stc-go/LICENSE`). The runtimes revl lowers to
(cordis-py, cordis, cordis-rs, cordis4j, stc-go) are separate projects under
their own licenses; revl does not relicense them. `backends/java/stubs/`
reproduces the cordis4j API surface for compilation only.

## Name and logo

"revl", "truc" and the revl logo are trademarks of Thomas Moussajee. Neither
license above grants trademark rights; see [TRADEMARK.md](TRADEMARK.md).

## Contributions

By contributing to this repository you agree that your contribution is licensed
under the license that covers the path it touches, as listed above. Automated
tooling and language models used to draft or edit files in this repository do so
under the direction of the copyright holder and create no separate authorship or
copyright claim.

## History

Versions of revl published before this file was added were released under the
MIT License with the copyright notice "revl contributors". Those releases stay
MIT; this document governs the tree from the commit that introduced it onward.
