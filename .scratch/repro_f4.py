import json, os, tempfile
tmp = tempfile.mkdtemp(prefix="f4-")
src = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops { emission fn shout(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }\n"
    "}\n")
path = os.path.join(tmp, "app.rvl"); open(path, "w").write(src)
from revl.compiler import compile_files
from revl.mcp.session import Session
from revl.mcp.composed import ComposedServer
ir = compile_files([path])
s = Session()
s.approval_policy = "auto"
s.load(ir, None, record=True)
srv = ComposedServer(s, composition="app")
print("advertised:", [t["name"] for t in srv._advertised])
sink = os.path.join(tmp, "sink.log")
name = srv._advertised[0]["name"]
resp = srv.handle({"jsonrpc":"2.0","id":1,"method":"tools/call",
                   "params":{"name":name,"arguments":{"sink":sink,"msg":"hi"}}})
print(json.dumps(resp, indent=1)[:1200])
print("sink exists (must be False):", os.path.exists(sink))
