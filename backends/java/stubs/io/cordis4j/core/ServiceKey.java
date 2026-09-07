package io.cordis4j.core;

public final class ServiceKey<T> {
    private final Class<T> type;
    private final String name;

    private ServiceKey(Class<T> type, String name) {
        this.type = type;
        this.name = name;
    }

    /** The default (unnamed) provision of `type`. Equivalent to `of(type, "")`. */
    public static <T> ServiceKey<T> of(Class<T> type) {
        return new ServiceKey<>(type, "");
    }

    /** A provision of `type` under provision key `name`. Two providers of the
     *  same service type are distinct keys, so a consumer that requires `name`
     *  binds THAT provider — not merely the last provider of the class. */
    public static <T> ServiceKey<T> of(Class<T> type, String name) {
        return new ServiceKey<>(type, name == null ? "" : name);
    }

    public Class<T> type() {
        return type;
    }

    public String name() {
        return name;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof ServiceKey<?> other)) {
            return false;
        }
        return type.equals(other.type) && name.equals(other.name);
    }

    @Override
    public int hashCode() {
        return type.hashCode() * 31 + name.hashCode();
    }

    @Override
    public String toString() {
        return name.isEmpty() ? type.getName() : type.getName() + "#" + name;
    }
}
