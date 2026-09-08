// The operator E-Stop on the java tier — roadmap item 443, issue #122.
//
// The java twin of `backends/go/placement_runner/estop/estop_test.go` and the
// rust `#[cfg(test)]` block in `backends/rust/placement_runner/src/estop.rs`: a
// standalone JDK program that RUNS the java latch reader, in-flight registry
// and halt inventory (`Estop`) against the same scenarios the go/rust suites
// pin, and prints `ESTOP_OK` when every assertion holds. It is driven by
// `backends/java/test_estop_java.py` (compile `Estop.java` + this, run, assert
// the sentinel), so the java tier's E1/E4/E5 behavior is proven by execution
// rather than by a substring match, exactly as the go/rust tiers are.
//
// Pure JDK, no cordis4j: argv[0] is a scratch directory for latch files.

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

public final class EstopChecks {
    private EstopChecks() {}

    static void check(boolean cond, String what) {
        if (!cond) {
            throw new AssertionError("E-Stop java check failed: " + what);
        }
    }

    static void write(Path p, String text) throws Exception {
        Files.write(p, text.getBytes(StandardCharsets.UTF_8));
    }

    public static void main(String[] argv) throws Exception {
        Path dir = Path.of(argv[0]);

        // 1. an absent latch reads as not-halted; an empty/null path too.
        String missing = dir.resolve("nope.estop").toString();
        check(Estop.readLatch(missing) == null, "an absent latch must read as null (not halted)");
        check(!Estop.estopEngagedAt(missing), "an absent latch must not engage the E-Stop");
        check(Estop.readLatch("") == null, "an empty path must read as null");
        check(Estop.readLatch(null) == null, "a null path must read as null");

        // 2. an armed latch reads as halted and carries its fields.
        Path armed = dir.resolve("halt.estop");
        write(armed, "{\"halted\":true,\"reason\":\"runaway loop\",\"operator\":\"ops@example\"}");
        Map<String, Object> record = Estop.readLatch(armed.toString());
        check(record != null, "an armed latch must read as halted");
        check("runaway loop".equals(record.get("reason"))
                && "ops@example".equals(record.get("operator")),
                "the latch's fields must be carried: " + record);
        check(Estop.estopEngagedAt(armed.toString()), "an armed latch must engage the E-Stop");

        // 3. the one failure mode this feature exists to prevent: a MALFORMED
        //    latch still reads as HALTED, matching estop.py::read_latch (E1).
        Path garbage = dir.resolve("garbage.estop");
        write(garbage, "{ this is not json");
        Map<String, Object> g = Estop.readLatch(garbage.toString());
        check(g != null && Boolean.TRUE.equals(g.get("halted")), "a malformed latch must still halt: " + g);
        check(Estop.estopEngagedAt(garbage.toString()), "a malformed latch must engage the E-Stop");

        // 4. a JSON value that is not an object (a bare array/number) also halts.
        Path arr = dir.resolve("arr.estop");
        write(arr, "[1, 2, 3]");
        check(Estop.estopEngagedAt(arr.toString()), "a non-object JSON latch must engage the E-Stop");

        // 5. latch-path precedence: explicit wins; a WAL derives <wal>.estop;
        //    with nothing and no env there is no latch. (Checked before any
        //    publishLatch, so the ambient override is still empty.)
        check(Estop.latchPath("/a/b.estop", "", true).equals("/a/b.estop"),
                "explicit latch path must win");
        check(Estop.latchPath("", "/run/session.wal", true).equals("/run/session.wal.estop"),
                "wal must derive <wal>.estop");
        check(Estop.latchPath("", "", false) == null, "no override and no env means no latch");

        // 6. estopEngaged() reads the PUBLISHED ambient latch (the runner
        //    publishes the spec's latch; a java process cannot set its own env).
        Path ambient = dir.resolve("ambient.estop");
        Estop.publishLatch(ambient.toString());
        check(Estop.latchPath(null, null, true).equals(ambient.toString()),
                "the published latch must be the resolved ambient path");
        check(!Estop.estopEngaged(), "no latch file yet: must not be engaged");
        write(ambient, "{\"halted\":true}");
        check(Estop.estopEngaged(), "the published latch is armed: must be engaged");

        // 7. the in-flight crossing registry: a begun crossing appears, an ended
        //    one is cleared (E4).
        long seq = Estop.beginCrossing("db", "query", "accept");
        boolean found = false;
        for (Estop.Crossing c : Estop.inFlightCrossings()) {
            if (c.seq == seq && "db".equals(c.key) && "query".equals(c.method)
                    && "accept".equals(c.direction)) {
                found = true;
            }
        }
        check(found, "a begun crossing must appear in the in-flight registry");
        Estop.endCrossing(seq);
        for (Estop.Crossing c : Estop.inFlightCrossings()) {
            check(c.seq != seq, "an ended crossing must be cleared from the registry");
        }

        // 8. the inventory shape and the halt line the conductor parses (E5).
        long s1 = Estop.beginCrossing("db", "write", "accept");
        Map<String, Object> latch = Map.of("reason", "runaway loop", "operator", "ops@example");
        Map<String, Object> inv = Estop.estopInventory("edge", Estop.inFlightCrossings(), latch);
        check("halted".equals(inv.get("verdict")) && Boolean.FALSE.equals(inv.get("resumable")),
                "the inventory must carry the halted verdict and no resume: " + inv);
        check("runaway loop".equals(inv.get("reason")) && "ops@example".equals(inv.get("operator")),
                "the inventory must carry the latch's reason/operator: " + inv);
        @SuppressWarnings("unchecked")
        List<Object> inFlight = (List<Object>) inv.get("inFlight");
        check(inFlight != null && !inFlight.isEmpty(), "the in-flight crossing must be reported: " + inv.get("inFlight"));
        @SuppressWarnings("unchecked")
        Map<String, Object> entry = (Map<String, Object>) inFlight.get(0);
        check("estop-ambiguous".equals(entry.get("kind")) && "unknown".equals(entry.get("outcome")),
                "a crossing in flight at the halt must be AMBIGUOUS with unknown outcome: " + entry);
        @SuppressWarnings("unchecked")
        List<Object> stranded = (List<Object>) inv.get("stranded");
        check(stranded != null && stranded.isEmpty(),
                "this tier keeps no witnessed-inverse ledger, so stranded is honestly empty: " + inv.get("stranded"));

        String line = Estop.estopHaltLine("edge", Estop.inFlightCrossings(), latch);
        String prefix = "[edge] " + Estop.HALTED_LINE + " ";
        check(line.startsWith(prefix), "the halt line must carry the conductor's prefix: " + line);
        Object parsed = Estop.Json.parse(line.substring(prefix.length()));
        check(parsed instanceof Map, "the halt line's payload must be a JSON object");
        @SuppressWarnings("unchecked")
        Map<String, Object> parsedMap = (Map<String, Object>) parsed;
        check("halted".equals(parsedMap.get("verdict")), "the halt line's JSON must carry the halted verdict: " + parsedMap);
        Estop.endCrossing(s1);

        System.out.println("ESTOP_OK");
    }
}
