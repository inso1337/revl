"""The redirect policy every EMITTED crossing carries.

Two constructs in this tree generate a host body that talks HTTP to a peer
named in the declaration: `revl import a2a` (the A2A `message/send` crossing,
`import_a2a.py`) and the composition `remote` row (the canonical bridge
envelope, `synthesize.py`). Both used their tier's default HTTP client with
its default redirect handling, and both tiers follow by default.

**A redirect is a REFUSAL CONDITION, not a transport detail.** The reason is
structural rather than defensive. The endpoint is part of what the file
DECLARED and what the composition ADMITTED: the A2A importer derives the reach
bound `net.<host>` from the endpoint's host alone (D-424c.10) and a `remote`
row writes `at host("h:port")` precisely so the operator can read where the
crossing goes. A transport that follows `Location` to another host makes that
declared bound stop describing where the crossing can actually reach — the
composition said where it would talk and the peer picks somewhere else. Three
separable consequences, each of which this policy closes:

  1. **Another host.** The declared endpoint is no longer the endpoint. The
     audit surface names `net.<declared-host>` while the process reaches
     whatever the peer's `Location` says, including plain `http://` after an
     `https://` was the thing that admitted.
  2. **A method change.** A 301, 302 or 303 is re-issued as a `GET` with the
     body dropped, on urllib and per the Fetch standard both. A crossing
     DECLARED as an `emission` — a write, an effect leaving the process —
     silently becomes a read that emits nothing. `on_failure` classifies the
     outcome of the crossing that was declared; it cannot classify a different
     crossing.
  3. **What rides along.** Every header on the original request follows to the
     new origin: a content type, but equally an `Authorization`, a bearer
     token, a `Secret[T]` a host body put on the wire. Cross-origin is
     therefore refused outright rather than stripped, because "stripped" is a
     judgement about which header was the credential and nothing here is
     entitled to make it.

So: **nothing is followed by default, on any tier.** If following is wanted it
is DECLARED — `--follow-redirects` on `revl import a2a`, `redirect(same_origin)`
on a `remote` row — and even then it is bounded to the origin that was
declared, and to the two status codes that preserve the method:

    | code          | followed when declared? | why                          |
    |---------------|-------------------------|------------------------------|
    | 301, 302, 303 | never                   | re-issues a POST as a GET    |
    | 307, 308      | same-origin only        | method and body preserved    |

A refusal names the rule and reports the target's ORIGIN only — never the full
`Location`, which is peer-supplied text, and never its userinfo, which would be
a live credential (item 421 F4, the same discipline `_redact_userinfo` applies
to a card `url`).

The emitted refusal is a FAULT and stays one under `on_failure(result)`. That
is not an oversight: `on_failure` says what happens when the DECLARED crossing
fails, and a redirect is the peer declining to be the endpoint at all. Folding
it into an in-band `Err("transport failure")` would lose the one diagnostic
that says the reach bound was contradicted.

This module emits SOURCE, not behaviour: the policy is compiled into the
generated body so it is readable in the file an operator reviews, rather than
applied from a runtime import the generated file would have to trust.

Per-tier defaults, for the record (only the first two have an emitted crossing;
the rest are here so a future tier's author has the answer in front of them):

    python   urllib.request.urlopen   FOLLOWS, POST -> GET on 301/302/303
    python   requests / httpx         FOLLOWS (`allow_redirects` / `follow_redirects=True`)
    ts       fetch                    FOLLOWS (`redirect: "follow"` is the default)
    go       net/http.Client          FOLLOWS, up to 10; stop with `CheckRedirect`
    rust     reqwest                  FOLLOWS, up to 10; stop with `redirect::Policy::none()`
    java     java.net.http.HttpClient DOES NOT follow (`Redirect.NEVER` is the default)
"""

from __future__ import annotations

#: The wall-clock bound on one emitted crossing, in seconds.
#:
#: Neither body carried one, so a peer that accepted the connection and then
#: said nothing blocked the emission forever — on the py tier that is a blocked
#: OS thread, and there is no supervision above the host body to time it out.
#: A crossing that never returns is not a crossing, so the bound is part of the
#: contract rather than a tunable; it is written into the generated source
#: where it can be read.
CROSSING_TIMEOUT = 30


def py_policy(tag: str, *, follow: bool) -> str:
    """The redirect-refusing opener, as python source for an emitted body.

    Indented four spaces: both call sites splice it into a host body that sits
    at one level of function indentation. `tag` prefixes every diagnostic
    (`a2a`, `remote`) so a refusal says which crossing refused.
    """
    return f'''
    # -- redirect policy (revl.crossing_redirect) ------------------------
    # A 3xx is a REFUSAL, not a transport detail: the endpoint is part of
    # what this file declared and what the composition admitted. urllib
    # follows by default and re-issues a 301/302/303 POST as a GET with the
    # body dropped, to whatever host `Location` names — which would make the
    # declared reach bound stop describing where this crossing can reach,
    # and would carry every header on this request (a credential, a
    # `Secret[T]`) to an origin nothing here named.
    _follow_redirects = {bool(follow)!r}

    class _RedirectRefused(RuntimeError):
        pass

    def _origin(_u):
        _p = _urlp.urlparse(_u)
        return (_p.scheme, (_p.hostname or "").lower(),
                _p.port or (443 if _p.scheme == "https" else 80))

    class _RedirectPolicy(_req.HTTPRedirectHandler):
        def redirect_request(self, _rq, _fp, _code, _msg, _hdrs, _new):
            # The target is reported as an ORIGIN, never as the raw
            # `Location`: that is peer-supplied text and its userinfo would
            # be a live credential.
            _to = "%s://%s:%d" % _origin(_new)
            if not _follow_redirects:
                raise _RedirectRefused(
                    "{tag}: redirect refused — HTTP %s to %s. Following a "
                    "redirect is not declared on this crossing, and the "
                    "endpoint is part of what was declared and admitted."
                    % (_code, _to))
            if _origin(_new) != _origin(_rq.full_url):
                raise _RedirectRefused(
                    "{tag}: redirect refused — HTTP %s to %s. A declared "
                    "follow is SAME-ORIGIN only; the declared endpoint is "
                    "the reach bound." % (_code, _to))
            if _code not in (307, 308):
                raise _RedirectRefused(
                    "{tag}: redirect refused — HTTP %s re-issues a POST as "
                    "a GET and drops the body, so the declared emission "
                    "would become a read. Only 307 and 308 preserve the "
                    "method." % (_code,))
            # Same origin, method-preserving. The method and the body are
            # carried EXPLICITLY rather than left to urllib's default, which
            # downgrades them.
            return _req.Request(_new, data=_rq.data,
                                headers=dict(_rq.header_items()),
                                origin_req_host=_rq.origin_req_host,
                                unverifiable=True, method=_rq.get_method())

    _opener = _req.build_opener(_RedirectPolicy)
'''


def ts_policy(tag: str, *, follow: bool, url_expr: str, send: str) -> str:
    """The same policy, TypeScript.

    `fetch` defaults to `redirect: "follow"`. `"manual"` is used rather than
    `"error"` so the refusal can name the status it refused; nothing is
    re-issued by the runtime either way, and no header, body or credential
    reaches an origin this file did not name. `send` is the name of a
    `(url: string) => Promise<Response>` the caller has already bound.
    """
    return f'''
      // -- redirect policy (revl.crossing_redirect) --------------------
      // `redirect: "manual"` above: nothing is followed by the runtime. A
      // 3xx is a REFUSAL — the endpoint is part of what was declared and
      // admitted, and the Fetch standard re-issues a 301/302/303 POST as a
      // GET with the body dropped, to whatever origin `Location` names.
      const {tag}FollowRedirects = {"true" if follow else "false"};
      const {tag}OriginOf = (u: string) => {{
        const p = new URL(u);
        return `${{p.protocol}}//${{p.host}}`;
      }};
      let {tag}Url: string = {url_expr};
      let res = await {send}({tag}Url);
      for (let hop = 0; ; hop++) {{
        const moved = res.type === "opaqueredirect"
          || (res.status >= 300 && res.status < 400);
        if (!moved) break;
        // An opaque redirect exposes no headers, so `location` reads null
        // and the refusal below is the only reachable answer for it.
        const loc = res.headers.get("location");
        if (!{tag}FollowRedirects || loc === null) {{
          throw new Error(
            `{tag}: redirect refused — HTTP ${{res.status}}. Following a redirect \
is not declared on this crossing, and the endpoint is part of what was declared \
and admitted.`);
        }}
        // Reported as an ORIGIN, never as the raw `Location`: that is
        // peer-supplied text and its userinfo would be a live credential.
        const next = new URL(loc, {tag}Url).toString();
        if ({tag}OriginOf(next) !== {tag}OriginOf({tag}Url)) {{
          throw new Error(
            `{tag}: redirect refused — HTTP ${{res.status}} to ${{{tag}OriginOf(next)}}. \
A declared follow is SAME-ORIGIN only; the declared endpoint is the reach bound.`);
        }}
        if (res.status !== 307 && res.status !== 308) {{
          throw new Error(
            `{tag}: redirect refused — HTTP ${{res.status}} re-issues a POST as a GET \
and drops the body, so the declared emission would become a read. Only 307 and 308 \
preserve the method.`);
        }}
        if (hop >= 4) {{
          throw new Error(
            "{tag}: redirect refused — too many same-origin hops; a declared follow \
does not chase a loop.");
        }}
        {tag}Url = next;
        res = await {send}({tag}Url);
      }}
'''
