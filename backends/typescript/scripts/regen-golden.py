"""Regenerate golden/user_cache.ts from the reference IR.

Uses the repo copy (../../examples/user_cache.ir.json) when this backend sits
inside the revl repo, falling back to the vendored byte-identical fixture.
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
