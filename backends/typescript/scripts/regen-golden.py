"""Regenerate the TypeScript goldens from their reference IR.

`user_cache.ts` uses the repo copy (../../examples/user_cache.ir.json) when
this backend sits inside the revl repo, falling back to the vendored
byte-identical fixture. `fr3_json.ts` is the FR-3 stdlib JSON module
(stdlib/json.rvl) emitted from a committed fixture; it is `tsc`-validated by
`npm run typecheck` (tsconfig includes golden/**), which pins the `Any` ->
`any` mapping (roadmap 79) against a bare-`Any` regression.
"""

import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from emit import emit  # noqa: E402

repo_ir = BACKEND.parent.parent / "examples" / "user_cache.ir.json"
fixture_ir = BACKEND / "tests" / "fixtures" / "user_cache.ir.json"
source = repo_ir if repo_ir.exists() else fixture_ir

ir = json.loads(source.read_text(encoding="utf-8"))
out = BACKEND / "golden" / "user_cache.ts"
out.write_text(emit(ir), encoding="utf-8")
print(f"regenerated {out} from {source}")

fr3_ir = BACKEND / "tests" / "fixtures" / "fr3_json.ir.json"
fr3_out = BACKEND / "golden" / "fr3_json.ts"
fr3_out.write_text(emit(json.loads(fr3_ir.read_text(encoding="utf-8"))),
                   encoding="utf-8")
print(f"regenerated {fr3_out} from {fr3_ir}")

# async extern (roadmap item 80): the harness `http_post` shape — an
# `emission async fn` awaited inside an async provide method. tsc-validated by
# `npm run typecheck`, which pins `Promise<T>` + awaited call sites against the
# original `Promise<string>` not assignable to `string` regression.
async_ir = BACKEND / "tests" / "fixtures" / "async_http.ir.json"
async_out = BACKEND / "golden" / "async_http.ts"
async_out.write_text(emit(json.loads(async_ir.read_text(encoding="utf-8"))),
                     encoding="utf-8")
print(f"regenerated {async_out} from {async_ir}")
