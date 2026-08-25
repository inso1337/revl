// playground.js — the revl playground v2.
// Same architecture as playground/app.js (the pure-Python compiler installed
// into Pyodide, called in-process), plus: a syntax-highlighted editor,
// ?example= deep links, and the site design system.
import { EXAMPLES } from "./examples.js";
import { highlightRevl } from "./revl-lang.js";

const WHEEL = "vendor/revl-2.0.0-py3-none-any.whl";
const CORDIS_WHEEL = "vendor/cordis-4.0.0-py3-none-any.whl";

// The in-process driver — the same functions the CLI calls.
const DRIVER = String.raw`
import json
from revl.compiler import compile_source
from revl.diagnostics import report
from revl.audit_diff import audit_report
from revl.plan import plan as build_plan, render as render_plan
from revl.errors import RevlError

def run_playground(source):
    out = {"ok": True}

    # plan runs the admission gate read-only: it works for BOTH an admissible
    # program and a refused one, so it always returns something to show.
    try:
        p = build_plan(source=source)
        out["plan"] = {
            "admissible": p.get("admissible"),
            "text": render_plan(p),
        }
    except Exception as exc:  # planner is best-effort; never blocks the rest
        out["plan"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        ir = compile_source(source, "playground.rvl")
    except RevlError as err:
        out["ok"] = False
        out["diagnostics"] = report(err)["diagnostics"]
        return json.dumps(out)
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        return json.dumps(out)

    out["ir"] = ir
    out["holes"] = ir.get("holes") or []
    try:
        out["audit"] = audit_report(ir)
    except Exception as exc:
        out["audit_error"] = f"{type(exc).__name__}: {exc}"
    return json.dumps(out)
`;

// The LIVE driver — the real cordis-py runtime under Pyodide, driven through
// revl.mcp.session.Session (the same machinery `revl mcp serve` uses).
// Session's sync verbs block on a private loop via run_until_complete, which
// Pyodide's WebLoop cannot block on — but with JSPI available, run_sync gives
// exactly that blocking, so one patch makes the whole session API work.
const LIVE_DRIVER = String.raw`
import json, sys, types

# cordis.hmr imports watchdog at module-import time; a browser has no
# filesystem watcher and live mode never uses HMR — give it an inert stand-in.
if "watchdog" not in sys.modules:
    _wd = types.ModuleType("watchdog")
    _ev = types.ModuleType("watchdog.events")
    _ev.FileSystemEventHandler = object
    _ob = types.ModuleType("watchdog.observers")
    class _NoopObserver:
        def schedule(self, *a, **k): pass
        def start(self): pass
        def stop(self): pass
        def join(self, *a, **k): pass
    _ob.Observer = _NoopObserver
    _wd.events = _ev
    _wd.observers = _ob
    sys.modules["watchdog"] = _wd
    sys.modules["watchdog.events"] = _ev
    sys.modules["watchdog.observers"] = _ob

# playground_host — the ONE module the meta composition's externs may import.
# It forwards to window.__metaHost, where the JS chrome exposes exactly three
# DOM operations; everything the revl-owned UI does to the page goes through
# here, which is why its G8 audit is short and true.
_ph = types.ModuleType("playground_host")

def _ph_mount(tab_id, label):
    import js
    js.window.__metaHost.mount(tab_id, label)
    return 1

def _ph_unmount(tab_id):
    import js
    js.window.__metaHost.unmount(tab_id)
    return 0

def _ph_select(tab_id):
    import js
    js.window.__metaHost.select(tab_id)

def _ph_mount_region(region_id):
    import js
    js.window.__metaHost.mountRegion(region_id)
    return 1

def _ph_unmount_region(region_id):
    import js
    js.window.__metaHost.unmountRegion(region_id)
    return 0

def _ph_editor_source():
    import js
    return js.window.__metaHost.editorSource()

_ph.mount = _ph_mount
_ph.unmount = _ph_unmount
_ph.select = _ph_select
_ph.mount_region = _ph_mount_region
_ph.unmount_region = _ph_unmount_region
def _ph_theme(name):
    import js
    js.window.__metaHost.theme(name)
    return 1

def _ph_theme_reset():
    import js
    js.window.__metaHost.themeReset()
    return 0

_ph.editor_source = _ph_editor_source
_ph.theme = _ph_theme
_ph.theme_reset = _ph_theme_reset
sys.modules["playground_host"] = _ph

from pyodide.ffi import can_run_sync, run_sync
from revl.compiler import compile_source
from revl.diagnostics import report
from revl.errors import RevlError
from revl.mcp import session as _sess

LIVE_OK = can_run_sync()
if LIVE_OK:
    _sess.Session._run = lambda self, coro: run_sync(coro)

_LIVE = {"s": None}


def _graph(ir):
    return [
        {"name": c["name"],
         "requires": sorted((c.get("requires") or {}).keys()),
         "provides": sorted((c.get("provides") or {}).keys())}
        for c in (ir.get("components") or [])
    ]


def _ops(ir, provided):
    services = ir.get("services") or {}
    out, seen = [], set()
    for c in ir.get("components") or []:
        for key, svc in sorted((c.get("provides") or {}).items()):
            if key not in provided or key in seen:
                continue
            seen.add(key)
            methods = services.get(svc, {}).get("methods", {})
            out.append({"key": key, "service": svc, "methods": [
                {"name": name,
                 "params": [p.get("name") for p in (spec.get("params") or [])],
                 "emission": bool(spec.get("emission"))}
                for name, spec in methods.items()]})
    return out


def _snapshot(trace=None, extra=None):
    s = _LIVE["s"]
    st = s.state()
    ir = s.ir or {}
    out = {"ok": True, "live": True, "state": st, "graph": _graph(ir),
           "ops": _ops(ir, set(st.get("providedKeys") or [])),
           "trace": trace or []}
    if extra:
        out.update(extra)
    return json.dumps(out, default=str)


def _fail(exc):
    if isinstance(exc, RevlError):
        return json.dumps({"ok": False, "refused": True,
                           "diagnostics": report(exc)["diagnostics"]})
    return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


_PLACEHOLDER = {"Str": "demo", "Int": 1, "Float": 1.0, "Bool": False}


def _demo_config(ir):
    """Fill config fields that have no default with typed placeholders, so any
    example boots; each fill is reported in the trace rather than hidden."""
    cfg, notes = {}, []
    for c in ir.get("components") or []:
        for field in c.get("config") or []:
            if field.get("default") is None and field["type"] in _PLACEHOLDER:
                value = _PLACEHOLDER[field["type"]]
                cfg.setdefault(c["name"], {})[field["name"]] = value
                notes.append({"channel": "config", "subject": f"{c['name']}.{field['name']}",
                              "detail": f"no default — filled with placeholder {value!r} for the live demo"})
    return cfg, notes


def live_boot(source):
    try:
        ir = compile_source(source, "live.rvl")
    except Exception as exc:
        return _fail(exc)
    old = _LIVE["s"]
    if old is not None and old.loaded:
        try:
            old.unload()
        except Exception:
            pass
    s = _sess.Session()
    _LIVE["s"] = s
    cfg, notes = _demo_config(ir)
    try:
        loaded = s.load(ir, config=cfg or None)
    except Exception as exc:
        _LIVE["s"] = None
        return _fail(exc)
    return _snapshot(trace=notes + (loaded.get("trace") or []))


def live_call(key, method, args_json):
    s = _LIVE["s"]
    if s is None or not s.loaded:
        return json.dumps({"ok": False, "error": "nothing is live — press Boot first"})
    try:
        args = json.loads(args_json) if args_json.strip() else []
        if not isinstance(args, list):
            args = [args]
        res = s.call(key, method, args)
    except Exception as exc:
        return _fail(exc)
    return _snapshot(trace=res.get("trace") or [],
                     extra={"result": res.get("result"),
                            "called": f"{key}.{method}"})


def live_swap(source):
    s = _LIVE["s"]
    if s is None or not s.loaded:
        return json.dumps({"ok": False, "error": "nothing is live — press Boot first"})
    replacing = tuple(c["name"] for c in (s.ir or {}).get("components") or [])
    try:
        # admission against the RUNNING manifest — the same gate revl_swap runs;
        # a refused candidate changes nothing
        ir = compile_source(source, "candidate.rvl", manifest=s.ir,
                            replacing=replacing)
        swapped = s.swap(ir)
    except Exception as exc:
        return _fail(exc)
    return _snapshot(trace=swapped.get("trace") or [],
                     extra={"swapped": True,
                            "handoff": swapped.get("handoff")})


def live_withdraw(name):
    """Withdraw ONE live component — dispose its fiber and let the reactive
    graph settle (R2/R3): dependents come down with it, each replaying its own
    accumulator, and the trace records the cascade the runtime actually
    produced."""
    s = _LIVE["s"]
    if s is None or not s.loaded:
        return json.dumps({"ok": False, "error": "nothing is live"})
    driver = s._require()
    fiber = driver.fibers.pop(name, None)
    if fiber is None:
        return json.dumps({"ok": False, "error": f"no live component named {name!r}"})
    try:
        s._run(fiber.dispose())
        s._run(driver._flush())
    except Exception as exc:
        return _fail(exc)
    return _snapshot(trace=driver.drain_events(), extra={"withdrew": name})


def live_plug(name):
    """Load ONE withdrawn component back from the composition's own emitted
    module; a dependent left PENDING by a withdrawal reactivates when its
    provision reappears."""
    s = _LIVE["s"]
    if s is None or not s.loaded:
        return json.dumps({"ok": False, "error": "nothing is live"})
    driver = s._require()
    if name in driver.fibers:
        return json.dumps({"ok": False, "error": f"{name!r} is already live"})
    if not any(c["name"] == name for c in (s.ir or {}).get("components") or []):
        return json.dumps({"ok": False, "error": f"the composition has no component {name!r}"})
    try:
        module = s._prepare_module(s.ir)
        s._run(s._plug(name, module))
    except Exception as exc:
        return _fail(exc)
    return _snapshot(trace=driver.drain_events(), extra={"plugged": name})


def live_unload():
    s = _LIVE["s"]
    if s is None or not s.loaded:
        return json.dumps({"ok": False, "error": "nothing is live"})
    try:
        un = s.unload()
    except Exception as exc:
        return _fail(exc)
    _LIVE["s"] = None
    return json.dumps({"ok": True, "live": False, "unloaded": un}, default=str)

json.dumps({"live_ok": LIVE_OK})
`;

let runFn = null;
let pyodideRef = null;
let liveOk = false;

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- editor: highlighted overlay ---------------------------------- */

const editor = $("editor");
const hlCode = $("hl-code");
const gutter = $("gutter");

function refreshEditor() {
  const src = editor.value;
  // trailing newline so the last (possibly empty) line keeps its height
  hlCode.innerHTML = highlightRevl(src) + "\n";
  const lines = src.split("\n").length;
  let g = "";
  for (let i = 1; i <= lines; i++) g += `<div>${i}</div>`;
  gutter.innerHTML = g;
  syncScroll();
}

function syncScroll() {
  const pre = hlCode.parentElement;
  pre.scrollTop = editor.scrollTop;
  pre.scrollLeft = editor.scrollLeft;
  gutter.scrollTop = editor.scrollTop;
}

editor.addEventListener("input", refreshEditor);
editor.addEventListener("scroll", syncScroll);
editor.addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    const { selectionStart: s, selectionEnd: t } = editor;
    editor.setRangeText("  ", s, t, "end");
    refreshEditor();
  }
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    runCurrent();
  }
});

function setSource(src) {
  editor.value = src;
  refreshEditor();
}

/* ---------- boot ---------------------------------------------------------- */

async function boot() {
  const setMsg = (m) => ($("overlay-msg").textContent = m);
  populateExamples();
  try {
    setMsg("Loading the Pyodide runtime…");
    const pyodide = await loadPyodide();
    window.__pyodide = pyodide; // reachable for the live-mode driver + debugging
    setMsg("Installing the revl compiler…");
    await pyodide.loadPackage("micropip");
    const micropip = pyodide.pyimport("micropip");
    await micropip.install(new URL(WHEEL, window.location.href).href);
    await micropip.install(new URL(CORDIS_WHEEL, window.location.href).href);
    setMsg("Wiring up…");
    pyodide.runPython(DRIVER);
    runFn = pyodide.globals.get("run_playground");
    pyodideRef = pyodide;
    setMsg("Waking the runtime (live mode)…");
    try {
      await pyodide.loadPackage("pyyaml"); // cordis imports yaml unconditionally
      const liveInfo = JSON.parse(await pyodide.runPythonAsync(LIVE_DRIVER));
      liveOk = !!liveInfo.live_ok;
    } catch (err) {
      console.warn("live mode unavailable:", err);
      liveOk = false;
    }
    $("overlay").classList.add("hidden");
    $("run").disabled = false;
    if (liveOk) {
      $("boot").disabled = false;
    } else {
      $("boot").title = "live mode needs a JSPI-capable browser (recent Chrome or Edge)";
    }
  } catch (err) {
    setMsg("Failed to load: " + err);
    console.error(err);
    return;
  }
  runCurrent();
  // ◐ the page boots itself: when live mode is available and the loaded
  // example is the playground's own composition, bring it up unprompted
  if (liveOk && isMetaSource(editor.value)) $("boot").click();
}

/* ---------- examples ------------------------------------------------------ */

function populateExamples() {
  const sel = $("examples");
  const groups = { accept: "compiles", reject: "refused by the gate" };
  const optgroups = {};
  for (const kind of Object.keys(groups)) {
    const og = document.createElement("optgroup");
    og.label = groups[kind];
    optgroups[kind] = og;
    sel.appendChild(og);
  }
  EXAMPLES.forEach((ex, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = ex.label;
    (optgroups[ex.kind] || sel).appendChild(opt);
  });
  sel.addEventListener("change", () => {
    setSource(EXAMPLES[Number(sel.value)].source);
    history.replaceState(null, "", "?example=" + EXAMPLES[Number(sel.value)].id);
    runCurrent();
  });

  // deep link: ?example=<id>
  const wanted = new URLSearchParams(location.search).get("example");
  let idx = EXAMPLES.findIndex((ex) => ex.id === wanted);
  if (idx < 0) idx = 0;
  sel.value = String(idx);
  setSource(EXAMPLES[idx].source);
}

/* ---------- run ----------------------------------------------------------- */

function runCurrent() {
  if (!runFn) return;
  let data;
  try {
    data = JSON.parse(runFn(editor.value));
  } catch (err) {
    data = { ok: false, error: String(err) };
  }
  render(data);
}

/* ---------- rendering ----------------------------------------------------- */

function render(data) {
  renderStatus(data);
  renderDiagnostics(data);
  renderAudit(data);
  renderPlan(data);
  renderIR(data);
  selectTab(data.ok ? currentTab() : "diagnostics");
}

function renderStatus(data) {
  const el = $("status");
  const holes = (data.holes || []).length;
  if (!data.ok) {
    const n = (data.diagnostics || []).length || (data.error ? 1 : 0);
    el.innerHTML =
      `<span class="verdict bad">REFUSED</span>` +
      `<span>the gate rejected this program (${n} diagnostic${n === 1 ? "" : "s"}) — see the why-trace</span>`;
    setDot("diagnostics", "bad");
    return;
  }
  const comps = ((data.ir || {}).components || []).length;
  if (holes) {
    el.innerHTML =
      `<span class="verdict warn">DRAFT</span>` +
      `<span>compiles, but ${holes} open hole${holes === 1 ? "" : "s"} — admission would still refuse it</span>`;
  } else {
    el.innerHTML =
      `<span class="verdict good">COMPILED</span>` +
      `<span>${comps} component${comps === 1 ? "" : "s"}, every guarantee satisfied</span>`;
  }
  setDot("diagnostics", holes ? "warn" : "good");
}

function renderDiagnostics(data) {
  const el = $("panel-diagnostics");
  if (data.ok) {
    const holes = data.holes || [];
    let html = `<div class="diag diag-ok">
      <div class="head"><span class="code-badge">OK</span>
      <span>No guarantee violations.</span></div>
      <p class="msg">The composition checked, lowered and linked. Open the
      <b>G8 audit</b> for its boundary surface and <b>Plan</b> for what
      admitting it would do.</p></div>`;
    if (holes.length) {
      html += `<div class="section-title">Open obligations (typed holes)</div>`;
      for (const h of holes) {
        html += `<div class="diag diag-warn">
          <div class="head"><span class="code-badge">T3</span>
          <span class="where">${esc(h.file || "")}:${esc(h.line || "")}</span></div>
          <p class="msg">${esc(h.message || "")}</p></div>`;
      }
    }
    el.innerHTML = html;
    return;
  }
  if (data.error) {
    el.innerHTML = `<div class="diag"><p class="msg">${esc(data.error)}</p></div>`;
    return;
  }
  el.innerHTML = (data.diagnostics || []).map(renderDiag).join("");
}

function renderDiag(d) {
  let html = `<div class="diag"><div class="head">
    <span class="code-badge">${esc(d.code || "REVL")}</span>
    <span class="where">${esc(d.file || "")}:${esc(d.line || "")}</span>
    <span class="pill-cat">${esc(d.category || "check")}</span></div>
    <p class="msg">${esc(d.message || "")}</p>`;
  if (d.guarantee) {
    html += `<div class="guarantee"><b>${esc(d.code)}</b> — ${esc(d.guarantee)}</div>`;
  }
  if (d.expected !== undefined || d.actual !== undefined) {
    html += `<div class="kv"><span>expected</span> <code>${esc(fmt(d.expected))}</code>
      &nbsp;·&nbsp; <span>actual</span> <code>${esc(fmt(d.actual))}</code></div>`;
  }
  if (d.hint) html += `<div class="hint"><b>hint:</b> ${esc(d.hint)}</div>`;
  if (d.why) html += renderWhy(d.why);
  html += `</div>`;
  return html;
}

function fmt(v) {
  if (v === null || v === undefined) return "—";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function renderWhy(why) {
  let html = `<div class="why"><h4>why — the derivation (${esc(why.kind || "trace")})</h4>`;
  if (Array.isArray(why.steps) && why.steps.length) {
    html += `<ul class="trace">`;
    for (const s of why.steps) {
      const loc = s.file ? `${esc(s.file)}:${esc(s.line)}` : "";
      html += `<li><span class="name">${esc(s.name || "")}</span>` +
        (s.kind ? ` <span class="detail">(${esc(s.kind)})</span>` : "") +
        (s.detail ? ` — <span class="detail">${esc(s.detail)}</span>` : "") +
        (loc ? `<br><span class="loc">${loc}</span>` : "") +
        `</li>`;
    }
    html += `</ul>`;
  }
  if (Array.isArray(why.path) && why.path.length) {
    html += `<div class="path-chain">${why.path.map(esc).join(" &rarr; ")}</div>`;
  }
  html += `</div>`;
  return html;
}

function renderAudit(data) {
  const el = $("panel-audit");
  if (!data.ok || !data.audit) {
    setDot("audit", data.ok ? "warn" : "bad");
    el.innerHTML = `<p class="empty">No audit — the program was ${
      data.ok ? "compiled but produced no audit" : "refused before it could be linked"
    }. The G8 boundary surface only exists for a composition that passed the gate.</p>`;
    return;
  }
  setDot("audit", "good");
  const audit = data.audit;
  const manifest = audit.manifest || {};
  const boundary = audit.boundary || {};
  const order = manifest.loadOrder || [];
  const byName = {};
  for (const c of manifest.components || []) byName[c.name] = c;

  let html = "";
  if (order.length) {
    html += `<div class="loadOrder"><b>load order</b> (providers first): ` +
      order.map(esc).join(' <span class="arrow">&rarr;</span> ') + `</div>`;
  }

  html += `<table class="audit"><caption>G8: the enumerable boundary surface, per component</caption>
    <thead><tr><th>component</th><th>requires</th><th>provides</th>
    <th>boundary (emissions / host code)</th></tr></thead><tbody>`;

  const names = order.length ? order : Object.keys(boundary);
  for (const name of names) {
    const c = byName[name] || {};
    const b = boundary[name] || {};
    const req = (c.inject || []).map((k) => `<span class="pill">${esc(k)}</span>`).join("") || "—";
    const prov = (c.provides || []).map((k) => `<span class="pill">${esc(k)}</span>`).join("") || "—";
    let bnd = "";
    const emis = b.emissions || [];
    const externs = b.externs || [];
    if (emis.length) {
      bnd += emis.map((e) => `<span class="pill emit">emit ${esc(e)}</span>`).join(" ");
      bnd += ` <span style="color:var(--faint);font-size:12px">(${b.compensated || 0} compensated)</span>`;
    }
    if (externs.length) {
      bnd += externs.map((e) => `<span class="pill emit">host ${esc(e.name)}</span>`).join(" ");
    }
    if (b.awaits) bnd += ` <span class="pill">iteration boundaries: ${esc(b.awaits)}</span>`;
    if (!bnd) bnd = `<span class="pill none">none — fully revertible</span>`;
    html += `<tr><td class="comp">${esc(name)}</td><td>${req}</td><td>${prov}</td><td>${bnd}</td></tr>`;
  }
  html += `</tbody></table>`;

  const dist = audit.distributability || {};
  const distNames = Object.keys(dist);
  if (distNames.length) {
    html += `<div class="section-title">distributability — which services may cross a process seam</div>`;
    html += `<table class="audit"><thead><tr><th>service</th><th>verdict</th><th>reasons</th></tr></thead><tbody>`;
    for (const s of distNames.sort()) {
      const v = dist[s];
      html += `<tr><td class="comp">${esc(s)}</td><td><span class="pill">${esc(v.verdict)}</span></td>
        <td>${esc((v.reasons || []).join("; "))}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  const externs = audit.externs || [];
  if (externs.length) {
    html += `<div class="section-title">externs — verbatim host code (unchecked inside, typed at the boundary)</div>`;
    html += externs.map((e) =>
      `<span class="pill">${esc(e.name)} [${esc(e.class)}] ${esc((e.backends || []).join("+") || "no bodies")}</span>`
    ).join(" ");
  }

  el.innerHTML = html;
}

function renderPlan(data) {
  const el = $("panel-plan");
  const plan = data.plan || {};
  if (plan.error) {
    setDot("plan", "warn");
    el.innerHTML = `<p class="empty">plan unavailable: ${esc(plan.error)}</p>`;
    return;
  }
  const admissible = plan.admissible;
  setDot("plan", admissible ? "good" : "bad");
  const badge = admissible
    ? `<span class="verdict good">ADMISSIBLE</span>`
    : `<span class="verdict bad">REJECTED</span>`;
  el.innerHTML = `<p style="margin-top:0">${badge} <span style="color:var(--faint);font-size:13px">
    dry run — what admitting this into a cold-start composition would do</span></p>
    <pre class="plan">${esc(plan.text || "")}</pre>`;
}

function renderIR(data) {
  const el = $("panel-ir");
  if (!data.ir) {
    setDot("ir", data.ok ? "warn" : "bad");
    el.innerHTML = `<p class="empty">No IR — a refused program never reaches lowering.
      The rejection is the output.</p>`;
    return;
  }
  setDot("ir", "good");
  el.innerHTML =
    `<p style="margin-top:0;color:var(--faint);font-size:13px">The lowered IR document —
     the same JSON <code>revl compile</code> emits, that every backend consumes.</p>` +
    `<pre class="json">${esc(JSON.stringify(data.ir, null, 2))}</pre>`;
}

/* ---------- tabs ----------------------------------------------------------- */

function setDot(name, cls) {
  const dot = $("dot-" + name);
  if (dot) dot.className = "dot " + (cls || "");
}
function currentTab() {
  const t = document.querySelector('.pg-tab[aria-selected="true"]');
  return t ? t.dataset.panel : "diagnostics";
}
function selectTab(name) {
  document.querySelectorAll(".pg-tab").forEach((t) =>
    t.setAttribute("aria-selected", String(t.dataset.panel === name)));
  document.querySelectorAll(".pg-panel").forEach((p) =>
    p.classList.toggle("active", p.id === "panel-" + name));
}
document.querySelectorAll(".pg-tab").forEach((t) =>
  t.addEventListener("click", async () => {
    const id = t.dataset.panel;
    if (metaActive) {
      // in meta mode the click is a service call on the running composition —
      // the DOM only changes when its `ui_select` emission crosses the boundary
      try {
        const r = await runLive("live_call(LIVE_KEY, LIVE_METHOD, LIVE_ARGS)",
          { LIVE_KEY: "tabs", LIVE_METHOD: "select", LIVE_ARGS: JSON.stringify([id]) });
        if (r.ok) { traceEvents(r.trace); return; }
      } catch { /* fall through to plain select */ }
    }
    selectTab(id);
  }));

$("run").addEventListener("click", runCurrent);

/* ---------- live mode ------------------------------------------------------ */

const sysPanel = $("sys-panel");
const sysGraph = $("sys-graph");
const sysOps = $("sys-ops");
const sysTrace = $("sys-trace");
const sysLabel = $("sys-label");

async function runLive(expr, args) {
  for (const [k, v] of Object.entries(args || {})) pyodideRef.globals.set(k, v);
  const json = await pyodideRef.runPythonAsync(expr);
  return JSON.parse(json);
}

function traceLine(html) {
  const at = new Date().toLocaleTimeString([], { hour12: false });
  sysTrace.insertAdjacentHTML("beforeend",
    `<span class="tr-at">${at}</span> ${html}\n`);
  sysTrace.parentElement.scrollTop = sysTrace.parentElement.scrollHeight;
}

const TR_COLORS = { fiber: "tr-fiber", host: "tr-host", load: "tr-load",
                    unload: "tr-load", emission: "tr-emit", divert: "tr-emit",
                    config: "tr-config" };

function traceEvents(events) {
  for (const e of events || []) {
    const cls = TR_COLORS[e.channel] || "tr-dim";
    traceLine(`<span class="${cls}">${esc(e.channel)}</span> ` +
      `<span class="tr-subject">${esc(e.subject || "")}</span>` +
      (e.detail ? ` <span class="tr-dim">— ${esc(e.detail)}</span>` : ""));
  }
}

/* graph: layered by dependency depth, wires provider → consumer */
function renderGraph(graph, states) {
  const stateOf = {};
  (states || []).forEach((c) => (stateOf[c.name] = c.state));
  const providerOf = {};
  graph.forEach((c) => c.provides.forEach((k) => (providerOf[k] = c.name)));
  const byName = {};
  graph.forEach((c) => (byName[c.name] = c));
  const depth = {};
  const depthOf = (name, seen) => {
    if (depth[name] !== undefined) return depth[name];
    if (seen.has(name)) return 0;
    seen.add(name);
    const c = byName[name];
    const deps = (c.requires || []).map((k) => providerOf[k]).filter((p) => p && byName[p]);
    depth[name] = deps.length ? Math.max(...deps.map((p) => depthOf(p, seen))) + 1 : 0;
    return depth[name];
  };
  graph.forEach((c) => depthOf(c.name, new Set()));

  const COLW = 200, ROWH = 84, NODEW = 168, NODEH = 62, PAD = 14;
  const cols = {};
  graph.forEach((c) => { (cols[depth[c.name]] ||= []).push(c); });
  const nCols = Object.keys(cols).length;
  const maxRows = Math.max(...Object.values(cols).map((l) => l.length));
  const width = Math.max(nCols * COLW, 320);
  const height = Math.max(maxRows * ROWH + PAD, 120);

  const pos = {};
  for (const [d, list] of Object.entries(cols)) {
    const offset = (height - list.length * ROWH) / 2;
    list.forEach((c, i) => {
      pos[c.name] = { x: Number(d) * COLW + PAD, y: offset + i * ROWH + PAD / 2 };
    });
  }

  let wires = "";
  graph.forEach((c) => {
    (c.requires || []).forEach((k) => {
      const p = providerOf[k];
      if (!p || !pos[p] || !pos[c.name]) return;
      // consumer sits deeper than its provider: draw provider → consumer
      const a = pos[p], b = pos[c.name];
      const x1 = a.x + NODEW, y1 = a.y + NODEH / 2;
      const x2 = b.x, y2 = b.y + NODEH / 2;
      const mx = (x1 + x2) / 2;
      wires += `<path d="M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}" />`;
    });
  });

  let nodes = "";
  graph.forEach((c) => {
    const present = stateOf[c.name] !== undefined;
    const st = present ? stateOf[c.name] : "withdrawn";
    const live = st === "ACTIVE";
    const p = pos[c.name];
    const btn = present
      ? `<button class="node-btn node-x" data-name="${esc(c.name)}"
           title="withdraw ${esc(c.name)} — dispose it and watch the reactive cascade">✕</button>`
      : `<button class="node-btn node-play" data-name="${esc(c.name)}"
           title="load ${esc(c.name)} back into the running composition">▶</button>`;
    nodes += `<div class="node ${live ? "live" : "dimmed"} ${present ? "" : "withdrawn"}"
      style="left:${p.x}px; top:${p.y}px; width:${NODEW}px">${btn}
      <div class="nname">${esc(c.name)}</div>
      <div class="nrole">${
        c.requires.length ? "requires " + c.requires.map(esc).join(", ") : "no requires"
      }${c.provides.length ? " · provides " + c.provides.map(esc).join(", ") : ""}</div>
      <span class="npill">${esc(st.toLowerCase())}</span></div>`;
  });

  sysGraph.innerHTML =
    `<div class="sys-canvas" style="width:${width + NODEW - COLW + PAD * 2}px; height:${height + PAD}px">
       <svg class="wires" width="100%" height="100%">${wires}</svg>${nodes}</div>`;
}

sysGraph.addEventListener("click", async (e) => {
  const btn = e.target.closest(".node-btn");
  if (!btn) return;
  const name = btn.dataset.name;
  const withdrawing = btn.classList.contains("node-x");
  btn.disabled = true;
  try {
    const r = await runLive(
      withdrawing ? "live_withdraw(LIVE_NAME)" : "live_plug(LIVE_NAME)",
      { LIVE_NAME: name });
    if (!r.ok) { liveFailure(r, withdrawing ? `withdraw ${name}` : `load ${name}`); return; }
    traceLine(withdrawing
      ? `<span class="tr-err">✕ withdrew ${esc(name)}</span> <span class="tr-dim">— its accumulator replayed backwards; anything that required it reacted (R2/R3)</span>`
      : `<span class="tr-ok">▶ loaded ${esc(name)}</span> <span class="tr-dim">— provision restored; pending dependents reactivate</span>`);
    traceEvents(r.trace);
    renderSystem(r);
  } catch (err) { traceLine(`<span class="tr-err">${esc(String(err))}</span>`); }
});

function renderOps(ops) {
  let html = "";
  for (const op of ops || []) {
    const options = op.methods.map((m) =>
      `<option value="${esc(m.name)}">${esc(m.name)}(${m.params.join(", ")})${m.emission ? " ⚡" : ""}</option>`
    ).join("");
    html += `<div class="op-row" data-key="${esc(op.key)}">
      <code class="op-key">${esc(op.key)}.</code>
      <select class="op-method" aria-label="method on ${esc(op.key)}">${options}</select>
      <input class="op-args" placeholder='args — e.g. "ada", 1' spellcheck="false" />
      <button class="btn btn-ghost btn-sm op-call">Call</button></div>`;
  }
  sysOps.innerHTML = html;
  sysOps.querySelectorAll(".op-row").forEach((row) => {
    const fire = () => callOp(row);
    row.querySelector(".op-call").addEventListener("click", fire);
    row.querySelector(".op-args").addEventListener("keydown", (e) => {
      if (e.key === "Enter") fire();
    });
  });
}

async function callOp(row) {
  const key = row.dataset.key;
  const method = row.querySelector(".op-method").value;
  const raw = row.querySelector(".op-args").value.trim();
  let argsJson = "[]";
  if (raw) {
    try { argsJson = JSON.stringify(JSON.parse(`[${raw}]`)); }
    catch { traceLine(`<span class="tr-err">args must be JSON values, e.g. "ada", 1</span>`); return; }
  }
  try {
    const r = await runLive("live_call(LIVE_KEY, LIVE_METHOD, LIVE_ARGS)",
      { LIVE_KEY: key, LIVE_METHOD: method, LIVE_ARGS: argsJson });
    if (!r.ok) { liveFailure(r, `call ${key}.${method}`); return; }
    traceEvents(r.trace);
    traceLine(`<span class="tr-call">→ ${esc(key)}.${esc(method)}(${esc(raw)})</span> ` +
      `<span class="tr-result">= ${esc(JSON.stringify(r.result))}</span>`);
    renderSystem(r);
  } catch (err) { traceLine(`<span class="tr-err">${esc(String(err))}</span>`); }
}

function renderSystem(snap) {
  lastSnap = snap;
  const st = snap.state || {};
  const names = new Set((st.components || []).map((c) => c.name));
  const hasSystemPanel = (snap.graph || []).some((c) => c.name === "SystemPanel");
  if (!hasSystemPanel || names.has("SystemPanel")) sysPanel.hidden = false;
  sysLabel.textContent = `${metaActive ? "◐ playground shell" : "live"} · generation ${st.generation ?? "?"}`;
  renderGraph(snap.graph || [], st.components || []);
  renderOps(snap.ops || []);
  const active = (st.components || []).filter((c) => c.state === "ACTIVE").length;
  $("status").innerHTML =
    `<span class="verdict good">LIVE</span>` +
    `<span>generation ${st.generation} — ${active} component${active === 1 ? "" : "s"} active, ` +
    `running in this tab on cordis-py</span>`;
  updateDock();
}

function liveFailure(r, what) {
  if (r.diagnostics) {
    render({ ok: false, diagnostics: r.diagnostics });
    const d = r.diagnostics[0] || {};
    const loc = d.line ? `:${d.line}` : "";
    traceLine(`<span class="tr-err">✗ ${esc(what)} REFUSED${loc} — ${esc(d.message || d.code || "")}` +
      `</span> <span class="tr-dim">(running system untouched; details in Diagnostics)</span>`);
  } else {
    traceLine(`<span class="tr-err">✗ ${esc(what)}: ${esc(r.error || "failed")}</span>`);
  }
}

/* ---------- meta mode: the playground's own pane as a revl composition ----- */

let metaActive = false;
const TAB_IDS = ["diagnostics", "audit", "plan", "ir"];
const tabEl = (id) => document.querySelector(`.pg-tab[data-panel="${id}"]`);
const DEFAULT_LABELS = { diagnostics: "Diagnostics", audit: "G8 audit", plan: "Plan", ir: "IR" };

window.__metaHost = {
  mount(id, label) {
    const t = tabEl(id);
    if (!t) return;
    // keep the status dot, replace the label text
    const dot = t.querySelector(".dot");
    t.textContent = "";
    if (dot) t.appendChild(dot);
    t.appendChild(document.createTextNode(label));
    t.style.display = "";
  },
  unmount(id) {
    const t = tabEl(id);
    if (!t) return;
    t.style.display = "none";
    const panel = document.getElementById("panel-" + id);
    if (panel) panel.classList.remove("active");
    if (t.getAttribute("aria-selected") === "true") {
      const next = TAB_IDS.find((x) => tabEl(x) && tabEl(x).style.display !== "none");
      if (next) selectTab(next);
    }
  },
  select(id) { selectTab(id); },
  mountRegion(id) {
    const el = document.querySelector(REGIONS[id]);
    if (el) { el.style.display = ""; el.hidden = false; }
    updateDock();
  },
  unmountRegion(id) {
    const el = document.querySelector(REGIONS[id]);
    if (el) el.style.display = "none";
    updateDock();
  },
  editorSource() { return editor.value; },
  theme(name) {
    const worn = document.body.dataset.ui;
    if (worn && worn !== name) {
      throw new Error(`the page already wears theme "${worn}" — the data-ui slot ` +
        `is one exclusive host resource, so mount one Style component at a time ` +
        `(withdraw the current one first)`);
    }
    document.body.dataset.ui = name;
  },
  themeReset() { delete document.body.dataset.ui; },
};

const REGIONS = {
  editor: ".editor",
  toolbar: ".pg-toolbar",
  status: "#status",
  system: "#sys-panel",
};

/* the ◐ dock — the JS chrome's one fixed foothold. It appears only when the
   composition has withdrawn part of the page, and offers the way back. */
let lastSnap = null;
const dock = document.createElement("div");
dock.className = "meta-dock";
dock.hidden = true;
dock.innerHTML = `<span>◐ part of this page is withdrawn</span>
  <button class="btn btn-primary btn-sm" id="dock-restore">▶ load it back</button>`;
document.body.appendChild(dock);

function updateDock() {
  const anyHidden = Object.values(REGIONS).some((sel) => {
    const el = document.querySelector(sel);
    return el && el.style.display === "none";
  });
  dock.hidden = !(metaActive && anyHidden);
}

dock.querySelector("#dock-restore").addEventListener("click", async () => {
  if (!lastSnap) { metaRestore(); return; }
  const present = new Set((lastSnap.state?.components || []).map((c) => c.name));
  const absent = (lastSnap.graph || []).map((c) => c.name).filter((n) => !present.has(n));
  for (const name of absent) {
    try {
      const r = await runLive("live_plug(LIVE_NAME)", { LIVE_NAME: name });
      if (r.ok) {
        traceLine(`<span class="tr-ok">▶ loaded ${esc(name)}</span> <span class="tr-dim">— restored from the dock</span>`);
        traceEvents(r.trace);
        renderSystem(r);
      }
    } catch { /* keep going */ }
  }
  updateDock();
});

function metaRestore() {
  metaActive = false;
  for (const id of TAB_IDS) {
    window.__metaHost.mount(id, DEFAULT_LABELS[id]);
  }
  for (const sel of Object.values(REGIONS)) {
    const el = document.querySelector(sel);
    if (el && sel !== "#sys-panel") el.style.display = "";
  }
  dock.hidden = true;
  delete document.body.dataset.ui;
  selectTab("diagnostics");
}

const isMetaSource = (src) => src.includes("playground_host");

$("boot").addEventListener("click", async () => {
  if (!liveOk) return;
  $("boot").disabled = true;
  try {
    const meta = isMetaSource(editor.value);
    if (meta) {
      // the revl composition takes ownership of the page: strip the JS-owned
      // tabs and regions so the activation below rebuilds them, mount by mount
      for (const id of TAB_IDS) {
        const t = tabEl(id);
        if (t) t.style.display = "none";
      }
      for (const [rid, sel] of Object.entries(REGIONS)) {
        if (rid === "system") continue; // shown by renderSystem on success
        const el = document.querySelector(sel);
        if (el) el.style.display = "none";
      }
    }
    const r = await runLive("live_boot(LIVE_SRC)", { LIVE_SRC: editor.value });
    if (!r.ok) {
      if (meta) metaRestore();
      liveFailure(r, "boot");
      return;
    }
    metaActive = meta;
    sysTrace.innerHTML = "";
    traceLine(meta
      ? `<span class="tr-swap">◐ meta</span> <span class="tr-dim">— this pane is now a revl composition; its tabs were just mounted by effect/undo pairs. Click around, edit the source, Swap.</span>`
      : `<span class="tr-load">▶ booted</span> <span class="tr-dim">— admitted through the gate, activated on cordis-py</span>`);
    traceEvents(r.trace);
    renderSystem(r);
  } finally { $("boot").disabled = false; }
});

$("sys-swap").addEventListener("click", async () => {
  try {
    const r = await runLive("live_swap(LIVE_SRC)", { LIVE_SRC: editor.value });
    if (!r.ok) { liveFailure(r, "swap"); return; }
    traceEvents(r.trace);
    traceLine(`<span class="tr-swap">⇄ hot-swapped</span> <span class="tr-dim">— ` +
      `editor code re-admitted against the running system` +
      `${r.handoff ? "; state crossed via handoff" : ""}</span>`);
    renderSystem(r);
  } catch (err) { traceLine(`<span class="tr-err">${esc(String(err))}</span>`); }
});

$("sys-unload").addEventListener("click", async () => {
  try {
    const r = await runLive("live_unload()", {});
    if (!r.ok) { liveFailure(r, "unload"); return; }
    const un = r.unloaded || {};
    traceEvents(un.trace);
    const checks = un.checks || {};
    const proof = Object.entries(checks).map(([k, v]) => `${k} ${v ? "✓" : "✗"}`).join(" · ");
    traceLine(un.noResidue
      ? `<span class="tr-ok">■ unloaded — ✓ no residue (${proof})</span>`
      : `<span class="tr-err">■ unloaded — residue detected! (${proof})</span>`);
    sysGraph.innerHTML = "";
    sysOps.innerHTML = "";
    sysLabel.textContent = "unloaded — no residue proven";
    $("status").innerHTML = un.noResidue
      ? `<span class="verdict good">NO RESIDUE</span><span>teardown replayed every effect backwards — the proof is checked, not asserted</span>`
      : `<span class="verdict bad">RESIDUE</span><span>teardown left state behind — see the trace</span>`;
    if (metaActive) {
      // the composition just unbuilt the pane it was rendering into — let that
      // sink in, then the JS chrome takes ownership back
      setTimeout(() => {
        metaRestore();
        traceLine(`<span class="tr-dim">◐ the revl-owned pane unbuilt itself — JS chrome resumed ownership</span>`);
      }, 2600);
    }
  } catch (err) { traceLine(`<span class="tr-err">${esc(String(err))}</span>`); }
});

boot();
