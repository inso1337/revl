import json, os, tempfile
base = tempfile.mkdtemp(prefix="jailtest-")
root = os.path.join(base, "root"); os.makedirs(root)
# a secret sitting OUTSIDE the sanctioned root
open(os.path.join(base, "secret.rvl"), "w").write(
    'pub fn leaked_secret_marker(n: Int) -> Int { return n }\n')
open(os.path.join(root, "a.rvl"), "w").write(
    'use "../secret.rvl" as s\ncomponent A { }\n')
os.chdir(root)
from revl.mcp import server
def call(name, args):
    r = server.handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                       "params":{"name":name,"arguments":args}})
    return r["result"]["structuredContent"]
print("root:", root)
print("=== revl_check files=[a.rvl] where a.rvl does `use \"../secret.rvl\"`")
print(json.dumps(call("revl_check", {"files":["a.rvl"]}))[:900])
print()
open(os.path.join(root, "b.rvl"), "w").write(
    'use "../../../../etc/passwd" as s\ncomponent B { }\n')
print("=== revl_check files=[b.rvl] -> use \"../../../../etc/passwd\"")
print(json.dumps(call("revl_check", {"files":["b.rvl"]}))[:900])
print()
open(os.path.join(root, "c.rvl"), "w").write(
    'use "../nope-not-here.rvl" as s\ncomponent C { }\n')
print("=== revl_check files=[c.rvl] -> use of a NONEXISTENT outside path")
print(json.dumps(call("revl_check", {"files":["c.rvl"]}))[:900])
