import json, os, tempfile
base = tempfile.mkdtemp(prefix="jailtest2-")
root = os.path.join(base, "root"); os.makedirs(root)
open(os.path.join(base, "secret.rvl"), "w").write(
    'pub fn leaked_secret_marker(n: Int) -> Int { return n }\n')
os.chdir(root)
from revl.mcp import server
def call(name, args):
    r = server.handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                       "params":{"name":name,"arguments":args}})
    return r["result"]["structuredContent"]
print("root:", root)
# agent supplies BOTH source and modules; the module does the traversal
print("=== source+modules, module does use \"../secret.rvl\"")
print(json.dumps(call("revl_check", {
    "source": 'use "m.rvl" as m\ncomponent A { }\n',
    "modules": {"m.rvl": 'use "../secret.rvl" as s\npub fn q(n: Int) -> Int { return n }\n'},
}))[:900])
print()
print("=== source+modules, module reads /etc/passwd")
print(json.dumps(call("revl_check", {
    "source": 'use "m.rvl" as m\ncomponent A { }\n',
    "modules": {"m.rvl": 'use "/etc/passwd" as s\npub fn q(n: Int) -> Int { return n }\n'},
}))[:900])
print()
print("=== modules key IS an absolute outside path (does the key get opened?)")
print(json.dumps(call("revl_check", {
    "source": 'use "/etc/passwd" as s\ncomponent A { }\n',
    "modules": {"zzz.rvl": 'pub fn q(n: Int) -> Int { return n }\n'},
}))[:900])
