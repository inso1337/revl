// Single-process, once-mode runner for `revl run --backend java --once`
// (docs/v2.0-roadmap.md §2, "Toward early production"). The multi-process
// PlacementRunner in this directory holds a composition live and serves its
// keys across sockets; this runner is the degenerate single-process form the
// `run` driver needs: load every component in load order on one stub-cordis4j
// Context, prove the composition is UP, tear it down LIFO (consumers before
// providers), and prove the live runtime holds no residue afterwards — the
// Java mirror of the rust runner's registry/reflect no-residue check
// (backends/rust/placement_runner/src/main.rs) and the py driver's
// registry.size==0 / reflect.store=={} teardown check (src/revl/run.py).
//
// It shares PlacementRunner's JSON parser (PlacementRunner.Json) — compile the
// two together — and carries no composition-specific knowledge: the emitted
// revl.Components (backends/java/emit.py) supplies the Plugin classes and the
// service interfaces, looked up reflectively from the spec.
//
// Residue is read through the *public* Context API: a provided key resolves
// with ctx.get(iface) while the composition is up, and — after a full LIFO
// teardown, when every provide-disposable has run and removed its entry — the
// same ctx.get must fail (no provider). No provider still resolving is the
// proof the registry was left empty, expressed without reaching into runtime
// internals, so the check reads the same against the real cordis4j Context.

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.Plugin;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

public final class RunOnce {
    static String name = "run";

    static void log(String channel, String subject, String detail) {
        System.out.println(("[" + name + "] " + pad(channel, 6) + "| " + pad(subject, 16) + "| " + detail).stripTrailing());
    }

    static String pad(String s, int n) {
        StringBuilder b = new StringBuilder(s);
        while (b.length() < n) b.append(' ');
        return b.toString();
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] argv) throws Exception {
        Map<String, Object> spec = (Map<String, Object>) PlacementRunner.Json.parse(Files.readString(Path.of(argv[0])));
        name = (String) spec.getOrDefault("name", "run");
        String container = (String) spec.getOrDefault("module", "revl.Components");
        Map<String, Object> config = (Map<String, Object>) spec.getOrDefault("config", Map.of());
        Map<String, Object> ifaces = (Map<String, Object>) spec.getOrDefault("ifaces", Map.of());
        List<Object> provides = (List<Object>) spec.getOrDefault("provides", List.of());
        List<Object> components = (List<Object>) spec.getOrDefault("components", List.of());

        Context ctx = new Context();

        // 1. load this composition's components, in load order (providers first)
        List<String> order = new ArrayList<>();
        List<Disposable> fibers = new ArrayList<>();
        List<String> faulted = new ArrayList<>();
        for (Object comp : components) {
            String cname = (String) comp;
            Class<?> cls = Class.forName(container + "$" + cname + "Plugin");
            Plugin plugin = (Plugin) instantiate(cls, (Map<String, Object>) config.getOrDefault(cname, Map.of()));
            // A faulting activation (a `fail` step, or the fault sweep's
            // injected `fail`) throws out of apply(). The emitted apply()
            // SELF-REVERTS first — it disposes its own effect scope and runs
            // the frame's Phase-2 compensations before rethrowing — so the
            // failed component leaves nothing behind. Mirror the go/py runners
            // (backends/go/placement_runner/main.go step 2 logs the load error
            // and continues; src/revl/run.py the same): record the load error
            // and press on to the LIFO teardown of the components that DID
            // come up and the no-residue proof, rather than letting the throw
            // abort the process. This is what lets the java tier drive a
            // faulting activation and JOIN the cross-tier fault-sweep
            // agreement (roadmap item 341 / fault-sweep item 125).
            Disposable disposable;
            try {
                disposable = plugin.apply(ctx);
            } catch (RuntimeException activationFault) {
                faulted.add(cname);
                log("load", cname, "load error: " + activationFault.getMessage());
                continue;
            }
            fibers.add(disposable);
            order.add(cname);
            log("load", cname, "state=Active");
        }

        // 2. every provided key resolves while the composition is up. When a
        //    component FAULTED during load (a `fail`; the fault sweep) the keys
        //    it would have provided are legitimately absent — the composition
        //    is up minus the failed provision — so a non-resolving key is
        //    expected there, not an error. On a normal run (nothing faulted)
        //    the UP proof stays strict: an absent key is a real failure and
        //    still aborts, exactly as before.
        for (Object key : provides) {
            String iface = (String) ifaces.get((String) key);
            if (iface == null) {
                continue;
            }
            try {
                ctx.get(Class.forName(iface)); // throws (no provider) if it is NOT up
            } catch (RuntimeException notUp) {
                if (faulted.isEmpty()) {
                    throw notUp;
                }
                log("provide", (String) key, "not live (a component faulted this run)");
                continue;
            }
            log("provide", (String) key, "live [" + simple(iface) + "]");
        }

        System.out.println("[" + name + "] UP");
        System.out.flush();

        // 3. teardown, consumers before providers (reverse load order) — the
        //    same LIFO contract run.py's _dispose_all and the rust runner enforce
        for (int idx = fibers.size() - 1; idx >= 0; idx--) {
            try {
                fibers.get(idx).dispose();
            } catch (Throwable ignored) {
            }
            log("swap", order.get(idx), "dispose -> inverses replay (LIFO)");
        }

        // 4. no-residue proof: after teardown no provided key still resolves
        int live = 0;
        for (Object key : provides) {
            String iface = (String) ifaces.get((String) key);
            if (iface == null) {
                continue;
            }
            try {
                ctx.get(Class.forName(iface));
                live++;
                log("residue", (String) key, "STILL LIVE [" + simple(iface) + "]");
            } catch (RuntimeException expected) {
                // good: the provider was withdrawn, nothing answers for this key
            }
        }
        log("residue", "provisions", live + " service(s) still provided");
        if (live == 0) {
            // item 322 Slice 2: a clean unload. Under REVL_WAL, stamp the WAL's
            // discharge + terminal marker so `revl recover` rolls this activation
            // FORWARD (a crash before this point leaves no marker -> roll-back).
            recordCleanUnload(container);
            System.out.println("[" + name + "] NO-RESIDUE — the composition left nothing behind");
        } else {
            System.out.println("[" + name + "] RESIDUE-LEFT — see the residue lines above");
        }
        System.out.println("[" + name + "] DOWN");
        System.out.flush();
    }

    // Stamp the WAL commit-path proof + terminal marker via the emitted recording
    // sink, reflectively so this runner still compiles against a Components
    // emitted WITHOUT --record (no sink present) and no-ops when REVL_WAL is
    // unset. The java mirror of go's crash producer calling revlRecordDischarge /
    // revlRecordActivationComplete on a clean dispose.
    static void recordCleanUnload(String container) {
        String wal = System.getenv("REVL_WAL");
        if (wal == null || wal.isEmpty()) {
            return;
        }
        try {
            Class<?> comp = Class.forName(container);
            comp.getMethod("revlRecordDischarge").invoke(null);
            comp.getMethod("revlRecordActivationComplete").invoke(null);
        } catch (ReflectiveOperationException absent) {
            // Components was emitted without --record (no sink): nothing to stamp.
        }
    }

    static String simple(String binaryName) {
        int at = Math.max(binaryName.lastIndexOf('$'), binaryName.lastIndexOf('.'));
        return at >= 0 ? binaryName.substring(at + 1) : binaryName;
    }

    static Object instantiate(Class<?> cls, Map<String, Object> config) throws Exception {
        if (config == null || config.isEmpty()) {
            return cls.getDeclaredConstructor().newInstance();
        }
        Object[] values = config.values().toArray();
        for (java.lang.reflect.Constructor<?> ctor : cls.getDeclaredConstructors()) {
            if (ctor.getParameterCount() == values.length) {
                Class<?>[] types = ctor.getParameterTypes();
                Object[] coerced = new Object[values.length];
                for (int i = 0; i < values.length; i++) {
                    coerced[i] = coerce(values[i], types[i]);
                }
                return ctor.newInstance(coerced);
            }
        }
        return cls.getDeclaredConstructor().newInstance();
    }

    static Object coerce(Object value, Class<?> type) {
        if (value instanceof Number n) {
            if (type == int.class || type == Integer.class) return n.intValue();
            if (type == long.class || type == Long.class) return n.longValue();
            if (type == double.class || type == Double.class) return n.doubleValue();
        }
        return value;
    }
}
