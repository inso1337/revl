// playground.js — the revl playground v2.
// Same architecture as playground/app.js (the pure-Python compiler installed
// into Pyodide, called in-process), plus: a syntax-highlighted editor,
// ?example= deep links, and the site design system.
import { EXAMPLES } from "./examples.js";
import { highlightRevl } from "./revl-lang.js";

const WHEEL = "vendor/revl-2.0.0-py3-none-any.whl";

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

let runFn = null;

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
    setMsg("Installing the revl compiler…");
    await pyodide.loadPackage("micropip");
    const micropip = pyodide.pyimport("micropip");
    await micropip.install(new URL(WHEEL, window.location.href).href);
    setMsg("Wiring up…");
    pyodide.runPython(DRIVER);
    runFn = pyodide.globals.get("run_playground");
    $("overlay").classList.add("hidden");
    $("run").disabled = false;
  } catch (err) {
    setMsg("Failed to load: " + err);
    console.error(err);
    return;
  }
  runCurrent();
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
  t.addEventListener("click", () => selectTab(t.dataset.panel)));

$("run").addEventListener("click", runCurrent);

boot();
