import sys, importlib.util, types
from pathlib import Path
ROOT = Path("/private/tmp/claude-502/-Users-inso-Projects/372db844-380f-4093-b2af-62b41c212794/scratchpad/wt-186")
sys.path.insert(0, str(ROOT / "src"))
from revl import compile_files, compile_source
from revl.manifest import manifest_wire
from revl.errors import RevlError, RevlErrors

ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
spec = importlib.util.spec_from_file_location("pyemit", ROOT / "backends" / "python" / "emit.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
stub = types.ModuleType("runtime"); stub.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules["runtime"] = stub
ns = {}
exec(compile(mod.emit(ir), "sl.py", "exec"), ns)
admit_ambient = ns["admit_ambient"]; admit_src = ns["admit_src"]

SVC = 'service Kv { fn get(k: Str) -> Str }\n'
def C(name, t=None, key="kv"):
    h = ("  handoff %s: %s\n" % (key, t)) if t else ""
    return SVC + 'component %s provides %s: Kv {\n%s  provide %s { fn get(k) { return k } }\n}\n' % (name, key, h, key)

def ref(x, m, repl=()):
    irm = compile_source(m, "running.rvl")
    try:
        compile_source(x, "diff.rvl", manifest=irm, replacing=list(repl))
        return ""
    except RevlErrors as e:
        return e.errors[0].message
    except RevlError as e:
        return e.message

def gate(x, m, repl=()):
    irm = compile_source(m, "running.rvl")
    w = manifest_wire(irm, replacing=repl) if repl else manifest_wire(irm)
    return admit_ambient(x, w), w

cases = [
 ("mismatch", C("Store","Int"), C("Store","Str"), ("Store",)),
 ("ok-same", C("Store","Str"), C("Store","Str"), ("Store",)),
 ("widen", C("Store","Opt[Str]"), C("Store","Str"), ("Store",)),
 ("narrow", C("Store","Str"), C("Store","Opt[Str]"), ("Store",)),
 ("cold", C("Store","Str"), C("Store",None), ("Store",)),
 ("optout", C("Store",None), C("Store","Str"), ("Store",)),
 ("noreplace", C("Store","Int"), C("Store","Str"), ()),
 ("fnty", C("Store","(Float) -> Str"), C("Store","(Int) -> Str"), ("Store",)),
 ("listw", C("Store","List[Float]"), C("Store","List[Int]"), ("Store",)),
 ("listn", C("Store","List[Int]"), C("Store","List[Float]"), ("Store",)),
 ("map", C("Store","Map[Str, Int]"), C("Store","Map[Str, Str]"), ("Store",)),
]
bad = 0
for name, x, m, repl in cases:
    r = ref(x, m, repl)
    g, w = gate(x, m, repl)
    gmsg = g.split("|",1)[1] if "|" in g else g
    ok = (r == gmsg)
    if not ok: bad += 1
    print(("OK " if ok else "XX "), name, "| wire:", w)
    if not ok:
        print("   ref :", repr(r))
        print("   gate:", repr(g))
print("failures", bad)
