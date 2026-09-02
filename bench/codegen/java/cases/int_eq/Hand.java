package bench;

/**
 * What a competent Java developer writes for `x == t` on two Int operands.
 *
 * `xs` is a `List<Long>`, so `x` arrives boxed either way; the difference is
 * the comparison itself. The emitted form is
 * `java.util.Objects.equals(x, t)`, which boxes `t` (a primitive `long`) on
 * every iteration, then does a null check, an `instanceof` and a virtual
 * `Long.equals`. The hand form unboxes once and compares with `lcmp`.
 */
public final class Hand {

    private Hand() {}

    public static long countEq(java.util.List<Long> xs, long t) {
        long n = 0L;
        for (Long x : xs) {
            if (x.longValue() == t) {
                n = Math.addExact(n, 1L);
            }
        }
        return n;
    }
}
