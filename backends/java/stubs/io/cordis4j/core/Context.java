package io.cordis4j.core;

public class Context {
    private final java.util.Map<Class<?>, Object> registry;

    public Context() {
        this(new java.util.HashMap<>());
    }

    private Context(java.util.Map<Class<?>, Object> registry) {
        this.registry = registry;
    }

    public <T> T get(Class<T> type) {
        Object value = registry.get(type);
        if (value == null) {
            throw new CordisException("no provider for " + type.getName());
        }
        return type.cast(value);
    }

    public <T> Disposable provide(ServiceKey<T> key, T impl) {
        registry.put(key.type(), impl);
        return () -> registry.remove(key.type(), impl);
    }

    /** Effect scope: tracked disposables run in reverse order (LIFO, G7). */
    public EffectScope effect() {
        return new EffectScope();
    }

    public Context isolate(Class<?> service, String realm) {
        // Stub: realm bookkeeping is a runtime concern; shape only.
        return this;
    }

    public void intercept(ServiceKey<?> key, Object metadata) {
        // Stub: interception metadata is a runtime concern; shape only.
    }

    public static final class EffectScope implements Disposable {
        private final java.util.ArrayDeque<Disposable> tracked = new java.util.ArrayDeque<>();

        public void track(Disposable disposable) {
            tracked.push(disposable);
        }

        @Override
        public void dispose() {
            while (!tracked.isEmpty()) {
                tracked.pop().dispose();
            }
        }
    }
}
