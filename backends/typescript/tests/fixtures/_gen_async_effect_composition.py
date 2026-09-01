"""One-off generator for async_effect_composition.ir.json (item 131) — NOT part
of the build. Mirrors _gen_witnessed_teardown.py: `compile_source` builds real
compiler output (the `effect await` / `await emit` admission + the `async: true`
step flags), so the ts vitest twin drives genuine IR, not hand-typed nodes.

    python3 backends/typescript/tests/fixtures/_gen_async_effect_composition.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# A Database provider backed by the Pool host, with one async method that parks
# on `Job.run` before it records (the controllable suspension source), and a
# Consumer that composes a sync acquisition (A) with an awaited async
# acquisition (B) — so the twin can dispose mid-flight and observe the LIFO
# teardown ACROSS the suspension (design §4).
_SRC = """
service Database {
  fn query(sql: Str) -> List[Row]
  async fn slow_open(sql: Str) -> List[Row]
}
component PgDatabase provides db: Database {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    async fn slow_open(sql) { await Job.run("B")  return pool.query(sql) }
  }
}
component Consumer requires db: Database {
  let la = effect db.query("ACQ A") undo db.query("UNDO A")
  let lb = effect await db.slow_open("ACQ B") undo db.query("UNDO B")
}
"""

if __name__ == "__main__":
    ir = compile_source(_SRC, "async_effect_composition.rvl")
    out = pathlib.Path(__file__).with_name("async_effect_composition.ir.json")
    out.write_text(json.dumps(ir, indent=2) + "\n")
    print(f"wrote {out}")
