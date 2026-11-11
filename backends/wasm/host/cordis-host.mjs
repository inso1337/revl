// A JavaScript host for components compiled by the wasm backend.
//
// The emitted module's import section is its dependency declaration:
// `coeffect:<key>` imports are calls a host (or another component) must bind,
// and `provide:<key>.<op>` exports are its provisions. This host carries that
// seam in the browser and in node: a component whose coeffects are not
// resolvable stays GATED (no instance is created), exactly as cordis-wasm's
// linker refuses to instantiate it, and activates once the host or a loaded
// provider supplies them.
//
// It is deliberately the synchronous base calculus: activation is driven by
// `activate_step() -> i32`, teardown by `deactivate()`, and a component's
// `provide:` exports are published only after activation. The async inertia
// layer (`host.job_run`), spawn/routed realms and the instance accessors are
// not carried here; those need the cordis-wasm runtime's state machine. An
// unsupported import is refused by name, never bound to a wrong function.

const COEFFECT_PREFIX = "coeffect:";
const PROVIDE_PREFIX = "provide:";
const MAX_ACTIVATION_STEPS = 100000;

export class CordisHost {
  // THE INVARIANT. It holds whenever no host operation is running (between
  // any two calls into the public API), and `__checkInvariant` checks it:
  //
  //   1. every entry in #providers belongs to a component whose state is
  //      `active`, and is that component's own export;
  //   2. every `active` component has a live instance and exports, and every
  //      one of its provisions is in #providers under its name;
  //   3. `deactivate` runs at most once per activation, successful or partial;
  //   4. a component that is `failed` or removed holds no instance and no
  //      provision;
  //   5. liveness: a component that was activated (fully or partly), has a
  //      `deactivate` export and is now `failed` or removed ran `deactivate`
  //      exactly once for that activation;
  //   6. no component at rest is `activating` or carries a pending unload.
  //
  // Two rules keep it true. Provisions are published only by #publish, the
  // last step of a successful activation, and withdrawn only by #withdraw,
  // the first step of #fail and of an unload. And no host mutation runs
  // NESTED inside user code: a load, unload or provideHost called while the
  // host is inside activate_step, deactivate, a start function, an export
  // reached through `call`, or the log callback is queued and applied, in
  // the order it was made, when the outer operation finishes.
  // The one exception is unloading the component that is activating: that
  // marks it, and its step loop stops at the next step.

  #hostCoeffects = new Map(); // key -> { op: fn }
  #components = new Map(); // name -> record
  #providers = new Map(); // "key.op" -> { component, exportName, fn }
  #log;
  #depth = 0; // > 0 while user code runs inside a host operation
  #deferred = []; // mutations requested at #depth > 0, applied in order
  #draining = false;
  #queuedLoads = new Map(); // name -> count of deferred loads not yet started
  #violations = []; // invariant 3 and 5 breaches, seen when they happen

  constructor({ log = () => {} } = {}) {
    this.#log = log;
  }

  /**
   * The keys a host provides (a browser resource, a clock, a test stub). The
   * ops are the OWN function properties of `ops`. Deferred when called from
   * inside activation, deactivation or the log callback.
   */
  provideHost(key, ops) {
    if (!ops || typeof ops !== "object") {
      throw new TypeError(`provideHost(${JSON.stringify(key)}) expects an object of ops`);
    }
    if (this.#depth > 0) {
      this.#deferred.push(() => this.provideHost(key, ops));
      return this;
    }
    return this.#outer(() => {
      this.#hostCoeffects.set(key, ops);
      this.#pump();
      return this;
    });
  }

  /**
   * The state of a component: absent | loading (compiling) | gated |
   * activating | active | failed.
   */
  state(name) {
    return this.#components.get(name)?.state ?? "absent";
  }

  /** Why a failed component failed (its error message), or null. */
  error(name) {
    return this.#components.get(name)?.error ?? null;
  }

  /** The coeffect imports still unresolved for a gated component. */
  missing(name) {
    return [...(this.#components.get(name)?.missing ?? [])];
  }

  /**
   * Compile and (if its coeffects resolve) instantiate a component.
   *
   * `source` is a fetchable URL string, a Response, an ArrayBuffer, a typed
   * array or a compiled WebAssembly.Module. A gated component's module is
   * compiled and kept; it instantiates as soon as `provideHost` or another
   * component publishes the missing ops. Called from inside activation,
   * deactivation or the log callback, the load starts when the outer
   * operation finishes; the returned promise settles as usual. It resolves
   * to a frozen snapshot `{ name, state, missing }`, never the host's own
   * record: `call` is the only way into a component.
   */
  load(name, source) {
    if (this.#depth > 0) {
      this.#queuedLoads.set(name, (this.#queuedLoads.get(name) ?? 0) + 1);
      return new Promise((resolve, reject) => {
        this.#deferred.push(() => {
          const left = this.#queuedLoads.get(name) - 1;
          if (left) this.#queuedLoads.set(name, left);
          else this.#queuedLoads.delete(name);
          this.#load(name, source).then(resolve, reject);
        });
      });
    }
    return this.#load(name, source);
  }

  async #load(name, source) {
    if (this.#components.has(name)) {
      throw new Error(`component ${JSON.stringify(name)} is already loaded`);
    }
    // The name is reserved BEFORE the await, so a second concurrent load of
    // the same name is refused instead of replacing this one.
    const record = {
      name,
      module: null,
      imports: [],
      state: "loading",
      missing: [],
      instance: null,
      exports: null,
      error: null,
      unloadRequested: false,
      activations: 0,
      deactivations: 0,
    };
    this.#components.set(name, record);
    try {
      record.module = await compile(source);
      record.imports = checkedImports(name, record.module);
    } catch (error) {
      if (this.#components.get(name) === record) this.#components.delete(name);
      throw error;
    }
    if (this.#components.get(name) !== record) {
      throw new Error(`component ${JSON.stringify(name)} was unloaded while it was loading`);
    }
    return this.#outer(() => {
      record.state = "gated";
      // Only THIS component's failure rejects this load; a failure the pump
      // meets in another component is recorded on that component instead.
      this.#attempt(record);
      this.#pump();
      return snapshot(record);
    });
  }

  /** Call an export on an ACTIVE component, e.g. `provide:motion.step`. */
  call(componentName, exportName, ...args) {
    const record = this.#components.get(componentName);
    if (!record) throw new Error(`component ${JSON.stringify(componentName)} is not loaded`);
    if (record.state !== "active") {
      throw new Error(
        `component ${JSON.stringify(componentName)} is ${record.state}; ` +
        `missing coeffects: ${this.missing(componentName).join(", ") || "none"}`);
    }
    const fn = record.exports[exportName];
    if (typeof fn !== "function") {
      throw new Error(`${componentName} has no exported function ${JSON.stringify(exportName)}`);
    }
    // The component's code is on the stack for the whole call, so a host op
    // it reaches that loads, unloads or provides is applied after it returns.
    if (this.#depth > 0) return this.#user(() => fn(...args));
    return this.#outer(() => this.#user(() => fn(...args)));
  }

  /**
   * Unload a component: its provisions are withdrawn and its compiled
   * inverses run through `deactivate`. Dependents already instantiated are
   * left for the caller to unload first (there is no reactive withdrawal in
   * this host). Returns whether a component of that name was loaded.
   *
   * Unloading the component that is activating stops its activation at the
   * next step. Any other unload called from inside activation, deactivation
   * or the log callback is applied when the outer operation finishes.
   */
  unload(name) {
    const record = this.#components.get(name);
    if (record?.state === "activating") {
      record.unloadRequested = true;
      return true;
    }
    if (this.#depth > 0) {
      // Queued even when the name is not loaded yet, so a deferred load
      // followed by this unload is applied in that order. The result says
      // whether the name will be loaded when the unload runs.
      const willExist = Boolean(record) || this.#queuedLoads.has(name);
      this.#deferred.push(() => this.unload(name));
      return willExist;
    }
    if (!record) return false;
    return this.#outer(() => {
      this.#withdraw(record);
      const wasActive = record.state === "active";
      const exports = record.exports;
      this.#components.delete(name);
      record.state = "absent";
      record.instance = null;
      record.exports = null;
      try {
        if (wasActive) this.#deactivate(record, exports);
      } finally {
        this.#retire(record);
      }
      return true;
    });
  }

  /**
   * TEST ONLY. Throws when the invariant at the top of this class does not
   * hold. Call it between host operations, never from inside one.
   */
  __checkInvariant() {
    const problems = [...this.#violations];
    for (const [opKey, provider] of this.#providers) {
      const record = this.#components.get(provider.component);
      if (!record || record.state !== "active") {
        problems.push(`provider ${opKey} belongs to ${provider.component}, which is ` +
          `${record?.state ?? "absent"}`);
      } else if (record.exports?.[provider.exportName] !== provider.fn) {
        problems.push(`provider ${opKey} is not ${provider.component}'s own export`);
      }
    }
    for (const record of this.#components.values()) {
      if (record.state === "active") {
        if (!record.instance || !record.exports) {
          problems.push(`${record.name} is active with no live instance`);
          continue;
        }
        for (const [opKey] of provisionsOf(record.module)) {
          if (this.#providers.get(opKey)?.component !== record.name) {
            problems.push(`${record.name} is active but ${opKey} is not published for it`);
          }
        }
      } else if (record.instance || record.exports) {
        problems.push(`${record.name} is ${record.state} but holds an instance`);
      }
      if (record.state === "activating") problems.push(`${record.name} is activating at rest`);
      if (record.unloadRequested) {
        problems.push(`${record.name} has a pending unload but is still ${record.state}`);
      }
      if (record.deactivations > record.activations) {
        problems.push(`${record.name}: deactivate ran ${record.deactivations} times ` +
          `for ${record.activations} activation(s)`);
      }
    }
    if (this.#deferred.length) problems.push(`${this.#deferred.length} deferred mutation(s) not applied`);
    if (problems.length) throw new Error(`host invariant broken:\n  ${problems.join("\n  ")}`);
  }

  // ------------------------------------------------------------- internals

  /** Run a top-level operation, then apply what user code deferred during it. */
  #outer(fn) {
    try {
      return fn();
    } finally {
      this.#drain();
    }
  }

  #drain() {
    if (this.#draining || this.#depth > 0) return;
    this.#draining = true;
    try {
      while (this.#deferred.length) {
        const mutation = this.#deferred.shift();
        try {
          mutation();
        } catch (error) {
          this.#note(`a deferred host call threw: ${describe(error)}`);
        }
      }
    } finally {
      this.#draining = false;
    }
  }

  /** Run user code (wasm activation, deactivation, the log callback). */
  #user(fn) {
    this.#depth++;
    try {
      return fn();
    } finally {
      this.#depth--;
    }
  }

  /** The log callback; one that throws is ignored, it never changes state. */
  #note(line) {
    try {
      this.#user(() => this.#log(line));
    } catch {
      // a logger's failure is not a component's failure
    }
  }

  #missingImports(record) {
    const missing = [];
    for (const imp of record.imports) {
      const key = imp.module.slice(COEFFECT_PREFIX.length);
      if (!this.#resolve(key, imp.name)) missing.push(`${key}.${imp.name}`);
    }
    return missing;
  }

  /** Conflicts are decided from the module's export list, so a conflicting
   *  component never runs its activation or its coeffect calls. */
  #checkConflicts(record, { undo = false } = {}) {
    for (const [opKey] of provisionsOf(record.module)) {
      const existing = this.#providers.get(opKey);
      if (existing && existing.component !== record.name) {
        this.#fail(record, new Error(
          `${record.name}: provision conflict on ${opKey}: ` +
          `${existing.component} already provides it`), { undo });
      }
    }
  }

  #tryInstantiate(record) {
    const missing = this.#missingImports(record);
    if (missing.length) {
      record.state = "gated";
      record.missing = missing;
      return false;
    }
    const imports = {};
    for (const imp of record.imports) {
      const key = imp.module.slice(COEFFECT_PREFIX.length);
      const namespace = (imports[imp.module] ??= {});
      namespace[imp.name] = (...args) => {
        const fn = this.#resolve(key, imp.name);
        if (!fn) {
          throw new Error(
            `${record.name}: coeffect ${key}.${imp.name} is not provided at call time`);
        }
        return fn(...args);
      };
    }
    this.#checkConflicts(record);
    record.state = "activating";
    record.missing = [];
    let instance;
    try {
      instance = this.#user(() => new WebAssembly.Instance(record.module, imports));
    } catch (error) {
      this.#fail(record, error);
    }
    record.instance = instance;
    record.exports = instance.exports;
    record.activations++;
    this.#stopIfUnloaded(record);
    if (typeof instance.exports.activate_step === "function") {
      let steps = 0;
      let done = false;
      while (!done) {
        try {
          done = this.#user(() => instance.exports.activate_step()) === 0;
        } catch (error) {
          this.#fail(record, error, { undo: true });
        }
        this.#stopIfUnloaded(record);
        if (!done && ++steps > MAX_ACTIVATION_STEPS) {
          this.#fail(record, new Error(
            `${record.name}: activation did not finish in ${MAX_ACTIVATION_STEPS} steps`),
            { undo: true });
        }
      }
    }
    // Nested mutations are deferred, so nothing can have published one of
    // these ops meanwhile; checked again so a slip cannot double-publish.
    this.#checkConflicts(record, { undo: true });
    // The log callback is the last user code of an activation. If it throws,
    // the component fails (deactivate runs once) and nothing was published.
    this.#user(() => this.#log(`active ${record.name}`));
    this.#stopIfUnloaded(record);
    this.#publish(record);
    return true;
  }

  /** An unload requested while activating: undo once, remove, and throw. */
  #stopIfUnloaded(record) {
    if (!record.unloadRequested) return;
    const exports = record.exports;
    this.#withdraw(record);
    if (this.#components.get(record.name) === record) this.#components.delete(record.name);
    record.state = "absent";
    record.instance = null;
    record.exports = null;
    this.#deactivate(record, exports, { quiet: true });
    this.#retire(record);
    throw new Error(`${record.name}: was unloaded while it was activating`);
  }

  /**
   * A record leaving for good (failed or removed). Checks invariant 5 now,
   * while the record is still in hand, so the check needs no history.
   */
  #retire(record) {
    record.unloadRequested = false;
    // a record unloaded while still loading has no module yet
    const hasDeactivate = record.module !== null && WebAssembly.Module.exports(record.module)
      .some((exp) => exp.kind === "function" && exp.name === "deactivate");
    if (hasDeactivate && record.activations > 0 && record.deactivations !== record.activations) {
      this.#violations.push(`${record.name} left ${record.state} with deactivate run ` +
        `${record.deactivations} time(s) for ${record.activations} activation(s)`);
    }
  }

  /** The last step of a successful activation: the ONLY place that publishes. */
  #publish(record) {
    record.state = "active";
    for (const [opKey, exportName] of provisionsOf(record.module)) {
      this.#providers.set(opKey, {
        component: record.name, exportName, fn: record.exports[exportName] });
    }
  }

  /** The ONLY place that withdraws; by component name, in every state. */
  #withdraw(record) {
    for (const [opKey, provider] of this.#providers) {
      if (provider.component === record.name) this.#providers.delete(opKey);
    }
  }

  /**
   * Run `deactivate` for one activation. A second call for the same
   * activation is refused (and recorded, which the invariant check reports).
   * With `quiet`, whatever it throws (even a non-Error) is logged rather than
   * thrown.
   */
  #deactivate(record, exports, { quiet = false } = {}) {
    if (typeof exports?.deactivate !== "function") return;
    if (record.deactivations >= record.activations) {
      this.#violations.push(`${record.name}: deactivate requested again for one activation`);
      return;
    }
    record.deactivations++;
    try {
      this.#user(() => exports.deactivate());
    } catch (error) {
      if (!quiet) throw error;
      this.#note(`${record.name}: deactivate after a failed activation threw: ${describe(error)}`);
    }
  }

  /** Try to instantiate; whatever throws, the record ends up failed or removed. */
  #attempt(record) {
    try {
      return this.#tryInstantiate(record);
    } catch (error) {
      // #fail throws: the same error, or the unload's when one was pending
      if (record.state !== "failed" && record.state !== "absent") {
        this.#fail(record, error, { undo: true });
      }
      throw error;
    }
  }

  /**
   * Mark `record` failed and throw `error`. Withdrawal and the bookkeeping
   * come first, so nothing after them can leave the record half failed. With
   * `undo`, a partly activated instance then runs `deactivate` once; whatever
   * that throws is logged and does not replace the original error. A failed
   * component holds no live instance and is never retried.
   */
  #fail(record, error, { undo = false } = {}) {
    // Unloaded while activating, then the activation failed anyway: the
    // unload wins, so the name is released and the load says why.
    if (record.unloadRequested) this.#stopIfUnloaded(record);
    this.#withdraw(record);
    const exports = record.exports;
    record.state = "failed";
    record.missing = [];
    record.instance = null;
    record.exports = null;
    record.error = describe(error);
    if (undo) this.#deactivate(record, exports, { quiet: true });
    this.#retire(record);
    this.#note(`failed ${record.name}: ${record.error}`);
    throw error;
  }

  #resolve(key, op) {
    const host = this.#hostCoeffects.get(key);
    // own properties only: an inherited `constructor` or `toString` is a
    // function on every object, not an op the host provided
    if (host && Object.hasOwn(host, op) && typeof host[op] === "function") return host[op];
    const provider = this.#providers.get(`${key}.${op}`);
    return provider ? provider.fn : null;
  }

  #pump() {
    let changed = true;
    while (changed) {
      changed = false;
      for (const record of [...this.#components.values()]) {
        if (record.state !== "gated") continue;
        try {
          if (this.#attempt(record)) changed = true;
        } catch {
          // #attempt has failed the record and recorded why; it is that
          // component's failure, not the caller's, so the pump goes on
        }
      }
    }
  }
}

/** What `load` hands back: a frozen copy, never the host's own record. */
function snapshot(record) {
  return Object.freeze({
    name: record.name, state: record.state, missing: Object.freeze([...record.missing]) });
}

/**
 * A module's provisions as [opKey, exportName]. Only FUNCTION exports named
 * `provide:<key>.<op>` count: a memory or global under that name is neither
 * published nor a conflict.
 */
function provisionsOf(module) {
  const out = [];
  for (const exp of WebAssembly.Module.exports(module)) {
    if (exp.kind !== "function" || !exp.name.startsWith(PROVIDE_PREFIX)) continue;
    out.push([exp.name.slice(PROVIDE_PREFIX.length), exp.name]);
  }
  return out;
}

/** An error's message, for any thrown value (a host op may throw null). */
function describe(error) {
  return error instanceof Error ? error.message : String(error?.message ?? error);
}

/** The module's imports, refused by name unless every one is a coeffect function. */
function checkedImports(name, module) {
  const imports = WebAssembly.Module.imports(module);
  for (const imp of imports) {
    if (imp.kind !== "function") {
      throw new Error(`${name}: unsupported import kind ${imp.kind} from ${imp.module}`);
    }
    if (imp.module.startsWith(COEFFECT_PREFIX)) continue;
    if (imp.module === "host") {
      throw new Error(
        `${name}: host import ${JSON.stringify(imp.name)} is not carried by the ` +
        "browser host (the async inertia layer and the route/spawn host ops need " +
        "the cordis-wasm runtime)");
    }
    throw new Error(
      `${name}: unsupported import namespace ${JSON.stringify(imp.module)}; the ` +
      "browser host carries coeffect imports only");
  }
  return imports;
}

async function compile(source) {
  if (source instanceof WebAssembly.Module) return source;
  if (source instanceof ArrayBuffer || ArrayBuffer.isView(source)) {
    return WebAssembly.compile(source);
  }
  if (typeof Response !== "undefined" && source instanceof Response) {
    if (!source.ok) throw new Error(`fetching the component failed: ${source.status}`);
    return WebAssembly.compile(await source.arrayBuffer());
  }
  if (typeof source === "string" && typeof fetch === "function") {
    const response = await fetch(source);
    if (!response.ok) throw new Error(`fetching ${source} failed: ${response.status}`);
    return WebAssembly.compile(await response.arrayBuffer());
  }
  throw new TypeError("load expects a URL, Response, ArrayBuffer, or typed array");
}
