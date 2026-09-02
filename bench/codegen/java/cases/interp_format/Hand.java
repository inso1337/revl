package bench;

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.Disposables;
import io.cordis4j.core.Plugin;
import io.cordis4j.core.ServiceKey;

/**
 * What a competent Java developer writes for `[req] ${msg} #${n} end`.
 *
 * The only difference from the emitted class is the body of `log`: string
 * concatenation, which javac compiles to a single invokedynamic
 * `makeConcatWithConstants` call site, instead of `String.format`, which
 * re-parses the format string, allocates a varargs array, a Formatter, a
 * StringBuilder and one FormatSpecifier per placeholder on every call.
 *
 * Every `%s` the emitter produces is a `String.valueOf` of its argument, so
 * concatenation is output-identical, including for null.
 */
public final class Hand {

    public interface Logger {
        String log(String msg, long n);
    }

    public static final class LLogger implements Logger {
        LLogger() {
        }

        @Override
        public String log(String msg, long n) {
            return "[req] " + msg + " #" + n + " end";
        }
    }

    public static final class LPlugin implements Plugin {
        public LPlugin() {
        }

        @Override
        public Disposable apply(Context ctx) {
            java.util.ArrayList<Disposable> undos = new java.util.ArrayList<>();
            undos.add(ctx.provide(ServiceKey.of(Logger.class), new LLogger()));
            java.util.Collections.reverse(undos);
            return Disposables.composite(undos.toArray(new Disposable[0]));
        }
    }

    private Hand() {}
}
