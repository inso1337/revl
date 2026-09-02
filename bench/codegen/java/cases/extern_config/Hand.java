package bench;

/**
 * What a competent Java developer writes for a config extern.
 *
 * The resolution logic is character-for-character the emitter's: same store,
 * same fail-loud message, same defaults-then-overlay order, same mutable
 * result map handed to the body. The only change is WHERE the two
 * compile-time constants live. The emitter rebuilds `new String[]{..}` and
 * `Map.of(..)` at every call site invocation; here they are `static final`
 * fields, built once when the class initializes.
 *
 * That is a pure hoist: nothing about the observable behaviour of `author`
 * changes, including the case where configuration is installed between two
 * calls, because the store is still read on every call.
 */
public final class Hand {

    static final java.util.Map<String, java.util.Map<String, Object>> CONFIG =
            new java.util.HashMap<>();

    private static final String[] AUTHOR_REQUIRED = {};
    private static final java.util.Map<String, Object> AUTHOR_DEFAULTS =
            java.util.Map.<String, Object>of("model", "opus", "retries", 3L);

    private Hand() {}

    static java.util.Map<String, Object> externConfig(
            String name, String[] required, java.util.Map<String, Object> defaults) {
        java.util.Map<String, Object> out = new java.util.HashMap<>(defaults);
        java.util.Map<String, Object> cfg = CONFIG.get(name);
        if (cfg == null) {
            if (required.length > 0) {
                throw new RuntimeException("config extern `" + name
                        + "` called before plug-time configuration was installed "
                        + "(required config: " + String.join(", ", required)
                        + "); configure it through the run driver's config seam");
            }
            return out;
        }
        java.util.List<String> missing = new java.util.ArrayList<>();
        for (String f : required) {
            if (!cfg.containsKey(f)) {
                missing.add(f);
            }
        }
        if (!missing.isEmpty()) {
            throw new RuntimeException("config extern `" + name
                    + "` called before plug-time configuration was installed "
                    + "(missing required config: " + String.join(", ", missing) + ")");
        }
        out.putAll(cfg);
        return out;
    }

    public static String author(String body) {
        java.util.Map<String, Object> config =
                externConfig("author", AUTHOR_REQUIRED, AUTHOR_DEFAULTS);
        return (String) config.get("model");
    }

    public static String go(String x) {
        return author(x);
    }
}
