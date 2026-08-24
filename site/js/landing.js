// landing.js — every animation on the revl landing page.
// No dependencies: IntersectionObserver + CSS classes + a few timers.
import { highlightStaticBlocks } from "./revl-lang.js";

highlightStaticBlocks();

const $ = (id) => document.getElementById(id);

/* ---------- reveal on scroll -------------------------------------------- */

const revealObs = new IntersectionObserver(
  (entries) => entries.forEach((e) => {
    if (e.isIntersecting) { e.target.classList.add("in"); revealObs.unobserve(e.target); }
  }),
  { threshold: 0.15 },
);
document.querySelectorAll(".reveal").forEach((el) => revealObs.observe(el));

/* ---------- hero: the live hot-swap loop -------------------------------- */

const swap = {
  panel: $("swap-panel"),
  label: $("swap-phase-label"),
  caption: $("swap-caption"),
  v1: $("node-v1"), v2: $("node-v2"),
  v1pill: $("v1-pill"), v2pill: $("v2-pill"),
  accRows: () => Array.from($("acc-rows").querySelectorAll(".acc-row")),
  accEmpty: $("acc-empty"),
  accVerdict: $("acc-verdict"),
  n: 0,
};

function cap(html) {
  swap.caption.innerHTML = `<span class="k">▸</span><span>${html}</span>`;
}

function swapCycle() {
  const rows = swap.accRows();
  // phase: run — reset everything
  swap.panel.dataset.phase = "run";
  swap.label.textContent = "running";
  swap.v1.classList.remove("dimmed", "gone");
  swap.v1.classList.add("live");
  swap.v1pill.textContent = "running";
  swap.v2.classList.remove("arriving", "live");
  swap.v2pill.textContent = "proposed";
  rows.forEach((r) => r.classList.remove("popping", "popped"));
  swap.accEmpty.classList.remove("shown");
  swap.accVerdict.innerHTML = "";
  cap("composition running — every mutation on the stack carries its inverse");

  // phase: check — v2 proposed, gate verdict
  setTimeout(() => {
    swap.panel.dataset.phase = "check";
    swap.label.textContent = "admission check";
    swap.v2.classList.add("arriving");
    cap(`swap proposed: UserCache v2 — gate: <span class="k">G1&hairsp;✓ G2&hairsp;✓ G3&hairsp;✓ G4&hairsp;✓&thinsp;…&thinsp;G8&hairsp;✓ admissible</span>`);
    setTimeout(() => { swap.v2pill.textContent = "checked ✓"; }, 900);
  }, 3400);

  // phase: unwind — replay the accumulator backwards, LIFO
  setTimeout(() => {
    swap.panel.dataset.phase = "unwind";
    swap.label.textContent = "unwinding v1";
    swap.v1.classList.add("dimmed");
    swap.v1.classList.remove("live");
    swap.v1pill.textContent = "tearing down";
    cap("unloading v1 — teardown is the accumulator, replayed backwards (LIFO)");
    // pop from the top of the stack: last row first
    [...rows].reverse().forEach((row, i) => {
      setTimeout(() => row.classList.add("popping"), i * 650);
      setTimeout(() => row.classList.add("popped"), i * 650 + 420);
    });
  }, 6400);

  // phase: done — no residue, v2 live
  setTimeout(() => {
    swap.panel.dataset.phase = "run";
    swap.label.textContent = "running";
    swap.accEmpty.classList.add("shown");
    swap.accVerdict.innerHTML = `<span class="badge badge-good">R4 · NO RESIDUE</span>`;
    swap.v1.classList.add("gone");
    swap.v2.classList.add("live");
    swap.v2pill.textContent = "serving";
    swap.n += 1;
    cap(`<span class="k">✓ no residue</span> — UserCache v2 live, Api never noticed · swap #${swap.n}`);
  }, 9100);
}

if (swap.panel) {
  swapCycle();
  setInterval(swapCycle, 12600);
}

/* ---------- the idea: scroll-driven accumulator ------------------------- */

const ideaVisual = $("idea-visual");
const ivEntries = ideaVisual ? Array.from(ideaVisual.querySelectorAll(".iv-entry")) : [];
const ivEmit = $("iv-emit1");
const ivVerdict = $("iv-verdict");
const ivAccNote = $("iv-acc-note");
const ivBoundaryNote = $("iv-boundary-note");
let ideaTimers = [];
let currentStep = 0;

function clearIdeaTimers() { ideaTimers.forEach(clearTimeout); ideaTimers = []; }
const later = (fn, ms) => ideaTimers.push(setTimeout(fn, ms));

function codeLines(selAdd) {
  ideaVisual.querySelectorAll(".cl").forEach((l) => l.classList.remove("hl-effect", "hl-emit", "faded"));
  selAdd.forEach(([sel, cls]) =>
    ideaVisual.querySelectorAll(sel).forEach((l) => l.classList.add(cls)));
}

function setIdeaStep(step) {
  if (!ideaVisual || step === currentStep) return;
  currentStep = step;
  clearIdeaTimers();
  ideaVisual.dataset.step = String(step);
  ivEntries.forEach((e) => e.classList.remove("in", "unwound"));
  ivEmit.classList.remove("in");

  if (step === 1) {
    codeLines([[".cl-effect", "hl-effect"], [".cl-effect2", "hl-effect"]]);
    ivEntries.forEach((e, i) => later(() => e.classList.add("in"), 300 + i * 550));
    ivAccNote.textContent = "each effect pushes its inverse";
    ivBoundaryNote.textContent = "nothing crosses silently";
    ivVerdict.innerHTML = `<span class="badge badge-good">CHECKED</span>
      <span>G4: every mutation has an inverse or an <b>emit</b> marker</span>`;
  } else if (step === 2) {
    codeLines([[".cl-emitline", "hl-emit"]]);
    ivEntries.forEach((e) => e.classList.add("in"));
    later(() => ivEmit.classList.add("in"), 350);
    ivAccNote.textContent = "revertible history stays local";
    ivBoundaryNote.textContent = "emissions are declared, counted, audited";
    ivVerdict.innerHTML = `<span class="badge badge-warn">DECLARED</span>
      <span>G8: boundary surface — 1 emission, enumerable by <b>revl audit</b></span>`;
  } else if (step === 3) {
    codeLines([[".cl-effect", "hl-effect"], [".cl-effect2", "hl-effect"]]);
    ivEntries.forEach((e) => e.classList.add("in"));
    // unwind LIFO: last entry first
    [...ivEntries].reverse().forEach((e, i) =>
      later(() => e.classList.add("unwound"), 500 + i * 600));
    ivAccNote.textContent = "replayed backwards — derived, not written";
    ivBoundaryNote.textContent = "teardown cannot emit (G5)";
    later(() => {
      ivVerdict.innerHTML = `<span class="badge badge-good">✓ NO RESIDUE</span>
        <span>G7: teardown is LIFO-complete over everything the component did</span>`;
    }, 500 + ivEntries.length * 600);
    ivVerdict.innerHTML = `<span class="badge badge-warn">UNWINDING</span>
      <span>running the accumulator backwards…</span>`;
  }
}

const stepEls = Array.from(document.querySelectorAll("#idea-steps .step"));
const stepObs = new IntersectionObserver(
  (entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting) {
        stepEls.forEach((s) => s.classList.toggle("active", s === e.target));
        setIdeaStep(Number(e.target.dataset.step));
      }
    });
  },
  { rootMargin: "-35% 0px -45% 0px" },
);
stepEls.forEach((el) => stepObs.observe(el));
setIdeaStep(1);
if (stepEls[0]) stepEls[0].classList.add("active");

/* ---------- the gate: typed terminal ------------------------------------ */

// Real compiler output, captured verbatim from `python -m revl compile`.
const TERM_SCRIPT = [
  { cmd: "python -m revl compile rejections/g3_dependency_cycle.rvl" },
  { out: [
    ['<span class="t-err">error</span><span class="t-dim">:</span> <span class="t-loc">g3_dependency_cycle.rvl:12</span><span class="t-dim">: dependency cycle: Alpha -&gt; Beta -&gt; Alpha</span> <span class="t-g">(G3)</span>'],
    ['<span class="t-dim">  why `Alpha` is in a dependency cycle:</span>'],
    ['<span class="t-g">    Alpha -&gt; Beta -&gt; Alpha</span>'],
    ['<span class="t-dim">      Alpha  </span><span class="t-loc">g3_dependency_cycle.rvl:12</span><span class="t-dim">  provides `a`</span>'],
    ['<span class="t-dim">      Beta   </span><span class="t-loc">g3_dependency_cycle.rvl:16</span><span class="t-dim">  provides `b`</span>'],
    ['<span class="t-dim">      Alpha  </span><span class="t-loc">g3_dependency_cycle.rvl:12</span>'],
  ]},
  { cmd: "python -m revl compile rejections/g4_missing_undo.rvl" },
  { out: [
    ['<span class="t-err">error</span><span class="t-dim">:</span> <span class="t-loc">g4_missing_undo.rvl:13</span><span class="t-dim">: effect has no `undo` and `Pool.open` is not pure</span>'],
    ['<span class="t-dim">  write `</span><span class="t-ok">effect Pool.open(...) undo &lt;expr&gt;</span><span class="t-dim">`, or mark the call `</span><span class="t-g">emit</span><span class="t-dim">`</span>'],
    ['<span class="t-dim">  if it deliberately crosses the system boundary</span> <span class="t-g">(G4)</span>'],
  ]},
];

const termEl = $("gate-term");
let termStarted = false;

async function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function runTerm() {
  for (;;) {
    let html = "";
    const draw = (tail) =>
      (termEl.innerHTML = html + tail + '<span class="t-caret"></span>');
    draw('<span class="t-prompt">$ </span>');
    await sleep(700);
    for (const seg of TERM_SCRIPT) {
      if (seg.cmd) {
        let typed = "";
        for (const ch of seg.cmd) {
          typed += ch;
          draw(`<span class="t-prompt">$ </span><span class="t-cmd">${typed}</span>`);
          await sleep(18 + Math.random() * 26);
        }
        html += `<span class="t-prompt">$ </span><span class="t-cmd">${typed}</span>\n`;
        draw("");
        await sleep(420);
      } else {
        for (const [line] of seg.out) {
          html += line + "\n";
          draw("");
          await sleep(110);
        }
        html += "\n";
        draw('<span class="t-prompt">$ </span>');
        await sleep(1300);
      }
    }
    await sleep(5200);
  }
}

if (termEl) {
  const termObs = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && !termStarted) {
      termStarted = true;
      runTerm();
      termObs.disconnect();
    }
  }, { threshold: 0.3 });
  termObs.observe(termEl);
}

/* ---------- runtimes: fan-out light-up ---------------------------------- */

const fan = $("fan-svg");
if (fan) {
  let t = 0;
  setInterval(() => {
    fan.querySelectorAll(".lane[data-t]").forEach((l) =>
      l.classList.toggle("lit", Number(l.dataset.t) === t));
    fan.querySelectorAll(".tier").forEach((g) =>
      g.querySelector("rect").classList.toggle("lit", Number(g.dataset.t) === t));
    t = (t + 1) % 6;
  }, 1000);
}

/* ---------- agents: the exchange, replayed ------------------------------ */

const exchange = $("exchange");
if (exchange) {
  const msgs = Array.from(exchange.querySelectorAll(".msg"));
  const play = () => {
    msgs.forEach((m) => m.classList.remove("in"));
    msgs.forEach((m, i) => setTimeout(() => m.classList.add("in"), 400 + i * 1100));
  };
  let playing = false;
  const xObs = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && !playing) {
      playing = true;
      play();
      setInterval(play, 15000);
      xObs.disconnect();
    }
  }, { threshold: 0.25 });
  xObs.observe(exchange);
}
