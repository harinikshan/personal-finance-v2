"""
gateway.py — the *only* path to an LLM in this project.

Every cognitive layer calls LLM Gateway V3 through this thin wrapper. No layer
imports a provider SDK (openai / google-generativeai / groq / ...) directly.
The gateway runs locally on port 8101 and is the substrate for perception,
memory and decision calls (auto_route) and for structured synthesis.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel

GATEWAY_URL = os.getenv("LLM_GATEWAY_V3_URL", "http://localhost:8101")
_TIMEOUT = float(os.getenv("LLM_GATEWAY_TIMEOUT", "600"))

# Opt-in worker-model override. Leave unset on a machine with real provider
# keys (the gateway then uses each provider's configured default). Set it to
# pin a specific local model, e.g. AGENT_MODEL_OVERRIDE=gemma4:e4b, when the
# only available worker is Ollama. It rides along as ChatRequest.model and does
# NOT disable auto_route (only an explicit `provider` does that).
_MODEL_OVERRIDE = os.getenv("AGENT_MODEL_OVERRIDE") or None

# Opt-in provider pin. Leave unset for normal auto_route + failover. Set it to
# force every call onto one known-good worker, e.g. AGENT_PROVIDER=groq, when
# the gateway's other wired providers are timing out and dragging the failover
# chain. An explicit provider disables auto_route (the gateway honours it as
# "caller knows best").
_PROVIDER_OVERRIDE = os.getenv("AGENT_PROVIDER") or None


class GatewayError(RuntimeError):
    pass


def _post(body: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in body.items() if v is not None}
    try:
        r = httpx.post(f"{GATEWAY_URL}/v1/chat", json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise GatewayError(f"cannot reach LLM Gateway V3 at {GATEWAY_URL}: {e}") from e
    if r.status_code >= 400:
        raise GatewayError(f"gateway {r.status_code}: {r.text[:400]}")
    return r.json()


def chat(
    *,
    messages: Optional[list[dict[str, Any]]] = None,
    prompt: Optional[str] = None,
    system: Optional[str] = None,
    provider: Optional[str] = None,
    auto_route: Optional[str] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    response_format: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Raw chat call. Returns the gateway's JSON response dict."""
    return _post({
        "messages": messages,
        "prompt": prompt,
        "system": system,
        "provider": provider or _PROVIDER_OVERRIDE,
        "model": model or _MODEL_OVERRIDE,
        "auto_route": auto_route,
        "tools": tools,
        "tool_choice": tool_choice,
        "response_format": response_format,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    })


def structured(
    schema_model: type[BaseModel],
    *,
    prompt: Optional[str] = None,
    messages: Optional[list[dict[str, Any]]] = None,
    system: Optional[str] = None,
    auto_route: Optional[str] = None,
    provider: Optional[str] = None,
    retries: int = 2,
    max_tokens: int = 2048,
    temperature: float = 0.2,
):
    """Call the gateway and return an instance of `schema_model`.

    Uses response_format=json_schema so the gateway constrains and validates the
    worker output to the model's JSON schema. No regex, no manual JSON scraping
    beyond the gateway's own `parsed` field. On a transient failure (or a worker
    that produced JSON the schema rejects) we re-generate up to `retries` times
    on the same routing path — robust whether the worker pool is one local model
    or seven cloud providers.
    """
    rf = {"type": "json_schema", "schema": schema_model.model_json_schema(), "name": "out"}

    # OpenAI-compatible workers (groq, github, cerebras, ...) reject json-mode
    # requests unless the literal word "json" appears in the messages. The
    # gateway maps our json_schema -> json_object for those providers, so we
    # guarantee the token here in a provider-agnostic way (harmless for native
    # structured-output providers like gemini).
    sys_text = system or ""
    if "json" not in sys_text.lower():
        sys_text = (sys_text + "\n\nRespond ONLY with valid JSON matching the schema.").strip()

    def _try():
        kw = dict(
            messages=messages, prompt=prompt, system=sys_text,
            response_format=rf, max_tokens=max_tokens, temperature=temperature,
        )
        if provider:
            kw["provider"] = provider
        else:
            kw["auto_route"] = auto_route
        resp = chat(**kw)
        parsed = resp.get("parsed")
        if parsed is None:
            txt = (resp.get("text") or "").strip()
            if not txt:
                raise GatewayError("worker returned neither parsed nor text")
            parsed = json.loads(txt)  # gateway already JSON-mode; not LLM free-form regex
        return schema_model.model_validate(parsed), resp

    last_err: Exception | None = None
    for _ in range(max(1, retries + 1)):
        try:
            return _try()
        except Exception as e:  # noqa: BLE001 — regenerate on transient/schema errors
            last_err = e
    raise last_err  # type: ignore[misc]


def to_tool_defs(specs) -> list[dict[str, Any]]:
    """Convert ToolSpec list -> gateway ToolDef dicts."""
    out = []
    for s in specs:
        out.append({
            "name": s.name,
            "description": s.description,
            "input_schema": s.input_schema or {"type": "object", "properties": {}},
        })
    return out
