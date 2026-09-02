import json, os, tempfile
base = tempfile.mkdtemp(prefix="legit-")
root = os.path.join(base, "root"); os.makedirs(os.path.join(root, "lib"))
open(os.path.join(root, "lib", "m.rvl"), "w").write('pub fn q(n: Int) -> Int { return n }\n')
open(os.path.join(base, "outside.rvl"), "w").write('pub fn q(n: Int) -> Int { return n }\n')
open(os.path.join(root, "app.rvl"), "w").write('use "../outside.rvl" as o\ncomponent A { }\n')
os.chdir(root)
from revl.mcp import server
def call(name, args):
    r = server.handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                       "params":{"name":name,"arguments":args}})
    return r["result"]["structuredContent"]
def ok(label, p):
    print(f"{label}: ok={p.get('ok')}", "" if p.get("ok") else json.dumps(p.get("diagnostics"))[:220])
ok("L1 transport source, relative use into a subdir on disk",
   call("revl_check", {"source": 'use "lib/m.rvl" as m\ncomponent A { }\n', "modules": {"zz.rvl": "// x\n"}}))
ok("L2 transport source, use of an in-memory module",
   call("revl_check", {"source": 'use "m.rvl" as m\ncomponent A { }\n', "modules": {"m.rvl": 'pub fn q(n: Int) -> Int { return n }\n'}}))
ok("L3 transport source, stdlib search-path spelling",
   call("revl_check", {"source": 'use "stdlib/str.rvl" as s\ncomponent A { }\n', "modules": {"zz.rvl": "// x\n"}}))
ok("L4 operator-authored file on disk with its own ../ import (files=)",
   call("revl_check", {"files": ["app.rvl"]}))
ok("L5 plain inline source, no imports",
   call("revl_check", {"source": 'component A { }\n'}))
