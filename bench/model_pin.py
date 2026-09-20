#!/usr/bin/env python3
"""Pin the model a framework-benchmark run used, and prove the pin by probing it.

Roadmap item 548 (issue #1267) requires the report to record "the model id,
quantisation, endpoint kind, sampling parameters and the checker version". The
reason is drift: an API model changes under a fixed name, so a reader who reruns
the suite against "the same model" is not running the same model, and the table
silently re-baselines. A local weights file with a content digest does not
drift, so the pin is only worth something if it carries the digest and the
reader can check it.

This module produces that pin as a JSON document. It does three things and
refuses to do a fourth:

  1. reads the endpoint's model catalogue and copies the identity out of it
     (id, digest, quantisation, parameter size, context length, family);
  2. records the sampling parameters the run will use, which are an input the
     operator chooses, not something the endpoint reports;
  3. optionally measures generation and prompt throughput, with n and a standard
     deviation, and marks the first sample as cold;
  4. it never fabricates any of the above. When the endpoint is unreachable the
     pin is written with `reachable: false` and the identity fields absent, and
     every consumer is expected to render the run as not-run rather than to
     quote a number from somewhere else.

Two endpoint kinds are understood:

  * `ollama`: `/api/tags` for identity, `/api/generate` for throughput. This
    kind is preferred because ollama reports `eval_count` and `eval_duration`
    separately from model load time, so a cold load does not contaminate the
    tokens/second figure the way a wall-clock measurement does.
  * `openai-compat`: `/v1/models` for identity and `/v1/chat/completions` for
    throughput. Identity is thinner (most servers report only an id) and
    throughput is wall-clock over `usage.completion_tokens`, which includes
    queueing and load. Both facts are recorded in the pin so a reader can see
    which kind produced a number.

Usage:
  python3 bench/model_pin.py --model <tag>                    # identity only
  python3 bench/model_pin.py --model <tag> --samples 5        # + throughput
  python3 bench/model_pin.py --model <tag> --write            # commit the pin
  python3 bench/model_pin.py --offline                        # unreachable pin

Nothing here imports revl; the checker version is stamped by
`bench/framework_bench.py`, which owns the report.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent
DEFAULT_PIN = BENCH / "results" / "framework-bench" / "model-pin.json"

# The endpoint the roadmap names. It is a loopback address on the operator's own
# machine: the point of the pin is that the weights are local, so there is no
# third-party service in the loop and nothing to publish to.
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

# The sampling parameters a benchmark run uses. Greedy decoding is not a style
# preference: a sampled run makes compile-rate a distribution over seeds, and
# this suite reports one number per cell with an n, so the decode has to be
# deterministic for that n to mean anything.
DEFAULT_SAMPLING = {
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 0,
    "max_output_tokens": 8192,
}

# The throughput probe's prompt. Short, fixed, and not one of the bench specs,
# so measuring the endpoint never warms a cache the scored run then benefits
# from.
PROBE_PROMPT = "Count from one to twenty in English words, one per line."


class PinError(RuntimeError):
    """The endpoint answered, but not in a shape the pin can be built from."""


def _machine() -> str:
    """A coarse machine string: enough to read a throughput number in context,
    and no more. Throughput is machine-bound, so a number without one is not
    interpretable; a CPU brand string or a hostname is not needed to interpret
    it and this file is published."""
    return (f"{platform.system()} {platform.release()} {platform.machine()} · "
            f"Python {platform.python_version()}")


def _post(url: str, payload: dict, timeout: int) -> dict:
    return _request(url, timeout, json.dumps(payload).encode("utf-8"))


def _get(url: str, timeout: int) -> dict:
    return _request(url, timeout, None)


def _request(url: str, timeout: int, body: bytes | None) -> dict:
    """One request to the named endpoint, with redirects refused.

    `bench/run.py`'s local runner refuses redirects for the reason that applies
    here too: urllib follows a redirect by default, re-issuing a POST as a GET
    against whatever host `Location` names. A pin that identified a different
    server than the one being measured would be worse than no pin.
    """
    req = urllib.request.Request(
        url, data=body, method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json"} if body is not None else {},
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise PinError(f"model pin: HTTP {code} redirect refused: "
                           f"{url} is the endpoint you named")

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise PinError(f"model pin: HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PinError(f"model pin: cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise PinError(f"model pin: timed out after {timeout}s: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PinError(f"model pin: unparseable JSON from {url}: {exc}") from exc


def identity_from_ollama_tags(catalogue: dict, model: str) -> dict:
    """The identity block for `model`, copied out of an ollama `/api/tags` body.

    Matching is exact first, then suffix, because the catalogue tag for a
    Hugging Face pull carries a registry prefix (`hf.co/...`) and a `-GGUF`
    repository suffix that the name in a roadmap or an issue usually omits. A
    suffix match is reported as such: a pin that quietly resolved a different
    tag than the one written down is the drift this file exists to prevent.
    """
    models = catalogue.get("models")
    if not isinstance(models, list):
        raise PinError("model pin: /api/tags carried no 'models' list")
    names = [m.get("name") for m in models if isinstance(m, dict)]

    exact = [m for m in models if isinstance(m, dict) and m.get("name") == model]
    resolution = "exact"
    hit = exact[0] if exact else None
    if hit is None:
        loose = [m for m in models if isinstance(m, dict)
                 and _loose_key(m.get("name", "")) == _loose_key(model)]
        if loose:
            hit, resolution = loose[0], "normalised"
    if hit is None:
        raise PinError(
            f"model pin: {model!r} is not in the endpoint's catalogue; "
            f"it holds {names}")

    details = hit.get("details") or {}
    reported = details.get("quantization_level")
    return {
        "requested": model,
        "resolved": hit.get("name"),
        "resolution": resolution,
        "digest": hit.get("digest"),
        # Two quantisation fields, because they can disagree and the report is
        # required to record the quantisation. ollama reports "unknown" for a
        # GGUF it did not convert itself, while the tag the operator pulled
        # names the quantisation explicitly. Recording only the endpoint's
        # answer would lose the one a reader needs to pull the same weights;
        # recording only the tag would assert something the endpoint did not
        # confirm.
        "quantisation": reported,
        "quantisation_reported_by_endpoint": reported,
        "quantisation_from_tag": _tag_quantisation(hit.get("name") or model),
        "parameter_size": details.get("parameter_size"),
        "context_length": details.get("context_length"),
        "family": details.get("family"),
        "format": details.get("format"),
        "size_bytes": hit.get("size"),
        "capabilities": hit.get("capabilities"),
    }


def _tag_quantisation(name: str) -> str | None:
    """The quantisation named by the tag, e.g. `Q4_K_M`. `None` when the tag
    carries no quantisation (a `:latest` tag names none)."""
    tag = name.rpartition(":")[2].strip()
    if not tag or tag.lower() in ("latest", ""):
        return None
    head = tag.split("-", 1)[0].upper()
    return tag if head.startswith(("Q", "F", "IQ", "BF")) else None


def _loose_key(name: str) -> str:
    """`hf.co/org/Model-GGUF:Q4_K_M` and `org/Model:Q4_K_M` reduce to the same
    key. Only registry prefix and a `-GGUF` repository suffix are dropped;
    nothing that could change the weights (org, model, quantisation tag) is."""
    key = name.strip().lower()
    for prefix in ("hf.co/", "huggingface.co/", "ollama.com/", "registry.ollama.ai/"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    repo, _, tag = key.partition(":")
    for suffix in ("-gguf", ".gguf"):
        if repo.endswith(suffix):
            repo = repo[: -len(suffix)]
    return f"{repo}:{tag}" if tag else repo


def identity_from_openai_models(catalogue: dict, model: str) -> dict:
    """The thin identity an OpenAI-compatible `/v1/models` can support.

    Everything a `/api/tags` body would have carried is absent here and is
    recorded as absent rather than guessed. A reader can then see that this pin
    does not fix the weights, only the name, which is exactly the drift the
    roadmap item asks the report to stop hiding.
    """
    data = catalogue.get("data")
    if not isinstance(data, list):
        raise PinError("model pin: /v1/models carried no 'data' list")
    ids = [d.get("id") for d in data if isinstance(d, dict)]
    if model not in ids:
        loose = [i for i in ids if isinstance(i, str)
                 and _loose_key(i) == _loose_key(model)]
        if not loose:
            raise PinError(
                f"model pin: {model!r} is not served by this endpoint; "
                f"it serves {ids}")
        resolved, resolution = loose[0], "normalised"
    else:
        resolved, resolution = model, "exact"
    return {
        "requested": model,
        "resolved": resolved,
        "resolution": resolution,
        "digest": None,
        "quantisation": None,
        "parameter_size": None,
        "context_length": None,
        "family": None,
        "format": None,
        "size_bytes": None,
        "capabilities": None,
        "identity_note": (
            "an OpenAI-compatible /v1/models body names the model and nothing "
            "else; this pin fixes the name, not the weights"),
    }


def _ollama_sample(endpoint: str, model: str, sampling: dict,
                   timeout: int) -> dict:
    body = _post(endpoint.rstrip("/") + "/api/generate", {
        "model": model,
        "prompt": PROBE_PROMPT,
        "stream": False,
        "options": {
            "temperature": sampling["temperature"],
            "top_p": sampling["top_p"],
            "seed": sampling["seed"],
            "num_predict": 128,
        },
    }, timeout)
    ec, ed = body.get("eval_count"), body.get("eval_duration")
    pc, pd = body.get("prompt_eval_count"), body.get("prompt_eval_duration")
    if not (isinstance(ec, int) and isinstance(ed, int) and ed > 0):
        raise PinError("model pin: ollama reported no usable eval_count/duration")
    sample = {
        "generation_tps": ec / (ed / 1e9),
        "generation_tokens": ec,
        # Load time is reported separately and is excluded on purpose: a cold
        # load of a multi-gigabyte weights file dominates wall clock and has
        # nothing to do with the model's throughput.
        "load_ms": (body.get("load_duration") or 0) / 1e6,
        "total_ms": (body.get("total_duration") or 0) / 1e6,
    }
    if isinstance(pc, int) and isinstance(pd, int) and pd > 0:
        sample["prompt_tps"] = pc / (pd / 1e9)
        sample["prompt_tokens"] = pc
    return sample


def _openai_sample(endpoint: str, model: str, sampling: dict,
                   timeout: int) -> dict:
    started = time.perf_counter()
    body = _post(endpoint.rstrip("/") + "/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "temperature": sampling["temperature"],
        "top_p": sampling["top_p"],
        "max_tokens": 128,
    }, timeout)
    elapsed = time.perf_counter() - started
    usage = body.get("usage") or {}
    out = usage.get("completion_tokens")
    if not isinstance(out, int) or out <= 0 or elapsed <= 0:
        raise PinError("model pin: endpoint reported no usable completion_tokens")
    return {
        "generation_tps": out / elapsed,
        "generation_tokens": out,
        "total_ms": elapsed * 1000,
        "wall_clock": True,
    }


def measure_throughput(endpoint: str, model: str, kind: str, samples: int,
                       sampling: dict, timeout: int) -> dict:
    """`samples` throughput samples, the first of which may be cold.

    The cold sample is kept and flagged rather than discarded. A discarded
    sample is a choice a reader cannot audit; a flagged one lets them see how
    much of the spread was load.
    """
    take = _ollama_sample if kind == "ollama" else _openai_sample
    rows: list[dict] = []
    for i in range(samples):
        row = take(endpoint, model, sampling, timeout)
        row["index"] = i
        row["cold"] = (i == 0)
        rows.append(row)

    warm = [r for r in rows if not r["cold"]] or rows
    gen = [r["generation_tps"] for r in warm]
    prompt = [r["prompt_tps"] for r in warm if "prompt_tps" in r]
    out = {
        "samples": rows,
        "n_warm": len(warm),
        "generation_tps_mean": statistics.fmean(gen),
        "generation_tps_sd": statistics.stdev(gen) if len(gen) > 1 else None,
        "cold_sample_included_in_mean": warm is rows,
    }
    if prompt:
        out["prompt_tps_mean"] = statistics.fmean(prompt)
        out["prompt_tps_sd"] = statistics.stdev(prompt) if len(prompt) > 1 else None
    return out


def build_pin(endpoint: str, model: str, kind: str, samples: int,
              sampling: dict, timeout: int, offline: bool) -> dict:
    pin = {
        "endpoint_kind": kind,
        "endpoint": endpoint,
        "endpoint_is_local": _is_loopback(endpoint),
        "sampling": dict(sampling),
        "machine": _machine(),
        "probe_prompt": PROBE_PROMPT,
    }
    if offline:
        pin["reachable"] = False
        pin["unreachable_reason"] = "--offline: no probe was attempted"
        pin["model"] = {"requested": model, "resolved": None,
                        "resolution": "unresolved"}
        return pin

    try:
        if kind == "ollama":
            catalogue = _get(endpoint.rstrip("/") + "/api/tags", timeout)
            pin["model"] = identity_from_ollama_tags(catalogue, model)
        else:
            catalogue = _get(endpoint.rstrip("/") + "/v1/models", timeout)
            pin["model"] = identity_from_openai_models(catalogue, model)
    except PinError as exc:
        pin["reachable"] = False
        pin["unreachable_reason"] = str(exc)
        pin["model"] = {"requested": model, "resolved": None,
                        "resolution": "unresolved"}
        return pin

    pin["reachable"] = True
    if samples > 0:
        resolved = pin["model"].get("resolved") or model
        try:
            pin["throughput"] = measure_throughput(
                endpoint, resolved, kind, samples, sampling, timeout)
        except PinError as exc:
            pin["throughput"] = {"measured": False, "reason": str(exc)}
    return pin


def _is_loopback(endpoint: str) -> bool:
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help=f"endpoint base URL (default {DEFAULT_ENDPOINT})")
    ap.add_argument("--kind", choices=["ollama", "openai-compat"],
                    default="ollama", help="endpoint kind (default ollama)")
    ap.add_argument("--model", default=None,
                    help="model tag to pin; defaults to hosts.json's pinned model")
    ap.add_argument("--samples", type=int, default=0,
                    help="throughput samples to take (0 = identity only)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-request seconds (a cold load can take minutes)")
    ap.add_argument("--offline", action="store_true",
                    help="write an unreachable pin without probing anything")
    ap.add_argument("--out", default=None, help="write the pin here")
    ap.add_argument("--write", action="store_true",
                    help=f"write the pin to {DEFAULT_PIN.relative_to(BENCH.parent)}")
    args = ap.parse_args(argv)

    model = args.model
    if model is None:
        hosts = json.loads((BENCH / "hosts.json").read_text())
        model = hosts["pinned_model"]["id"]

    pin = build_pin(args.endpoint, model, args.kind, args.samples,
                    DEFAULT_SAMPLING, args.timeout, args.offline)

    text = json.dumps(pin, indent=2) + "\n"
    target = Path(args.out) if args.out else (DEFAULT_PIN if args.write else None)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        print(f"wrote {target}")
    else:
        print(text, end="")
    return 0 if pin.get("reachable") or args.offline else 1


if __name__ == "__main__":
    raise SystemExit(main())
