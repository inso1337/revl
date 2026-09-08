// The operator E-Stop's shared vocabulary on the java tier — roadmap item 443,
// issue #122. The java twin of `backends/go/placement_runner/estop/estop.go`,
// `backends/rust/placement_runner/src/estop.rs`, `backends/typescript/estop.ts`
// and `src/revl/estop.py`.
//
// `docs/design/443-estop.md` is the reasoning of record and
// `docs/design/443-estop-tier-contract.md` fixes the E1-E8 contract every
// honoring runtime meets identically. Item 443 landed the halt on the py
// reference tier; go and rust honored it next (PR #611). The java tier kept its
// cooperative teardown and had NO E-Stop, so a placement halt SIGKILLed a java
// child and reported its residue UNKNOWN. This class is the java tier honoring
// the latch, shared by both java placement runners (the stub `PlacementRunner`
// on JDK 17 and the reactive `RealPlacementRunner` on cordis4j / JDK 21):
//
//   - the latch READER (`latchPath`, `readLatch`, `estopEngaged`), byte-for-byte
//     the rule `src/revl/estop.py::read_latch` applies — including the
//     fail-closed rule that a malformed OR unreadable latch still reads as
//     HALTED (E1) — so the tiers cannot drift on what an armed (or corrupted)
//     latch means;
//   - the in-flight crossing REGISTRY (`beginCrossing`/`endCrossing`/
//     `inFlightCrossings`): a crossing still executing when the button is hit is
//     the AMBIGUOUS one (item 440, E4);
//   - the halt INVENTORY (`estopInventory`/`estopHaltLine`), shaped into the
//     merged residue schema `src/revl/placement.py::_estop_halt_report` reads
//     (E5), byte-compatible with the shape the py runner, the go/rust tiers and
//     the ts tier emit.
//
// A java process cannot rewrite its own environment the way the go runner does
// with `os.Setenv`, so the runner publishes the spec's latch here via
// `publishLatch`; `estopEngaged` consults that override first and the ambient
// `REVL_ESTOP_LATCH` only as the fallback (the conductor sets both). The seam
// wiring lives in the runners (the accept seam in `PlacementRunner.Stub`, the
// dispatch seam in each `BridgeClient`, the idle watcher after `UP`); this
// class is the vocabulary they share. Pure JDK, no cordis4j and no runner
// dependency, so it compiles and is exercised standalone.

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class Estop {
    private Estop() {}

    // The ambient latch path, equivalent to `--estop-latch FILE`. The conductor
    // (`src/revl/placement.py`) hands a honoring child the latch in its spec and
    // also in this environment variable; the runner publishes the spec value so
    // the seams and the watcher all read one latch. Kept identical to
    // `estop.py::LATCH_ENV` / `estop.ts::LATCH_ENV` / `estop.go::LatchEnv`.
    public static final String LATCH_ENV = "REVL_ESTOP_LATCH";

    // What a latch-honoring child prints when the latch trips: its own in-flight
    // inventory, on one line, so the conductor merges it without a second
    // channel. Kept identical to `estop.py::HALTED_LINE` / `estop.go::HaltedLine`.
    public static final String HALTED_LINE = "HALTED";

    // The spec-published latch. A java process cannot set its own environment,
    // so the runner calls `publishLatch` with the spec's `estopLatch` before the
    // first crossing; `latchPath` then prefers it over the ambient variable.
    private static volatile String ambientLatch = null;

    // Publish the spec latch so every seam and the watcher read one path. A
    // placement that was never armed carries no latch and this is never called,
    // so an unarmed run is byte-identical to the pre-443 runner (E3).
    public static void publishLatch(String path) {
        if (path != null && !path.isEmpty()) {
            ambientLatch = path;
        }
    }

    // The latch file to act on: an explicit path, else `<wal>.estop`, else the
    // published spec latch, else the ambient `REVL_ESTOP_LATCH`. Mirrors
    // `estop.py::latch_path` / `estop.go::LatchPath`: deriving it from the WAL is
    // not a convenience but the durable rendezvous the reconciliation path
    // (`revl recover --wal`) names.
    public static String latchPath(String latch, String wal, boolean env) {
        if (latch != null && !latch.isEmpty()) {
            return latch;
        }
        if (wal != null && !wal.isEmpty()) {
            return wal + ".estop";
        }
        String published = ambientLatch;
        if (published != null && !published.isEmpty()) {
            return published;
        }
        if (env) {
            String fromEnv = System.getenv(LATCH_ENV);
            return (fromEnv == null || fromEnv.isEmpty()) ? null : fromEnv;
        }
        return null;
    }

    // The halt an operator wrote at `path`, or null when the latch is absent.
    //
    // A latch that EXISTS but does not parse still reads as HALTED. Failing open
    // on a malformed emergency stop is the one failure mode this feature exists
    // to prevent, so every reader — the py runtime seam, the CLI, the conductor,
    // the ts/go/rust tiers and now this — applies the same rule (E1). The same
    // reasoning covers a latch we cannot READ: an EACCES on a latch whose
    // permissions changed, or an EISDIR on a path an operator turned into a
    // directory, is an existing-but-unreadable latch, not an absent one, so it
    // too reads as HALTED. Only a genuinely absent latch (NoSuchFileException)
    // reads as not-halted, matching `estop.py::read_latch`
    // (FileNotFoundError -> None, every other OSError -> fail closed).
    public static Map<String, Object> readLatch(String path) {
        if (path == null || path.isEmpty()) {
            return null;
        }
        String text;
        try {
            text = new String(Files.readAllBytes(Path.of(path)), StandardCharsets.UTF_8);
        } catch (NoSuchFileException absent) {
            return null;
        } catch (java.io.FileNotFoundException absent) {
            return null;
        } catch (java.io.IOException unreadable) {
            // Existing but unreadable (EACCES, EISDIR, ...): fail CLOSED.
            return unreadable();
        }
        Object record;
        try {
            record = Json.parse(text);
        } catch (RuntimeException malformed) {
            return unreadable();
        }
        if (record instanceof Map<?, ?> map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> typed = (Map<String, Object>) map;
            return typed;
        }
        // A JSON value that is not an object (a bare array/number/string) halts.
        return unreadable();
    }

    private static Map<String, Object> unreadable() {
        Map<String, Object> record = new LinkedHashMap<>();
        record.put("halted", true);
        record.put("reason", "operator halt (unreadable latch)");
        record.put("operator", "unknown");
        return record;
    }

    // Whether a halt is in force on the latch at `path`.
    public static boolean estopEngagedAt(String path) {
        return readLatch(path) != null;
    }

    // Whether a halt is in force on the latch this process watches (the
    // published spec latch, else the ambient `REVL_ESTOP_LATCH`). The seams
    // consult this on each incoming or outgoing crossing: the cost is one file
    // read per crossing WHILE a latch is armed, and nothing at all when none is
    // — the default — because `latchPath` short-circuits to null.
    public static boolean estopEngaged() {
        return estopEngagedAt(latchPath(null, null, true));
    }

    // --- the in-flight crossing registry (item 443, issue #122; E4) ----------

    // One boundary crossing recorded while it is in flight. A crossing still in
    // the registry when the latch trips is AMBIGUOUS: its at-most-once attempt
    // may or may not have landed (item 440).
    public static final class Crossing {
        public final String key;
        public final String method;
        public final String direction; // "accept" (an incoming call being answered)
                                        // or "dispatch" (an outgoing proxy call)
        public final long seq;

        Crossing(String key, String method, String direction, long seq) {
            this.key = key;
            this.method = method;
            this.direction = direction;
            this.seq = seq;
        }
    }

    private static final Object REGISTRY_LOCK = new Object();
    private static final Map<Long, Crossing> IN_FLIGHT = new LinkedHashMap<>();
    private static long seqCounter = 0;

    // Record a crossing as in flight and return its sequence number. The seam
    // pairs it with `endCrossing` in a finally block so a throwing handler still
    // leaves the registry clean.
    public static long beginCrossing(String key, String method, String direction) {
        synchronized (REGISTRY_LOCK) {
            long seq = ++seqCounter;
            IN_FLIGHT.put(seq, new Crossing(key, method, direction, seq));
            return seq;
        }
    }

    // Clear a recorded crossing once its handler returns.
    public static void endCrossing(long seq) {
        synchronized (REGISTRY_LOCK) {
            IN_FLIGHT.remove(seq);
        }
    }

    // A snapshot of the crossings executing right now, ordered by sequence so
    // the inventory is deterministic.
    public static List<Crossing> inFlightCrossings() {
        synchronized (REGISTRY_LOCK) {
            List<Crossing> out = new ArrayList<>(IN_FLIGHT.values());
            out.sort((a, b) -> Long.compare(a.seq, b.seq));
            return out;
        }
    }

    // --- the halt inventory (item 443, issue #122; E5) -----------------------

    private static String stringField(Map<String, Object> record, String key, String fallback) {
        if (record != null) {
            Object value = record.get(key);
            if (value instanceof String s && !s.isEmpty()) {
                return s;
            }
        }
        return fallback;
    }

    // Shape the crossings that were in flight when the button was hit into the
    // merged residue schema (`src/revl/placement.py::_estop_halt_report`),
    // byte-compatible with the shape the py runner and the go/rust/ts tiers
    // emit.
    //
    // A crossing still executing when the operator armed the latch is AMBIGUOUS
    // — its at-most-once attempt may or may not have landed (item 440), the
    // designed outcome of an operator halt, not an edge case. This tier keeps no
    // witnessed-inverse ledger, so `stranded` is empty and HONESTLY so: the halt
    // reports what it can name (the crossings in flight) rather than inventing a
    // book it does not keep, and the conductor never reads that empty list as
    // "nothing was owed" because the ambiguous crossings are still reported.
    public static Map<String, Object> estopInventory(
            String process, List<Crossing> crossings, Map<String, Object> record) {
        List<Object> inFlightEntries = new ArrayList<>();
        for (Crossing c : crossings) {
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("kind", "estop-ambiguous");
            entry.put("state", "unresolved");
            entry.put("component", c.key);
            entry.put("method", c.method);
            entry.put("seq", c.seq);
            entry.put("entry", "crossing");
            entry.put("direction", c.direction);
            entry.put("attemptedFlag", true);
            entry.put("outcome", "unknown");
            inFlightEntries.add(entry);
        }
        Map<String, Object> inventory = new LinkedHashMap<>();
        inventory.put("process", process);
        inventory.put("verdict", "halted");
        inventory.put("reason", stringField(record, "reason", "operator halt"));
        inventory.put("operator", stringField(record, "operator", "unknown"));
        inventory.put("activations", new ArrayList<>());
        inventory.put("inFlight", inFlightEntries);
        inventory.put("stranded", new ArrayList<>());
        inventory.put("resumable", false);
        return inventory;
    }

    // The single line a latch-honoring child prints when the button is hit:
    // `[name] HALTED {inventory}`. The conductor parses it off stdout by the
    // `HALTED_LINE` prefix (`src/revl/placement.py::pump`) and merges the
    // inventory into the halt report without a second channel — the exact
    // contract the py runner and the go/rust/ts tiers already meet.
    public static String estopHaltLine(
            String process, List<Crossing> crossings, Map<String, Object> record) {
        return "[" + process + "] " + HALTED_LINE + " "
                + Json.write(estopInventory(process, crossings, record));
    }

    // --- a minimal JSON reader/writer (no dependencies) ----------------------
    //
    // Estop is shared by both runners, each of which carries its OWN inlined
    // Json (PlacementRunner.Json / RealPlacementRunner.Json), so it cannot
    // borrow either; it keeps this compact copy to stay standalone. The subset
    // is exactly what a latch record and an inventory need.

    static final class Json {
        final String s;
        int i;
        Json(String s) { this.s = s; }

        static Object parse(String s) { Json j = new Json(s); j.ws(); Object v = j.value(); return v; }

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
}
