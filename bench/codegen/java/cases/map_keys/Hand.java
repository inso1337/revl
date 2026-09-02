package bench;

/**
 * What a competent Java developer writes for `Map.keys()`.
 *
 * The sort and its code-point comparator are load-bearing (docs/strings.md
 * pins canonical Str order past U+FFFF), so they stay. What goes is the
 * `List.copyOf` at the end: `ks` is a freshly allocated list that no caller
 * has a reference to, so wrapping it is enough to make it unmodifiable and
 * the second array copy buys nothing.
 */
public final class Hand {

    private Hand() {}

    public static <V> java.util.List<String> mapKeys(java.util.Map<String, V> m) {
        java.util.List<String> ks = new java.util.ArrayList<>(m.keySet());
        ks.sort((a, b) -> {
            int i = 0, j = 0;
            while (i < a.length() && j < b.length()) {
                int ca = a.codePointAt(i), cb = b.codePointAt(j);
                if (ca != cb) { return Integer.compare(ca, cb); }
                i += Character.charCount(ca); j += Character.charCount(cb);
            }
            return Boolean.compare(i >= a.length(), j >= b.length());
        });
        return java.util.Collections.unmodifiableList(ks);
    }
}
