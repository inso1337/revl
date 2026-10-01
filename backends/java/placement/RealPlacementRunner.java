// A java-placed placement process on the REAL reactive cordis4j runtime
// (github.com/1na-ko/cordis4j), as opposed to PlacementRunner, which runs on
// the non-reactive in-repo stubs. Same spec shape; the difference is reactive
// lifecycle:
//
//   - the consumer's components load through ctx.inject(deps, ...), so they
//     activate when their cross-process deps are provided and DEACTIVATE
//     reactively when a dep is withdrawn (paper Algorithm 3, Theorem 63);
//   - a peer-death monitor connects to each provider socket and, on EOF
//     (the provider process died), disposes that key's provide-binding on the
//     main thread; the withdrawal makes the injected consumer deactivate and
//     run its inverses, with no exception thrown.
//
// cordis4j is single-threaded (a context must not be touched off its thread),
// so the monitor threads only signal; the main thread does every context call.
//
// It also SERVES (issue #1581). A process whose spec carries `serve` binds the
// unix socket and runs an accept loop on its own thread. Each connection thread
// reads a request, applies the E-Stop accept check, and hands the call to the
// MAIN thread through the same event queue the peer-death monitor uses; the
// main thread checks the served-key and declared-method allowlists, resolves
// the key (the shared realm first, then the one isolating component context
// that provides it), runs the method with the crossing recorded in flight, and
// completes the reply the connection thread is waiting on. Before this, the
// runner bound nothing, so a java provider printed `UP` and answered no one.
//
// Compiled + run against the real cordis4j classes:
//   javac --release 21 -cp <cordis4j-classes> -d out RealPlacementRunner.java revl/Components.java
//   java -cp <cordis4j-classes>:out RealPlacementRunner <spec.json>
// Output is line-prefixed "[name]" so the conductor can interleave processes.

import io.cordis4j.core.Context;
import io.cordis4j.core.Contexts;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.ServiceKey;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.StandardProtocolFamily;
import java.nio.channels.ServerSocketChannel;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.Channels;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.Optional;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

public final class RealPlacementRunner {
    static volatile String name = "?";
    static final String STOP = "__stop__";

    // The one choke point every console line passes through: a declared
    // Secret[T] is scrubbed HERE, never at each printer.
    static void log(String channel, String subject, String detail) {
        System.out.println(("[" + name + "] " + pad(channel, 8) + "| " + pad(redactSecrets(subject), 16)
                + "| " + redactSecrets(detail)).stripTrailing());
    }

    // --- the declared Secret[T] registry (item 421 F6): PlacementRunner's twin --
    //
    // The emitted Components registers every declared `Secret[T]` value with a
    // registry of its own (revlMarkSecret in a plugin constructor for a config
    // field, at the head of a provide method for a parameter, revlSecretResult
    // around an extern's return) and exposes revlRedactText over it. This
    // runner keeps no registry: it BINDS to the container's, reflectively, so
    // a Components emitted for a document that declares no Secret[T] (which
    // carries no registry, byte-identical to before) binds nothing and the
    // funnel is the identity. Every line this process prints and every failure
    // it sends back across the seam passes through it, so a sink added later
    // reads an already-redacted line.
    static volatile java.util.function.UnaryOperator<String> secretRedactor = text -> text;

    static void bindSecretRegistry(String container) {
        Method redact;
        try {
            redact = Class.forName(container).getMethod("revlRedactText", String.class);
        } catch (ReflectiveOperationException absent) {
            return; // no declared Secret[T] in this document: nothing to redact
        }
        secretRedactor = text -> {
            try {
                return (String) redact.invoke(null, text);
            } catch (ReflectiveOperationException failure) {
                // A funnel that cannot run must not let the text through.
                throw new IllegalStateException("secret redaction failed", failure);
            }
        };
    }

    static String redactSecrets(String text) {
        return text == null ? null : secretRedactor.apply(text);
    }

    // The FATAL line's own funnel. `secretRedactor` deliberately THROWS when the
    // container's redactor cannot be invoked (a funnel that cannot run must not
    // let text through), and this is the one caller that cannot propagate that:
    // a failure raised inside the failure path would escape as exactly the bare
    // stack trace this funnel exists to remove. So a funnel that cannot run
    // withholds the detail instead of passing it through.
    static String redactFatal(String text) {
        try {
            return redactSecrets(text);
        } catch (RuntimeException funnelDown) {
            return "<detail withheld: redaction unavailable>";
        }
    }

    static String pad(String s, int n) {
        StringBuilder b = new StringBuilder(s);
        while (b.length() < n) b.append(' ');
        return b.toString();
    }

    // The uncaught-failure funnel (issue #814, the java half) — PlacementRunner's
    // twin, and the same contract: the load path funnels a refused seam and the
    // probe path funnels its own failures, while anything else used to escape
    // `main` as a bare stack trace on System.err, which the conductor merges
    // verbatim (`placement.py::pump`, stderr=STDOUT). The reactive runtime adds
    // one more source than the stub runner has: a withdrawal monitor runs on its
    // own thread and its failures surface on whichever thread raised them.
    //
    // One redacted line, non-zero exit: still loud, still machine-visible, no
    // longer an unanalysed crossing. `halt`, not `exit`, for the reason the
    // E-Stop watcher below halts: a process that died mid-boot must not run the
    // shutdown hook and print `DOWN`, which is the conductor's clean-teardown
    // signal (E7). The py tier reaches the same place with `raise SystemExit(1)`.
    //
    // `failAndHalt` is the ONE composer of that line, so the thread that runs
    // `main` and the other threads this runner starts cannot drift apart.
    public static void main(String[] argv) {
        installThreadFunnel();
        try {
            run(argv);
        } catch (Throwable fatal) {
            failAndHalt(fatal);
        }
    }

    // One redacted line, then `halt` — the disposition `main`'s catch and the
    // default uncaught-exception handler both use.
    static void failAndHalt(Throwable fatal) {
        String label = name == null ? "?" : name;
        String detail = fatal.getMessage();
        String message = detail == null
                ? fatal.getClass().getSimpleName()
                : fatal.getClass().getSimpleName() + ": " + detail;
        System.err.println("[" + label + "] FATAL " + redactFatal(message));
        System.err.flush();
        Runtime.getRuntime().halt(1);
    }

    // The per-thread half of the same funnel. A java `try` covers the thread
    // that runs it, so `main`'s catch cannot see a failure on any OTHER thread,
    // and the JVM's default handler prints it as a bare stack trace on
    // System.err — the same unfunnelled channel, one thread over. This runner
    // starts several: the `revl-estop` watcher, a `peer-monitor` per peer, and
    // the withdrawal monitor the reactive runtime owns. `peer-monitor` is the
    // one that matters most: it runs the crossing, and a crossing's frame holds
    // the arguments it was called with, which is where a declared `Secret[T]`
    // travels.
    //
    // `Thread.setDefaultUncaughtExceptionHandler` is the only process-wide
    // channel java offers, and it must be installed BEFORE `run` starts any of
    // those threads. A thread with a handler of its own would bypass this one;
    // none of them sets one.
    static void installThreadFunnel() {
        Thread.setDefaultUncaughtExceptionHandler((thread, fatal) -> {
            try {
                failAndHalt(fatal);
            } catch (Throwable funnelDown) {
                // The funnel itself failed. Withhold the detail and die: a bare
                // stack trace here is exactly what this channel exists to
                // remove, and a funnel that cannot run must not let text
                // through. Nothing was printed yet — the line is composed
                // before it is written — so this is still one line.
                System.err.println("[?] FATAL <detail withheld: funnel unavailable>");
                System.err.flush();
                Runtime.getRuntime().halt(1);
            }
        });
    }

    @SuppressWarnings("unchecked")
    static void run(String[] argv) throws Exception {
        Map<String, Object> spec = (Map<String, Object>) Json.parse(Files.readString(Path.of(argv[0])));
        name = (String) spec.get("name");
        String container = (String) spec.getOrDefault("module", "revl.Components");
        bindSecretRegistry(container); // before the first line is printed

        // item 443 / issue #122: publish the spec's E-Stop latch to the ambient
        // path the dispatch seam (`BridgeClient.call`) and the idle watcher below
        // read. A java process cannot rewrite its own environment the way the go
        // runner does with `os.Setenv`, so the latch travels here. Done BEFORE any
        // proxy dials, so a latch already armed at boot is honored from the first
        // crossing; a placement that was never armed carries no latch and this is
        // a no-op, so an unarmed run is byte-identical to the pre-443 runner (E3).
        // The reactive runner honors the SAME contract and prints the SAME
        // `[name] HALTED {json}` line as the JDK-17 stub `PlacementRunner`.
        Estop.publishLatch((String) spec.get("estopLatch"));
        Map<String, Object> ifaces = (Map<String, Object>) spec.getOrDefault("ifaces", Map.of());
        Map<String, Object> config = (Map<String, Object>) spec.getOrDefault("config", Map.of());
        Map<String, Object> proxies = (Map<String, Object>) spec.getOrDefault("proxies", Map.of());
        List<Object> components = (List<Object>) spec.getOrDefault("components", List.of());
        List<Object> probes = (List<Object>) spec.getOrDefault("probe", List.of());

        Context root = Contexts.create();
        BlockingQueue<Object> events = new LinkedBlockingQueue<>();
        Map<String, Disposable> bindings = new LinkedHashMap<>();
        Set<ServiceKey<?>> deps = new HashSet<>();

        // 1. provide each cross-consumed key via a generic reflection proxy, and
        //    watch the provider for death (its withdrawal deactivates us).
        for (Map.Entry<String, Object> entry : proxies.entrySet()) {
            String key = entry.getKey();
            Map<String, Object> info = (Map<String, Object>) entry.getValue();
            String socket = (String) info.get("socket");
            Class<?> iface = Class.forName((String) ifaces.get(key));
            Object proxy = Proxy.newProxyInstance(iface.getClassLoader(), new Class<?>[]{iface},
                    new ForwardingHandler(new BridgeClient(socket), key));
            bindings.put(key, provide(root, key, iface, proxy));
            deps.add(serviceKey(key, iface));
            startMonitor(key, socket, events);
            log("proxy", key, "-> " + socket + " (reactive)");
        }

        // 2. load components. With cross-deps they are reactive consumers: one
        //    inject fiber gated on the deps, so a withdrawal deactivates it.
        //    Every context a component is applied in is wrapped, so the realm
        //    each isolating component provides into is kept for the serve path.
        if (!deps.isEmpty()) {
            root.inject(deps, ctx -> {
                serveCtx = ctx;
                List<Disposable> domains = new ArrayList<>();
                for (Object comp : components) {
                    String cname = (String) comp;
                    try {
                        domains.add(loadPlugin(container, cname, config).apply(REALMS.track(ctx, cname)));
                        log("load", cname, "ACTIVE (reactive)");
                    } catch (Exception e) {
                        throw new RuntimeException(e);
                    }
                }
                for (Object p : probes) runProbe(ctx, ifaces, (String) p);
                // first-reverted cleanup: fires when the fiber deactivates on
                // withdrawal, evidence of reactive teardown (no exception).
                return () -> {
                    serveCtx = null;
                    REALMS.clear();
                    log("withdraw", "deactivated", "consumer unloaded reactively; inverses run");
                    for (int i = domains.size() - 1; i >= 0; i--) {
                        try { domains.get(i).dispose(); } catch (Throwable ignored) {}
                    }
                };
            });
        } else {
            serveCtx = root;
            for (Object comp : components) {
                String cname = (String) comp;
                io.cordis4j.core.Plugin plugin = loadPlugin(container, cname, config);
                bindings.put("comp:" + cname, root.plugin(c -> plugin.apply(REALMS.track(c, cname))));
                log("load", cname, "ACTIVE");
            }
            for (Object p : probes) runProbe(root, ifaces, (String) p);
        }

        // 3. serve the keys other processes need (issue #1581). Bound after the
        //    components load, the order the stub runner uses; a consumer that
        //    dials first retries until the socket exists.
        Served served = Served.of((Map<String, Object>) spec.get("serve"), ifaces);
        Endpoint endpoint = null;
        if (served != null) {
            endpoint = new Endpoint(served.socket, events);
            endpoint.start();
            log("serve", String.join(", ", served.ifaces.keySet()), "-> " + served.socket);
        }

        // THE SHUTDOWN HOOK GOES UP BEFORE THE `UP` LINE, and that ordering is
        // the whole contract (the java half of the fix py got in #226; issue
        // 290). `[name] UP` is what the conductor waits on: `run_placement`'s
        // `--once` path blocks on every child's UP and then calls `stop_all`
        // immediately, so the SIGTERM can land microseconds after the print.
        // Printing first left a window in which the JVM's DEFAULT SIGTERM
        // disposition was still in force — no hook registered means the JVM dies
        // without running one — so a signal arriving inside it killed this
        // process outright: no LIFO unwind, no inverses replayed, no clean-unload
        // WAL marker, no `DOWN` (G7 and R4, both violated). The window belongs to
        // the LAST process to boot — every earlier one is still being waited on —
        // which is why it lands on the process most likely to still owe an
        // inverse and a residue proof.
        //
        // `events` is an unbounded BlockingQueue, so a STOP offered in the old
        // window is merely PARKED and the loop below takes it on its first
        // iteration: the unwind runs either way, and `UP` means what it says —
        // loaded, serving, AND able to be stopped.
        //
        // The hook WAITS for the main loop to print `DOWN` (issue #1581). It used
        // to offer STOP and return at once, and the JVM halts as soon as its hooks
        // return, so the main thread was usually killed mid-teardown: no `DOWN`,
        // and the conductor reported `teardown HALTED`. The wait is bounded so a
        // wedged teardown still lets the JVM go.
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            SHUTTING_DOWN = true;
            events.offer(STOP);
            try { DOWN_PRINTED.await(25, TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
        }));

        System.out.println("[" + name + "] UP");
        System.out.flush();

        startEstopWatcher();
        mainLoop(events, bindings, endpoint, served, container);
        System.out.println("[" + name + "] DOWN");
        System.out.flush();
        DOWN_PRINTED.countDown();
        // Inside the shutdown hook's wait, `System.exit` would block forever (the
        // JVM is already shutting down); returning lets the hook finish instead.
        if (!SHUTTING_DOWN) System.exit(0);
    }

    // --- the main thread's state (every context call happens on it) ----------

    // The context the served keys' components were applied in: `root`, or the
    // reactive inject fiber's context while it is active. Null while a
    // withdrawn dependency keeps the components deactivated.
    static Context serveCtx;
    static final Realms REALMS = new Realms();
    static volatile boolean SHUTTING_DOWN = false;
    static final CountDownLatch DOWN_PRINTED = new CountDownLatch(1);

    static io.cordis4j.core.Plugin loadPlugin(String container, String cname, Map<String, Object> config)
            throws Exception {
        Class<?> cls = Class.forName(container + "$" + cname + "Plugin");
        @SuppressWarnings("unchecked")
        Map<String, Object> own = (Map<String, Object>) config.getOrDefault(cname, Map.of());
        return (io.cordis4j.core.Plugin) instantiate(cls, own);
    }

    // 4. main loop: every context call stays on this thread (cordis4j D8). Three
    //    kinds of event arrive: STOP, a peer's death (its key), and a served call.
    static void mainLoop(BlockingQueue<Object> events, Map<String, Disposable> bindings,
                         Endpoint endpoint, Served served, String container) throws InterruptedException {
        boolean consumer = !bindings.isEmpty() && bindings.keySet().stream().noneMatch(k -> k.startsWith("comp:"));
        while (true) {
            Object event = events.take();
            if (event instanceof Call call) {
                call.reply.complete(dispatch(call, served));
                continue;
            }
            if (STOP.equals(event)) {
                if (endpoint != null) endpoint.close();
                refusePending(events);
                teardown(bindings);
                // item 322 Slice 2: a graceful stop is a clean unload. Under
                // REVL_WAL, stamp the WAL's discharge + terminal marker via the
                // emitted sink so `revl recover` rolls this activation FORWARD
                // (an abrupt death before this leaves no marker -> roll-back) —
                // the real-cordis4j sibling of RunOnce.recordCleanUnload.
                recordCleanUnload(container);
                return;
            }
            Disposable binding = bindings.remove((String) event);
            if (binding != null) {
                log("peer", (String) event, "provider died: withdrawing the binding");
                binding.dispose(); // withdrawal -> the injected consumer deactivates reactively
            }
            // every proxied provider gone: the consumer is fully withdrawn. A
            // process that also serves stops too, so its own consumers see it
            // die and withdraw in turn, the cascade the stub-free path has.
            if (consumer && bindings.isEmpty()) {
                if (endpoint != null) endpoint.close();
                refusePending(events);
                return;
            }
        }
    }

    static void refusePending(BlockingQueue<Object> events) {
        for (Object left : events.toArray()) {
            if (left instanceof Call call) {
                Map<String, Object> reply = new LinkedHashMap<>();
                reply.put("ok", false);
                reply.put("error", "process " + name + " is stopping; the call was not dispatched");
                call.reply.complete(reply);
            }
        }
    }

    // item 443 / issue #122: the idle watcher (E6). The dispatch seam refuses
    // lazily, at the NEXT crossing, which is useless for a process parked on
    // the event queue waiting to be stopped: it crosses nothing and would sit
    // through the emergency. So the runner polls the latch and, on the button,
    // prints its in-flight inventory on one `[name] HALTED {json}` line (the
    // conductor merges it by prefix) and calls `Runtime.getRuntime().halt`,
    // which runs NO teardown and prints no `DOWN` (E7). Started only when the
    // placement is armed.
    static void startEstopWatcher() {
        final String watchLatch = Estop.latchPath(null, null, true);
        if (watchLatch == null) return;
        Thread watcher = new Thread(() -> {
            while (true) {
                Map<String, Object> record = Estop.readLatch(watchLatch);
                if (record != null) {
                    System.out.println(
                            Estop.estopHaltLine(name, Estop.inFlightCrossings(), record));
                    System.out.flush();
                    try { Thread.sleep(50); } catch (InterruptedException ignored) {}
                    Runtime.getRuntime().halt(1); // E7: die where it stands, non-zero, no DOWN
                }
                try { Thread.sleep(20); } catch (InterruptedException ignored) { return; }
            }
        }, "revl-estop");
        watcher.setDaemon(true);
        watcher.start();
    }

    static void teardown(Map<String, Disposable> bindings) {
        List<Disposable> all = new ArrayList<>(bindings.values());
        for (int i = all.size() - 1; i >= 0; i--) {
            try { all.get(i).dispose(); } catch (Throwable ignored) {}
        }
    }

    // item 322 Slice 2: stamp the WAL commit-path proof + terminal marker via the
    // emitted recording sink, reflectively so this runner still compiles against a
    // Components emitted WITHOUT --record (no sink present) and no-ops when
    // REVL_WAL is unset. The real-cordis4j mirror of RunOnce.recordCleanUnload.
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

    // --- the serve path (issue #1581) ----------------------------------------

    // One served call, handed from a connection thread to the main thread. The
    // connection thread blocks on `reply` until the main thread completes it.
    record Call(String key, String method, List<Object> args,
                CompletableFuture<Map<String, Object>> reply) {}

    // What this process serves, read off the spec's `serve` block: each key's
    // interface, and the operations its service declaration admits (`methods`,
    // the allowlist `placement.py` reads off the IR). A request outside either
    // is refused and never dispatched, so the served surface is exactly the
    // enumerable one (G8), the rule the py bridge applies with the same words.
    static final class Served {
        final String socket;
        final Map<String, Class<?>> ifaces = new LinkedHashMap<>();
        final Map<String, Set<String>> methods = new LinkedHashMap<>();

        private Served(String socket) { this.socket = socket; }

        @SuppressWarnings("unchecked")
        static Served of(Map<String, Object> serve, Map<String, Object> ifaces) throws ClassNotFoundException {
            if (serve == null) return null;
            List<Object> keys = (List<Object>) serve.getOrDefault("keys", List.of());
            if (keys.isEmpty()) return null;
            Served out = new Served((String) serve.get("socket"));
            Map<String, Object> declared = (Map<String, Object>) serve.getOrDefault("methods", Map.of());
            for (Object k : keys) {
                String key = (String) k;
                out.ifaces.put(key, Class.forName((String) ifaces.get(key)));
                Object ops = declared.get(key);
                if (ops instanceof List<?> list) {
                    Set<String> names = new java.util.TreeSet<>();
                    for (Object op : list) names.add(String.valueOf(op));
                    out.methods.put(key, names);
                }
            }
            return out;
        }

        Class<?> iface(String key) {
            Class<?> iface = ifaces.get(key);
            if (iface == null) throw new SeamRefusal("key '" + key + "' is not exported by this process");
            return iface;
        }

        void checkMethod(String key, String method) {
            Set<String> allowed = methods.get(key);
            if (allowed != null && !allowed.contains(method)) {
                String listed = allowed.isEmpty() ? "(none)" : String.join(", ", allowed);
                throw new SeamRefusal("method '" + method + "' is not exported for key '" + key
                        + "' (exported: " + listed + ")");
            }
        }
    }

    // A refusal decided by this runner, not raised by the provider: its text is
    // the whole reply, with no exception class in front of it.
    static final class SeamRefusal extends RuntimeException {
        SeamRefusal(String message) { super(message); }
    }

    // Runs ON THE MAIN THREAD. The allowlists, the key's resolution and the
    // method's run all touch the context, which cordis4j confines to it.
    @SuppressWarnings("unchecked")
    static Map<String, Object> dispatch(Call call, Served served) {
        Map<String, Object> reply = new LinkedHashMap<>();
        try {
            Class<?> iface = served.iface(call.key());
            served.checkMethod(call.key(), call.method());
            if (serveCtx == null) {
                throw new SeamRefusal("no provider for key '" + call.key() + "' right now: this "
                        + "process's components are deactivated while a dependency is withdrawn");
            }
            Object service = resolveKey(serveCtx, iface, call.key());
            Method m = findMethod(iface, call.method(), call.args().size());
            // Recorded as in flight WHILE the method runs: a crossing still
            // executing when the latch trips is the AMBIGUOUS one the halt
            // inventory names (item 440). Cleared in a finally.
            long seq = Estop.beginCrossing(call.key(), call.method(), "accept");
            Object result;
            try {
                result = m.invoke(service, coerceArgs(m, call.args()));
            } finally {
                Estop.endCrossing(seq);
            }
            reply.put("ok", true);
            reply.put("value", BridgeCodec.encode(result));
        } catch (SeamRefusal refused) {
            reply.put("ok", false);
            reply.put("error", redactSecrets(refused.getMessage()));
        } catch (Throwable t) {
            reply.put("ok", false);
            reply.put("error", seamFailure(t, call.args()));
        }
        return reply;
    }

    // A key resolves in the py tier's `resolve_key` order: the shared realm when
    // the key is provided there; otherwise the ONE isolating component context
    // that provides it. cordis4j cannot read an isolated realm by its label (an
    // isolate always mints a fresh child; docs/contract-errata.md, "cordis4j
    // global-realm divergence"), so the runner keeps the context each component
    // isolated into (`Realms`) and reads the provision there. A key two contexts
    // isolate is refused by name: a call names a key, not a realm.
    @SuppressWarnings({"unchecked", "rawtypes"})
    static Object resolveKey(Context ctx, Class<?> iface, String key) {
        ServiceKey sk = ServiceKey.of((Class) iface, key);
        Optional<Object> shared = ctx.find(sk);
        if (shared.isPresent()) return shared.get();
        List<Realms.Isolated> holders = REALMS.providing(sk);
        if (holders.size() == 1) return holders.get(0).ctx().get(sk);
        if (holders.size() > 1) {
            List<String> named = new ArrayList<>();
            for (Realms.Isolated h : holders) named.add("`" + h.component() + "` in realm `" + h.realm() + "`");
            throw new SeamRefusal("key '" + key + "' is provided in " + holders.size() + " realms ("
                    + String.join(", ", named) + "); a call names a key, not a realm, so it has no "
                    + "single provider to reach");
        }
        return ctx.get(sk); // absent everywhere: cordis4j's own NoSuchServiceException
    }

    // The contexts each component isolated into. Every context a component is
    // applied in is wrapped (`track`); the wrapper records the child an
    // `isolate` returns, and wraps that child too, so a nested isolate is seen.
    // Only the main thread applies components, so the list needs no lock.
    static final class Realms {
        record Isolated(String component, String realm, Context ctx) {}

        final List<Isolated> isolated = new ArrayList<>();

        Context track(Context ctx, String component) {
            return (Context) Proxy.newProxyInstance(Context.class.getClassLoader(),
                    new Class<?>[]{Context.class}, (proxy, method, args) -> {
                        Object out;
                        try {
                            out = method.invoke(ctx, args);
                        } catch (java.lang.reflect.InvocationTargetException wrapped) {
                            throw wrapped.getCause();
                        }
                        if (method.getName().equals("isolate") && out instanceof Context child) {
                            isolated.add(new Isolated(component, String.valueOf(args[1]), child));
                            return track(child, component);
                        }
                        return out;
                    });
        }

        @SuppressWarnings({"unchecked", "rawtypes"})
        List<Isolated> providing(ServiceKey key) {
            List<Isolated> out = new ArrayList<>();
            for (Isolated i : isolated) {
                try {
                    if (i.ctx().find(key).isPresent()) out.add(i);
                } catch (IllegalStateException disposed) {
                    // the component was withdrawn and its child discarded
                }
            }
            return out;
        }

        void clear() { isolated.clear(); }
    }

    // The unix-socket endpoint. Connection threads only read, check the E-Stop
    // latch and wait; the main thread does the dispatch.
    static final class Endpoint {
        final String path;
        final BlockingQueue<Object> events;
        ServerSocketChannel server;
        volatile boolean running = true;

        Endpoint(String path, BlockingQueue<Object> events) { this.path = path; this.events = events; }

        void start() throws Exception {
            Files.deleteIfExists(Path.of(path));
            server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(path));
            Thread t = new Thread(this::acceptLoop, "bridge-serve");
            t.setDaemon(true);
            t.start();
        }

        void acceptLoop() {
            while (running) {
                try {
                    SocketChannel ch = server.accept();
                    Thread handler = new Thread(() -> serveConn(ch), "bridge-conn");
                    handler.setDaemon(true);
                    handler.start();
                } catch (Exception e) { return; }
            }
        }

        @SuppressWarnings("unchecked")
        void serveConn(SocketChannel ch) {
            try (ch) {
                BufferedReader r = new BufferedReader(new InputStreamReader(Channels.newInputStream(ch), StandardCharsets.UTF_8));
                BufferedWriter w = new BufferedWriter(new OutputStreamWriter(Channels.newOutputStream(ch), StandardCharsets.UTF_8));
                String line;
                while ((line = r.readLine()) != null) {
                    Map<String, Object> reply;
                    List<Object> args = List.of();
                    try {
                        Map<String, Object> req = (Map<String, Object>) Json.parse(line);
                        String key = (String) req.get("key");
                        String method = (String) req.get("method");
                        args = (List<Object>) req.getOrDefault("args", List.of());
                        reply = estopEngaged() ? estopRefusal(key, method) : handOver(key, method, args);
                    } catch (Throwable t) {
                        reply = new LinkedHashMap<>();
                        reply.put("ok", false);
                        reply.put("error", seamFailure(t, args));
                    }
                    w.write(Json.write(reply)); w.write("\n"); w.flush();
                }
            } catch (Exception ignored) {}
        }

        static boolean estopEngaged() { return Estop.estopEngaged(); }

        // item 443 / issue #122: the ACCEPT side of the E-Stop seam, checked
        // here so a halted process refuses without involving the main thread.
        // No inverse is replayed and nothing is discharged: the caller's
        // attempt lands in item 440's ambiguous tier (docs/design/443-estop.md).
        static Map<String, Object> estopRefusal(String key, String method) {
            Map<String, Object> reply = new LinkedHashMap<>();
            reply.put("ok", false);
            reply.put("error", "revl E-Stop engaged: this process is HALTED and "
                    + "refuses new crossings (key " + key + ", method " + method
                    + "); see docs/design/443-estop.md");
            return reply;
        }

        Map<String, Object> handOver(String key, String method, List<Object> args) throws Exception {
            if (!running) throw new SeamRefusal("process " + name + " is stopping; the call was not dispatched");
            CompletableFuture<Map<String, Object>> reply = new CompletableFuture<>();
            events.put(new Call(key, method, args, reply));
            return reply.get();
        }

        void close() {
            running = false;
            try { if (server != null) server.close(); } catch (Exception ignored) {}
            try { Files.deleteIfExists(Path.of(path)); } catch (Exception ignored) {}
        }
    }

    // --- what a failure may carry BACK across the seam (item 421 F5) ---------
    //
    // PlacementRunner's twin: the consumer is on the other side of a trust
    // boundary, so every argument value the call was made with is scrubbed out
    // of the host error text, and then every declared Secret[T] value is, while
    // the exception's type and the sentence around it survive. The marker must
    // equal confidential.REDACTED_ARG on the python tier.
    static final String REDACTED_ARG = "<redacted:arg>";
    static final int MIN_MATCHABLE_ARG = 3;

    static void argNeedles(Object value, Set<String> into) {
        if (value == null || value instanceof Boolean) return;
        if (value instanceof String s) {
            if (s.length() >= MIN_MATCHABLE_ARG) into.add(s);
        } else if (value instanceof Number n) {
            String form = String.valueOf(n);
            if (form.length() >= MIN_MATCHABLE_ARG) into.add(form);
        } else if (value instanceof List<?> items) {
            for (Object item : items) argNeedles(item, into);
        } else if (value instanceof Map<?, ?> record) {
            for (Object item : record.values()) argNeedles(item, into);
        }
    }

    static String seamFailure(Throwable t, List<Object> args) {
        Throwable failure = unwrapDispatch(t);
        String text = failure.getClass().getSimpleName() + ": " + failure.getMessage();
        Set<String> needles = new HashSet<>();
        argNeedles(args, needles);
        List<String> ordered = new ArrayList<>(needles);
        ordered.sort((a, b) -> Integer.compare(b.length(), a.length()));
        for (String needle : ordered) {
            if (!needle.isEmpty()) text = text.replace(needle, REDACTED_ARG);
        }
        return redactSecrets(text);
    }

    static Throwable unwrapDispatch(Throwable t) {
        while (t instanceof java.lang.reflect.InvocationTargetException wrapped && wrapped.getCause() != null) {
            t = wrapped.getCause();
        }
        return t;
    }

    // --- peer-death monitor: an idle connection whose EOF means the provider died ---

    static void startMonitor(String key, String path, BlockingQueue<Object> events) {
        Thread t = new Thread(() -> {
            SocketChannel ch = null;
            for (int attempt = 0; attempt < 200 && ch == null; attempt++) {
                try {
                    ch = SocketChannel.open(StandardProtocolFamily.UNIX);
                    ch.connect(UnixDomainSocketAddress.of(path));
                } catch (Exception e) {
                    ch = null;
                    try { Thread.sleep(50); } catch (InterruptedException ignored) {}
                }
            }
            if (ch == null) return;
            try (SocketChannel open = ch) {
                ByteBuffer buf = ByteBuffer.allocate(64);
                while (open.read(buf) >= 0) buf.clear(); // block until EOF (provider death)
            } catch (Exception ignored) {}
            events.offer(key);
        }, "peer-monitor-" + key);
        t.setDaemon(true);
        t.start();
    }

    // --- provided-service probe ---------------------------------------------

    static void runProbe(Context ctx, Map<String, Object> ifaces, String expr) {
        try {
            int dot = expr.indexOf('.'), open = expr.indexOf('(', dot), close = expr.lastIndexOf(')');
            String key = expr.substring(0, dot).trim();
            String method = expr.substring(dot + 1, open).trim();
            String argStr = expr.substring(open + 1, close).trim();
            List<Object> args = new ArrayList<>();
            if (!argStr.isEmpty()) {
                for (String piece : splitArgs(argStr)) {
                    String t = piece.trim();
                    if ((t.startsWith("'") && t.endsWith("'")) || (t.startsWith("\"") && t.endsWith("\"")))
                        args.add(t.substring(1, t.length() - 1));
                    else if (t.equals("true") || t.equals("false")) args.add(Boolean.parseBoolean(t));
                    else args.add(Long.parseLong(t));
                }
            }
            Class<?> iface = Class.forName((String) ifaces.get(key));
            Object service = resolveKey(ctx, iface, key);
            Method m = findMethod(iface, method, args.size());
            Object value = m.invoke(service, coerceArgs(m, args));
            log("probe", expr, "=> " + render(value));
        } catch (Exception ex) {
            // The probe dispatches reflectively, so without the unwrap every
            // failure read "ERROR null". `log` funnels the text through the
            // registry; a seam reply arrives already redacted.
            Throwable failure = ex;
            while (failure instanceof java.lang.reflect.InvocationTargetException wrapped && wrapped.getCause() != null) {
                failure = wrapped.getCause();
            }
            log("probe", expr, "ERROR " + failure.getClass().getSimpleName() + ": " + failure.getMessage());
        }
    }

    static List<String> splitArgs(String s) {
        List<String> out = new ArrayList<>();
        int depth = 0, start = 0;
        boolean inStr = false;
        char q = 0;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (inStr) { if (c == q) inStr = false; }
            else if (c == '\'' || c == '"') { inStr = true; q = c; }
            else if (c == '(' || c == '[') depth++;
            else if (c == ')' || c == ']') depth--;
            else if (c == ',' && depth == 0) { out.add(s.substring(start, i)); start = i + 1; }
        }
        out.add(s.substring(start));
        return out;
    }

    // --- reflection helpers -------------------------------------------------

    @SuppressWarnings({"unchecked", "rawtypes"})
    static Disposable provide(Context root, String name, Class<?> iface, Object impl) {
        return root.provide((ServiceKey) serviceKey(name, iface), impl);
    }

    static ServiceKey<?> serviceKey(String name, Class<?> iface) {
        return ServiceKey.of(iface, name);
    }

    static Object instantiate(Class<?> cls, Map<String, Object> config) throws Exception {
        if (config == null || config.isEmpty()) return cls.getDeclaredConstructor().newInstance();
        Object[] values = config.values().toArray();
        for (Constructor<?> ctor : cls.getDeclaredConstructors()) {
            if (ctor.getParameterCount() == values.length) {
                Class<?>[] types = ctor.getParameterTypes();
                Object[] coerced = new Object[values.length];
                for (int i = 0; i < values.length; i++) coerced[i] = coerce(values[i], types[i]);
                return ctor.newInstance(coerced);
            }
        }
        return cls.getDeclaredConstructor().newInstance();
    }

    static Method findMethod(Class<?> iface, String name, int arity) {
        for (Method m : iface.getMethods()) {
            if (m.getName().equals(name) && m.getParameterCount() == arity) return m;
        }
        throw new RuntimeException("no method " + name + "/" + arity + " on " + iface.getName());
    }

    static Object[] coerceArgs(Method m, List<Object> args) {
        Class<?>[] types = m.getParameterTypes();
        Object[] out = new Object[args.size()];
        for (int i = 0; i < args.size(); i++) out[i] = coerce(args.get(i), types[i]);
        return out;
    }

    static Object coerce(Object value, Class<?> type) {
        if (value == null) return null;
        if (type == long.class || type == Long.class) return ((Number) value).longValue();
        if (type == int.class || type == Integer.class) return ((Number) value).intValue();
        if (type == double.class || type == Double.class) return ((Number) value).doubleValue();
        if (type == boolean.class || type == Boolean.class) return value;
        if (type == String.class) return value.toString();
        return value;
    }

    static String render(Object v) {
        if (v == null) return "null";
        if (v instanceof java.util.Optional<?> o) return o.isPresent() ? render(o.get()) : "None";
        if (v instanceof String) return "\"" + v + "\"";
        return String.valueOf(v);
    }

    // --- the generic consumer-side proxy ------------------------------------

    static final class ForwardingHandler implements InvocationHandler {
        final BridgeClient client;
        final String key;
        ForwardingHandler(BridgeClient client, String key) { this.client = client; this.key = key; }

        public Object invoke(Object proxy, Method method, Object[] args) {
            if (method.getDeclaringClass() == Object.class) {
                switch (method.getName()) {
                    case "toString": return "BridgeProxy(" + key + ")";
                    case "hashCode": return System.identityHashCode(proxy);
                    case "equals": return proxy == (args == null ? null : args[0]);
                    default: return null;
                }
            }
            List<Object> callArgs = new ArrayList<>();
            // issue #1627: encoded as a reply is, so an Optional crosses as
            // its value or `null`, never as its `toString()`
            if (args != null) for (Object a : args) callArgs.add(BridgeCodec.encode(a));
            return BridgeCodec.decode(client.call(key, method.getName(), callArgs), method.getGenericReturnType());
        }

        Object coerceReturn(Object value, Class<?> ret) {
            if (ret == void.class || ret == Void.class) return null;
            if (ret == long.class || ret == Long.class) return value == null ? 0L : ((Number) value).longValue();
            if (ret == int.class || ret == Integer.class) return value == null ? 0 : ((Number) value).intValue();
            if (ret == boolean.class || ret == Boolean.class) return Boolean.TRUE.equals(value);
            if (ret == String.class) return value == null ? null : value.toString();
            if (java.util.Optional.class.isAssignableFrom(ret))
                return java.util.Optional.ofNullable(value == null ? null : value.toString());
            if (java.util.List.class.isAssignableFrom(ret))
                return value instanceof List ? value : new ArrayList<>();
            return value;
        }
    }

    // --- transport: a blocking JSON-over-unix-socket client -----------------

    static final class BridgeClient {
        final String path;
        BridgeClient(String path) { this.path = path; }

        Object call(String key, String method, List<Object> args) {
            // item 443 / issue #122 — the DISPATCH side of the E-Stop seam. Once
            // an operator arms the latch, this process stops DISPATCHING new
            // crossings: the outgoing call is REFUSED before it leaves the
            // process. It throws rather than withdrawing the proxy, because a halt
            // is not a peer death — reactive withdrawal would propagate the
            // graceful unwind the E-Stop exists to avoid. The refused caller's
            // attempt is item 440's ambiguous tier (docs/design/443-estop.md).
            if (Estop.estopEngaged()) {
                throw new RuntimeException("revl E-Stop engaged: this process is HALTED and "
                        + "refuses to dispatch new crossings (key " + key + ", method " + method
                        + ") — docs/design/443-estop.md");
            }
            // Record the crossing as in flight for its round-trip: a crossing
            // still out when the latch trips is the AMBIGUOUS one the inventory
            // names (item 440). Cleared in a finally so a throwing round trip
            // still leaves the registry clean.
            long seq = Estop.beginCrossing(key, method, "dispatch");
            try {
                RuntimeException last = null;
                for (int attempt = 0; attempt < 200; attempt++) {
                    try (SocketChannel ch = SocketChannel.open(StandardProtocolFamily.UNIX)) {
                        ch.connect(UnixDomainSocketAddress.of(path));
                        BufferedWriter w = new BufferedWriter(new OutputStreamWriter(Channels.newOutputStream(ch), StandardCharsets.UTF_8));
                        BufferedReader r = new BufferedReader(new InputStreamReader(Channels.newInputStream(ch), StandardCharsets.UTF_8));
                        Map<String, Object> req = new LinkedHashMap<>();
                        req.put("key", key); req.put("method", method); req.put("args", args);
                        w.write(Json.write(req)); w.write("\n"); w.flush();
                        String line = r.readLine();
                        if (line == null) throw new RuntimeException("bridge peer closed the connection");
                        @SuppressWarnings("unchecked")
                        Map<String, Object> reply = (Map<String, Object>) Json.parse(line);
                        if (!Boolean.TRUE.equals(reply.get("ok"))) throw new RuntimeException(String.valueOf(reply.get("error")));
                        return reply.get("value");
                    } catch (java.io.IOException io) {
                        last = new RuntimeException(io);
                        try { Thread.sleep(50); } catch (InterruptedException ignored) {}
                    }
                }
                throw last != null ? last : new RuntimeException("bridge connect failed");
            } finally {
                Estop.endCrossing(seq);
            }
        }
    }

    // --- a minimal JSON encoder/decoder (no dependencies) -------------------

    static final class Json {
        final String s;
        int i;
        Json(String s) { this.s = s; }

        static Object parse(String s) { Json j = new Json(s); j.ws(); return j.value(); }

        Object value() {
            ws();
            char c = s.charAt(i);
            switch (c) {
                case '{': return object();
                case '[': return array();
                case '"': return string();
                case 't': i += 4; return Boolean.TRUE;
                case 'f': i += 5; return Boolean.FALSE;
                case 'n': i += 4; return null;
                default: return number();
            }
        }

        Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String k = string();
                ws(); i++; // ':'
                m.put(k, value());
                ws();
                if (s.charAt(i) == ',') { i++; continue; }
                i++; // '}'
                return m;
            }
        }

        List<Object> array() {
            List<Object> a = new ArrayList<>();
            i++; ws();
            if (s.charAt(i) == ']') { i++; return a; }
            while (true) {
                a.add(value());
                ws();
                if (s.charAt(i) == ',') { i++; continue; }
                i++; // ']'
                return a;
            }
        }

        String string() {
            StringBuilder b = new StringBuilder();
            i++;
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') break;
                if (c == '\\') {
                    char e = s.charAt(i++);
                    switch (e) {
                        case 'n': b.append('\n'); break;
                        case 't': b.append('\t'); break;
                        case 'r': b.append('\r'); break;
                        case 'b': b.append('\b'); break;
                        case 'f': b.append('\f'); break;
                        case '/': b.append('/'); break;
                        case '"': b.append('"'); break;
                        case '\\': b.append('\\'); break;
                        case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                        default: b.append(e);
                    }
                } else {
                    b.append(c);
                }
            }
            return b.toString();
        }

        Object number() {
            int start = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String n = s.substring(start, i);
            if (n.contains(".") || n.contains("e") || n.contains("E")) return Double.parseDouble(n);
            return Long.parseLong(n);
        }

        void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }

        static String write(Object v) {
            StringBuilder b = new StringBuilder();
            writeTo(v, b);
            return b.toString();
        }

        static void writeTo(Object v, StringBuilder b) {
            if (v == null) { b.append("null"); return; }
            if (v instanceof String str) { b.append('"'); esc(str, b); b.append('"'); return; }
            if (v instanceof Boolean || v instanceof Number) { b.append(v); return; }
            if (v instanceof Map<?, ?> m) {
                b.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : m.entrySet()) {
                    if (!first) b.append(',');
                    first = false;
                    b.append('"'); esc(String.valueOf(e.getKey()), b); b.append("\":");
                    writeTo(e.getValue(), b);
                }
                b.append('}');
                return;
            }
            if (v instanceof List<?> list) {
                b.append('[');
                for (int k = 0; k < list.size(); k++) { if (k > 0) b.append(','); writeTo(list.get(k), b); }
                b.append(']');
                return;
            }
            b.append('"'); esc(v.toString(), b); b.append('"');
        }

        static void esc(String s, StringBuilder b) {
            for (int k = 0; k < s.length(); k++) {
                char c = s.charAt(k);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\t': b.append("\\t"); break;
                    case '\r': b.append("\\r"); break;
                    default: b.append(c);
                }
            }
        }
    }

    // Canonical ADT/Result wire codec (docs/interop-bridge.md "Canonical value
    // encoding"): scalars/List/records/Map/Opt are plain JSON; a user ADT or
    // Result value is {"$kind":"<Case>","$value":<payload>} ($value omitted for
    // a nullary case). Decode is type-directed (rebuilds the native value from
    // the method's generic return type), so encode stays type-free. Inlined
    // (not a shared file) so the single-file javac builds pick it up.
    static final class BridgeCodec {
        private BridgeCodec() {}
        private static final Object NO_PAYLOAD = new Object();

        static Object encode(Object v) {
            if (v == null || v instanceof Boolean || v instanceof Number || v instanceof String) return v;
            if (v instanceof java.util.Optional<?> o) return o.isPresent() ? encode(o.get()) : null;
            if (v instanceof java.util.List<?> list) {
                java.util.List<Object> out = new java.util.ArrayList<>();
                for (Object e : list) out.add(encode(e));
                return out;
            }
            if (v instanceof java.util.Map<?, ?> m) {
                java.util.Map<String, Object> out = new java.util.LinkedHashMap<>();
                for (java.util.Map.Entry<?, ?> e : m.entrySet()) out.put(String.valueOf(e.getKey()), encode(e.getValue()));
                return out;
            }
            Class<?> cls = v.getClass();
            if (isAdtVariant(cls)) {
                java.util.Map<String, Object> out = new java.util.LinkedHashMap<>();
                out.put("$kind", cls.getSimpleName());
                Object payload = singlePayload(v, cls);
                if (payload != NO_PAYLOAD) out.put("$value", encode(payload));
                return out;
            }
            java.util.Map<String, Object> rec = new java.util.LinkedHashMap<>();
            try {
                if (cls.isRecord()) {
                    for (java.lang.reflect.RecordComponent rc : cls.getRecordComponents())
                        rec.put(rc.getName(), encode(rc.getAccessor().invoke(v)));
                } else {
                    for (java.lang.reflect.Field f : dataFields(cls)) { f.setAccessible(true); rec.put(f.getName(), encode(f.get(v))); }
                }
            } catch (ReflectiveOperationException ex) {
                throw new RuntimeException("encode " + cls.getName() + ": " + ex.getMessage(), ex);
            }
            return rec;
        }

        static boolean isAdtVariant(Class<?> cls) {
            for (Class<?> i : cls.getInterfaces()) if (i.isSealed()) return true;
            return false;
        }

        static Object singlePayload(Object v, Class<?> cls) {
            try {
                if (cls.isRecord()) {
                    java.lang.reflect.RecordComponent[] rc = cls.getRecordComponents();
                    return rc.length == 0 ? NO_PAYLOAD : rc[0].getAccessor().invoke(v);
                }
                java.util.List<java.lang.reflect.Field> fields = dataFields(cls);
                if (fields.isEmpty()) return NO_PAYLOAD;
                fields.get(0).setAccessible(true);
                return fields.get(0).get(v);
            } catch (ReflectiveOperationException ex) {
                throw new RuntimeException("payload of " + cls.getName() + ": " + ex.getMessage(), ex);
            }
        }

        static java.util.List<java.lang.reflect.Field> dataFields(Class<?> cls) {
            java.util.List<java.lang.reflect.Field> out = new java.util.ArrayList<>();
            for (java.lang.reflect.Field f : cls.getDeclaredFields())
                if (!java.lang.reflect.Modifier.isStatic(f.getModifiers()) && !f.isSynthetic()) out.add(f);
            return out;
        }

        static Object decode(Object json, java.lang.reflect.Type target) {
            Class<?> raw = rawClass(target);
            if (raw == java.util.Optional.class)
                return java.util.Optional.ofNullable(json == null ? null : decode(json, typeArg(target, 0)));
            if (json == null) return null;
            if (java.util.List.class.isAssignableFrom(raw) && json instanceof java.util.List<?> list) {
                java.lang.reflect.Type elem = typeArg(target, 0);
                java.util.List<Object> out = new java.util.ArrayList<>();
                for (Object e : list) out.add(decode(e, elem));
                return out;
            }
            if (json instanceof java.util.Map<?, ?> jm) {
                if (jm.containsKey("$kind")) return decodeAdt(jm, raw, target);
                if (java.util.Map.class.isAssignableFrom(raw)) {
                    java.util.Map<String, Object> out = new java.util.LinkedHashMap<>();
                    for (java.util.Map.Entry<?, ?> e : jm.entrySet()) out.put(String.valueOf(e.getKey()), e.getValue());
                    return out;
                }
                return decodeRecord(jm, raw);
            }
            return coerceScalar(json, raw);
        }

        static Object decodeAdt(java.util.Map<?, ?> jm, Class<?> raw, java.lang.reflect.Type target) {
            String kind = (String) jm.get("$kind");
            Object payloadJson = jm.get("$value");
            try {
                Class<?> variant = Class.forName(raw.getName() + "$" + kind);
                java.lang.reflect.Constructor<?> ctor = variant.getDeclaredConstructors()[0];
                ctor.setAccessible(true);
                if (ctor.getParameterCount() == 0) return ctor.newInstance();
                java.lang.reflect.Type payloadType = raw.getSimpleName().equals("RevlResult")
                        ? typeArg(target, kind.equals("Err") ? 1 : 0)
                        : ctor.getGenericParameterTypes()[0];
                return ctor.newInstance(decode(payloadJson, payloadType));
            } catch (ReflectiveOperationException ex) {
                throw new RuntimeException("decode ADT " + raw.getName() + "." + kind + ": " + ex.getMessage(), ex);
            }
        }

        static Object decodeRecord(java.util.Map<?, ?> jm, Class<?> raw) {
            try {
                if (raw.isRecord()) {
                    java.lang.reflect.RecordComponent[] rc = raw.getRecordComponents();
                    Class<?>[] types = new Class<?>[rc.length];
                    Object[] args = new Object[rc.length];
                    for (int i = 0; i < rc.length; i++) {
                        types[i] = rc[i].getType();
                        args[i] = decode(jm.get(rc[i].getName()), rc[i].getGenericType());
                    }
                    java.lang.reflect.Constructor<?> ctor = raw.getDeclaredConstructor(types);
                    ctor.setAccessible(true);
                    return ctor.newInstance(args);
                }
                java.util.List<java.lang.reflect.Field> fields = dataFields(raw);
                java.lang.reflect.Constructor<?> ctor = raw.getDeclaredConstructors()[0];
                ctor.setAccessible(true);
                java.lang.reflect.Type[] pts = ctor.getGenericParameterTypes();
                Object[] args = new Object[fields.size()];
                for (int i = 0; i < fields.size(); i++) {
                    java.lang.reflect.Type t = i < pts.length ? pts[i] : fields.get(i).getGenericType();
                    args[i] = decode(jm.get(fields.get(i).getName()), t);
                }
                return ctor.newInstance(args);
            } catch (ReflectiveOperationException ex) {
                throw new RuntimeException("decode record " + raw.getName() + ": " + ex.getMessage(), ex);
            }
        }

        static Object coerceScalar(Object v, Class<?> type) {
            if (type == void.class || type == Void.class) return null;
            if (v == null) return null;
            if ((type == long.class || type == Long.class) && v instanceof Number n) return n.longValue();
            if ((type == int.class || type == Integer.class) && v instanceof Number n) return n.intValue();
            if ((type == double.class || type == Double.class) && v instanceof Number n) return n.doubleValue();
            if (type == boolean.class || type == Boolean.class) return Boolean.TRUE.equals(v);
            if (type == String.class) return v.toString();
            return v;
        }

        static Class<?> rawClass(java.lang.reflect.Type t) {
            if (t instanceof Class<?> c) return c;
            if (t instanceof java.lang.reflect.ParameterizedType p) return (Class<?>) p.getRawType();
            return Object.class;
        }

        static java.lang.reflect.Type typeArg(java.lang.reflect.Type t, int i) {
            if (t instanceof java.lang.reflect.ParameterizedType p) {
                java.lang.reflect.Type[] args = p.getActualTypeArguments();
                if (i < args.length) return args[i];
            }
            return Object.class;
        }
    }

    private RealPlacementRunner() {}
}
