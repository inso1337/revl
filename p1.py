import sys
sys.path.insert(0, "src")
from revl.compiler import compile_source
from revl.manifest import manifest_wire
M = '''service Kv { fn get(k: Str) -> Str }
component Store provides kv: Kv {
  handoff kv: Str
  provide kv { fn get(k) { return k } }
}
service D { fn q(s: Str) -> Int }
component Db requires kv: Kv provides db: D {
  provide db { fn q(s) { let x = s   return 0 } }
}
'''
ir = compile_source(M, "running.rvl")
print(manifest_wire(ir))
print(manifest_wire(ir, replacing=("Store",)))
