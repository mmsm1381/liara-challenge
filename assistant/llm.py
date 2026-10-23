"""Liara AI chat-completions transport (OpenAI-compatible).

Ported from the previous agent's integrations/model.py: same error mapping and
no automatic retries, but `parse_reply` now accepts tool calls. Two tool modes:
`native` (OpenAI `tools`) and `prompt_json` (the model answers with
{"tool": ..., "args": ...} or {"answer": ...} and the server validates it).
"""

import json
import re
from dataclasses import dataclass, field

import requests
from django.conf import settings


class ModelFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None


def is_configured() -> bool:
    return bool(settings.LLM_BASE_URL and settings.LLM_API_KEY and settings.LLM_MODEL)


def _post(payload, timeout):
    url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    key = settings.LLM_API_KEY
    if any(c in key for c in "\r\n"):
        raise ModelFailure("configuration")
    headers = {
        "Authorization": key if key.lower().startswith("bearer ") else f"Bearer {key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=(5, timeout), allow_redirects=False)
    except requests.Timeout:
        raise ModelFailure("timeout") from None
    except requests.RequestException:
        raise ModelFailure("unavailable") from None
    if resp.status_code in (401, 403):
        raise ModelFailure("authentication")
    if resp.status_code == 429:
        raise ModelFailure("rate_limited")
    if resp.status_code in (400, 404, 422):
        raise ModelFailure("configuration")
    if resp.status_code != 200:
        raise ModelFailure("unavailable")
    if len(resp.content) > 256 * 1024:
        raise ModelFailure("invalid_response")
    try:
        return resp.json()
    except ValueError:
        raise ModelFailure("invalid_response") from None


def parse_reply(data) -> ModelReply:
    try:
        choice = data["choices"][0]
        message = choice["message"]
        if message.get("refusal"):
            raise ModelFailure("refused")
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                raise ModelFailure("invalid_response") from None
            if not isinstance(args, dict):
                raise ModelFailure("invalid_response")
            calls.append(ToolCall(id=raw.get("id") or fn["name"], name=fn["name"], args=args))
        text = message.get("content") or ""
        if not calls and not text.strip():
            raise ModelFailure("invalid_response")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return ModelReply(text=text, tool_calls=calls, usage=usage)
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ModelFailure("invalid_response") from None


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_prompt_json(text: str, tool_names: set[str]) -> ModelReply:
    """prompt_json mode: the model returns {"tool": name, "args": {...}} or {"answer": "..."}."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        # Plain prose is accepted as the final answer.
        return ModelReply(text=text)
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return ModelReply(text=text)
    if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
        if obj["tool"] not in tool_names or not isinstance(obj.get("args", {}), dict):
            raise ModelFailure("invalid_response")
        return ModelReply(tool_calls=[ToolCall(id="pj", name=obj["tool"], args=obj.get("args", {}))])
    if isinstance(obj, dict) and isinstance(obj.get("answer"), str):
        return ModelReply(text=obj["answer"])
    return ModelReply(text=text)


def chat(messages, tools=None, *, timeout=None, max_tokens=900) -> ModelReply:
    """One completion. `tools` is a list of OpenAI tool specs (native mode only)."""
    if not is_configured():
        raise ModelFailure("configuration")
    timeout = timeout or settings.LLM_TIMEOUT
    payload = {"model": settings.LLM_MODEL, "stream": False, "max_tokens": max_tokens, "messages": messages}
    if tools and settings.LLM_TOOL_MODE == "native":
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post(payload, timeout)
    if tools and settings.LLM_TOOL_MODE == "prompt_json":
        reply = parse_reply(data)
        return parse_prompt_json(reply.text, {t["function"]["name"] for t in tools})
    return parse_reply(data)
