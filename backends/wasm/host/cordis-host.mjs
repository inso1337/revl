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
  #hostCoeffects = new Map(); // key -> { op: fn }
  #components = new Map(); // name -> record
  #providers = new Map(); // "key.op" -> { component, fn }
  #log;

  constructor({ log = () => {} } = {}) {
    this.#log = log;
  }

  /** The keys a host provides (a browser resource, a clock, a test stub). */
  provideHost(key, ops) {
    if (!ops || typeof ops !== "object") {
      throw new TypeError(`provideHost(${JSON.stringify(key)}) expects an object of ops`);
    }
    this.#hostCoeffects.set(key, ops);
    this.#pump();
    return this;
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
   * `source` is a fetchable URL string, a Response, an ArrayBuffer or a typed
   * array. A gated component's module is compiled and kept; it instantiates as
   * soon as `provideHost` or another component publishes the missing ops.
   */
  async load(name, source) {
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
    record.state = "gated";
    // Only THIS component's failure rejects this load; a failure the pump
    // meets in another component is recorded on that component instead.
    this.#attempt(record);
    this.#pump();
    return record;
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
    return fn(...args);
  }

  /**
   * Unload a component: its compiled inverses run through `deactivate`, and its
   * provisions are withdrawn. Dependents already instantiated are left for the
   * caller to unload first (there is no reactive withdrawal in this host).
   */
  unload(name) {
    const record = this.#components.get(name);
    if (!record) return false;
    if (record.state === "active" && typeof record.exports.deactivate === "function") {
      record.exports.deactivate();
    }
    // withdrawn in every state, so no provision can outlive its component
    for (const [opKey, provider] of this.#providers) {
      if (provider.component === name) this.#providers.delete(opKey);
    }
    this.#components.delete(name);
    return true;
  }

  #missingImports(record) {
    const missing = [];
    for (const imp of record.imports) {
      const key = imp.module.slice(COEFFECT_PREFIX.length);
      if (!this.#resolve(key, imp.name)) missing.push(`${key}.${imp.name}`);
    }
    return missing;
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
    // A provision conflict is decided from the module's export list, before
    // anything is instantiated, so a conflicting component never runs its
    // activation (or its coeffect calls) and has nothing to undo.
    const provisions = this.#provisions(record);
    // `activating` is skipped by #pump, so a host op that re-enters the host
    // (provideHost, load) while this component activates cannot retry it.
    record.state = "activating";
    let instance;
    try {
      instance = new WebAssembly.Instance(record.module, imports);
    } catch (error) {
      this.#fail(record, error);
    }
    record.instance = instance;
    record.exports = instance.exports;
    if (typeof instance.exports.activate_step === "function") {
      let steps = 0;
      let done = false;
      while (!done) {
        try {
          done = instance.exports.activate_step() === 0;
        } catch (error) {
          this.#fail(record, error, { undo: true });
        }
        if (!done && ++steps > MAX_ACTIVATION_STEPS) {
          this.#fail(record, new Error(
            `${record.name}: activation did not finish in ${MAX_ACTIVATION_STEPS} steps`),
            { undo: true });
        }
      }
    }
    // a host op may have unloaded this component while it activated
    if (record.state !== "activating" || this.#components.get(record.name) !== record) {
      return false;
    }
    // Checked again: a nested pump during activation may have published one
    // of these ops for another component.
    this.#provisions(record, { undo: true });
    record.state = "active";
    record.missing = [];
    for (const [opKey, exportName] of provisions) {
      this.#providers.set(opKey, { component: record.name, fn: instance.exports[exportName] });
    }
    this.#log(`active ${record.name}`);
    return true;
  }

  /**
   * The component's provisions as [opKey, exportName], read from the module's
   * export list, failing the component on a conflict. Only FUNCTION exports
   * named `provide:<key>.<op>` count: a memory or global under that name is
   * neither published nor a conflict. Checked before instantiation, so a
   * conflicting component never runs its activation or its coeffect calls.
   */
  #provisions(record, { undo = false } = {}) {
    const provisions = [];
    for (const exp of WebAssembly.Module.exports(record.module)) {
      if (exp.kind !== "function" || !exp.name.startsWith(PROVIDE_PREFIX)) continue;
      const opKey = exp.name.slice(PROVIDE_PREFIX.length);
      const existing = this.#providers.get(opKey);
      if (existing && existing.component !== record.name) {
        this.#fail(record, new Error(
          `${record.name}: provision conflict on ${opKey}: ` +
          `${existing.component} already provides it`), { undo });
      }
      provisions.push([opKey, exp.name]);
    }
    return provisions;
  }

  /** Try to instantiate; whatever throws, the record ends up failed. */
  #attempt(record) {
    try {
      return this.#tryInstantiate(record);
    } catch (error) {
      if (record.state !== "failed") {
        try {
          this.#fail(record, error, { undo: true });
        } catch {
          // #fail rethrows the same error, thrown below
        }
      }
      throw error;
    }
  }

  /**
   * Mark `record` failed and throw `error`. The bookkeeping comes first, so
   * nothing after it can leave the record half failed. With `undo`, a partly
   * activated instance then runs `deactivate`; whatever that throws (even a
   * non-Error) is logged and does not replace the original error. A failed
   * component holds no live instance and is never retried.
   */
  #fail(record, error, { undo = false } = {}) {
    const exports = record.exports;
    record.state = "failed";
    record.missing = [];
    record.instance = null;
    record.exports = null;
    record.error = describe(error);
    this.#log(`failed ${record.name}: ${record.error}`);
    if (undo && typeof exports?.deactivate === "function") {
      try {
        exports.deactivate();
      } catch (undoError) {
        this.#log(`${record.name}: deactivate after a failed activation threw: ${describe(undoError)}`);
      }
    }
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
      for (const record of this.#components.values()) {
        if (record.state !== "gated") continue;
        try {
          if (this.#attempt(record)) changed = true;
        } catch {
          // #attempt has failed the record and recorded why; it is that
          // component's failure, not the caller's, so the pump goes on
          changed = true;
        }
      }
    }
  }
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
