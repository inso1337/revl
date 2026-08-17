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
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;

public final class RealPlacementRunner {
    static String name = "?";
    static final String STOP = "__stop__";

    static void log(String channel, String subject, String detail) {
        System.out.println(("[" + name + "] " + pad(channel, 8) + "| " + pad(subject, 16) + "| " + detail).stripTrailing());
    }

    static String pad(String s, int n) {
        StringBuilder b = new StringBuilder(s);
        while (b.length() < n) b.append(' ');
        return b.toString();
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] argv) throws Exception {
        Map<String, Object> spec = (Map<String, Object>) Json.parse(Files.readString(Path.of(argv[0])));
        name = (String) spec.get("name");
        String container = (String) spec.getOrDefault("module", "revl.Components");
        Map<String, Object> ifaces = (Map<String, Object>) spec.getOrDefault("ifaces", Map.of());
        Map<String, Object> config = (Map<String, Object>) spec.getOrDefault("config", Map.of());
        Map<String, Object> proxies = (Map<String, Object>) spec.getOrDefault("proxies", Map.of());
        List<Object> components = (List<Object>) spec.getOrDefault("components", List.of());
        List<Object> probes = (List<Object>) spec.getOrDefault("probe", List.of());

        Context root = Contexts.create();
        BlockingQueue<String> events = new LinkedBlockingQueue<>();
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
            bindings.put(key, provide(root, iface, proxy));
            deps.add(serviceKey(iface));
            startMonitor(key, socket, events);
            log("proxy", key, "-> " + socket + " (reactive)");
        }

        // 2. load components. With cross-deps they are reactive consumers: one
        //    inject fiber gated on the deps, so a withdrawal deactivates it.
        if (!deps.isEmpty()) {
            root.inject(deps, ctx -> {
                List<Disposable> domains = new ArrayList<>();
                for (Object comp : components) {
                    String cname = (String) comp;
                    try {
                        Class<?> cls = Class.forName(container + "$" + cname + "Plugin");
                        io.cordis4j.core.Plugin plugin =
                                (io.cordis4j.core.Plugin) instantiate(cls, (Map<String, Object>) config.getOrDefault(cname, Map.of()));
                        domains.add(plugin.apply(ctx));
                        log("load", cname, "ACTIVE (reactive)");
                    } catch (Exception e) {
                        throw new RuntimeException(e);
                    }
                }
                for (Object p : probes) runProbe(ctx, ifaces, (String) p);
                // first-reverted cleanup: fires when the fiber deactivates on
                // withdrawal, evidence of reactive teardown (no exception).
                return () -> {
                    log("withdraw", "deactivated", "consumer unloaded reactively; inverses run");
                    for (int i = domains.size() - 1; i >= 0; i--) {
                        try { domains.get(i).dispose(); } catch (Throwable ignored) {}
                    }
                };
            });
        } else {
            for (Object comp : components) {
                String cname = (String) comp;
                Class<?> cls = Class.forName(container + "$" + cname + "Plugin");
                io.cordis4j.core.Plugin plugin =
                        (io.cordis4j.core.Plugin) instantiate(cls, (Map<String, Object>) config.getOrDefault(cname, Map.of()));
                bindings.put("comp:" + cname, root.plugin(plugin));
                log("load", cname, "ACTIVE");
            }
            for (Object p : probes) runProbe(root, ifaces, (String) p);
        }

        System.out.println("[" + name + "] UP");
        System.out.flush();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> events.offer(STOP)));

        // 3. main loop: every context call stays on this thread (cordis4j D8).
        while (true) {
            String event = events.take();
            if (event.equals(STOP)) {
                teardown(bindings);
                break;
            }
            Disposable binding = bindings.remove(event);
            if (binding != null) {
                log("peer", event, "provider died: withdrawing the binding");
                binding.dispose(); // withdrawal -> the injected consumer deactivates reactively
            }
            if (bindings.isEmpty()) break; // every proxied provider gone; the consumer is fully withdrawn
        }
        System.out.println("[" + name + "] DOWN");
        System.out.flush();
        System.exit(0);
    }

    static void teardown(Map<String, Disposable> bindings) {
        List<Disposable> all = new ArrayList<>(bindings.values());
        for (int i = all.size() - 1; i >= 0; i--) {
            try { all.get(i).dispose(); } catch (Throwable ignored) {}
        }
    }

    // --- peer-death monitor: an idle connection whose EOF means the provider died ---

    static void startMonitor(String key, String path, BlockingQueue<String> events) {
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
            Object service = ctx.get((Class) iface);
            Method m = findMethod(iface, method, args.size());
            Object value = m.invoke(service, coerceArgs(m, args));
            log("probe", expr, "=> " + render(value));
        } catch (Exception ex) {
            log("probe", expr, "ERROR " + ex.getMessage());
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
    static Disposable provide(Context root, Class<?> iface, Object impl) {
        return root.provide((ServiceKey) serviceKey(iface), impl);
    }

    static ServiceKey<?> serviceKey(Class<?> iface) {
        return ServiceKey.of(iface);
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
            if (args != null) for (Object a : args) callArgs.add(a);
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
