# Codegen benchmarks: java backend

What the revl java emitter produces, measured against the Java a competent
Java developer writes by hand for the same semantics.

```
python3 bench/codegen/java/run.py --static     # no JDK required
python3 bench/codegen/java/run.py              # every case, needs a JDK
python3 bench/codegen/java/run.py router       # one case
python3 bench/codegen/java/run.py --class-sizes
python3 bench/codegen/java/run.py --json       # machine-readable
```

Exit code 77 from the default mode means no working JDK was found and nothing
was measured. That is deliberate: `/usr/bin/java` and `/usr/bin/javac` exist
on macOS even with no JDK installed and fail with "Unable to locate a Java
Runtime", so being on PATH proves nothing and the driver probes `-version`
instead. A JDK 21 or newer is required (the emitted sources are compiled with
`--release 21`). `--static` needs no JDK at all.

**Before you believe a 77, look where the package manager put the JDK.** Item
433's whole perf audit was written as static because that stub answered for
`java`, while a working openjdk sat keg-only at `/opt/homebrew/opt/openjdk` and
so was absent from PATH by design. Homebrew, `jenv`, SDKMAN and a bundled IDE
runtime all install a JDK that `shutil.which("java")` will not find:

```
export JAVA_HOME=/opt/homebrew/opt/openjdk   # or: /usr/libexec/java_home -V
export PATH="$JAVA_HOME/bin:$PATH"
java -version
```

A 77 means "not on PATH", never "not installed".

## What a case is

Each directory under `cases/` holds three files.

| file | role |
|---|---|
| `case.rvl` | the revl program, emitted exactly as `revl emit --backend java` emits it |
| `Hand.java` | the yardstick: same semantics, same class shape, written the way a Java developer would write it |
| `Drive.java` | a `bench.Drive` exposing `NAME`, `N`, `WARMUP`, `setup()`, `emitted(int)`, `hand(int)` |

`Hand.java` is a yardstick, not a rewrite. It keeps the emitted structure
(same interfaces, same provider indirection, same persistent-collection
semantics) and changes only the one emitter choice the case is about, so the
measurement attributes the difference to that choice and not to a different
program.

`setup()` asserts that the two sides produce equal output before anything is
measured. A case cannot win by computing less.

## What is measured

**Allocated bytes per op**, from
`com.sun.management.ThreadMXBean.getCurrentThreadAllocatedBytes()`, reported
as the minimum over several samples. This is a count, not a duration: it does
not move when another process takes the CPU, so it stays meaningful on a
machine running other work. It is also the measure that maps onto the emitter
choices under audit, since a hoisted constant, a dropped defensive copy and an
unboxed comparison each change it by a fixed amount per call.

**Compiled class-file bytes** per side, under `--class-sizes`.

**Emitted source bytes and static allocation-site counts**, under `--static`,
with no JDK involved. These are a shape argument, not a measurement, and the
output says so: the counts cover the whole emitted unit, including runtime
helpers the program may never call.

## What is not measured, and why

**No timing.** This harness reports no duration. A wall-clock number taken on
a loaded machine measures the load, and interleaving the two arms does not
rescue it, because the arms sample different moments and therefore different
contention. On the JVM it is worse still, since the same contention distorts
JIT compilation. A number that looks measured but is not is worse than no
number.

Timing a candidate emitter change is a separate, serialized exercise on a
quiet machine, and it belongs in whatever runs that exercise, not here.

## Adding a case

Create `cases/<name>/` with the three files. `run.py` picks it up with no
registration. Push integer workloads past 127 so that boxing shows up as real
allocation instead of a `Long.valueOf` cache hit, and size `N` so one op does
enough work that per-call harness overhead is not a meaningful share of the
allocation sample.
