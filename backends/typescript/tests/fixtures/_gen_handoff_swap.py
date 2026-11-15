"""One-off generator for the state hand-off fixture. NOT part of the build.

Item 53 (docs/state-handoff.md) on the ts tier: a provider that declares a
`handoff` hands its effect-created state to a compatible successor on a swap,
so the successor starts WARM instead of cold. `tests/handoff_swap.test.ts`
drives the pair this script writes:

  * the running generation: `Cache` provides `cache`, declares
    `handoff cache: Map[Str, Str]` and runs `version() == 1`;
  * the successor: `CacheV2` re-provides `cache`, declares the same hand-off
    and runs `version() == 2`. It is compiled against the running manifest with
    `replacing=("Cache",)`, so this file is proof that the SAME admission gate
    the py tier uses admits the candidate; an incompatible shape (e.g.
    `Map[Str, Int]`) is refused there and never reaches the runtime.

The two components are emitted into ONE module because this tier typechecks one
test file as one program and two modules providing the same service would merge
two augmentations of `cordis`' `Context` (TS2717). Each component is still real
`compile_source` output; only the component lists are concatenated.

Run once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_handoff_swap.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _cache(component: str, version: int) -> str:
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
component {component} provides cache: Store {{
  handoff cache: Map[Str, Str]
  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
"""


def build() -> dict:
    running = compile_source(_cache("Cache", 1), "handoff_swap.rvl")
    # The successor is admitted against the running manifest exactly as a
    # swap's candidate is: same provided key, §5-compatible declared state.
    successor = compile_source(
        _cache("CacheV2", 2), "handoff_swap_v2.rvl",
        manifest=running, replacing=("Cache",))
    combined = dict(running)
    combined["components"] = running["components"] + successor["components"]
    return combined


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "handoff_swap.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
