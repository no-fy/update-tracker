#!/usr/bin/env python3
"""Low-level OpenRouter access, shared by the dashboard-wide assistant.

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
as before. This module only knows how to talk to OpenRouter -- what to ask
it, which tools to offer, and the confirm-before-mutating loop live in
aiagent.py.
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


def _http_error_detail(exc):
    detail = exc.read().decode("utf-8", "replace")
    try:
        detail = (json.loads(detail).get("error") or {}).get("message", detail)
    except ValueError:
        pass
    return detail[:300]


def chat_completion(messages, tools=None, api_key_override=None, model_override=None,
                     max_tokens=MAX_OUTPUT_TOKENS, timeout=45):
    """One non-streaming call to OpenRouter's chat completions endpoint.
    Returns the parsed response body (OpenAI-shaped: choices[0].message, usage)."""
    key = api_key_override or api_key()
    if not key:
        raise ChatError(
            "The AI assistant is not configured on this dashboard "
            "(set an OpenRouter API key in Settings, or CUD_OPENROUTER_API_KEY)."
        )

    payload = {
        "model": model_override or model(),
        "max_tokens": max_tokens,
        "messages": messages,
        # OpenRouter's own extension: ask for cost/token accounting on the response.
        "usage": {"include": True},
    }
    if tools:
        payload["tools"] = tools

    request = urllib.request.Request(
        CHAT_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers=_headers(key)
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ChatError("OpenRouter error: %s" % _http_error_detail(exc))
    except urllib.error.URLError as exc:
        raise ChatError("Could not reach OpenRouter: %s" % exc.reason)


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
