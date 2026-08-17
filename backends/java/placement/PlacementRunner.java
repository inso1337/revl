// One java-placed process of a placement composition (spawned by
// src/revl/placement.py for a process whose backend is "java").
//
// Same spec shape as src/revl/_process_runner.py: it brings its slice of the
// composition up on a cordis4j Context. Because cordis4j services are Java
// interfaces, the consumer-side proxy is GENERIC: one InvocationHandler
// (java.lang.reflect.Proxy) forwards any interface method over the bridge's
// newline-delimited JSON wire, so no per-service codegen is needed (contrast
// the Rust tier, whose static traits force emitter-generated proxies).
//
// The stub cordis4j Context in backends/java/stubs is non-reactive, so this
// runner composes plugins manually in load order (proxies first, then its own
// components). Peer-death-as-withdrawal is a reactive-runtime property and
// needs the real cordis4j jar (REVL_CORDIS4J_CLASSES); that is the follow-up.
//
// Output is line-prefixed "[name]" so the conductor can interleave processes.
// Usage: java -cp <classes> PlacementRunner <spec.json>

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.Plugin;
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
import java.nio.channels.Channels;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;

public final class PlacementRunner {
    static String name = "?";
    static final List<Disposable> fibers = new ArrayList<>();

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
        Map<String, Object> spec = (Map<String, Object>) Json.parse(Files.readString(Path.of(argv[0])));
        name = (String) spec.get("name");
        String container = (String) spec.getOrDefault("module", "revl.Components");
        Map<String, Object> ifaces = (Map<String, Object>) spec.getOrDefault("ifaces", Map.of());
        Map<String, Object> config = (Map<String, Object>) spec.getOrDefault("config", Map.of());

        Context ctx = new Context();

        // 1. proxies for keys provided by other processes
        Map<String, Object> proxies = (Map<String, Object>) spec.getOrDefault("proxies", Map.of());
        for (Map.Entry<String, Object> entry : proxies.entrySet()) {
            String key = entry.getKey();
            Map<String, Object> info = (Map<String, Object>) entry.getValue();
            String socket = (String) info.get("socket");
            Class<?> iface = Class.forName((String) ifaces.get(key));
            BridgeClient client = new BridgeClient(socket);
            Object proxy = Proxy.newProxyInstance(iface.getClassLoader(), new Class<?>[]{iface},
                    new ForwardingHandler(client, key));
            Disposable undo = provide(ctx, iface, proxy);
            fibers.add(() -> { undo.dispose(); client.close(); });
            log("proxy", key, "-> " + socket);
        }

        // 2. this process's own components, in load order
        for (Object comp : (List<Object>) spec.getOrDefault("components", List.of())) {
            String cname = (String) comp;
            Class<?> cls = Class.forName(container + "$" + cname + "Plugin");
            Plugin plugin = (Plugin) instantiate(cls, (Map<String, Object>) config.getOrDefault(cname, Map.of()));
            Disposable disposable = plugin.apply(ctx);
            fibers.add(disposable);
            log("load", cname, "ACTIVE");
        }

        // 3. serve keys other processes need
        Map<String, Object> serve = (Map<String, Object>) spec.get("serve");
        Stub stub = null;
        if (serve != null) {
            List<Object> keys = (List<Object>) serve.get("keys");
            Map<String, Class<?>> served = new java.util.HashMap<>();
            for (Object k : keys) served.put((String) k, Class.forName((String) ifaces.get((String) k)));
            stub = new Stub(ctx, (String) serve.get("socket"), served);
            stub.start();
            log("serve", String.join(", ", served.keySet()), "-> " + serve.get("socket"));
        }

        // 4. probes: call provided services (may cross a seam)
        for (Object p : (List<Object>) spec.getOrDefault("probe", List.of())) {
            String expr = (String) p;
            try {
                Object value = probe(ctx, ifaces, expr);
                log("probe", expr, "=> " + render(value));
            } catch (Exception ex) {
                log("probe", expr, "ERROR " + ex.getMessage());
            }
        }

        System.out.println("[" + name + "] UP");
        System.out.flush();

        // 5. hold until SIGTERM/SIGINT, then tear down consumers-first
        CountDownLatch latch = new CountDownLatch(1);
        final Stub stubRef = stub;
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            for (int i = fibers.size() - 1; i >= 0; i--) {
                try { fibers.get(i).dispose(); } catch (Throwable ignored) {}
            }
            if (stubRef != null) stubRef.close();
            System.out.println("[" + name + "] DOWN");
            System.out.flush();
        }));
        latch.await();
    }

    // --- provided-service probe: parse `<key>.<method>('a', 'b')` -----------

    static Object probe(Context ctx, Map<String, Object> ifaces, String expr) throws Exception {
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
        return m.invoke(service, coerceArgs(m, args));
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

    @SuppressWarnings("unchecked")
    static Disposable provide(Context ctx, Class<?> iface, Object impl) throws Exception {
        Object key = ServiceKey.class.getMethod("of", Class.class).invoke(null, iface);
        return (Disposable) Context.class.getMethod("provide", ServiceKey.class, Object.class).invoke(ctx, key, impl);
    }

    static Object instantiate(Class<?> cls, Map<String, Object> config) throws Exception {
        if (config == null || config.isEmpty()) {
            return cls.getDeclaredConstructor().newInstance();
        }
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

        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
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
            Object value = client.call(key, method.getName(), callArgs);
            return coerceReturn(value, method.getReturnType());
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
            for (int attempt = 0; attempt < 100; attempt++) { // retry while the provider comes up
                try (SocketChannel ch = SocketChannel.open(StandardProtocolFamily.UNIX)) {
                    ch.connect(UnixDomainSocketAddress.of(path));
                    BufferedWriter w = new BufferedWriter(new OutputStreamWriter(Channels.newOutputStream(ch), StandardCharsets.UTF_8));
                    BufferedReader r = new BufferedReader(new InputStreamReader(Channels.newInputStream(ch), StandardCharsets.UTF_8));
                    Map<String, Object> req = new java.util.LinkedHashMap<>();
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

        void close() {}
    }

    // --- transport: the provider-side stub ----------------------------------

    static final class Stub {
        final Context ctx;
        final String path;
        final Map<String, Class<?>> served;
        ServerSocketChannel server;
        volatile boolean running = true;

        Stub(Context ctx, String path, Map<String, Class<?>> served) {
            this.ctx = ctx; this.path = path; this.served = served;
        }

        void start() throws Exception {
            Files.deleteIfExists(Path.of(path));
            server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(path));
            Thread t = new Thread(this::acceptLoop, "bridge-stub");
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
                    Map<String, Object> reply = new java.util.LinkedHashMap<>();
                    try {
                        Map<String, Object> req = (Map<String, Object>) Json.parse(line);
                        String key = (String) req.get("key");
                        Class<?> iface = served.get(key);
                        if (iface == null) throw new RuntimeException("key " + key + " not exported");
                        Object service = ctx.get((Class) iface);
                        List<Object> args = (List<Object>) req.getOrDefault("args", List.of());
                        Method m = findMethod(iface, (String) req.get("method"), args.size());
                        Object result = m.invoke(service, coerceArgs(m, args));
                        reply.put("ok", true);
                        reply.put("value", jsonable(result));
                    } catch (Throwable t) {
                        reply.put("ok", false);
                        reply.put("error", t.getClass().getSimpleName() + ": " + t.getMessage());
                    }
                    w.write(Json.write(reply)); w.write("\n"); w.flush();
                }
            } catch (Exception ignored) {}
        }

        Object jsonable(Object v) {
            if (v instanceof java.util.Optional<?> opt) return opt.orElse(null);
            return v;
        }

        void close() {
            running = false;
            try { if (server != null) server.close(); } catch (Exception ignored) {}
            try { Files.deleteIfExists(Path.of(path)); } catch (Exception ignored) {}
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
            Map<String, Object> m = new java.util.LinkedHashMap<>();
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
            i++; // opening quote
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

        @SuppressWarnings("unchecked")
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

    private PlacementRunner() {}
}
