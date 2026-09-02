package bench;

/**
 * Emitted-vs-hand-written A/B for one codegen case.
 *
 * Every case ships a {@code bench.Drive} with the same members, and this
 * harness never learns anything case-specific:
 *
 * <pre>
 *   public static final String NAME;   // case identity, echoed into the JSON
 *   public static final int N;         // workload size for one "op"
 *   public static final int WARMUP;    // ops to run per side before sampling
 *   public static void setup();        // one-time wiring, not measured
 *   public static long emitted(int n); // drives revl.Components
 *   public static long hand(int n);    // drives the hand-written equivalent
 * </pre>
 *
 * Both sides return a checksum that this harness accumulates and prints, so
 * neither body can be dead-code-eliminated.
 *
 * <h2>What is measured, and what deliberately is not</h2>
 *
 * <b>Allocated bytes per op</b>, from
 * {@code com.sun.management.ThreadMXBean.getCurrentThreadAllocatedBytes()}.
 * That counter reports bytes this thread has handed to the allocator. It is a
 * count, not a duration: it does not move when another process takes the CPU,
 * so it stays meaningful on a machine running other work. It is also the
 * measure that maps onto the emitter choices under audit, since a hoisted
 * constant, a dropped defensive copy and an unboxed comparison each change it
 * by a fixed amount per call.
 *
 * <b>No timing.</b> This harness reports no duration, and that is on purpose.
 * A wall-clock number taken on a loaded machine measures the load, and
 * interleaving the two arms does not rescue it: the arms sample different
 * moments and therefore different contention. On the JVM it is worse still,
 * because the same contention distorts JIT compilation. A number that looks
 * measured but is not is worse than no number, so timing belongs to a
 * separate, serialized run on a quiet machine and not to this file.
 *
 * The warmup below is iteration-counted rather than clock-bounded for the same
 * reason. Its job is only to get class initialization, invokedynamic call-site
 * linkage and the first-call inflation of the reflective probe out of the
 * allocation sample.
 */
public final class Bench {

    private static final int ALLOC_SAMPLES = 15;

    private Bench() {}

    public static void main(String[] args) throws Exception {
        final int n = Drive.N;
        Drive.setup();

        long sink = 0L;

        // Alternate, so neither side is measured against a profile the other
        // polluted, and so both sides' call sites are linked before sampling.
        for (int i = 0; i < Drive.WARMUP; i++) {
            sink += Drive.emitted(n);
            sink += Drive.hand(n);
        }

        // The minimum over several samples. A sample can only be inflated (by
        // a safepoint, a TLAB refill accounted to this thread, a lazily linked
        // call site), never deflated, so the minimum is the closest estimate
        // of the steady-state per-op cost.
        long allocEmitted = -1L;
        long allocHand = -1L;
        AllocProbe probe = AllocProbe.create();
        if (probe != null) {
            allocEmitted = Long.MAX_VALUE;
            allocHand = Long.MAX_VALUE;
            for (int i = 0; i < ALLOC_SAMPLES; i++) {
                long before = probe.bytes();
                sink += Drive.emitted(n);
                long mid = probe.bytes();
                sink += Drive.hand(n);
                long after = probe.bytes();
                allocEmitted = Math.min(allocEmitted, mid - before);
                allocHand = Math.min(allocHand, after - mid);
            }
        }

        StringBuilder json = new StringBuilder();
        json.append('{');
        append(json, "case", Drive.NAME).append(',');
        append(json, "n", n).append(',');
        append(json, "alloc_bytes_emitted", allocEmitted).append(',');
        append(json, "alloc_bytes_hand", allocHand).append(',');
        append(json, "checksum", sink);
        json.append('}');
        System.out.println(json);
    }

    private static StringBuilder append(StringBuilder out, String key, String value) {
        return out.append('"').append(key).append("\":\"").append(value).append('"');
    }

    private static StringBuilder append(StringBuilder out, String key, long value) {
        return out.append('"').append(key).append("\":").append(value);
    }

    /**
     * Per-thread allocation counter, or null when the running JVM does not
     * expose one. Reached reflectively so this harness still compiles and runs
     * on a JVM whose management extension is absent or disabled, in which case
     * the allocation columns report -1.
     */
    private static final class AllocProbe {
        private final java.lang.reflect.Method method;
        private final Object bean;

        private AllocProbe(java.lang.reflect.Method method, Object bean) {
            this.method = method;
            this.bean = bean;
        }

        static AllocProbe create() {
            try {
                Object bean = java.lang.management.ManagementFactory.getThreadMXBean();
                Class<?> sun = Class.forName("com.sun.management.ThreadMXBean");
                if (!sun.isInstance(bean)) {
                    return null;
                }
                java.lang.reflect.Method m = sun.getMethod("getCurrentThreadAllocatedBytes");
                AllocProbe probe = new AllocProbe(m, bean);
                // Call it twice: the first invocation inflates the reflective
                // accessor, and neither call should be charged to a sample.
                probe.bytes();
                return probe.bytes() < 0 ? null : probe;
            } catch (Exception unavailable) {
                return null;
            }
        }

        long bytes() {
            try {
                return (Long) method.invoke(bean);
            } catch (Exception unavailable) {
                return -1L;
            }
        }
    }
}
