# findings — extern dedent (agent/fr13-extern-dedent)

Found by the lighthouse workload (the real HTTP ModelProvider): a
multi-line `extern` body breaks the emitted Python.

## 1. Refusal log

- **Multi-line `@py` extern body → broken emitted Python** —
  ```revl
  extern emission fn http_post(url: Str, body: Str) -> Str
    = @py {
        import urllib.request
        req = urllib.request.Request(url, ...)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8")
      }
  ```
  emitted as:
  ```python
  def http_post(url, body):
      import urllib.request
            req = urllib.request.Request(
  ```
  `IndentationError: unexpected indent` at exec time. Verdict:
  **`caught-bug`-shaped gap in the py emitter** — `_emit_externs` did
  `bodies["py"].strip()` then wrote each line at a fixed indent of 1, so
  the *source file's* indentation (whatever the author used inside the
  `@py { }` block) landed verbatim inside the function body. Column-0
  source happened to work; any conventional indentation broke Python
  (whose syntax *is* indentation). The ts emitter had the same verbatim
  copy (cosmetic — JS ignores whitespace).
  Fix: `textwrap.dedent(bodies["py"].strip("\n"))` before splitting lines
  (and the ts twin for consistency). Minimal repro in `ext_indent2.rvl`;
  harness + frontend + golden + v2-emit + py-forward-refs suites all
  green after.

## 2. Friction log

- `[slow]` **The failure surface was the emitted code, not the frontend.**
  `revl compile` succeeds; the error appears when the *emitted module* is
  exec'd — an IndentationError in generated code. There is no `revl emit
  --check` fast path for "does the emitted source parse" on py (the
  conformance `--validate` does run real compilers, but not on every
  component). A `--validate` default on `compile` would have caught this
  at the frontend, not after boot.
- `[nit]` **No extern-body formatting convention documented** —
  guide-ai-agents.md shows single-line bodies only; nothing says whether
  multi-line bodies must be column-0. The dedent fix removes the question.

## 3. What revl gave us

- The **tier-honesty gate worked**: with the fix, a multi-line extern is
  now portable between py and ts with the same source, and rust/java/go
  still refuse with the honest "no @rs/@java/@go body" message. The
  harness's single G8 boundary crossing is now the *only* thing that
  differs per tier — exactly the "one boundary, enumerated" design.

## 4. Time-to-green

- Compose → refuse → fix cycles: **1** (the IndentationError at exec),
  then a minimal-repro probe to confirm it was the emitter, not the
  harness, then the dedent fix + regression run. The stall was short
  because the failure was deterministic and the emitted source was right
  there to read.

## 5. Cost ledger

- `tooling` — no `--validate`-style parse check on `compile` for emitted
  py (the conformance suite has it, the daily loop doesn't). A compile
  flag that runs the emitted source through `ast.parse`/`node --check`
  would turn this class of emitter bug into a red compile.
- `diagnostic` — none; the failure was loud (IndentationError) even if
  late.

**Single change that would cut the most cost next:** `compile --validate`
(or a `revl emit --check`) that parses emitted code with the tier's real
compiler by default, not only in the conformance suite. It converts the
whole "emitter produced something the tier rejects" class from
boot-time discovery to compile-time discovery.
