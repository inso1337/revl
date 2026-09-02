import json, os, tempfile
os.chdir(tempfile.mkdtemp(prefix="mcpcwd-"))
from revl.mcp import server
def call(name, args):
    r = server.handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                       "params":{"name":name,"arguments":args}})
    return r["result"]["structuredContent"]
outside = "/tmp/outside-jail-probe.rvl"
open(outside,"w").write('pub fn secret_marker(n: Int) -> Int { return n }\n')
for src in [f'use "{outside}" as probe\n',
            f'use "{outside}" {{ secret_marker }}\n',
            'use "/etc/passwd" as p\n',
            'use "/etc/no-such-file-xyz" as p\n',
            'use "../../../../etc/passwd" as p\n']:
    print("=== ", src.strip())
    print(json.dumps(call("revl_check", {"source": src + "component A { }\n"}))[:600])
