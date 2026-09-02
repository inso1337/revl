import json, os, sys, tempfile
os.chdir(tempfile.mkdtemp(prefix="mcpcwd-"))
from revl.mcp import server

def call(name, args):
    r = server.handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                       "params":{"name":name,"arguments":args}})
    return r["result"]["structuredContent"]

print("cwd (sanctioned root):", os.getcwd())
print("--- A: files=[/etc/passwd] via revl_check")
p = call("revl_check", {"files":["/etc/passwd"]})
print(json.dumps(p)[:500])
print("--- B: files=[/etc/hosts-does-not-exist] via revl_check")
p = call("revl_check", {"files":["/etc/definitely-not-here.rvl"]})
print(json.dumps(p)[:500])
print("--- C: inline source with use \"/etc/passwd\"")
p = call("revl_check", {"source":'use "/etc/passwd"\ncomponent A { }\n'})
print(json.dumps(p)[:700])
print("--- D: inline source with use of a real outside .rvl")
outside = "/tmp/outside-jail-probe.rvl"
open(outside,"w").write('pub fn secret_marker() -> Int { return 7 }\n')
p = call("revl_check", {"source":f'use "{outside}"\ncomponent A {{ }}\n'})
print(json.dumps(p)[:700])
print("--- E: use with ../ traversal")
p = call("revl_check", {"source":'use "../../../../etc/passwd"\ncomponent A { }\n'})
print(json.dumps(p)[:700])
