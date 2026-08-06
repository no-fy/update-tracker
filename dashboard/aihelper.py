#!/usr/bin/env python3
"""AI-assisted log troubleshooting, via OpenRouter.

Off by default: nothing here ever runs unless an OpenRouter API key is
configured, either as `dashboard.openrouter_api_key` in config.json (set from
Settings, in the browser) or the CUD_OPENROUTER_API_KEY env var (checked when
config.json has none, same precedence as `dashboard.password`). Raw HTTP via
urllib, not an SDK -- this project has no dependencies beyond the standard
library, on either side. OpenRouter's endpoint is OpenAI-compatible (chat
completions), which is why the request/response shapes below look different
from a native Anthropic call.

The dashboard is the only thing that calls out to OpenRouter; agents never
do, so a host on an isolated LAN with no internet access still works exactly
as before. Log lines are supplied by the browser (whatever it is currently
showing), not re-fetched here, so this module has no dependency on collector
or the agents at all.
"""

import json
import os
import time
import urllib.error
import urllib.request

# OpenRouter's own alias for "cheapest current Claude" -- overridable from
# Settings, or CUD_OPENROUTER_MODEL, pointing at anything in the catalog
# list_models() returns (https://openrouter.ai/models).
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_CACHE_SECONDS = 3600
MAX_LOG_CHARS = 12000
MAX_HISTORY_MESSAGES = 20
MAX_OUTPUT_TOKENS = 1024


class ChatError(Exception):
    """Refused before calling the API, or the API itself returned an error."""


def api_key():
    return os.environ.get("CUD_OPENROUTER_API_KEY")


def model():
    return os.environ.get("CUD_OPENROUTER_MODEL") or DEFAULT_MODEL


def available(api_key_override=None):
    return bool(api_key_override or api_key())


def _headers(key):
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        # Optional per OpenRouter's docs, but they use it for their own
        # public leaderboards/attribution -- harmless to include.
        "X-Title": "container-update-dashboard",
    }


def _system_prompt(container_name, host_label, log_lines):
    log_text = "\n".join(log_lines or [])[-MAX_LOG_CHARS:]
    if not log_text:
        log_text = "(no log lines are currently loaded)"
    return (
        "You are helping a systems administrator troubleshoot a Docker "
        "container by reading its logs. The container is \"%s\" on host "
        "\"%s\". Be concise and specific: quote the exact log line(s) that "
        "support your answer, name the likely root cause, and suggest a "
        "next step. Say plainly when the logs don't contain enough "
        "information to tell -- don't guess.\n\n"
        "Recent log output:\n\n%s" % (container_name, host_label, log_text)
    )


def _build_messages(container_name, host_label, log_lines, history, message):
    messages = [{"role": "system", "content": _system_prompt(container_name, host_label, log_lines)}]
    for turn in (history or [])[-MAX_HISTORY_MESSAGES:]:
        role = turn.get("role") if isinstance(turn, dict) else None
        content = turn.get("content") if isinstance(turn, dict) else None
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)[:4000]})
    messages.append({"role": "user", "content": message})
    return messages


def _http_error_detail(exc):
    detail = exc.read().decode("utf-8", "replace")
    try:
        detail = (json.loads(detail).get("error") or {}).get("message", detail)
    except ValueError:
        pass
    return detail[:300]


def ask(container_name, host_label, log_lines, history, message,
        api_key_override=None, model_override=None):
    """One-shot, non-streaming call. Returns {"reply": str, "usage": dict}."""
    key = api_key_override or api_key()
    if not key:
        raise ChatError(
            "AI troubleshooting is not configured on this dashboard "
            "(set an OpenRouter API key in Settings, or CUD_OPENROUTER_API_KEY)."
        )
    message = (message or "").strip()
    if not message:
        raise ChatError("A message is required.")

    body = json.dumps({
        "model": model_override or model(),
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": _build_messages(container_name, host_label, log_lines, history, message),
        # OpenRouter's own extension: ask for cost/token accounting on the response.
        "usage": {"include": True},
    }).encode("utf-8")

    request = urllib.request.Request(CHAT_URL, data=body, method="POST", headers=_headers(key))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ChatError("OpenRouter error: %s" % _http_error_detail(exc))
    except urllib.error.URLError as exc:
        raise ChatError("Could not reach OpenRouter: %s" % exc.reason)

    choices = payload.get("choices") or []
    if not choices:
        error = payload.get("error")
        if error:
            raise ChatError("OpenRouter error: %s" % str(error.get("message", error))[:300])
        return {"reply": "(no response)", "usage": payload.get("usage") or {}}
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    return {"reply": text or "(no response)", "usage": payload.get("usage") or {}}


def ask_stream(container_name, host_label, log_lines, history, message,
                api_key_override=None, model_override=None):
    """Streaming call. Yields {"delta": str} pieces as they arrive, then a
    final {"usage": dict} (or an {"error": str} at any point, terminal)."""
    key = api_key_override or api_key()
    if not key:
        yield {"error": "AI troubleshooting is not configured on this dashboard "
                         "(set an OpenRouter API key in Settings, or CUD_OPENROUTER_API_KEY)."}
        return
    message = (message or "").strip()
    if not message:
        yield {"error": "A message is required."}
        return

    body = json.dumps({
        "model": model_override or model(),
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": _build_messages(container_name, host_label, log_lines, history, message),
        "stream": True,
        "usage": {"include": True},
    }).encode("utf-8")

    request = urllib.request.Request(CHAT_URL, data=body, method="POST", headers=_headers(key))
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        yield {"error": "OpenRouter error: %s" % _http_error_detail(exc)}
        return
    except urllib.error.URLError as exc:
        yield {"error": "Could not reach OpenRouter: %s" % exc.reason}
        return

    try:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            choices = event.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield {"delta": delta}
            if event.get("usage"):
                yield {"usage": event["usage"]}
    except (urllib.error.URLError, OSError) as exc:
        yield {"error": "Lost connection to OpenRouter: %s" % exc}
    finally:
        response.close()


_models_cache = {"ts": 0.0, "models": None}


def list_models():
    """The public OpenRouter model catalog -- no API key required to read it.
    Cached in memory for an hour; every dashboard restart refetches once."""
    now = time.time()
    if _models_cache["models"] is not None and now - _models_cache["ts"] < MODELS_CACHE_SECONDS:
        return _models_cache["models"]

    request = urllib.request.Request(MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))

    models = []
    for item in payload.get("data") or []:
        model_id = item.get("id")
        if not model_id:
            continue
        pricing = item.get("pricing") or {}
        models.append({
            "id": model_id,
            "name": item.get("name") or model_id,
            "context_length": item.get("context_length"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
        })
    models.sort(key=lambda m: (m["name"] or "").lower())
    _models_cache["models"] = models
    _models_cache["ts"] = now
    return models
