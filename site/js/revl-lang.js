// revl-lang.js — a small tokenizer + HTML highlighter for revl source.
// Shared by the landing page (static snippets) and the playground editor.
// No dependencies; produces <span class="tok-*"> markup styled by site.css.

const KEYWORDS = new Set([
  "service", "component", "provides", "requires", "provide", "config",
  "let", "var", "fn", "extern", "pure", "acquire", "use", "pub", "type",
  "match", "if", "else", "while", "for", "of", "in", "return",
  "test", "prop", "fault", "lifecycle", "isolate", "intercept", "realm",
  "with", "verified", "hole", "async", "await", "fail", "assert",
  "step", "at", "true", "false",
  // timers, instances, state handoff, lifecycle-test statements
  "every", "after", "spawn", "handoff", "advance",
  "load", "unload", "call", "no_residue",
]);

// The four words that ARE the language's story get their own colors.
const EFFECT_WORDS = new Set(["effect", "undo"]);
const EMIT_WORDS = new Set(["emit", "emission"]);

const esc = (s) =>
  s.replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const span = (cls, text) => `<span class="tok-${cls}">${esc(text)}</span>`;

/** Highlight revl source; returns HTML. */
export function highlightRevl(src) {
  let out = "";
  let i = 0;
  const n = src.length;

  const isIdStart = (c) => /[A-Za-z_]/.test(c);
  const isId = (c) => /[A-Za-z0-9_]/.test(c);

  while (i < n) {
    const c = src[i];

    // line comment
    if (c === "/" && src[i + 1] === "/") {
      let j = i;
      while (j < n && src[j] !== "\n") j++;
      out += span("com", src.slice(i, j));
      i = j;
      continue;
    }

    // template string with ${…} interpolation
    if (c === "`") {
      let j = i + 1;
      out += span("str", "`");
      let chunk = "";
      while (j < n && src[j] !== "`") {
        if (src[j] === "$" && src[j + 1] === "{") {
          if (chunk) { out += span("str", chunk); chunk = ""; }
          let k = j + 2, depth = 1;
          while (k < n && depth > 0) {
            if (src[k] === "{") depth++;
            else if (src[k] === "}") depth--;
            if (depth > 0) k++;
          }
          out += span("interp", src.slice(j, Math.min(k + 1, n)));
          j = Math.min(k + 1, n);
        } else {
          chunk += src[j];
          j++;
        }
      }
      if (chunk) out += span("str", chunk);
      if (j < n) { out += span("str", "`"); j++; }
      i = j;
      continue;
    }

    // plain string
    if (c === '"') {
      let j = i + 1;
      while (j < n && src[j] !== '"' && src[j] !== "\n") {
        if (src[j] === "\\") j++;
        j++;
      }
      if (j < n && src[j] === '"') j++;
      out += span("str", src.slice(i, j));
      i = j;
      continue;
    }

    // @py / @ts / @rust … host-block markers
    if (c === "@" && isIdStart(src[i + 1] || "")) {
      let j = i + 1;
      while (j < n && isId(src[j])) j++;
      out += span("host", src.slice(i, j));
      i = j;
      continue;
    }

    // number (with an optional duration unit: 30s, 5m, 250ms, 1h)
    if (/[0-9]/.test(c)) {
      let j = i;
      while (j < n && /[0-9._]/.test(src[j])) j++;
      const unit = src.slice(j).match(/^(ms|[smh])(?![A-Za-z0-9_])/);
      if (unit) j += unit[1].length;
      out += span("num", src.slice(i, j));
      i = j;
      continue;
    }

    // identifier / keyword / type
    if (isIdStart(c)) {
      let j = i;
      while (j < n && isId(src[j])) j++;
      const word = src.slice(i, j);
      if (EFFECT_WORDS.has(word)) out += span("eff", word);
      else if (EMIT_WORDS.has(word)) out += span("emit", word);
      else if (KEYWORDS.has(word)) out += span("kw", word);
      else if (/^[A-Z]/.test(word)) out += span("type", word);
      else {
        // function name position: previous non-space token was `fn`
        const before = src.slice(0, i);
        const prev = before.match(/(\w+)\s*$/);
        if (prev && prev[1] === "fn") out += span("fn", word);
        else out += esc(word);
      }
      i = j;
      continue;
    }

    // punctuation & operators (batch consecutive)
    if (/[{}()\[\].,:;=<>+\-*/%!?|&$]/.test(c)) {
      out += span("punc", c);
      i++;
      continue;
    }

    out += esc(c);
    i++;
  }
  return out;
}

/** Highlight every <pre data-lang="revl"><code> block in the document. */
export function highlightStaticBlocks(root = document) {
  root.querySelectorAll('[data-lang="revl"]').forEach((pre) => {
    const codeEl = pre.querySelector("code") || pre;
    codeEl.innerHTML = highlightRevl(codeEl.textContent);
  });
}
