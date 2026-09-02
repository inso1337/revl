"""One-off generator for host_trace_secret.ir.json — NOT part of the build.

The TS-tier half of roadmap item 421 F6: a value the author declared `Secret[T]`
must not reach the host trace verbatim.

`runtime.record` interpolates a `Map` key and value, a `pool.query`/`pool.execute`
sql, a stream item, a job name and a component's resolved config straight into
`hostLog` — this tier's shared observability channel, exported and forwarded to
any `onHostEvent` subscriber a host installs. Nothing on the tier knew what a
declared `Secret[T]` was: the marking exists in the IR
(`externs[i].secret_return`, `params[i].secret`, a config field's `secret`) and
only the py emitter read it.

The composition below carries all three declarations at once, because they fail
independently:

  * `mint_token` declares a `Secret[Str]` RETURN — the ORIGIN, where the value
    enters the value world (`let t = emit mint_token(u); effect store.insert(t,
    v)`, the shipped `demo/components/user_cache.rvl` idiom);
  * `Cache.store` declares a `Secret[Str]` PARAMETER — the RECEIVER, reached with
    no origin in sight;
  * `Vault` declares a `Secret[Str]` CONFIG field, which the `<Component>.config`
    trace line used to spell out in full.

The whole `.rvl` source is compiled by `compile_source`, so the component body,
provide and method shapes are real compiler output rather than hand-assembled
IR. Run once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_host_trace_secret.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_SOURCE = (
    "extern emission[vault.mint] fn mint_token(u: Str) -> Secret[Str] = @ts {\n"
    "  return 'SEKRIT-CANARY-421-F6'\n"
    "}\n"
    "service Cache {\n"
    "  emission fn put(u: Str) -> Str\n"
    "  emission fn store(token: Secret[Str]) -> Str\n"
    # the false-positive control: an ordinary parameter, declared nowhere near
    # `Secret[T]`, must still be recorded verbatim or the trace is worthless.
    "  emission fn note(text: Str) -> Str\n"
    "}\n"
    "component UserCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "\n"
    "  provide cache {\n"
    "    fn put(u) {\n"
    "      let t = emit mint_token(u)\n"
    "      effect store.insert(t, 'PUBLIC-VALUE-421')\n"
    "      undo   store.remove(t)\n"
    "      return 'ok'\n"
    "    }\n"
    "    fn store(token) {\n"
    "      effect store.insert(token, 'PUBLIC-VALUE-421')\n"
    "      undo   store.remove(token)\n"
    "      return 'ok'\n"
    "    }\n"
    "    fn note(text) {\n"
    "      effect store.insert(text, 'PUBLIC-VALUE-421')\n"
    "      undo   store.remove(text)\n"
    "      return 'ok'\n"
    "    }\n"
    "  }\n"
    "}\n"
    "component Vault {\n"
    "  config { api_key: Secret[Str] = 'CONFIG-CANARY-421-F6', region: Str = 'eu' }\n"
    "}\n"
)


def build() -> dict:
    return compile_source(_SOURCE, "host_trace_secret.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "host_trace_secret.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
