package bench;

/**
 * What a competent Java developer writes for a persistent one-element append.
 *
 * The semantics are the emitter's: `push` returns a NEW immutable list and
 * leaves its argument alone, so the hand form copies too. What it does not do
 * is route a single append through two Stream objects, a concat spliterator
 * and a stream-terminal collector.
 */
public final class Hand {

    private Hand() {}

    public static <T> java.util.List<T> push(java.util.List<T> xs, T v) {
        java.util.ArrayList<T> out = new java.util.ArrayList<>(xs.size() + 1);
        out.addAll(xs);
        out.add(v);
        return java.util.Collections.unmodifiableList(out);
    }

    public static java.util.List<Long> build(long n) {
        java.util.List<Long> out = java.util.List.of();
        long i = 0L;
        while (i < n) {
            out = push(out, i);
            i = Math.addExact(i, 1L);
        }
        return out;
    }
}
